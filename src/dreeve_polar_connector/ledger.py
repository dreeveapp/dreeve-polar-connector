"""Persistent record of which Polar exercises have already been handled.

Dreeve deletes files from its watch folder as soon as it imports them, so "is the file still there?" can
never answer "did we already fetch this?". This ledger is the only thing that can, which makes it the one
piece of state whose loss costs a full re-download.

Two properties follow. Writes are atomic — a half-written ledger is worse than none — and they are
batched: the file is rewritten once per cycle, not once per exercise.

`polarUserId` is recorded from v1 even though the connector is single-user, because this file is
version-gated and adding a field to it later would mean a migration.
"""

import asyncio
import json
import logging
from collections import Counter
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pydantic.alias_generators import to_camel

from dreeve_polar_connector.state_files import discard_interrupted_write, write_atomically

LEDGER_VERSION = 1
LEDGER_FILENAME = "ledger.json"

logger = logging.getLogger(__name__)

# Every model inlines its own ConfigDict(...) literal rather than sharing a module-level constant.
# The pydantic mypy plugin only reads `frozen=True` — and only marks fields read-only for mypy —
# off a literal ConfigDict(...) call written directly in the class body. Sharing a constant, or
# merging two with `|`, silently loses that. Verified against mypy --strict.


class CorruptLedger(Exception):
    """Raised rather than silently starting over: a fresh ledger re-downloads everything since SINCE."""


class ExerciseStatus(StrEnum):
    PENDING = "pending"
    DELIVERED = "delivered"
    SKIPPED = "skipped"
    FAILED = "failed"
    NO_FILE = "no-file"

    @property
    def is_terminal(self) -> bool:
        """Terminal exercises are never reconsidered, so re-listing the same 30 days stays cheap."""
        return self in TERMINAL_STATUSES


TERMINAL_STATUSES = frozenset({ExerciseStatus.DELIVERED, ExerciseStatus.SKIPPED, ExerciseStatus.NO_FILE})


class LedgerEntry(BaseModel):
    """Frozen: every change goes through `Ledger`, which is what keeps the dirty flag honest.

    `exercise_id` is the dict key in the file rather than a field inside the value, so it is excluded
    from serialisation and injected on load.
    """

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, frozen=True)

    exercise_id: str = Field(default="", exclude=True)
    polar_user_id: int
    start_time: datetime
    sport: str
    status: ExerciseStatus
    first_seen: datetime
    detailed_sport: str = ""
    files: tuple[str, ...] = ()
    attempts: int = 0
    last_attempt: datetime | None = None
    error: str | None = None


class LedgerFile(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    version: int = LEDGER_VERSION
    resolved_since: datetime | None = None
    last_successful_sync: datetime | None = None
    exercises: dict[str, LedgerEntry] = Field(default_factory=dict)


class Ledger:
    def __init__(self, path: Path, contents: LedgerFile | None = None) -> None:
        self._path = path
        self._file = contents or LedgerFile()
        self._dirty = False

    @classmethod
    def load(cls, path: Path) -> "Ledger":
        discard_interrupted_write(path)

        if not path.exists():
            logger.info("No ledger at %s yet, starting with an empty one", path)
            return cls(path)

        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exception:
            raise CorruptLedger(
                f"{path} is not valid JSON: {exception}. Inspect it, or delete it to start over — "
                f"a fresh ledger re-downloads everything Polar still has."
            ) from exception
        except OSError as exception:
            raise CorruptLedger(f"{path} could not be read: {exception}.") from exception

        if not isinstance(raw, dict):
            raise CorruptLedger(f"{path} should contain an object, got {type(raw).__name__}.")

        version = raw.get("version")
        if version != LEDGER_VERSION:
            raise CorruptLedger(
                f"{path} is version {version!r}, this build reads version {LEDGER_VERSION}. "
                f"It was most likely written by a newer connector; upgrade rather than downgrade."
            )

        try:
            contents = LedgerFile.model_validate(raw)
        except ValidationError as exception:
            raise CorruptLedger(f"{path} could not be read: {exception}.") from exception

        # The key is the identity; put it back on the value so callers can pass entries around.
        contents.exercises = {
            exercise_id: entry.model_copy(update={"exercise_id": exercise_id})
            for exercise_id, entry in contents.exercises.items()
        }

        return cls(path, contents)

    @property
    def is_dirty(self) -> bool:
        return self._dirty

    @property
    def resolved_since(self) -> datetime | None:
        return self._file.resolved_since

    def resolve_since(self, instant: datetime) -> datetime:
        """Resolves SINCE once and remembers it, so `SINCE=now` does not skip exercises we missed."""
        if self._file.resolved_since is None:
            self._file.resolved_since = instant
            self._dirty = True

        return self._file.resolved_since

    @property
    def last_successful_sync(self) -> datetime | None:
        return self._file.last_successful_sync

    def mark_successful_sync(self, at: datetime) -> None:
        self._file.last_successful_sync = at
        self._dirty = True

    def __contains__(self, exercise_id: str) -> bool:
        return exercise_id in self._file.exercises

    def __len__(self) -> int:
        return len(self._file.exercises)

    def entry(self, exercise_id: str) -> LedgerEntry | None:
        return self._file.exercises.get(exercise_id)

    def entries(self) -> tuple[LedgerEntry, ...]:
        return tuple(self._file.exercises.values())

    def counts_by_status(self) -> dict[ExerciseStatus, int]:
        counted = Counter(entry.status for entry in self._file.exercises.values())

        return {status: counted.get(status, 0) for status in ExerciseStatus}

    def mark_seen(
        self,
        exercise_id: str,
        *,
        polar_user_id: int,
        start_time: datetime,
        sport: str,
        detailed_sport: str,
        at: datetime,
    ) -> LedgerEntry:
        """Registers an exercise as pending. A known exercise is left exactly as it is."""
        known = self._file.exercises.get(exercise_id)
        if known is not None:
            return known

        entry = LedgerEntry(
            exercise_id=exercise_id,
            polar_user_id=polar_user_id,
            start_time=start_time,
            sport=sport,
            detailed_sport=detailed_sport,
            status=ExerciseStatus.PENDING,
            first_seen=at,
        )
        self._file.exercises[exercise_id] = entry
        self._dirty = True

        return entry

    def mark_delivered(self, exercise_id: str, files: tuple[str, ...], at: datetime) -> LedgerEntry:
        return self._record_attempt(exercise_id, ExerciseStatus.DELIVERED, at, files=files)

    def mark_no_file(self, exercise_id: str, at: datetime) -> LedgerEntry:
        """Permanent: Polar has no file for this exercise and never will, so it is never retried."""
        return self._record_attempt(exercise_id, ExerciseStatus.NO_FILE, at)

    def mark_failed(self, exercise_id: str, error: str, at: datetime) -> LedgerEntry:
        return self._record_attempt(exercise_id, ExerciseStatus.FAILED, at, error=error)

    def mark_skipped(self, exercise_id: str, at: datetime) -> LedgerEntry:
        """Below SINCE or excluded by SPORTS. Nothing was downloaded, so the attempt count stands still."""
        return self._update(exercise_id, status=ExerciseStatus.SKIPPED, last_attempt=at, error=None)

    def _record_attempt(
        self,
        exercise_id: str,
        status: ExerciseStatus,
        at: datetime,
        files: tuple[str, ...] = (),
        error: str | None = None,
    ) -> LedgerEntry:
        known = self._require(exercise_id)

        return self._update(
            exercise_id,
            status=status,
            files=files or known.files,
            attempts=known.attempts + 1,
            last_attempt=at,
            error=error,
        )

    def _update(self, exercise_id: str, **changes: Any) -> LedgerEntry:
        entry = self._require(exercise_id).model_copy(update=changes)
        self._file.exercises[exercise_id] = entry
        self._dirty = True

        return entry

    def _require(self, exercise_id: str) -> LedgerEntry:
        known = self._file.exercises.get(exercise_id)
        if known is None:
            raise KeyError(f"Exercise '{exercise_id}' was never seen; call mark_seen() first.")

        return known

    async def flush(self) -> None:
        """Writes the whole ledger, or nothing at all. Called once per cycle."""
        if not self._dirty:
            return

        await asyncio.to_thread(write_atomically, self._path, json.dumps(self.to_dict(), indent=2) + "\n")
        self._dirty = False

    def to_dict(self) -> dict[str, Any]:
        payload = self._file.model_dump(by_alias=True, mode="json")
        payload["exercises"] = {
            exercise_id: payload["exercises"][exercise_id] for exercise_id in sorted(payload["exercises"])
        }

        return payload
