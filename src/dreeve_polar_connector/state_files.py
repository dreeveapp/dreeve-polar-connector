"""All-or-nothing writes for the JSON files under STATE_DIR.

Shared by the ledger and the weight state so the two cannot drift apart on the one property both depend
on: a crash mid-write leaves the previous file intact, never a half-written one.
"""

import logging
import os
from pathlib import Path

TEMP_SUFFIX = ".tmp"

logger = logging.getLogger(__name__)


def write_atomically(path: Path, text: str) -> None:
    """Synchronous on purpose: the atomicity is in the ordering, and threads do not change it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(path.name + TEMP_SUFFIX)

    with temp_path.open("w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        # Without this the rename can land before the contents do, and a power cut leaves an empty file.
        os.fsync(handle.fileno())

    temp_path.replace(path)


def discard_interrupted_write(path: Path) -> None:
    """A leftover temp file means a previous write died midway; its contents are partial."""
    temp_path = path.with_name(path.name + TEMP_SUFFIX)
    if not temp_path.exists():
        return

    logger.warning("Discarding %s, left behind by an interrupted write", temp_path)
    temp_path.unlink(missing_ok=True)
