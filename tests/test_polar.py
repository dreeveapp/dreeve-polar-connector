"""The adapter's whole job is to keep `sync.py` ignorant of `accesslink`, so these tests assert the
protocol shape and the sport matching rather than any HTTP behaviour."""

from datetime import UTC, datetime

import pytest

from dreeve_polar_connector.accesslink.exceptions import AccessLinkError
from dreeve_polar_connector.accesslink.models import Exercise as WireExercise
from dreeve_polar_connector.accesslink.models import RateLimit
from dreeve_polar_connector.config import FallbackFormat
from dreeve_polar_connector.polar import (
    AccessLinkPolarClient,
    AuthenticationFailed,
    AuthorizationRequired,
    Exercise,
    PolarClient,
    PolarError,
    RateLimitSnapshot,
)

STARTED = datetime(2026, 7, 28, 6, 13, 42, tzinfo=UTC)


def _exercise(sport: str = "RUNNING", detailed_sport: str = "RUNNING_TREADMILL") -> Exercise:
    return Exercise(
        exercise_id="2AC312F",
        start_time=STARTED,
        sport=sport,
        detailed_sport=detailed_sport,
        device="Polar Vantage V3",
    )


def test_authorization_required_is_not_an_authentication_failure() -> None:
    # A fresh install has no token. Treating that as a dead token would stop the daemon permanently
    # instead of letting it wait for someone to visit /authorize.
    assert not issubclass(AuthorizationRequired, AuthenticationFailed)
    assert issubclass(AuthorizationRequired, PolarError)


def test_the_connector_error_base_is_the_accesslink_one() -> None:
    # Re-export, not re-translation: accesslink was given the names the connector wants.
    assert PolarError is AccessLinkError


def test_no_sport_filter_matches_everything() -> None:
    assert _exercise().matches(())


def test_a_sport_filter_matches_the_coarse_name() -> None:
    assert _exercise().matches(("running", "cycling"))


def test_a_sport_filter_matches_the_detailed_name() -> None:
    assert _exercise(sport="OTHER", detailed_sport="WATERSPORTS_WATERSKI").matches(("watersports_waterski",))


def test_a_sport_filter_is_case_insensitive() -> None:
    assert _exercise(sport="RUNNING").matches(("running",))


def test_an_unlisted_sport_does_not_match() -> None:
    assert not _exercise(sport="RUNNING", detailed_sport="RUNNING_TREADMILL").matches(("cycling",))


def test_an_exercise_without_a_detailed_sport_still_matches_on_the_coarse_one() -> None:
    assert _exercise(sport="RUNNING", detailed_sport="").matches(("running",))


class _FakeAccessLink:
    """Only the three calls the adapter uses, so the protocol is what is being tested."""

    def __init__(self) -> None:
        self.downloaded: list[tuple[str, str]] = []
        self.rate_limit = RateLimit()

    async def list_exercises(self) -> tuple[WireExercise, ...]:
        return (
            WireExercise.model_validate(
                {
                    "id": "2AC312F",
                    "start_time": "2026-07-28T08:13:42",
                    "start_time_utc_offset": 120,
                    "sport": "RUNNING",
                    "detailed_sport_info": "RUNNING_TREADMILL",
                    "device": "Polar Vantage V3",
                }
            ),
        )

    async def download_fit(self, exercise_id: str) -> bytes:
        self.downloaded.append((exercise_id, "fit"))
        return b"fit bytes"

    async def download_tcx(self, exercise_id: str) -> bytes:
        self.downloaded.append((exercise_id, "tcx"))
        return b"<tcx/>"

    async def download_gpx(self, exercise_id: str) -> bytes:
        self.downloaded.append((exercise_id, "gpx"))
        return b"<gpx/>"


def test_the_adapter_satisfies_the_protocol() -> None:
    assert isinstance(AccessLinkPolarClient(_FakeAccessLink()), PolarClient)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_wire_exercises_become_domain_exercises() -> None:
    adapter = AccessLinkPolarClient(_FakeAccessLink())  # type: ignore[arg-type]

    exercises = await adapter.list_exercises()

    assert exercises == (_exercise(),)


@pytest.mark.asyncio
async def test_fit_downloads_are_passed_through() -> None:
    fake = _FakeAccessLink()

    assert await AccessLinkPolarClient(fake).download_fit("2AC312F") == b"fit bytes"  # type: ignore[arg-type]
    assert fake.downloaded == [("2AC312F", "fit")]


@pytest.mark.asyncio
async def test_the_tcx_fallback_is_routed_to_the_tcx_endpoint() -> None:
    fake = _FakeAccessLink()

    await AccessLinkPolarClient(fake).download_fallback("2AC312F", FallbackFormat.TCX)  # type: ignore[arg-type]

    assert fake.downloaded == [("2AC312F", "tcx")]


@pytest.mark.asyncio
async def test_the_gpx_fallback_is_routed_to_the_gpx_endpoint() -> None:
    fake = _FakeAccessLink()

    await AccessLinkPolarClient(fake).download_fallback("2AC312F", FallbackFormat.GPX)  # type: ignore[arg-type]

    assert fake.downloaded == [("2AC312F", "gpx")]


@pytest.mark.asyncio
async def test_asking_for_the_none_fallback_is_a_programming_error() -> None:
    # `sync.py` must check FallbackFormat.NONE before calling; a silent no-op would hide the bug.
    with pytest.raises(ValueError, match="FALLBACK_FORMAT"):
        await AccessLinkPolarClient(_FakeAccessLink()).download_fallback(  # type: ignore[arg-type]
            "2AC312F", FallbackFormat.NONE
        )


def test_the_rate_limit_crosses_the_seam_as_a_domain_snapshot() -> None:
    # Not accesslink's RateLimit: loop.py and status.py must not import from the client library.
    fake = _FakeAccessLink()
    fake.rate_limit = RateLimit.from_headers(
        {"RateLimit-Usage": "12,340", "RateLimit-Limit": "520,5100", "RateLimit-Reset": "640,80000"}
    )

    snapshot = AccessLinkPolarClient(fake).current_rate_limit()  # type: ignore[arg-type]

    assert snapshot == RateLimitSnapshot(
        short_usage=12, short_limit=520, long_usage=340, long_limit=5100, reset_seconds=640
    )


def test_an_unknown_rate_limit_is_an_empty_snapshot() -> None:
    assert AccessLinkPolarClient(_FakeAccessLink()).current_rate_limit() == RateLimitSnapshot()  # type: ignore[arg-type]
