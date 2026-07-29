"""Partial visibility is the failure this module exists to prevent, so that is what the tests check."""

import os
import re
import stat
from pathlib import Path

import pytest

from dreeve_polar_connector.config import ConflictPolicy
from dreeve_polar_connector.delivery import (
    DeliveryOutcome,
    ExerciseFile,
    UndeliverableFile,
    WatchFolder,
    WatchFolderUnusable,
)

FILE = ExerciseFile(name="2AC312F.fit", contents=b"fit bytes")


@pytest.mark.asyncio
async def test_prepare_creates_the_folder(tmp_path: Path) -> None:
    watch = tmp_path / "watch"

    await WatchFolder(watch).prepare()

    assert watch.is_dir()


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores the missing write permission this test relies on")
@pytest.mark.asyncio
async def test_prepare_refuses_an_unwritable_folder(tmp_path: Path) -> None:
    # Skipped as root, which is how the dev container runs `make check`. The behaviour still matters:
    # a watch folder the connector cannot write to must fail on boot, not mid-cycle.
    watch = tmp_path / "watch"
    watch.mkdir()
    watch.chmod(stat.S_IRUSR | stat.S_IXUSR)

    try:
        with pytest.raises(WatchFolderUnusable, match="not writable"):
            await WatchFolder(watch).prepare()
    finally:
        watch.chmod(stat.S_IRWXU)


@pytest.mark.asyncio
async def test_prepare_discards_a_leftover_part_file(tmp_path: Path) -> None:
    # A `.part` left by a killed process is a fragment of a download; it will be fetched again.
    watch = tmp_path / "watch"
    watch.mkdir()
    leftover = watch / ".2AC312F.fit.part"
    leftover.write_bytes(b"half a file")

    await WatchFolder(watch).prepare()

    assert not leftover.exists()


@pytest.mark.asyncio
async def test_a_delivered_file_has_the_right_contents(tmp_path: Path) -> None:
    folder = WatchFolder(tmp_path)

    outcome = await folder.deliver(FILE)

    assert outcome is DeliveryOutcome.WRITTEN
    assert (tmp_path / "2AC312F.fit").read_bytes() == b"fit bytes"


@pytest.mark.asyncio
async def test_delivery_leaves_no_part_file_behind(tmp_path: Path) -> None:
    await WatchFolder(tmp_path).deliver(FILE)

    assert list(tmp_path.glob(".*")) == []


@pytest.mark.asyncio
async def test_a_delivered_file_is_readable_by_whoever_imports_it(tmp_path: Path) -> None:
    # A file Dreeve cannot read fails the import silently, which is the worst outcome available.
    await WatchFolder(tmp_path).deliver(FILE)

    mode = (tmp_path / "2AC312F.fit").stat().st_mode

    assert mode & stat.S_IROTH


@pytest.mark.asyncio
async def test_an_existing_file_is_skipped_by_default(tmp_path: Path) -> None:
    (tmp_path / "2AC312F.fit").write_bytes(b"already imported")

    outcome = await WatchFolder(tmp_path, ConflictPolicy.SKIP).deliver(FILE)

    assert outcome is DeliveryOutcome.SKIPPED
    assert (tmp_path / "2AC312F.fit").read_bytes() == b"already imported"


@pytest.mark.asyncio
async def test_an_existing_file_can_be_overwritten(tmp_path: Path) -> None:
    (tmp_path / "2AC312F.fit").write_bytes(b"stale")

    outcome = await WatchFolder(tmp_path, ConflictPolicy.OVERWRITE).deliver(FILE)

    assert outcome is DeliveryOutcome.WRITTEN
    assert (tmp_path / "2AC312F.fit").read_bytes() == b"fit bytes"


@pytest.mark.asyncio
async def test_a_failed_write_raises_for_one_file_without_killing_the_cycle(tmp_path: Path) -> None:
    missing = tmp_path / "not-created"

    with pytest.raises(UndeliverableFile, match=re.escape("2AC312F.fit")):
        await WatchFolder(missing).deliver(FILE)
