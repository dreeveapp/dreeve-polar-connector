"""The version is read by hatchling at build time, so a malformed one breaks packaging, not tests."""

import re

import dreeve_polar_connector

SEMVER = re.compile(r"^\d+\.\d+\.\d+$")


def test_version_is_a_semver_string() -> None:
    assert SEMVER.match(dreeve_polar_connector.__version__)
