"""The weight state only answers "what did we last record in Dreeve?". Losing it costs one repeated,
idempotent push, so unlike the ledger an unreadable file is replaced rather than refused."""

import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from dreeve_polar_connector.weight_state import (
    WEIGHT_STATE_FILENAME,
    WEIGHT_STATE_VERSION,
    PushedWeight,
    WeightState,
)

PUSHED = PushedWeight(
    on=date(2026, 10, 30),
    kilograms=80.4,
    source="SOURCE_MEASURED",
    pushed_at=datetime(2026, 10, 31, 8, 0, tzinfo=UTC),
)


def test_a_missing_file_means_nothing_was_pushed_yet(tmp_path: Path) -> None:
    state = WeightState.load(tmp_path / WEIGHT_STATE_FILENAME)

    assert state.last_pushed is None


@pytest.mark.asyncio
async def test_a_recorded_push_round_trips(tmp_path: Path) -> None:
    state = WeightState.load(tmp_path / WEIGHT_STATE_FILENAME)
    state.record_push(PUSHED)
    await state.flush()

    assert WeightState.load(tmp_path / WEIGHT_STATE_FILENAME).last_pushed == PUSHED


@pytest.mark.asyncio
async def test_the_file_is_camel_case_and_versioned(tmp_path: Path) -> None:
    state = WeightState.load(tmp_path / WEIGHT_STATE_FILENAME)
    state.record_push(PUSHED)
    await state.flush()

    written = json.loads((tmp_path / WEIGHT_STATE_FILENAME).read_text(encoding="utf-8"))

    assert written == {
        "version": WEIGHT_STATE_VERSION,
        "lastPushed": {
            "on": "2026-10-30",
            "kilograms": 80.4,
            "source": "SOURCE_MEASURED",
            "pushedAt": "2026-10-31T08:00:00Z",
        },
    }


@pytest.mark.asyncio
async def test_an_unchanged_state_is_not_rewritten(tmp_path: Path) -> None:
    await WeightState.load(tmp_path / WEIGHT_STATE_FILENAME).flush()

    assert not (tmp_path / WEIGHT_STATE_FILENAME).exists()


@pytest.mark.asyncio
async def test_flushing_leaves_no_temp_file_behind(tmp_path: Path) -> None:
    state = WeightState.load(tmp_path / WEIGHT_STATE_FILENAME)
    state.record_push(PUSHED)
    await state.flush()

    assert sorted(path.name for path in tmp_path.iterdir()) == [WEIGHT_STATE_FILENAME]


def test_a_leftover_temp_file_is_discarded_on_load(tmp_path: Path) -> None:
    (tmp_path / f"{WEIGHT_STATE_FILENAME}.tmp").write_text("{half", encoding="utf-8")

    WeightState.load(tmp_path / WEIGHT_STATE_FILENAME)

    assert not (tmp_path / f"{WEIGHT_STATE_FILENAME}.tmp").exists()


@pytest.mark.parametrize(
    "contents",
    [
        "{not json",
        "[]",
        json.dumps({"version": 99, "lastPushed": None}),
        json.dumps({"version": WEIGHT_STATE_VERSION, "lastPushed": {"on": "yesterday"}}),
    ],
    ids=["invalid-json", "not-an-object", "other-version", "unreadable-entry"],
)
def test_an_unreadable_file_is_replaced_with_a_warning(
    tmp_path: Path, contents: str, caplog: pytest.LogCaptureFixture
) -> None:
    (tmp_path / WEIGHT_STATE_FILENAME).write_text(contents, encoding="utf-8")

    state = WeightState.load(tmp_path / WEIGHT_STATE_FILENAME)

    assert state.last_pushed is None
    assert "starting over" in caplog.text
