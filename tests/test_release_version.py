"""The release workflow derives the git tag from `__version__`, so a bad bump has to be caught before
anything is published. These tests drive the checker against throwaway repositories rather than this
one, whose own tag history is deliberately left imperfect."""

import os
import subprocess
from pathlib import Path

import pytest

from release_version import GitUnavailable, MissingVersion, main, problems, read_version

VERSION_FILE = Path("src/dreeve_polar_connector/__init__.py")


def _git(root: Path, *arguments: str) -> None:
    subprocess.run(["git", *arguments], cwd=root, check=True, capture_output=True)  # noqa: S603, S607


def _set_version(root: Path, version: str) -> None:
    path = root / VERSION_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'"""Package."""\n\n__version__ = "{version}"\n', encoding="utf-8")


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A repository shaped like this one: one commit, a version file, no tags yet.

    Git is pointed at no configuration at all for the duration, because local `user.email` and
    `user.name` alone still let everything else leak in from the developer's own config.
    `commit.gpgsign = true` is the one that bites hardest — every commit here then dies with "signing
    failed: No secret key" — but `core.hooksPath`, `init.templateDir` and `diff.external` would each
    change what these tests measure. This is the module designed to run on a bare host, so it must not
    depend on how that host happens to be set up.
    """
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    _git(tmp_path, "init", "--quiet")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    _set_version(tmp_path, "0.1.0")
    _git(tmp_path, "add", "--all")
    _git(tmp_path, "commit", "--quiet", "--message", "initial")
    return tmp_path


def test_the_version_is_read_out_of_the_package_file(repo: Path) -> None:
    _set_version(repo, "0.2.7")

    assert read_version(repo) == "0.2.7"


def test_a_non_semver_version_is_refused(repo: Path) -> None:
    _set_version(repo, "0.2")

    assert problems(repo) == ["__version__ is '0.2', which is not MAJOR.MINOR.PATCH."]


def test_a_version_file_without_an_assignment_is_an_error(repo: Path) -> None:
    (repo / VERSION_FILE).write_text('"""Package."""\n', encoding="utf-8")

    with pytest.raises(MissingVersion, match="no `__version__"):
        read_version(repo)


def test_a_bump_above_every_tag_is_releasable(repo: Path) -> None:
    _git(repo, "tag", "v0.1.0")
    _set_version(repo, "0.1.1")

    assert problems(repo) == []


def test_a_version_equal_to_the_highest_tag_is_refused(repo: Path) -> None:
    _git(repo, "tag", "v0.2.0")
    _set_version(repo, "0.2.0")

    # Order-independent: both problems have to be reported, but nothing in the design promises which
    # comes first, and a test that pins the order fails the next time one is added.
    assert sorted(problems(repo)) == sorted(
        [
            "v0.2.0 is already tagged, so this version has been released.",
            "__version__ is 0.2.0, which does not advance on the highest tag v0.2.0.",
        ]
    )


def test_a_version_below_the_highest_tag_is_refused(repo: Path) -> None:
    _git(repo, "tag", "v0.2.0")
    _set_version(repo, "0.1.9")

    assert problems(repo) == ["__version__ is 0.1.9, which does not advance on the highest tag v0.2.0."]


def test_versions_are_compared_numerically_not_as_strings(repo: Path) -> None:
    """The comparison that would go wrong silently. As strings `"0.9.1" > "0.10.0"`, so a lexicographic
    implementation would wave this through and tag a release below one that already exists."""
    _git(repo, "tag", "v0.9.0")
    _git(repo, "tag", "v0.10.0")
    _set_version(repo, "0.9.1")

    assert problems(repo) == ["__version__ is 0.9.1, which does not advance on the highest tag v0.10.0."]


def test_a_repository_with_no_tags_yet_is_releasable(repo: Path) -> None:
    """The first release: there is no highest tag to advance on, and that is not a problem."""
    _set_version(repo, "0.1.0")

    assert problems(repo) == []


def test_a_version_with_a_leading_zero_component_is_refused(repo: Path) -> None:
    """`0.10.01` is not PEP 440, so hatchling would build 0.10.1 while the workflow tagged v0.10.01."""
    _set_version(repo, "0.10.01")

    assert problems(repo) == ["__version__ is '0.10.01', which is not MAJOR.MINOR.PATCH."]


def test_tags_that_are_not_plain_semver_are_ignored(repo: Path) -> None:
    _git(repo, "tag", "v0.1.0")
    _git(repo, "tag", "v9.9.9-rc1")
    _set_version(repo, "0.1.1")

    assert problems(repo) == []


def test_allow_existing_tag_treats_this_versions_tag_as_this_runs_own(repo: Path) -> None:
    """The state a release re-run starts from: the tag was pushed, the Release was never created. Strictly
    this version has to stay releasable, or the documented re-run path cannot get past the first step."""
    _git(repo, "tag", "v0.1.0")
    _git(repo, "tag", "v0.1.1")
    _set_version(repo, "0.1.1")

    assert problems(repo, allow_existing_tag=True) == []
    assert sorted(problems(repo)) == sorted(
        [
            "v0.1.1 is already tagged, so this version has been released.",
            "__version__ is 0.1.1, which does not advance on the highest tag v0.1.1.",
        ]
    )


def test_allow_existing_tag_still_refuses_a_version_below_another_tag(repo: Path) -> None:
    """Only the version's own tag is discounted. A genuinely stale version stays refused, because some
    other tag is still higher — otherwise the flag would be a way to re-release the past."""
    _git(repo, "tag", "v0.1.4")
    _git(repo, "tag", "v0.2.0")
    _set_version(repo, "0.1.4")

    assert problems(repo, allow_existing_tag=True) == [
        "__version__ is 0.1.4, which does not advance on the highest tag v0.2.0."
    ]


def test_from_index_judges_the_staged_version_not_the_working_tree(repo: Path) -> None:
    """The hook fires on a staged bump, so the two have to be able to disagree and be told apart."""
    _git(repo, "tag", "v0.1.0")
    _set_version(repo, "0.1.1")
    _git(repo, "add", "--all")
    _set_version(repo, "0.1.0")

    assert problems(repo, from_index=True) == []
    assert sorted(problems(repo)) == sorted(
        [
            "v0.1.0 is already tagged, so this version has been released.",
            "__version__ is 0.1.0, which does not advance on the highest tag v0.1.0.",
        ]
    )


def test_a_directory_that_is_not_a_repository_is_reported_not_raised_raw(tmp_path: Path) -> None:
    """`git tag --list` fails outside a repository, and the caller has to be able to report it."""
    _set_version(tmp_path, "0.1.0")

    with pytest.raises(GitUnavailable, match="not a git repository"):
        problems(tmp_path)


def test_git_missing_from_path_is_reported_not_raised_raw(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The hook runs on a host, where git being absent is a story the user should be able to read."""
    monkeypatch.setenv("PATH", str(repo / "nothing-here"))

    with pytest.raises(GitUnavailable, match="not found on PATH"):
        problems(repo)


def test_validate_exits_zero_for_a_releasable_version(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _git(repo, "tag", "v0.1.0")
    _set_version(repo, "0.1.1")
    monkeypatch.chdir(repo)

    assert main(["validate"]) == 0


def test_validate_exits_non_zero_and_lists_every_problem(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _git(repo, "tag", "v0.2.0")
    _set_version(repo, "0.2.0")
    monkeypatch.chdir(repo)

    assert main(["validate"]) == 1
    errors = capsys.readouterr().err
    assert "already tagged" in errors
    assert "does not advance" in errors


def test_validate_reports_a_missing_assignment_instead_of_crashing(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (repo / VERSION_FILE).write_text('"""Package."""\n', encoding="utf-8")
    monkeypatch.chdir(repo)

    assert main(["validate"]) == 1
    assert "no `__version__" in capsys.readouterr().err


def test_validate_accepts_the_allow_existing_tag_flag(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _git(repo, "tag", "v0.1.1")
    _set_version(repo, "0.1.1")
    monkeypatch.chdir(repo)

    assert main(["validate"]) == 1
    assert main(["validate", "--allow-existing-tag"]) == 0


def test_validate_from_index_judges_the_staged_version(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _git(repo, "tag", "v0.1.0")
    _set_version(repo, "0.1.1")
    _git(repo, "add", "--all")
    _set_version(repo, "0.1.0")
    monkeypatch.chdir(repo)

    assert main(["validate", "--from-index"]) == 0
    assert main(["validate"]) == 1


def test_validate_reports_an_unusable_git_instead_of_crashing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _set_version(tmp_path, "0.1.0")
    monkeypatch.chdir(tmp_path)

    assert main(["validate"]) == 1
    assert "not a git repository" in capsys.readouterr().err


def test_print_writes_the_version_to_stdout(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _set_version(repo, "0.3.1")
    monkeypatch.chdir(repo)

    assert main(["print"]) == 0
    assert capsys.readouterr().out == "0.3.1\n"
