"""The loop's contract: never hammer, never give up silently, and never retry a rejected token."""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from dreeve_polar_connector.config import Config
from dreeve_polar_connector.delivery import WatchFolder, WatchFolderUnusable
from dreeve_polar_connector.ledger import LEDGER_FILENAME, Ledger
from dreeve_polar_connector.loop import SyncLoop, heartbeat_path
from dreeve_polar_connector.polar import (
    AuthenticationFailed,
    AuthorizationRequired,
    ConsentRequired,
    RateLimited,
)
from dreeve_polar_connector.status import AuthorizationState, Status
from dreeve_polar_connector.sync import Sync
from dreeve_polar_connector.tokens import CorruptTokenStore
from tests.stubs import StubClock, StubPolar, exercise

NOW = datetime(2026, 7, 29, 8, 0, tzinfo=UTC)
POLAR_USER = 475


def _config(tmp_path: Path, **overrides: str) -> Config:
    return Config.from_env(
        {
            "POLAR_CLIENT_ID": "a-client",
            "POLAR_CLIENT_SECRET": "a-secret",
            "WATCH_DIR": str(tmp_path / "watch"),
            "STATE_DIR": str(tmp_path / "state"),
            "POLAR_TOKENS": str(tmp_path / "tokens"),
            "DOWNLOAD_DELAY_SECONDS": "0",
            "MAX_CYCLES": "1",
            **overrides,
        }
    )


def _loop(
    tmp_path: Path,
    factory: Callable[[Callable[[], bool]], Awaitable[Sync]],
    *,
    wake: asyncio.Event | None = None,
    authorization_stamp: Callable[[], tuple[int, datetime] | None] | None = None,
    **overrides: str,
) -> tuple[SyncLoop, Status]:
    config = _config(tmp_path, **overrides)
    status = Status(config.poll_interval, NOW, authorize_url="http://nas.local:8080/authorize")
    clock = StubClock(NOW)

    return SyncLoop(config, factory, status, clock, wake=wake, authorization_stamp=authorization_stamp), status


def _working_factory(tmp_path: Path, client: StubPolar) -> Callable[[Callable[[], bool]], Awaitable[Sync]]:
    config = _config(tmp_path)

    async def factory(should_stop: Callable[[], bool]) -> Sync:
        sync = Sync(
            config=config,
            ledger=Ledger.load(config.state_dir / LEDGER_FILENAME),
            client=client,
            watch_folder=WatchFolder(config.watch_dir, config.on_conflict),
            polar_user_id=POLAR_USER,
            clock=StubClock(NOW),
            should_stop=should_stop,
        )
        await sync.watch_folder_ready()
        return sync

    return factory


def _failing_factory(error: Exception) -> Callable[[Callable[[], bool]], Awaitable[Sync]]:
    async def factory(should_stop: Callable[[], bool]) -> Sync:  # noqa: ARG001 - matches the real signature
        raise error

    return factory


@pytest.mark.asyncio
async def test_a_cycle_runs_and_is_reported(tmp_path: Path) -> None:
    loop, status = _loop(tmp_path, _working_factory(tmp_path, StubPolar([exercise()])))

    await loop.run()

    assert status.to_dict(NOW)["cycles"] == 1
    assert status.to_dict(NOW)["lastCycle"]["delivered"] == 1


@pytest.mark.asyncio
async def test_max_cycles_stops_the_loop(tmp_path: Path) -> None:
    client = StubPolar([])
    loop, _ = _loop(tmp_path, _working_factory(tmp_path, client), MAX_CYCLES="3")

    await loop.run()

    assert client.listed == 3


@pytest.mark.asyncio
async def test_a_successful_cycle_writes_the_heartbeat(tmp_path: Path) -> None:
    # The healthcheck's fallback when HTTP_ADDR is off.
    loop, _ = _loop(tmp_path, _working_factory(tmp_path, StubPolar([])))

    await loop.run()

    assert heartbeat_path(tmp_path / "state").exists()


@pytest.mark.asyncio
async def test_the_sync_is_built_once_and_reused(tmp_path: Path) -> None:
    built = 0
    working = _working_factory(tmp_path, StubPolar([]))

    async def counting(should_stop: Callable[[], bool]) -> Sync:
        nonlocal built
        built += 1
        return await working(should_stop)

    loop, _ = _loop(tmp_path, counting, MAX_CYCLES="3")
    await loop.run()

    assert built == 1


@pytest.mark.asyncio
async def test_a_missing_authorization_keeps_the_container_up_and_says_what_to_open(tmp_path: Path) -> None:
    loop, status = _loop(tmp_path, _failing_factory(AuthorizationRequired("nobody has authorized")))

    await loop.run()

    payload = status.to_dict(NOW)
    assert payload["authorization"] == AuthorizationState.REQUIRED
    assert payload["authorizeUrl"] == "http://nas.local:8080/authorize"


@pytest.mark.asyncio
async def test_a_missing_authorization_is_retried_every_cycle(tmp_path: Path) -> None:
    # Cheap, and it is how a token created at /authorize gets picked up without a restart.
    attempts = 0

    async def factory(should_stop: Callable[[], bool]) -> Sync:  # noqa: ARG001 - matches the real signature
        nonlocal attempts
        attempts += 1
        raise AuthorizationRequired("nobody has authorized")

    loop, _ = _loop(tmp_path, factory, MAX_CYCLES="3")
    await loop.run()

    assert attempts == 3


@pytest.mark.asyncio
async def test_a_rejected_token_is_never_retried(tmp_path: Path) -> None:
    # Hammering an endpoint that is saying no is what turns a bad hour into a blocked account.
    attempts = 0

    async def factory(should_stop: Callable[[], bool]) -> Sync:  # noqa: ARG001 - matches the real signature
        nonlocal attempts
        attempts += 1
        raise AuthenticationFailed("Polar rejected the token")

    loop, status = _loop(tmp_path, factory, MAX_CYCLES="3")
    await loop.run()

    assert attempts == 1
    assert status.to_dict(NOW)["authorization"] == AuthorizationState.REVOKED


@pytest.mark.asyncio
async def test_a_rejected_token_is_still_never_retried_when_nothing_has_changed(tmp_path: Path) -> None:
    # The recovery below must not weaken this: an unchanged token stays terminal even with a stamp
    # reader wired up, or every cycle would re-offer Polar the same credential it just refused.
    attempts = 0

    async def factory(should_stop: Callable[[], bool]) -> Sync:  # noqa: ARG001 - matches the real signature
        nonlocal attempts
        attempts += 1
        raise AuthenticationFailed("Polar rejected the token")

    loop, status = _loop(tmp_path, factory, MAX_CYCLES="3", authorization_stamp=lambda: (POLAR_USER, NOW))
    await loop.run()

    assert attempts == 1
    assert status.to_dict(NOW)["authorization"] == AuthorizationState.REVOKED


@pytest.mark.asyncio
async def test_a_fresh_authorization_revives_a_rejected_one(tmp_path: Path) -> None:
    # A rejection is terminal for the *token*, not for the process. Without this, completing /authorize
    # again left a perfectly good token on disk that the loop would never read: /healthz stayed at 503
    # until someone restarted the container, and nothing in the logs said a restart was what was needed.
    working = _working_factory(tmp_path, StubPolar([]))
    attempts = 0
    reauthorized = False

    async def factory(should_stop: Callable[[], bool]) -> Sync:
        nonlocal attempts, reauthorized
        attempts += 1
        if attempts == 1:
            # Stands in for a human completing /authorize while the loop waits out its interval.
            reauthorized = True
            raise AuthenticationFailed("Polar rejected the token")

        return await working(should_stop)

    def stamp() -> tuple[int, datetime] | None:
        return (POLAR_USER, NOW + timedelta(minutes=1)) if reauthorized else (POLAR_USER, NOW)

    loop, status = _loop(tmp_path, factory, MAX_CYCLES="3", authorization_stamp=stamp)
    await loop.run()

    payload = status.to_dict(NOW)
    assert attempts == 2
    assert payload["authorization"] == AuthorizationState.OK
    assert payload["polarUser"] == POLAR_USER


@pytest.mark.asyncio
async def test_a_second_rejection_of_the_new_token_is_terminal_again(tmp_path: Path) -> None:
    # Recovery must not become a retry loop: if Polar refuses the replacement too, that replacement is
    # what gets recorded as rejected, so the next cycle sees no change and stops trying.
    attempts = 0

    async def factory(should_stop: Callable[[], bool]) -> Sync:  # noqa: ARG001 - matches the real signature
        nonlocal attempts
        attempts += 1
        raise AuthenticationFailed("Polar rejected the token")

    def stamp() -> tuple[int, datetime] | None:
        # One replacement authorization appears after the first rejection, and nothing changes after
        # that — which is what a token store actually looks like when a human re-authorizes once.
        return (POLAR_USER, NOW + timedelta(minutes=1)) if attempts else (POLAR_USER, NOW)

    loop, _ = _loop(tmp_path, factory, MAX_CYCLES="5", authorization_stamp=stamp)
    await loop.run()

    assert attempts == 2


@pytest.mark.asyncio
async def test_a_corrupt_token_store_does_not_revive_a_rejected_token(tmp_path: Path) -> None:
    # `current()` reads the file on every call, so the stamp reader can raise. An unreadable store is
    # not evidence that anything was re-authorized, so it must leave the rejection terminal.
    attempts = 0

    async def factory(should_stop: Callable[[], bool]) -> Sync:  # noqa: ARG001 - matches the real signature
        nonlocal attempts
        attempts += 1
        raise AuthenticationFailed("Polar rejected the token")

    def stamp() -> tuple[int, datetime] | None:
        raise CorruptTokenStore("tokens.json is not valid JSON")

    loop, _ = _loop(tmp_path, factory, MAX_CYCLES="3", authorization_stamp=stamp)
    await loop.run()

    assert attempts == 1


@pytest.mark.asyncio
async def test_a_missing_consent_is_reported_and_not_retried(tmp_path: Path) -> None:
    loop, status = _loop(tmp_path, _failing_factory(ConsentRequired("consents not accepted")), MAX_CYCLES="3")

    await loop.run()

    assert status.to_dict(NOW)["authorization"] == AuthorizationState.CONSENT_REQUIRED


@pytest.mark.asyncio
async def test_a_rate_limit_backs_off_exponentially(tmp_path: Path) -> None:
    loop, status = _loop(tmp_path, _failing_factory(RateLimited("429")), MAX_CYCLES="1", POLL_INTERVAL="900")

    await loop.run()

    assert status.to_dict(NOW)["backoffSeconds"] == 1800.0
    assert loop.next_delay() == 1800.0


@pytest.mark.asyncio
async def test_the_backoff_is_capped(tmp_path: Path) -> None:
    loop, _ = _loop(
        tmp_path,
        _failing_factory(RateLimited("429")),
        MAX_CYCLES="10",
        POLL_INTERVAL="900",
        MAX_BACKOFF_SECONDS="3600",
    )

    await loop.run()

    assert loop.next_delay() == 3600.0


@pytest.mark.asyncio
async def test_a_successful_cycle_clears_the_backoff(tmp_path: Path) -> None:
    # The plan's version of this test cleared `client.list_error` around a bare `asyncio.sleep(0)`,
    # betting that cycle 2 would start before the assertion. But cycle 2 only starts after the loop's
    # real (wall-clock) wait between cycles elapses or is woken, and after cycle 1 fails that wait is
    # the just-computed 1800-second backoff — `asyncio.sleep(0)` does not advance real time, so the
    # plan's version hangs for a genuine 1800 seconds instead of asserting anything. This version
    # waits for the observable effect of cycle 1 (backoffSeconds == 1800.0) and then uses the wake
    # event to cut the real wait short, the same way a webhook would in production.
    client = StubPolar([], list_error=RateLimited("429"))
    wake = asyncio.Event()
    loop, status = _loop(tmp_path, _working_factory(tmp_path, client), wake=wake, MAX_CYCLES="2", POLL_INTERVAL="900")

    task = asyncio.ensure_future(loop.run())

    async def clear_the_error_once_backed_off() -> None:
        while status.to_dict(NOW)["backoffSeconds"] != 1800.0:  # noqa: ASYNC110 - spin-wait, not a real sleep
            await asyncio.sleep(0)
        client.list_error = None
        wake.set()

    await asyncio.wait_for(clear_the_error_once_backed_off(), timeout=5)
    await asyncio.wait_for(task, timeout=5)

    assert loop.next_delay() != 1800.0


@pytest.mark.asyncio
async def test_the_delay_is_jittered_within_the_configured_percentage(tmp_path: Path) -> None:
    # So every deployment of this image does not hit Polar on the same second of the interval.
    loop, _ = _loop(tmp_path, _working_factory(tmp_path, StubPolar([])), POLL_INTERVAL="1000", POLL_JITTER_PCT="10")

    delays = {loop.next_delay() for _ in range(50)}

    assert len(delays) > 1
    assert all(900.0 <= delay <= 1100.0 for delay in delays)


@pytest.mark.asyncio
async def test_zero_jitter_gives_the_exact_interval(tmp_path: Path) -> None:
    loop, _ = _loop(tmp_path, _working_factory(tmp_path, StubPolar([])), POLL_INTERVAL="1000", POLL_JITTER_PCT="0")

    assert loop.next_delay() == 1000.0


@pytest.mark.asyncio
async def test_a_stop_request_ends_the_loop(tmp_path: Path) -> None:
    loop, _ = _loop(tmp_path, _working_factory(tmp_path, StubPolar([])), MAX_CYCLES="0", POLL_INTERVAL="3600")

    task = asyncio.ensure_future(loop.run())
    await asyncio.sleep(0)
    loop.request_stop()

    await asyncio.wait_for(task, timeout=5)

    assert loop.stopping


@pytest.mark.asyncio
async def test_a_wake_event_cuts_the_wait_short(tmp_path: Path) -> None:
    # This is what makes a webhook worth having: latency drops from POLL_INTERVAL to seconds.
    wake = asyncio.Event()
    client = StubPolar([])
    loop, _ = _loop(tmp_path, _working_factory(tmp_path, client), wake=wake, MAX_CYCLES="2", POLL_INTERVAL="3600")

    task = asyncio.ensure_future(loop.run())
    while client.listed < 1:  # noqa: ASYNC110 - spin-waiting on a stub's counter, not a real sleep
        await asyncio.sleep(0)
    wake.set()

    await asyncio.wait_for(task, timeout=5)

    assert client.listed == 2


@pytest.mark.asyncio
async def test_the_wake_event_is_cleared_after_being_acted_on(tmp_path: Path) -> None:
    wake = asyncio.Event()
    loop, _ = _loop(
        tmp_path, _working_factory(tmp_path, StubPolar([])), wake=wake, MAX_CYCLES="2", POLL_INTERVAL="3600"
    )

    task = asyncio.ensure_future(loop.run())
    wake.set()
    await asyncio.wait_for(task, timeout=5)

    assert not wake.is_set()


@pytest.mark.asyncio
async def test_an_unusable_watch_folder_is_recorded_and_not_fatal(tmp_path: Path) -> None:
    # A `sync_factory` calls `watch_folder.prepare()` on every cycle it runs (every cycle while
    # unauthorized, and the first cycle after authorization); if the folder is unwritable that must be
    # a failed cycle, not a crash that takes the whole container down with it.
    loop, status = _loop(
        tmp_path, _failing_factory(WatchFolderUnusable("watch folder is not writable")), MAX_CYCLES="2"
    )

    await loop.run()

    assert status.to_dict(NOW)["lastError"] is not None


@pytest.mark.asyncio
async def test_an_unusable_watch_folder_mid_cycle_is_recorded_and_not_fatal(tmp_path: Path) -> None:
    # Symmetrical with the factory-side failure above: whatever raises `WatchFolderUnusable` out of
    # `Sync.run_once()`, `_run_one_cycle`'s except tuple must treat it the same way as `UndeliverableFile`
    # or `OSError` rather than let it propagate out of `SyncLoop.run()`.
    client = StubPolar([], list_error=WatchFolderUnusable("watch folder vanished mid-cycle"))
    loop, status = _loop(tmp_path, _working_factory(tmp_path, client), MAX_CYCLES="1")

    await loop.run()

    assert status.to_dict(NOW)["lastError"] is not None


@pytest.mark.asyncio
async def test_an_unexpected_cycle_failure_is_recorded_and_the_loop_continues(tmp_path: Path) -> None:
    client = StubPolar([], list_error=OSError("the state volume vanished"))
    loop, status = _loop(tmp_path, _working_factory(tmp_path, client), MAX_CYCLES="2")

    await loop.run()

    assert status.to_dict(NOW)["lastError"] is not None
    assert client.listed == 2


@pytest.mark.asyncio
async def test_a_successful_cycle_reports_the_rate_limit_budget(tmp_path: Path) -> None:
    client = StubPolar([])
    client.rate_limit = (12, 520, 340, 5100, 640)
    loop, status = _loop(tmp_path, _working_factory(tmp_path, client))

    await loop.run()

    assert status.to_dict(NOW)["rateLimit"] == {
        "shortUsage": 12,
        "shortLimit": 520,
        "longUsage": 340,
        "longLimit": 5100,
        "resetSeconds": 640,
    }


@pytest.mark.asyncio
async def test_an_unknown_rate_limit_is_not_reported(tmp_path: Path) -> None:
    loop, status = _loop(tmp_path, _working_factory(tmp_path, StubPolar([])))

    await loop.run()

    assert status.to_dict(NOW)["rateLimit"] is None


@pytest.mark.asyncio
async def test_a_token_from_the_cli_login_is_reported_as_authorized(tmp_path: Path) -> None:
    # Regression: `status.authorized()` used to be called only from the server's /callback route, so a
    # token created by `dreeve-polar-connector login` left /status saying "required" and /healthz
    # returning 503 forever — while the connector was in fact syncing perfectly. Docker would mark a
    # working container unhealthy indefinitely.
    loop, status = _loop(tmp_path, _working_factory(tmp_path, StubPolar([])))

    await loop.run()

    payload = status.to_dict(NOW)
    assert payload["authorization"] == AuthorizationState.OK
    assert payload["polarUser"] == POLAR_USER
    assert payload["authorizeUrl"] is None
    assert payload["healthy"] is True
