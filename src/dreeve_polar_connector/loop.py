"""The daemon: one cycle, then wait, forever.

Four things make this more than a `while True`. The wait is jittered, so every deployment of this image
does not hit Polar on the same second of the interval. A rate limit backs the interval off exponentially
instead of retrying into the wall. A token Polar has *rejected* is never retried at all — the container
stays up, reports unhealthy and waits for a human, because hammering an endpoint that is saying no is
what turns a bad hour into a blocked account. And the wait can be cut short by a webhook.

Note the asymmetry between `AuthorizationRequired` and `AuthenticationFailed`. The first means nobody has
authorized yet: cheap to re-check every cycle, and that is how a token created at `/authorize` is picked
up without restarting the container. The second means the token is dead, and is terminal — but terminal
for *that token*, not for the process. When a different authorization appears in the token store, the
loop tries again; see `_a_new_authorization_arrived`.
"""

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from pathlib import Path

from dreeve_polar_connector.config import Config
from dreeve_polar_connector.delivery import UndeliverableFile, WatchFolderUnusable
from dreeve_polar_connector.ledger import CorruptLedger
from dreeve_polar_connector.polar import (
    AuthenticationFailed,
    AuthorizationRequired,
    ConsentRequired,
    PolarError,
    RateLimited,
)
from dreeve_polar_connector.status import HEARTBEAT_FILENAME, Status
from dreeve_polar_connector.sync import Clock, Sync
from dreeve_polar_connector.tokens import CorruptTokenStore

logger = logging.getLogger(__name__)

SyncFactory = Callable[[Callable[[], bool]], Awaitable[Sync]]
# Identifies *which* authorization the token store currently holds, without exposing the token itself:
# the Polar user and when it was authorized. `None` means the store holds nothing. Comparing two of
# these is how the loop tells "the credential Polar refused" from "a credential it has not seen yet".
AuthorizationStamp = tuple[int, datetime]
StampReader = Callable[[], AuthorizationStamp | None]

# Failures that are recoverable next cycle rather than fatal to the process: logged, recorded on
# `Status`, and retried. Kept in one place, like `sync.FATAL_TO_THE_CYCLE`, so the two `except` clauses
# below cannot drift apart.
RECOVERABLE_CYCLE_FAILURE = (
    PolarError,
    CorruptLedger,
    CorruptTokenStore,
    UndeliverableFile,
    WatchFolderUnusable,
    OSError,
)


class SyncLoop:
    def __init__(
        self,
        config: Config,
        sync_factory: SyncFactory,
        status: Status,
        clock: Clock,
        wake: asyncio.Event | None = None,
        authorization_stamp: StampReader | None = None,
    ) -> None:
        self._config = config
        self._sync_factory = sync_factory
        self._status = status
        self._clock = clock
        self._stop = asyncio.Event()
        self._wake = wake or asyncio.Event()
        # Optional so a caller that cannot read the token store keeps the old behaviour: a rejection is
        # then terminal for the life of the process, because nothing can prove the credential changed.
        self._authorization_stamp = authorization_stamp
        self._sync: Sync | None = None
        self._authorization_rejected = False
        # Which authorization the current attempt uses, and which one Polar refused. Captured before
        # the factory runs rather than read back afterwards: a `/callback` completing mid-cycle would
        # otherwise make the loop record the *new* token as the rejected one and never try it.
        self._attempted_stamp: AuthorizationStamp | None = None
        self._rejected_stamp: AuthorizationStamp | None = None
        self._backoff_seconds = 0.0

    def request_stop(self) -> None:
        """Signal handler. The cycle in flight finishes its current file and flushes the ledger."""
        if not self._stop.is_set():
            logger.info("Shutdown requested; finishing what is in flight")
        self._stop.set()

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    async def run(self) -> None:
        cycles = 0
        while not self._stop.is_set():
            await self._run_one_cycle()
            cycles += 1

            if self._config.max_cycles and cycles >= self._config.max_cycles:
                logger.info("Reached MAX_CYCLES (%d); stopping", self._config.max_cycles)
                break
            if self._stop.is_set():
                break

            delay = self.next_delay()
            self._status.sleeping_until(self._clock.now() + timedelta(seconds=delay))
            logger.info("Next cycle in %d seconds", round(delay))
            await self._wait(delay)

    def next_delay(self) -> float:
        """The interval, jittered — or the current backoff, which is deliberately not jittered."""
        if self._backoff_seconds:
            return self._backoff_seconds

        spread = self._config.poll_interval * self._config.poll_jitter_pct / 100

        return self._config.poll_interval + random.uniform(-spread, spread)  # noqa: S311 - jitter, not crypto

    async def _wait(self, delay: float) -> None:
        """Sleeps until the delay elapses, a stop is requested, or a webhook wakes us.

        The delay is asked of the injected clock rather than a raw `asyncio.wait(timeout=...)`, the
        same way `Sync._pause` asks the clock rather than sleeping directly. That is what lets a
        `StubClock` resolve a multi-cycle test instantly instead of really waiting out POLL_INTERVAL
        between every cycle, while `SystemClock` still sleeps for real in production.
        """
        waiters = [
            asyncio.ensure_future(self._clock.sleep(delay)),
            asyncio.ensure_future(self._stop.wait()),
            asyncio.ensure_future(self._wake.wait()),
        ]
        try:
            await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for waiter in waiters:
                waiter.cancel()
            await asyncio.gather(*waiters, return_exceptions=True)

        if self._wake.is_set():
            self._wake.clear()
            logger.info("Woken early by a webhook; syncing now")

    async def _run_one_cycle(self) -> None:
        sync = await self._resolve_sync()
        if sync is None:
            return

        try:
            result = await sync.run_once()
        except RateLimited as exception:
            self._back_off()
            logger.warning("Polar is rate-limiting us; backing off for %d seconds", round(self._backoff_seconds))
            self._status.rate_limited(self._backoff_seconds, str(exception))
        except AuthorizationRequired as exception:
            self._not_authorized(exception)
        except ConsentRequired as exception:
            self._consent_missing(exception)
        except AuthenticationFailed as exception:
            self._authorization_was_rejected(exception)
        except RECOVERABLE_CYCLE_FAILURE as exception:
            logger.error("Cycle failed: %s", exception)
            self._status.cycle_failed(str(exception))
        else:
            self._backoff_seconds = 0.0
            self._status.cycle_succeeded(result, sync.ledger.counts_by_status(), self._clock.now())
            self._report_rate_limit(sync)
            await self._touch_heartbeat()
            logger.info("Cycle finished: %s", result)

    async def _resolve_sync(self) -> Sync | None:
        """Built once and kept: rebuilding it every cycle would re-read the token store every hour."""
        if self._sync is not None:
            return self._sync

        if self._authorization_rejected and not self._a_new_authorization_arrived():
            return None

        self._attempted_stamp = self._read_stamp()
        try:
            # The cycle asks this between exercises, so a stop request lands within one file.
            self._sync = await self._sync_factory(self._stop.is_set)
            # Building a Sync means a usable token was found, so say so here rather than only in the
            # server's /callback route. A token created by the `login` command never passes through
            # that route, and without this the connector would sync happily while /status reported
            # "required" and /healthz returned 503 — leaving Docker to call a working container
            # unhealthy for as long as it ran.
            self._status.authorized(self._sync.polar_user_id)
        except AuthorizationRequired as exception:
            self._not_authorized(exception)
        except ConsentRequired as exception:
            self._consent_missing(exception)
        except AuthenticationFailed as exception:
            self._authorization_was_rejected(exception)
        except RateLimited as exception:
            self._back_off()
            self._status.rate_limited(self._backoff_seconds, str(exception))
        except RECOVERABLE_CYCLE_FAILURE as exception:
            logger.error("Could not start a sync: %s", exception)
            self._status.cycle_failed(str(exception))

        return self._sync

    def _a_new_authorization_arrived(self) -> bool:
        """True when the token store holds something other than the authorization Polar refused.

        This is what makes a rejection recoverable without a restart. It reads the store rather than
        waiting to be told, so it works for every way a token can appear: the server's `/callback`, the
        `login` command run in a second container, or a `tokens.json` restored from a backup.
        """
        if self._authorization_stamp is None:
            return False

        stamp = self._read_stamp()
        # An empty store is not evidence of anything new, and neither is the same stamp as before.
        if stamp is None or stamp == self._rejected_stamp:
            return False

        logger.info("A different Polar authorization has appeared since the last one was rejected; trying it")
        self._authorization_rejected = False

        return True

    def _read_stamp(self) -> AuthorizationStamp | None:
        """Never raises: an unreadable store must not crash the loop, and it is reported elsewhere.

        `CorruptTokenStore` reaching here means the file cannot be parsed, which is not evidence that
        anyone re-authorized — so treating it as "nothing new" keeps a rejection terminal. The factory
        raises the same error on the next cycle it actually runs, and that is what reports it.
        """
        if self._authorization_stamp is None:
            return None

        try:
            return self._authorization_stamp()
        except (CorruptTokenStore, OSError) as exception:
            logger.warning("Could not read which authorization is stored: %s", exception)
            return None

    def _not_authorized(self, exception: Exception) -> None:
        """Not terminal: retried cheaply next cycle, which picks up a fresh /authorize without a restart."""
        logger.warning("%s", exception)
        self._status.authorization_required(str(exception))
        self._sync = None

    def _consent_missing(self, exception: Exception) -> None:
        """Terminal until something changes: only the user can accept consents, in Polar Flow.

        Accepting a consent does not itself write a new token, so this normally stays terminal for the
        life of the process. Re-authorizing afterwards does write one, which is the one route back.
        """
        self._authorization_rejected = True
        self._rejected_stamp = self._attempted_stamp
        self._sync = None
        self._status.consent_required(str(exception))
        logger.error("Polar will not release data until all consents are accepted in Polar Flow: %s", exception)

    def _authorization_was_rejected(self, exception: Exception) -> None:
        # Terminal for this credential. Whatever we have, Polar does not want it, and asking again every
        # cycle is a retry storm against an endpoint that is saying no. Recorded rather than flagged, so
        # a *different* authorization appearing later is allowed through by `_a_new_authorization_arrived`.
        self._authorization_rejected = True
        self._rejected_stamp = self._attempted_stamp
        self._sync = None
        self._status.authorization_revoked(str(exception))
        logger.error("Polar rejected the access token, and this connector will not try again: %s", exception)

    def _report_rate_limit(self, sync: Sync) -> None:
        limit = sync.current_rate_limit()
        if not limit.is_known:
            return

        self._status.rate_limit_seen(
            short_usage=limit.short_usage,
            short_limit=limit.short_limit,
            long_usage=limit.long_usage,
            long_limit=limit.long_limit,
            reset_seconds=limit.reset_seconds,
        )

    def _back_off(self) -> None:
        doubled = self._backoff_seconds * 2 if self._backoff_seconds else self._config.poll_interval * 2
        self._backoff_seconds = min(float(doubled), float(self._config.max_backoff_seconds))

    async def _touch_heartbeat(self) -> None:
        """The healthcheck's fallback when HTTP_ADDR is off."""
        heartbeat = heartbeat_path(self._config.state_dir)
        try:
            await asyncio.to_thread(_touch, heartbeat)
        except OSError as exception:
            logger.warning("Could not write the heartbeat file %s: %s", heartbeat, exception)


def heartbeat_path(state_dir: Path) -> Path:
    return state_dir / HEARTBEAT_FILENAME


def _touch(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
