"""The ledger's contract in three parts: which exercises are still owed, that a corrupt file is refused
rather than silently replaced, and that a flush is all-or-nothing."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from dreeve_polar_connector.ledger import (
    LEDGER_FILENAME,
    LEDGER_VERSION,
    CorruptLedger,
    ExerciseStatus,
    Ledger,
)

NOW = datetime(2026, 7, 29, 8, 0, tzinfo=UTC)
STARTED = datetime(2026, 7, 28, 6, 13, 42, tzinfo=UTC)
USER = 475


def _seeded(path: Path) -> Ledger:
    ledger = Ledger.load(path / LEDGER_FILENAME)
    ledger.mark_seen(
        "2AC312F", polar_user_id=USER, start_time=STARTED, sport="RUNNING", detailed_sport="RUNNING", at=NOW
    )
    return ledger


def test_a_missing_file_starts_an_empty_ledger(tmp_path: Path) -> None:
    ledger = Ledger.load(tmp_path / LEDGER_FILENAME)

    assert len(ledger) == 0
    assert ledger.resolved_since is None
    assert ledger.last_successful_sync is None


def test_a_seen_exercise_is_pending(tmp_path: Path) -> None:
    ledger = _seeded(tmp_path)
    entry = ledger.entry("2AC312F")

    assert entry is not None
    assert entry.status is ExerciseStatus.PENDING
    assert entry.polar_user_id == USER
    assert entry.start_time == STARTED
    assert entry.attempts == 0


def test_seeing_a_known_exercise_again_changes_nothing(tmp_path: Path) -> None:
    ledger = _seeded(tmp_path)
    ledger.mark_delivered("2AC312F", ("2AC312F.fit",), NOW)

    ledger.mark_seen("2AC312F", polar_user_id=USER, start_time=STARTED, sport="CYCLING", detailed_sport="", at=NOW)
    entry = ledger.entry("2AC312F")

    assert entry is not None
    assert entry.status is ExerciseStatus.DELIVERED
    assert entry.sport == "RUNNING"


def test_marking_an_unseen_exercise_is_a_programming_error(tmp_path: Path) -> None:
    ledger = Ledger.load(tmp_path / LEDGER_FILENAME)

    with pytest.raises(KeyError, match="never seen"):
        ledger.mark_delivered("nope", (), NOW)


@pytest.mark.parametrize(
    "status,terminal",
    [
        (ExerciseStatus.PENDING, False),
        (ExerciseStatus.FAILED, False),
        (ExerciseStatus.DELIVERED, True),
        (ExerciseStatus.SKIPPED, True),
        (ExerciseStatus.NO_FILE, True),
    ],
)
def test_which_statuses_are_terminal(status: ExerciseStatus, terminal: bool) -> None:
    # Terminal entries are never reconsidered, which is what keeps re-listing 30 days cheap.
    assert status.is_terminal is terminal


def test_delivering_records_the_files_and_counts_the_attempt(tmp_path: Path) -> None:
    ledger = _seeded(tmp_path)

    ledger.mark_delivered("2AC312F", ("2AC312F.fit",), NOW)
    entry = ledger.entry("2AC312F")

    assert entry is not None
    assert entry.status is ExerciseStatus.DELIVERED
    assert entry.files == ("2AC312F.fit",)
    assert entry.attempts == 1
    assert entry.last_attempt == NOW


def test_failing_records_the_error(tmp_path: Path) -> None:
    ledger = _seeded(tmp_path)

    ledger.mark_failed("2AC312F", "Polar timed out", NOW)
    entry = ledger.entry("2AC312F")

    assert entry is not None
    assert entry.status is ExerciseStatus.FAILED
    assert entry.error == "Polar timed out"
    assert entry.attempts == 1


def test_skipping_does_not_count_as_an_attempt(tmp_path: Path) -> None:
    # Nothing was downloaded, so the attempt budget must not be spent.
    ledger = _seeded(tmp_path)

    ledger.mark_skipped("2AC312F", NOW)
    entry = ledger.entry("2AC312F")

    assert entry is not None
    assert entry.status is ExerciseStatus.SKIPPED
    assert entry.attempts == 0


def test_since_resolves_once_and_is_remembered(tmp_path: Path) -> None:
    # Re-resolving `SINCE=now` every boot would silently skip everything recorded while we were down.
    ledger = Ledger.load(tmp_path / LEDGER_FILENAME)

    first = ledger.resolve_since(NOW)
    second = ledger.resolve_since(datetime(2026, 12, 1, tzinfo=UTC))

    assert first == NOW
    assert second == NOW


def test_counts_by_status_reports_every_status(tmp_path: Path) -> None:
    ledger = _seeded(tmp_path)

    counts = ledger.counts_by_status()

    assert counts[ExerciseStatus.PENDING] == 1
    assert counts[ExerciseStatus.DELIVERED] == 0


@pytest.mark.asyncio
async def test_a_flushed_ledger_round_trips(tmp_path: Path) -> None:
    path = tmp_path / LEDGER_FILENAME
    ledger = _seeded(tmp_path)
    ledger.mark_delivered("2AC312F", ("2AC312F.fit",), NOW)
    ledger.mark_successful_sync(NOW)
    await ledger.flush()

    reloaded = Ledger.load(path)
    entry = reloaded.entry("2AC312F")

    assert entry is not None
    assert entry.exercise_id == "2AC312F"
    assert entry.status is ExerciseStatus.DELIVERED
    assert entry.files == ("2AC312F.fit",)
    assert reloaded.last_successful_sync == NOW


@pytest.mark.asyncio
async def test_the_exercise_id_is_the_key_and_is_not_repeated_in_the_value(tmp_path: Path) -> None:
    path = tmp_path / LEDGER_FILENAME
    ledger = _seeded(tmp_path)
    await ledger.flush()

    raw = json.loads(path.read_text(encoding="utf-8"))

    assert list(raw["exercises"]) == ["2AC312F"]
    assert "exerciseId" not in raw["exercises"]["2AC312F"]


@pytest.mark.asyncio
async def test_a_clean_ledger_is_not_rewritten(tmp_path: Path) -> None:
    path = tmp_path / LEDGER_FILENAME
    ledger = Ledger.load(path)

    await ledger.flush()

    assert not path.exists()


@pytest.mark.asyncio
async def test_flushing_leaves_no_temp_file_behind(tmp_path: Path) -> None:
    ledger = _seeded(tmp_path)
    await ledger.flush()

    assert list(tmp_path.glob("*.tmp")) == []


def test_a_leftover_temp_file_is_discarded_on_load(tmp_path: Path) -> None:
    # A temp file means a previous flush died mid-write; its contents are partial by definition.
    path = tmp_path / LEDGER_FILENAME
    temp = tmp_path / f"{LEDGER_FILENAME}.tmp"
    temp.write_text("{ half written", encoding="utf-8")

    Ledger.load(path)

    assert not temp.exists()


def test_invalid_json_is_refused_rather_than_silently_replaced(tmp_path: Path) -> None:
    path = tmp_path / LEDGER_FILENAME
    path.write_text("{ not json", encoding="utf-8")

    with pytest.raises(CorruptLedger, match="not valid JSON"):
        Ledger.load(path)


def test_a_newer_version_is_refused_and_says_to_upgrade(tmp_path: Path) -> None:
    path = tmp_path / LEDGER_FILENAME
    path.write_text(json.dumps({"version": LEDGER_VERSION + 1, "exercises": {}}), encoding="utf-8")

    with pytest.raises(CorruptLedger, match="upgrade"):
        Ledger.load(path)


def test_an_unreadable_entry_is_refused(tmp_path: Path) -> None:
    path = tmp_path / LEDGER_FILENAME
    path.write_text(
        json.dumps({"version": LEDGER_VERSION, "exercises": {"2AC312F": {"status": "delivered"}}}),
        encoding="utf-8",
    )

    with pytest.raises(CorruptLedger, match="2AC312F"):
        Ledger.load(path)


def test_an_unknown_status_is_refused(tmp_path: Path) -> None:
    path = tmp_path / LEDGER_FILENAME
    path.write_text(
        json.dumps(
            {
                "version": LEDGER_VERSION,
                "exercises": {
                    "2AC312F": {
                        "polarUserId": USER,
                        "startTime": STARTED.isoformat(),
                        "sport": "RUNNING",
                        "status": "teleported",
                        "firstSeen": NOW.isoformat(),
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(CorruptLedger):
        Ledger.load(path)
