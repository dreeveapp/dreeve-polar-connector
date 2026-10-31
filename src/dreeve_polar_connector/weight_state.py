"""What weight sync last recorded in Dreeve, kept in its own file next to the ledger.

Separate from the ledger because the ledger is version-gated: a field added there means a migration.
And treated differently from it on purpose: losing the ledger costs a full re-download, losing this
costs one repeated push of the current weight for the same date, which Dreeve simply overwrites. So an
unreadable file is logged and replaced rather than refused — refusing would protect nothing.
"""

import asyncio
import json
import logging
from datetime import date, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, ValidationError
from pydantic.alias_generators import to_camel

from dreeve_polar_connector.state_files import discard_interrupted_write, write_atomically

WEIGHT_STATE_VERSION = 1
WEIGHT_STATE_FILENAME = "weight.json"

logger = logging.getLogger(__name__)


class PushedWeight(BaseModel):
    """One weight Dreeve accepted. `kilograms` is Polar's value, before any conversion for Dreeve."""

    # Inlined, as in ledger.py: the pydantic mypy plugin only reads `frozen=True` off a literal.
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, frozen=True)

    on: date
    kilograms: float
    source: str
    pushed_at: datetime


class WeightStateFile(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    version: int = WEIGHT_STATE_VERSION
    last_pushed: PushedWeight | None = None


class WeightState:
    def __init__(self, path: Path, contents: WeightStateFile | None = None) -> None:
        self._path = path
        self._file = contents or WeightStateFile()
        self._dirty = False

    @classmethod
    def load(cls, path: Path) -> "WeightState":
        discard_interrupted_write(path)

        if not path.exists():
            return cls(path)

        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or raw.get("version") != WEIGHT_STATE_VERSION:
                raise ValueError(f"expected a version {WEIGHT_STATE_VERSION} object")
            contents = WeightStateFile.model_validate(raw)
        except (OSError, ValueError, ValidationError) as exception:
            # ValidationError is a ValueError; named anyway so the intent survives a pydantic change.
            logger.warning(
                "Could not read %s (%s); starting over. The current weight is pushed to Dreeve again, "
                "which only replaces the value it already has for that date.",
                path,
                exception,
            )
            return cls(path)

        return cls(path, contents)

    @property
    def last_pushed(self) -> PushedWeight | None:
        return self._file.last_pushed

    def record_push(self, pushed: PushedWeight) -> None:
        self._file.last_pushed = pushed
        self._dirty = True

    async def flush(self) -> None:
        """Writes the whole file, or nothing at all."""
        if not self._dirty:
            return

        payload = self._file.model_dump(by_alias=True, mode="json")
        await asyncio.to_thread(write_atomically, self._path, json.dumps(payload, indent=2) + "\n")
        self._dirty = False
