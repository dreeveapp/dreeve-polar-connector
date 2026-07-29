"""One sync cycle: list, diff, cap, download, deliver, record.

The interesting part is not the happy path, it is what each kind of failure does. Polar's errors mean
very different things — back off for a while, stop and ask a human, try again next cycle, never try
again — and treating them alike is how an account gets blocked or an exercise gets lost. Each one is
handled exactly once, here.

There is no sync window to compute. `GET /v3/exercises` takes no date parameters and always returns
everything Polar still holds, so the ledger does all the diffing and a cycle costs one list call plus one
download per new exercise.
"""

import asyncio
import logging
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from dreeve_polar_connector.config import Config, FallbackFormat
from dreeve_polar_connector.delivery import DeliveryOutcome, ExerciseFile, UndeliverableFile, WatchFolder
from dreeve_polar_connector.ledger import ExerciseStatus, Ledger, LedgerEntry
from dreeve_polar_connector.polar import (
    AuthenticationFailed,
    ConnectionFailed,
    ConsentRequired,
    NotFound,
    PolarClient,
    RateLimited,
    RateLimitSnapshot,
)

logger = logging.getLogger(__name__)

# The failures that end a whole cycle rather than one exercise. Kept in one place so the two
# `except` clauses in this module cannot drift apart.
FATAL_TO_THE_CYCLE = (RateLimited, AuthenticationFailed, ConsentRequired)


class Clock(Protocol):
    def now(self) -> datetime: ...

    async def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


@dataclass(frozen=True)
class CycleResult:
    listed: int = 0
    delivered: int = 0
    failed: int = 0
    skipped: int = 0
    without_file: int = 0
    backlog: int = 0

    def __str__(self) -> str:
        return (
            f"listed {self.listed}, delivered {self.delivered}, no file {self.without_file}, "
            f"skipped {self.skipped}, failed {self.failed}, {self.backlog} waiting for a later cycle"
        )


class Sync:
    def __init__(
        self,
        config: Config,
        ledger: Ledger,
        client: PolarClient,
        watch_folder: WatchFolder,
        polar_user_id: int,
        clock: Clock,
        should_stop: Callable[[], bool] = lambda: False,
    ) -> None:
        self._config = config
        self._ledger = ledger
        self._client = client
        self._watch_folder = watch_folder
        self._polar_user_id = polar_user_id
        self._clock = clock
        self._should_stop = should_stop

    @property
    def ledger(self) -> Ledger:
        """Read-only, for reporting. Everything that changes the ledger goes through this class."""
        return self._ledger

    @property
    def polar_user_id(self) -> int:
        """Whose token this cycle runs on. Read by the loop to report authorization state."""
        return self._polar_user_id

    async def watch_folder_ready(self) -> None:
        """Proves the folder is writable before a cycle rather than halfway through one."""
        await self._watch_folder.prepare()

    def current_rate_limit(self) -> RateLimitSnapshot:
        """Passed through for reporting; the cycle itself does not act on it."""
        return self._client.current_rate_limit()

    async def run_once(self) -> CycleResult:
        """Raises the cycle-fatal errors; every other failure is absorbed per exercise."""
        started_at = self._clock.now()
        since = self._ledger.resolve_since(self._sync_start(started_at))

        try:
            listed = await self._discover(since, started_at)
        except Exception:
            # Broader than `FATAL_TO_THE_CYCLE` on purpose: `resolve_since` (above) may have just
            # dirtied `resolved_since` in memory, and `ConnectionFailed` or `UnexpectedResponse` from
            # `list_exercises` are just as capable of reaching here as the cycle-fatal errors are.
            # Losing that value to an unflushed exit would let a restart re-resolve SINCE against a
            # later boot time, silently narrowing the sync window the "resolves exactly once"
            # invariant exists to protect.
            await self._flush()
            raise

        candidates = self._waiting_for_a_download()
        cap = self._config.max_downloads_per_cycle
        batch, backlog = candidates[:cap], candidates[cap:]

        if self._config.dry_run:
            for entry in batch:
                logger.info("Would download exercise %s from %s", entry.exercise_id, entry.start_time.date())
            logger.info("Dry run: nothing downloaded, nothing written")
            return CycleResult(listed=listed, backlog=len(candidates))

        if backlog:
            logger.info(
                "Downloading %d of %d waiting exercises this cycle; the rest follow next time",
                len(batch),
                len(candidates),
            )

        counted: Counter[ExerciseStatus] = Counter()
        try:
            await self._download_batch(batch, started_at, counted)
        except FATAL_TO_THE_CYCLE:
            # The ledger still holds everything learned up to here; losing it means downloading it again.
            await self._flush()
            raise

        self._ledger.mark_successful_sync(started_at)
        await self._flush()

        return CycleResult(
            listed=listed,
            delivered=counted[ExerciseStatus.DELIVERED],
            failed=counted[ExerciseStatus.FAILED],
            skipped=counted[ExerciseStatus.SKIPPED],
            without_file=counted[ExerciseStatus.NO_FILE],
            backlog=len(backlog),
        )

    def _sync_start(self, now: datetime) -> datetime:
        """Whatever the first run resolved always wins, so `SINCE=now` never moves after a restart."""
        if self._ledger.resolved_since is not None:
            return self._ledger.resolved_since

        return self._config.since.resolve(now)

    async def _discover(self, since: datetime, started_at: datetime) -> int:
        """Registers anything new. Known-and-settled exercises are never reconsidered."""
        exercises = await self._client.list_exercises()

        for found in exercises:
            known = self._ledger.entry(found.exercise_id)
            if known is not None and known.status.is_terminal:
                continue

            self._ledger.mark_seen(
                found.exercise_id,
                polar_user_id=self._polar_user_id,
                start_time=found.start_time,
                sport=found.sport,
                detailed_sport=found.detailed_sport,
                at=started_at,
            )
            if found.start_time < since or not found.matches(self._config.sports):
                self._ledger.mark_skipped(found.exercise_id, started_at)

        return len(exercises)

    def _waiting_for_a_download(self) -> list[LedgerEntry]:
        """Everything still owed, oldest first, listed this cycle or not.

        A backlog left by the per-cycle cap outlives the listing it was found in: Polar drops exercises
        after 30 days, so anything left waiting too long could stop being listable at all.
        """
        waiting = [
            entry
            for entry in self._ledger.entries()
            if not entry.status.is_terminal and entry.attempts < self._config.max_attempts
        ]

        return sorted(waiting, key=lambda entry: entry.start_time)

    async def _download_batch(
        self, batch: list[LedgerEntry], started_at: datetime, counted: Counter[ExerciseStatus]
    ) -> None:
        for index, entry in enumerate(batch):
            if self._should_stop():
                # Shutdown was asked for. Whatever is in flight finishes; the rest stay pending.
                logger.info("Stopping after %d of %d exercises; the rest stay recorded as waiting", index, len(batch))
                return
            if index:
                await self._pause()
            counted[await self._handle(entry, started_at)] += 1

    async def _handle(self, entry: LedgerEntry, started_at: datetime) -> ExerciseStatus:
        exercise_id = entry.exercise_id
        try:
            files = await self._files_for(entry)
        except FATAL_TO_THE_CYCLE:
            # Not this exercise's problem, and retrying any of them now only makes it worse.
            raise
        except ConnectionFailed as exception:
            logger.warning("Could not reach Polar for exercise %s; trying again next cycle: %s", exercise_id, exception)
            self._ledger.mark_failed(exercise_id, str(exception), started_at)
            return ExerciseStatus.FAILED
        # Deliberately broad: one bad exercise must not take the rest of the cycle down with it.
        except Exception as exception:
            logger.exception("Unexpected failure on exercise %s", exercise_id)
            self._ledger.mark_failed(exercise_id, str(exception), started_at)
            return ExerciseStatus.FAILED

        if not files:
            logger.info("Exercise %s has no file to download; recording it as such", exercise_id)
            self._ledger.mark_no_file(exercise_id, started_at)
            return ExerciseStatus.NO_FILE

        try:
            written = [
                file.name for file in files if await self._watch_folder.deliver(file) is not DeliveryOutcome.SKIPPED
            ]
        except UndeliverableFile as exception:
            logger.warning("Exercise %s could not be delivered; trying again next cycle: %s", exercise_id, exception)
            self._ledger.mark_failed(exercise_id, str(exception), started_at)
            return ExerciseStatus.FAILED

        self._ledger.mark_delivered(exercise_id, tuple(file.name for file in files), started_at)
        logger.info("Imported exercise %s as %s", exercise_id, ", ".join(written) or "a file that was already there")

        return ExerciseStatus.DELIVERED

    async def _files_for(self, entry: LedgerEntry) -> tuple[ExerciseFile, ...]:
        try:
            contents = await self._client.download_fit(entry.exercise_id)
        except NotFound:
            # Polar answers 404 both for "this exercise has no FIT" and "this exercise is gone", so the
            # fallback is worth asking for. If that 404s too, there is genuinely nothing.
            return await self._fallback_for(entry)

        return (ExerciseFile(name=f"{entry.exercise_id}.fit", contents=contents),)

    async def _fallback_for(self, entry: LedgerEntry) -> tuple[ExerciseFile, ...]:
        """Some exercises have no FIT, and Dreeve imports TCX and GPX too."""
        fallback = self._config.fallback_format
        if fallback is FallbackFormat.NONE:
            return ()

        logger.info("No FIT for exercise %s; asking for %s instead", entry.exercise_id, fallback.value)
        await self._pause()
        try:
            contents = await self._client.download_fallback(entry.exercise_id, fallback)
        except NotFound:
            return ()

        return (ExerciseFile(name=f"{entry.exercise_id}.{fallback.value}", contents=contents),)

    async def _pause(self) -> None:
        """Space out requests. Polar is far more forgiving of a slow client than a busy one."""
        if self._config.download_delay_seconds > 0:
            await self._clock.sleep(self._config.download_delay_seconds)

    async def _flush(self) -> None:
        if self._config.dry_run:
            return

        await self._ledger.flush()
