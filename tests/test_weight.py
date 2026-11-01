"""Weight sync's policy, one test per rule: what counts as a reading, when it is pushed, which date it is
filed under, and which failures end the cycle versus wait for the next one."""

from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from dreeve_polar_connector.config import Config
from dreeve_polar_connector.dreeve import DreeveRejected, DreeveUnreachable
from dreeve_polar_connector.polar import (
    AuthenticationFailed,
    BodyWeight,
    ConnectionFailed,
    ConsentRequired,
    RateLimited,
    UnexpectedResponse,
)
from dreeve_polar_connector.weight import WeightOutcome, WeightSync
from dreeve_polar_connector.weight_state import WEIGHT_STATE_FILENAME, PushedWeight, WeightState
from tests.stubs import StubPolar

STARTED = datetime(2026, 10, 31, 8, 0, tzinfo=UTC)
MEASURED_AT = datetime(2026, 10, 30, 7, 12, tzinfo=UTC)
BERLIN = ZoneInfo("Europe/Berlin")


def _measured(kilograms: float = 80.4, *, source: str = "SOURCE_MEASURED", at: datetime = MEASURED_AT) -> BodyWeight:
    return BodyWeight(kilograms=kilograms, source=source, effective_at=at)


class StubDreeve:
    """Records what would have been sent, or raises `error` instead."""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.recorded: list[tuple[date, float]] = []

    async def record_weight(self, on: date, weight: float) -> None:
        if self.error is not None:
            raise self.error
        self.recorded.append((on, weight))


def _config(tmp_path: Path, **overrides: str) -> Config:
    return Config.from_env(
        {
            "POLAR_CLIENT_ID": "a-client",
            "POLAR_CLIENT_SECRET": "a-secret",
            "STATE_DIR": str(tmp_path / "state"),
            "WEIGHT_SYNC": "true",
            "DREEVE_URL": "http://dreeve:8080",
            "DREEVE_API_KEY": "drv_key",
            **overrides,
        }
    )


def _weight_sync(
    tmp_path: Path, polar: StubPolar, dreeve: StubDreeve, *, zone: ZoneInfo = BERLIN, **overrides: str
) -> WeightSync:
    config = _config(tmp_path, **overrides)

    return WeightSync(
        config=config,
        polar=polar,
        dreeve=dreeve,
        state=WeightState.load(config.state_dir / WEIGHT_STATE_FILENAME),
        zone=zone,
    )


def _stored(tmp_path: Path) -> PushedWeight | None:
    return WeightState.load(tmp_path / "state" / WEIGHT_STATE_FILENAME).last_pushed


@pytest.mark.asyncio
async def test_a_new_measurement_is_recorded_in_dreeve_and_remembered(tmp_path: Path) -> None:
    dreeve = StubDreeve()

    result = await _weight_sync(tmp_path, StubPolar(weight=_measured()), dreeve).run_once(STARTED)

    assert result.outcome is WeightOutcome.PUSHED
    assert result.on == date(2026, 10, 30)
    assert result.value == 80.4
    assert dreeve.recorded == [(date(2026, 10, 30), 80.4)]
    assert _stored(tmp_path) == PushedWeight(
        on=date(2026, 10, 30), kilograms=80.4, source="SOURCE_MEASURED", pushed_at=STARTED
    )


@pytest.mark.asyncio
async def test_a_weight_the_user_typed_in_counts_as_a_reading(tmp_path: Path) -> None:
    dreeve = StubDreeve()

    result = await _weight_sync(tmp_path, StubPolar(weight=_measured(source="SOURCE_USER")), dreeve).run_once(STARTED)

    assert result.outcome is WeightOutcome.PUSHED


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["SOURCE_DEFAULT", "SOURCE_ESTIMATE"])
async def test_a_default_or_estimated_weight_is_not_a_reading(tmp_path: Path, source: str) -> None:
    dreeve = StubDreeve()

    result = await _weight_sync(tmp_path, StubPolar(weight=_measured(source=source)), dreeve).run_once(STARTED)

    assert result.outcome is WeightOutcome.NOT_MEASURED
    assert dreeve.recorded == []
    assert _stored(tmp_path) is None


@pytest.mark.asyncio
async def test_no_weight_in_polar_records_nothing(tmp_path: Path) -> None:
    dreeve = StubDreeve()

    result = await _weight_sync(tmp_path, StubPolar(weight=None), dreeve).run_once(STARTED)

    assert result.outcome is WeightOutcome.NO_DATA
    assert dreeve.recorded == []


@pytest.mark.asyncio
async def test_the_same_weight_again_is_not_recorded_twice(tmp_path: Path) -> None:
    dreeve = StubDreeve()
    await _weight_sync(tmp_path, StubPolar(weight=_measured()), dreeve).run_once(STARTED)

    result = await _weight_sync(tmp_path, StubPolar(weight=_measured()), dreeve).run_once(STARTED)

    assert result.outcome is WeightOutcome.UNCHANGED
    assert dreeve.recorded == [(date(2026, 10, 30), 80.4)]


@pytest.mark.asyncio
async def test_an_unrelated_profile_change_does_not_file_the_weight_under_a_new_date(tmp_path: Path) -> None:
    # Editing resting heart rate in Polar bumps `modified` without touching the weight.
    dreeve = StubDreeve()
    await _weight_sync(tmp_path, StubPolar(weight=_measured()), dreeve).run_once(STARTED)
    later = _measured(at=datetime(2026, 11, 5, 9, 0, tzinfo=UTC))

    result = await _weight_sync(tmp_path, StubPolar(weight=later), dreeve).run_once(STARTED)

    assert result.outcome is WeightOutcome.UNCHANGED
    assert len(dreeve.recorded) == 1


@pytest.mark.asyncio
async def test_a_difference_below_the_reported_precision_is_unchanged(tmp_path: Path) -> None:
    dreeve = StubDreeve()
    await _weight_sync(tmp_path, StubPolar(weight=_measured(80.4)), dreeve).run_once(STARTED)

    result = await _weight_sync(tmp_path, StubPolar(weight=_measured(80.44)), dreeve).run_once(STARTED)

    assert result.outcome is WeightOutcome.UNCHANGED


@pytest.mark.asyncio
async def test_a_changed_weight_is_recorded_under_its_own_date(tmp_path: Path) -> None:
    dreeve = StubDreeve()
    await _weight_sync(tmp_path, StubPolar(weight=_measured(80.4)), dreeve).run_once(STARTED)
    lighter = _measured(79.8, at=datetime(2026, 11, 6, 6, 30, tzinfo=UTC))

    result = await _weight_sync(tmp_path, StubPolar(weight=lighter), dreeve).run_once(STARTED)

    assert result.outcome is WeightOutcome.PUSHED
    assert dreeve.recorded == [(date(2026, 10, 30), 80.4), (date(2026, 11, 6), 79.8)]


@pytest.mark.asyncio
async def test_the_date_is_the_local_one_not_utcs(tmp_path: Path) -> None:
    # 23:30 UTC on the 30th is already the 31st in Berlin (UTC+1 in November).
    dreeve = StubDreeve()
    late = _measured(at=datetime(2026, 10, 30, 23, 30, tzinfo=UTC))

    await _weight_sync(tmp_path, StubPolar(weight=late), dreeve).run_once(STARTED)

    assert dreeve.recorded == [(date(2026, 10, 31), 80.4)]


@pytest.mark.asyncio
async def test_pounds_are_sent_when_dreeve_reads_pounds(tmp_path: Path) -> None:
    dreeve = StubDreeve()

    weight_sync = _weight_sync(tmp_path, StubPolar(weight=_measured(80.4)), dreeve, DREEVE_WEIGHT_UNIT="lb")

    result = await weight_sync.run_once(STARTED)

    assert dreeve.recorded == [(date(2026, 10, 30), 177.3)]
    assert result.value == 177.3
    # Remembered in Polar's unit, so switching DREEVE_WEIGHT_UNIT does not read as a weight change.
    stored = _stored(tmp_path)
    assert stored is not None
    assert stored.kilograms == 80.4


@pytest.mark.asyncio
async def test_a_dry_run_sends_and_remembers_nothing(tmp_path: Path) -> None:
    dreeve = StubDreeve()

    result = await _weight_sync(tmp_path, StubPolar(weight=_measured()), dreeve, DRY_RUN="true").run_once(STARTED)

    assert result.outcome is WeightOutcome.DRY_RUN
    assert dreeve.recorded == []
    assert _stored(tmp_path) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        DreeveUnreachable("Could not reach Dreeve at http://dreeve:8080: refused."),
        DreeveRejected(401, "Dreeve rejected DREEVE_API_KEY (401)."),
    ],
    ids=["unreachable", "rejected"],
)
async def test_a_dreeve_failure_waits_for_the_next_cycle_and_remembers_nothing(
    tmp_path: Path, error: Exception
) -> None:
    result = await _weight_sync(tmp_path, StubPolar(weight=_measured()), StubDreeve(error)).run_once(STARTED)

    assert result.outcome is WeightOutcome.FAILED
    assert result.error == str(error)
    assert _stored(tmp_path) is None


@pytest.mark.asyncio
async def test_the_failed_weight_is_pushed_once_dreeve_is_back(tmp_path: Path) -> None:
    polar = StubPolar(weight=_measured())
    await _weight_sync(tmp_path, polar, StubDreeve(DreeveUnreachable("down"))).run_once(STARTED)
    dreeve = StubDreeve()

    result = await _weight_sync(tmp_path, polar, dreeve).run_once(STARTED)

    assert result.outcome is WeightOutcome.PUSHED
    assert dreeve.recorded == [(date(2026, 10, 30), 80.4)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error", [ConnectionFailed("Could not reach Polar."), UnexpectedResponse("Not JSON.")], ids=["network", "garbled"]
)
async def test_a_polar_failure_that_is_not_cycle_fatal_is_a_failed_weight(tmp_path: Path, error: Exception) -> None:
    result = await _weight_sync(tmp_path, StubPolar(weight_error=error), StubDreeve()).run_once(STARTED)

    assert result.outcome is WeightOutcome.FAILED
    assert result.error is not None
    assert str(error) in result.error


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [RateLimited("slow down"), AuthenticationFailed("token revoked"), ConsentRequired("accept consents")],
    ids=["rate-limited", "token-rejected", "consent"],
)
async def test_polars_cycle_fatal_failures_are_not_swallowed(tmp_path: Path, error: Exception) -> None:
    with pytest.raises(type(error)):
        await _weight_sync(tmp_path, StubPolar(weight_error=error), StubDreeve()).run_once(STARTED)


@pytest.mark.asyncio
async def test_a_repeated_failure_is_logged_once(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level("DEBUG")
    weight_sync = _weight_sync(tmp_path, StubPolar(weight=_measured()), StubDreeve(DreeveUnreachable("down")))

    await weight_sync.run_once(STARTED)
    await weight_sync.run_once(STARTED)

    warnings = [record for record in caplog.records if record.levelname == "WARNING" and "down" in record.message]
    assert len(warnings) == 1
