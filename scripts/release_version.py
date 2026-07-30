"""Checks on `__version__`, shared by the pre-commit hook and the release workflow.

`__version__` in `src/dreeve_polar_connector/__init__.py` is the single source of truth for a release:
the workflow derives the git tag from it. That only holds while a bump is well formed, so this refuses a
version that is not semver, that does not advance on the highest tag already published, or that has been
tagged before — and reports every problem it finds at once, the way `InvalidConfiguration` does, because
fixing one problem per run is misery.

The version is parsed out of the file rather than imported: the hook runs on a host where this project
is not installed, and the workflow's verify job runs before any Python environment has been built.

Two flags exist for callers whose idea of "the version" differs from "whatever is on disk right now".
`--from-index` reads the staged blob, because the hook has to judge what is about to be committed rather
than what happens to be in the working tree. `--allow-existing-tag` discounts a tag matching the version
under test, because a release re-run legitimately arrives with its own tag already pushed.
"""

import argparse
import re
import subprocess
import sys
from pathlib import Path

VERSION_FILE = Path("src/dreeve_polar_connector/__init__.py")
VERSION_ASSIGNMENT = re.compile(r'^__version__ = "(?P<version>[^"]*)"$', re.MULTILINE)

# Each component refuses a leading zero. `0.10.01` is not PEP 440, so hatchling would normalise the
# wheel to 0.10.1 while the workflow tagged v0.10.01 — the tag-versus-metadata drift this exists to stop.
COMPONENT = r"(?:0|[1-9]\d*)"
SEMVER = re.compile(rf"^(?P<major>{COMPONENT})\.(?P<minor>{COMPONENT})\.(?P<patch>{COMPONENT})$")
TAG_PREFIX = "v"

Version = tuple[int, int, int]


class MissingVersion(Exception):
    """The version file is absent, or holds no `__version__` assignment to read."""


class GitUnavailable(Exception):
    """Git could not answer a question about this directory: it is not a repository, or git is missing."""


def read_version(root: Path) -> str:
    """Parse `__version__` out of the version file under `root` without importing the package."""
    path = root / VERSION_FILE
    try:
        contents = path.read_text(encoding="utf-8")
    except OSError as exception:
        raise MissingVersion(f"{path} could not be read: {exception}.") from exception

    match = VERSION_ASSIGNMENT.search(contents)
    if match is None:
        raise MissingVersion(f'{path} has no `__version__ = "..."` assignment.')

    return match.group("version")


def parse(version: str) -> Version | None:
    """None rather than an exception: a malformed version is one of several problems to report, not a
    reason to stop looking."""
    match = SEMVER.match(version)
    if match is None:
        return None

    return int(match.group("major")), int(match.group("minor")), int(match.group("patch"))


def git(root: Path, *arguments: str) -> str:
    """Run git in `root` and hand back its stdout.

    Both ways git can fail here become `GitUnavailable`, carrying git's own wording. The alternative is
    a traceback in the middle of someone's commit, which says nothing about what to do next — and the
    stderr that explains it would otherwise be swallowed by `capture_output`.
    """
    try:
        completed = subprocess.run(  # noqa: S603
            # `git` from PATH on purpose: this runs on a developer's host and on a runner, and neither
            # guarantees the same absolute path.
            ["git", *arguments],  # noqa: S607
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        )
    except FileNotFoundError as exception:
        raise GitUnavailable("git was not found on PATH.") from exception
    except subprocess.CalledProcessError as exception:
        detail = exception.stderr.strip() or f"exit status {exception.returncode}"
        raise GitUnavailable(f"`git {' '.join(arguments)}` failed: {detail}") from exception

    return completed.stdout


def read_staged_version(root: Path) -> str:
    """Parse `__version__` out of the staged blob rather than the working tree.

    The hook fires on a staged bump, so the index is what is about to be committed; the working tree may
    hold something else entirely. The same regex does the parsing as for the on-disk read, so the two
    paths cannot quietly disagree about what counts as an assignment.
    """
    contents = git(root, "show", f":{VERSION_FILE.as_posix()}")

    match = VERSION_ASSIGNMENT.search(contents)
    if match is None:
        raise MissingVersion(f'the staged {VERSION_FILE} has no `__version__ = "..."` assignment.')

    return match.group("version")


def published_tags(root: Path) -> dict[str, Version]:
    """Every `v<semver>` tag in the repository, by name. Tags that are not plain semver are skipped
    rather than rejected: this only has to answer "what is the highest release so far"."""
    listed = git(root, "tag", "--list", f"{TAG_PREFIX}*")

    tags: dict[str, Version] = {}
    for line in listed.splitlines():
        name = line.strip()
        parsed = parse(name.removeprefix(TAG_PREFIX))
        if parsed is not None:
            tags[name] = parsed

    return tags


def problems(root: Path, *, allow_existing_tag: bool = False, from_index: bool = False) -> list[str]:
    """Every reason this version cannot be released, in one list. Empty means releasable."""
    version = read_staged_version(root) if from_index else read_version(root)
    parsed = parse(version)
    if parsed is None:
        # Nothing else can be checked against a version that has no ordering.
        return [f"__version__ is '{version}', which is not MAJOR.MINOR.PATCH."]

    found: list[str] = []
    tags = published_tags(root)

    if allow_existing_tag:
        # A release re-run reaches this step with its own tag already pushed, so that tag is this run's
        # leftover rather than a prior release. Dropping it before either check is what makes the whole
        # of `problems` agree on that reading: nothing can complain that the version is already tagged,
        # and the advance check judges the version against the highest *other* tag, which still stops a
        # genuinely stale version. Whether the release really happened is settled by the Release itself.
        tags.pop(f"{TAG_PREFIX}{version}", None)

    if f"{TAG_PREFIX}{version}" in tags:
        found.append(f"{TAG_PREFIX}{version} is already tagged, so this version has been released.")

    highest = max(tags.values(), default=None)
    if highest is not None and parsed <= highest:
        readable = ".".join(str(part) for part in highest)
        found.append(f"__version__ is {version}, which does not advance on the highest tag {TAG_PREFIX}{readable}.")

    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="release_version.py",
        description="Check that __version__ is a releasable bump, or print it.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate", help="Exit 0 when __version__ can be released, non-zero otherwise.")
    validate.add_argument(
        "--allow-existing-tag",
        action="store_true",
        help="Treat a tag matching this version as this run's own leftover rather than a prior release.",
    )
    validate.add_argument(
        "--from-index",
        action="store_true",
        help="Read __version__ from the staged blob instead of the working tree.",
    )
    commands.add_parser("print", help="Print __version__, for a workflow to capture.")
    arguments = parser.parse_args(argv)

    root = Path.cwd()
    try:
        if arguments.command == "print":
            print(read_version(root))
            return 0

        found = problems(
            root,
            allow_existing_tag=arguments.allow_existing_tag,
            from_index=arguments.from_index,
        )
    except (MissingVersion, GitUnavailable) as exception:
        print(f"error: {exception}", file=sys.stderr)
        return 1

    if found:
        print("\n".join(["Not releasable:", *(f"  - {problem}" for problem in found)]), file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
