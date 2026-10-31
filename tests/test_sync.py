"""Every failure branch gets its own test, because each one means something different to the daemon and
mixing them up is how activities get lost or an account gets blocked."""

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from dreeve_polar_connector.config import Config, FallbackFormat
from dreeve_polar_connector.delivery import WatchFolder
from dreeve_polar_connector.dreeve import DreeveUnreachable
from dreeve_polar_connector.ledger import LEDGER_FILENAME, ExerciseStatus, Ledger
from dreeve_polar_connector.polar import (
    AuthenticationFailed,
    BodyWeight,
    ConnectionFailed,
    ConsentRequired,
    NotFound,
    RateLimited,
    UnexpectedResponse,
)
from dreeve_polar_connector.sync import Sync
from dreeve_polar_connector.weight import WeightOutcome, WeightSync
from dreeve_polar_connector.weight_state import WEIGHT_STATE_FILENAME, WeightState
from tests.stubs import FIT_BYTES, TCX_BYTES, StubClock, StubPolar, exercise

POLAR_USER = 475
NOW = datetime(2026, 7, 29, 8, 0, tzinfo=UTC)


def _config(tmp_path: Path, **overrides: str) -> Config:
    environment = {
        "POLAR_CLIENT_ID": "a-client",
        "POLAR_CLIENT_SECRET": "a-secret",
        "WATCH_DIR": str(tmp_path / "watch"),
        "STATE_DIR": str(tmp_path / "state"),
        "POLAR_TOKENS": str(tmp_path / "tokens"),
        "DOWNLOAD_DELAY_SECONDS": "0",
        **overrides,
    }

    return Config.from_env(environment)


def _sync(tmp_path: Path, client: StubPolar, clock: StubClock | None = None, **overrides: str) -> Sync:
    config = _config(tmp_path, **overrides)

    return Sync(
        config=config,
        ledger=Ledger.load(config.state_dir / LEDGER_FILENAME),
        client=client,
        watch_folder=WatchFolder(config.watch_dir, config.on_conflict),
        polar_user_id=POLAR_USER,
        clock=clock or StubClock(NOW),
    )


@pytest.mark.asyncio
async def test_a_new_exercise_is_delivered_as_a_fit_file(tmp_path: Path) -> None:
    sync = _sync(tmp_path, StubPolar([exercise()]))
    await sync.watch_folder_ready()

    result = await sync.run_once()

    assert result.delivered == 1
    assert (tmp_path / "watch" / "2AC312F.fit").read_bytes() == FIT_BYTES


@pytest.mark.asyncio
async def test_a_delivered_exercise_is_recorded_and_never_downloaded_twice(tmp_path: Path) -> None:
    client = StubPolar([exercise()])
    sync = _sync(tmp_path, client)
    await sync.watch_folder_ready()

    await sync.run_once()
    await sync.run_once()

    assert client.fit_requests == ["2AC312F"]
    assert sync.ledger.counts_by_status()[ExerciseStatus.DELIVERED] == 1


@pytest.mark.asyncio
async def test_the_ledger_survives_a_restart(tmp_path: Path) -> None:
    client = StubPolar([exercise()])
    first = _sync(tmp_path, client)
    await first.watch_folder_ready()
    await first.run_once()

    second = _sync(tmp_path, client)
    await second.run_once()

    assert client.fit_requests == ["2AC312F"]


@pytest.mark.asyncio
async def test_an_exercise_older_than_since_is_skipped_without_downloading(tmp_path: Path) -> None:
    old = exercise("OLD", start_time=NOW - timedelta(days=20))
    client = StubPolar([old])
    sync = _sync(tmp_path, client, SINCE="-7d")
    await sync.watch_folder_ready()

    result = await sync.run_once()

    assert result.skipped == 0  # skipping happens at discovery, not as a download outcome
    assert client.fit_requests == []
    entry = sync.ledger.entry("OLD")
    assert entry is not None
    assert entry.status is ExerciseStatus.SKIPPED


@pytest.mark.asyncio
async def test_an_excluded_sport_is_skipped_without_downloading(tmp_path: Path) -> None:
    client = StubPolar([exercise(sport="SWIMMING", detailed_sport="POOL_SWIMMING")])
    sync = _sync(tmp_path, client, SPORTS="running,cycling")
    await sync.watch_folder_ready()

    await sync.run_once()

    assert client.fit_requests == []
    entry = sync.ledger.entry("2AC312F")
    assert entry is not None
    assert entry.status is ExerciseStatus.SKIPPED


@pytest.mark.asyncio
async def test_widening_the_sport_filter_does_not_resurrect_a_skipped_exercise(tmp_path: Path) -> None:
    # SKIPPED is terminal. Documented behaviour: delete the ledger to reconsider old exercises.
    client = StubPolar([exercise(sport="SWIMMING", detailed_sport="POOL_SWIMMING")])
    await (await _prepared(tmp_path, client, SPORTS="running")).run_once()

    sync = _sync(tmp_path, client, SPORTS="running,swimming")
    await sync.run_once()

    assert client.fit_requests == []


@pytest.mark.asyncio
async def test_since_resolves_once_so_a_restart_does_not_move_the_floor(tmp_path: Path) -> None:
    clock = StubClock(NOW)
    first = _sync(tmp_path, StubPolar(), clock, SINCE="now")
    await first.watch_folder_ready()
    await first.run_once()

    clock.advance(86400 * 3)
    recorded_while_down = exercise("LATER", start_time=NOW + timedelta(days=1))
    client = StubPolar([recorded_while_down])
    second = _sync(tmp_path, client, clock, SINCE="now")
    result = await second.run_once()

    assert result.delivered == 1


@pytest.mark.asyncio
async def test_an_exercise_without_a_fit_falls_back_to_tcx(tmp_path: Path) -> None:
    client = StubPolar([exercise()], fit_errors={"2AC312F": NotFound("no fit")})
    sync = _sync(tmp_path, client)
    await sync.watch_folder_ready()

    result = await sync.run_once()

    assert result.delivered == 1
    assert client.fallback_requests == [("2AC312F", FallbackFormat.TCX)]
    assert (tmp_path / "watch" / "2AC312F.tcx").read_bytes() == TCX_BYTES


@pytest.mark.asyncio
async def test_an_exercise_with_neither_a_fit_nor_a_fallback_is_recorded_as_having_no_file(tmp_path: Path) -> None:
    # A 404 on both means the exercise was deleted in Flow, or aged past the 30-day window. Terminal.
    client = StubPolar(
        [exercise()],
        fit_errors={"2AC312F": NotFound("no fit")},
        fallback_errors={"2AC312F": NotFound("gone")},
    )
    sync = _sync(tmp_path, client)
    await sync.watch_folder_ready()

    result = await sync.run_once()

    assert result.without_file == 1
    entry = sync.ledger.entry("2AC312F")
    assert entry is not None
    assert entry.status is ExerciseStatus.NO_FILE


@pytest.mark.asyncio
async def test_no_fallback_is_attempted_when_the_format_is_none(tmp_path: Path) -> None:
    client = StubPolar([exercise()], fit_errors={"2AC312F": NotFound("no fit")})
    sync = _sync(tmp_path, client, FALLBACK_FORMAT="none")
    await sync.watch_folder_ready()

    result = await sync.run_once()

    assert client.fallback_requests == []
    assert result.without_file == 1


@pytest.mark.asyncio
async def test_a_connection_failure_is_retried_next_cycle(tmp_path: Path) -> None:
    client = StubPolar([exercise()], fit_errors={"2AC312F": ConnectionFailed("blip")})
    sync = _sync(tmp_path, client)
    await sync.watch_folder_ready()

    result = await sync.run_once()

    assert result.failed == 1
    entry = sync.ledger.entry("2AC312F")
    assert entry is not None
    assert entry.status is ExerciseStatus.FAILED
    assert entry.attempts == 1

    client.fit_errors = {}
    assert (await sync.run_once()).delivered == 1


@pytest.mark.asyncio
async def test_an_exercise_is_abandoned_after_max_attempts(tmp_path: Path) -> None:
    client = StubPolar([exercise()], fit_errors={"2AC312F": ConnectionFailed("blip")})
    sync = _sync(tmp_path, client, MAX_ATTEMPTS="2")
    await sync.watch_folder_ready()

    await sync.run_once()
    await sync.run_once()
    await sync.run_once()

    assert client.fit_requests == ["2AC312F", "2AC312F"]


@pytest.mark.asyncio
async def test_a_rate_limit_while_listing_aborts_the_cycle(tmp_path: Path) -> None:
    sync = _sync(tmp_path, StubPolar(list_error=RateLimited("429")))
    await sync.watch_folder_ready()

    with pytest.raises(RateLimited):
        await sync.run_once()


@pytest.mark.asyncio
async def test_a_rate_limit_mid_batch_keeps_what_was_already_learned(tmp_path: Path) -> None:
    # Losing the ledger here would mean downloading the delivered file again.
    client = StubPolar(
        [exercise("FIRST"), exercise("SECOND")],
        fit_errors={"SECOND": RateLimited("429")},
    )
    sync = _sync(tmp_path, client)
    await sync.watch_folder_ready()

    with pytest.raises(RateLimited):
        await sync.run_once()

    reloaded = Ledger.load(tmp_path / "state" / LEDGER_FILENAME)
    first = reloaded.entry("FIRST")
    assert first is not None
    assert first.status is ExerciseStatus.DELIVERED


@pytest.mark.asyncio
async def test_a_listing_failure_that_is_not_cycle_fatal_still_flushes_resolved_since(tmp_path: Path) -> None:
    # `resolve_since` runs and dirties `resolved_since` in memory before `_discover` is ever called.
    # `ConnectionFailed` and `UnexpectedResponse` are not in `FATAL_TO_THE_CYCLE`, so nothing caught them
    # on the way out of `_discover` and the dirtied value was never written — a container restart during
    # a run of listing failures would then re-resolve SINCE against the new boot time.
    client = StubPolar(list_error=UnexpectedResponse("Polar's listing response was not valid JSON"))
    sync = _sync(tmp_path, client, SINCE="now")
    await sync.watch_folder_ready()

    with pytest.raises(UnexpectedResponse):
        await sync.run_once()

    reloaded = Ledger.load(tmp_path / "state" / LEDGER_FILENAME)
    assert reloaded.resolved_since == NOW


@pytest.mark.asyncio
async def test_a_dead_token_aborts_the_cycle(tmp_path: Path) -> None:
    sync = _sync(tmp_path, StubPolar(list_error=AuthenticationFailed("401")))
    await sync.watch_folder_ready()

    with pytest.raises(AuthenticationFailed):
        await sync.run_once()


@pytest.mark.asyncio
async def test_a_missing_consent_aborts_the_cycle(tmp_path: Path) -> None:
    sync = _sync(tmp_path, StubPolar(list_error=ConsentRequired("403")))
    await sync.watch_folder_ready()

    with pytest.raises(ConsentRequired):
        await sync.run_once()


@pytest.mark.asyncio
async def test_an_unexpected_error_on_one_exercise_does_not_kill_the_cycle(tmp_path: Path) -> None:
    client = StubPolar(
        [exercise("FIRST"), exercise("SECOND")],
        fit_errors={"FIRST": RuntimeError("something nobody predicted")},
    )
    sync = _sync(tmp_path, client)
    await sync.watch_folder_ready()

    result = await sync.run_once()

    assert result.failed == 1
    assert result.delivered == 1


@pytest.mark.asyncio
async def test_the_per_cycle_cap_leaves_the_rest_as_backlog(tmp_path: Path) -> None:
    client = StubPolar([exercise(f"E{index}") for index in range(5)])
    sync = _sync(tmp_path, client, MAX_DOWNLOADS_PER_CYCLE="2")
    await sync.watch_folder_ready()

    result = await sync.run_once()

    assert result.delivered == 2
    assert result.backlog == 3


@pytest.mark.asyncio
async def test_the_oldest_waiting_exercise_is_downloaded_first(tmp_path: Path) -> None:
    newest = exercise("NEW", start_time=NOW - timedelta(hours=1))
    oldest = exercise("OLD", start_time=NOW - timedelta(days=5))
    client = StubPolar([newest, oldest])
    sync = _sync(tmp_path, client, MAX_DOWNLOADS_PER_CYCLE="1")
    await sync.watch_folder_ready()

    await sync.run_once()

    assert client.fit_requests == ["OLD"]


@pytest.mark.asyncio
async def test_downloads_are_paced(tmp_path: Path) -> None:
    clock = StubClock(NOW)
    client = StubPolar([exercise("FIRST"), exercise("SECOND")])
    sync = _sync(tmp_path, client, clock, DOWNLOAD_DELAY_SECONDS="2")
    await sync.watch_folder_ready()

    await sync.run_once()

    assert clock.slept == [2.0]


@pytest.mark.asyncio
async def test_a_dry_run_downloads_nothing_and_writes_nothing(tmp_path: Path) -> None:
    client = StubPolar([exercise()])
    sync = _sync(tmp_path, client, DRY_RUN="true")
    await sync.watch_folder_ready()

    result = await sync.run_once()

    assert client.fit_requests == []
    assert result.backlog == 1
    assert not (tmp_path / "state" / LEDGER_FILENAME).exists()


@pytest.mark.asyncio
async def test_a_stop_request_finishes_the_file_in_flight_and_leaves_the_rest_pending(tmp_path: Path) -> None:
    client = StubPolar([exercise("FIRST"), exercise("SECOND")])
    config = _config(tmp_path)
    sync = Sync(
        config=config,
        ledger=Ledger.load(config.state_dir / LEDGER_FILENAME),
        client=client,
        watch_folder=WatchFolder(config.watch_dir, config.on_conflict),
        polar_user_id=POLAR_USER,
        clock=StubClock(NOW),
        should_stop=lambda: True,
    )
    await sync.watch_folder_ready()

    result = await sync.run_once()

    assert result.delivered == 0
    assert client.fit_requests == []


@pytest.mark.asyncio
async def test_an_empty_polar_account_is_a_successful_cycle(tmp_path: Path) -> None:
    sync = _sync(tmp_path, StubPolar([]))
    await sync.watch_folder_ready()

    result = await sync.run_once()

    assert result == type(result)(listed=0)
    assert sync.ledger.last_successful_sync == NOW


async def _prepared(tmp_path: Path, client: StubPolar, **overrides: str) -> Sync:
    sync = _sync(tmp_path, client, None, **overrides)
    await sync.watch_folder_ready()

    return sync


WEIGHT = BodyWeight(kilograms=80.4, source="SOURCE_MEASURED", effective_at=datetime(2026, 7, 28, 6, 0, tzinfo=UTC))
WEIGHT_ENV = {"WEIGHT_SYNC": "true", "DREEVE_URL": "http://dreeve:8080", "DREEVE_API_KEY": "drv_key"}


class RecordingDreeve:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.recorded: list[tuple[date, float]] = []

    async def record_weight(self, on: date, weight: float) -> None:
        if self.error is not None:
            raise self.error
        self.recorded.append((on, weight))


def _weighing_sync(tmp_path: Path, client: StubPolar, dreeve: RecordingDreeve, **overrides: str) -> Sync:
    config = _config(tmp_path, **WEIGHT_ENV, **overrides)

    return Sync(
        config=config,
        ledger=Ledger.load(config.state_dir / LEDGER_FILENAME),
        client=client,
        watch_folder=WatchFolder(config.watch_dir, config.on_conflict),
        polar_user_id=POLAR_USER,
        clock=StubClock(NOW),
        weight_sync=WeightSync(
            config=config,
            polar=client,
            dreeve=dreeve,
            state=WeightState.load(config.state_dir / WEIGHT_STATE_FILENAME),
            zone=UTC,
        ),
    )


@pytest.mark.asyncio
async def test_without_weight_sync_polar_is_never_asked_for_a_weight(tmp_path: Path) -> None:
    client = StubPolar([exercise()], weight=WEIGHT)
    sync = _sync(tmp_path, client)
    await sync.watch_folder_ready()

    result = await sync.run_once()

    assert result.weight is None
    assert client.weight_requests == 0
    assert "weight" not in str(result)


@pytest.mark.asyncio
async def test_a_cycle_records_the_weight_after_the_exercises(tmp_path: Path) -> None:
    dreeve = RecordingDreeve()
    sync = _weighing_sync(tmp_path, StubPolar([exercise()], weight=WEIGHT), dreeve)
    await sync.watch_folder_ready()

    result = await sync.run_once()

    assert result.delivered == 1
    assert result.weight is not None
    assert result.weight.outcome is WeightOutcome.PUSHED
    assert dreeve.recorded == [(date(2026, 7, 28), 80.4)]
    assert str(result).endswith("weight pushed")


@pytest.mark.asyncio
async def test_a_weight_that_dreeve_refuses_does_not_fail_the_cycle(tmp_path: Path) -> None:
    sync = _weighing_sync(tmp_path, StubPolar([exercise()], weight=WEIGHT), RecordingDreeve(DreeveUnreachable("down")))
    await sync.watch_folder_ready()

    result = await sync.run_once()

    assert result.delivered == 1
    assert result.weight is not None
    assert result.weight.outcome is WeightOutcome.FAILED


@pytest.mark.asyncio
async def test_a_rate_limit_on_the_weight_keeps_what_the_cycle_delivered(tmp_path: Path) -> None:
    sync = _weighing_sync(tmp_path, StubPolar([exercise()], weight_error=RateLimited("slow down")), RecordingDreeve())
    await sync.watch_folder_ready()

    with pytest.raises(RateLimited):
        await sync.run_once()

    ledger = Ledger.load(tmp_path / "state" / LEDGER_FILENAME)
    entry = ledger.entry("2AC312F")
    assert entry is not None
    assert entry.status is ExerciseStatus.DELIVERED


@pytest.mark.asyncio
async def test_a_dry_run_still_reports_the_weight_it_would_record(tmp_path: Path) -> None:
    dreeve = RecordingDreeve()
    sync = _weighing_sync(tmp_path, StubPolar([exercise()], weight=WEIGHT), dreeve, DRY_RUN="true")

    result = await sync.run_once()

    assert result.weight is not None
    assert result.weight.outcome is WeightOutcome.DRY_RUN
    assert dreeve.recorded == []
