"""Fakes shared across the connector tests.

`StubPolar` implements the `PolarClient` protocol, which is what lets the entire sync cycle be tested
without a Polar account or a network.
"""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from dreeve_polar_connector.config import FallbackFormat
from dreeve_polar_connector.polar import Exercise, RateLimitSnapshot

FIT_BYTES = b"fit bytes"
TCX_BYTES = b"<TrainingCenterDatabase/>"
GPX_BYTES = b"<gpx/>"


def exercise(
    exercise_id: str = "2AC312F",
    *,
    start_time: datetime | None = None,
    sport: str = "RUNNING",
    detailed_sport: str = "RUNNING_TREADMILL",
) -> Exercise:
    return Exercise(
        exercise_id=exercise_id,
        start_time=start_time or datetime(2026, 7, 28, 6, 13, 42, tzinfo=UTC),
        sport=sport,
        detailed_sport=detailed_sport,
        device="Polar Vantage V3",
    )


class StubPolar:
    """Scripted responses plus a log of what was asked for.

    `fit_errors` and `fallback_errors` map an exercise id to an exception to raise instead of returning
    bytes, which is how every failure branch in `sync.py` gets exercised.
    """

    def __init__(
        self,
        exercises: Sequence[Exercise] = (),
        *,
        list_error: Exception | None = None,
        fit_errors: dict[str, Exception] | None = None,
        fallback_errors: dict[str, Exception] | None = None,
    ) -> None:
        self.exercises = list(exercises)
        self.list_error = list_error
        self.fit_errors = fit_errors or {}
        self.fallback_errors = fallback_errors or {}
        self.listed = 0
        self.fit_requests: list[str] = []
        self.fallback_requests: list[tuple[str, FallbackFormat]] = []
        # (short_usage, short_limit, long_usage, long_limit, reset_seconds)
        self.rate_limit: tuple[int | None, int | None, int | None, int | None, int | None] = (
            None,
            None,
            None,
            None,
            None,
        )

    async def list_exercises(self) -> tuple[Exercise, ...]:
        self.listed += 1
        if self.list_error is not None:
            raise self.list_error

        return tuple(self.exercises)

    async def download_fit(self, exercise_id: str) -> bytes:
        self.fit_requests.append(exercise_id)
        error = self.fit_errors.get(exercise_id)
        if error is not None:
            raise error

        return FIT_BYTES

    async def download_fallback(self, exercise_id: str, fallback: FallbackFormat) -> bytes:
        self.fallback_requests.append((exercise_id, fallback))
        error = self.fallback_errors.get(exercise_id)
        if error is not None:
            raise error

        return TCX_BYTES if fallback is FallbackFormat.TCX else GPX_BYTES

    def current_rate_limit(self) -> RateLimitSnapshot:
        short_usage, short_limit, long_usage, long_limit, reset_seconds = self.rate_limit

        return RateLimitSnapshot(
            short_usage=short_usage,
            short_limit=short_limit,
            long_usage=long_usage,
            long_limit=long_limit,
            reset_seconds=reset_seconds,
        )


class StubClock:
    """A clock that never waits. Sleeps are recorded so pacing can be asserted without spending time."""

    def __init__(self, now: datetime | None = None) -> None:
        self._now = now or datetime(2026, 7, 29, 8, 0, tzinfo=UTC)
        self.slept: list[float] = []

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
