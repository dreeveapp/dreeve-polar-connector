"""Putting a file into Dreeve's watch folder.

Dreeve scans that folder on its own schedule and imports whatever it finds, then deletes it. It has no
idea we are still writing, so a file must never be visible under its final name until it is complete:
write to a hidden `.part` alongside it, then rename. The rename is atomic because the temp file lives in
the very same directory, and therefore on the same filesystem.

The writes are synchronous inside `asyncio.to_thread`. The atomicity lives in the ordering — write,
fsync, rename — and moving that ordering into an async library would only add ways to get it wrong.
"""

import asyncio
import logging
import os
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from dreeve_polar_connector.config import ConflictPolicy

PART_SUFFIX = ".part"
PROBE_NAME = ".dreeve-polar-connector-write-test"
# Readable by whatever imports it, whichever user that turns out to be. PUID/PGID decide ownership;
# a delivered file Dreeve cannot read fails the import silently, the worst outcome available.
DEFAULT_FILE_MODE = 0o644

logger = logging.getLogger(__name__)


class WatchFolderUnusable(Exception):
    """Raised on boot. Nothing this connector does is worth anything if it cannot write where told."""


class UndeliverableFile(Exception):
    """Raised for a single file, so the cycle can carry on with the rest and retry this one later."""


class DeliveryOutcome(StrEnum):
    WRITTEN = "written"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class ExerciseFile:
    name: str
    contents: bytes


class WatchFolder:
    def __init__(
        self,
        path: Path,
        on_conflict: ConflictPolicy = ConflictPolicy.SKIP,
        file_mode: int = DEFAULT_FILE_MODE,
    ) -> None:
        self._path = path
        self._on_conflict = on_conflict
        self._file_mode = file_mode

    async def prepare(self) -> None:
        """Creates the folder and proves it can be written to, on boot rather than mid-cycle."""
        await asyncio.to_thread(self._prepare)

    def _prepare(self) -> None:
        try:
            self._path.mkdir(parents=True, exist_ok=True)
        except OSError as exception:
            raise WatchFolderUnusable(f"Watch folder {self._path} could not be created: {exception}.") from exception

        probe = self._path / PROBE_NAME
        try:
            probe.touch()
        except OSError as exception:
            raise WatchFolderUnusable(
                f"Watch folder {self._path} is not writable: {exception}. "
                f"Check the bind mount and the PUID/PGID the container runs as."
            ) from exception
        finally:
            probe.unlink(missing_ok=True)

        self._discard_partial_files()

    def _discard_partial_files(self) -> None:
        """A `.part` left by a killed process is a fragment of a download; it will be fetched again."""
        for leftover in self._path.glob(f".*{PART_SUFFIX}"):
            logger.warning("Discarding %s, left behind by an interrupted delivery", leftover)
            leftover.unlink(missing_ok=True)

    async def deliver(self, file: ExerciseFile) -> DeliveryOutcome:
        return await asyncio.to_thread(self._deliver, file)

    def _deliver(self, file: ExerciseFile) -> DeliveryOutcome:
        destination = self._path / file.name

        if destination.exists() and self._on_conflict is ConflictPolicy.SKIP:
            logger.info("%s is already in the watch folder, leaving it alone", file.name)
            return DeliveryOutcome.SKIPPED

        temp_path = self._path / f".{file.name}{PART_SUFFIX}"
        try:
            with temp_path.open("wb") as handle:
                handle.write(file.contents)
                handle.flush()
                # The ledger will call this exercise delivered; make sure the bytes outlive a power cut.
                os.fsync(handle.fileno())

            temp_path.chmod(self._file_mode)
            temp_path.replace(destination)
        except OSError as exception:
            temp_path.unlink(missing_ok=True)
            raise UndeliverableFile(f"{file.name} could not be delivered to {self._path}: {exception}.") from exception

        logger.info("Delivered %s (%d bytes)", file.name, len(file.contents))

        return DeliveryOutcome.WRITTEN
