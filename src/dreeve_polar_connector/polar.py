"""The seam between the connector and the Polar client.

`sync.py` depends on the `PolarClient` protocol and on the frozen `Exercise` below, never on
`accesslink` directly. That is what makes the client swappable: if this project later depends on
`polar-flow-api` instead of its own sub-package, this file is the only one that changes.

The exceptions are re-exported rather than translated, because `accesslink` was deliberately given the
names the connector wants. Only `AuthorizationRequired` is new, and it is a sibling of
`AuthenticationFailed` rather than a subclass — see its docstring.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from dreeve_polar_connector.accesslink.client import AccessLinkClient
from dreeve_polar_connector.accesslink.exceptions import (
    AccessLinkError as PolarError,
)
from dreeve_polar_connector.accesslink.exceptions import (
    AuthenticationFailed,
    ConnectionFailed,
    ConsentRequired,
    NotFound,
    RateLimited,
    UnexpectedResponse,
)
from dreeve_polar_connector.accesslink.models import Exercise as WireExercise
from dreeve_polar_connector.accesslink.models import PhysicalInfo
from dreeve_polar_connector.accesslink.models import RateLimit as WireRateLimit
from dreeve_polar_connector.config import FallbackFormat

__all__ = [
    "AccessLinkPolarClient",
    "AuthenticationFailed",
    "AuthorizationRequired",
    "BodyWeight",
    "ConnectionFailed",
    "ConsentRequired",
    "Exercise",
    "NotFound",
    "PolarClient",
    "PolarError",
    "RateLimitSnapshot",
    "RateLimited",
    "UnexpectedResponse",
]


class AuthorizationRequired(PolarError):
    """Nobody has authorized this connector yet, and no request was made looking for one.

    Deliberately NOT a subclass of `AuthenticationFailed`. That one is terminal — the token is dead, so
    stop and wait for a human. This one is cheap to retry every cycle, which is exactly how a token
    created at `/authorize` gets picked up without restarting the container. If it were a subclass, an
    `except AuthenticationFailed` clause would swallow it and turn a fresh install into a permanent
    failure.
    """


@dataclass(frozen=True)
class RateLimitSnapshot:
    """Polar's published budget, as a connector-side value.

    Deliberately not `accesslink.models.RateLimit`: keeping the client library's types out of
    `loop.py` and `status.py` is what makes the client swappable.
    """

    short_usage: int | None = None
    short_limit: int | None = None
    long_usage: int | None = None
    long_limit: int | None = None
    reset_seconds: int | None = None

    @property
    def is_known(self) -> bool:
        return self != RateLimitSnapshot()


@dataclass(frozen=True)
class Exercise:
    """What the connector needs to know about a training session. Start time is always UTC."""

    exercise_id: str
    start_time: datetime
    sport: str
    detailed_sport: str
    device: str

    def matches(self, sports: tuple[str, ...]) -> bool:
        """SPORTS is matched against either name, so 'cycling' and 'watersports_waterski' both work."""
        if not sports:
            return True

        return self.sport.lower() in sports or self.detailed_sport.lower() in sports


@dataclass(frozen=True)
class BodyWeight:
    """The user's current weight in Polar, as a connector-side value.

    `source` is Polar's `weight_source` verbatim ("" when absent). Which sources count as a real reading
    is weight sync's policy, not this seam's. `effective_at` is when the value last changed, in UTC.
    """

    kilograms: float
    source: str
    effective_at: datetime


@runtime_checkable
class PolarClient(Protocol):
    """The sliver of Polar that `sync.py` depends on. Small enough to fake in one class."""

    async def list_exercises(self) -> tuple[Exercise, ...]: ...

    async def download_fit(self, exercise_id: str) -> bytes: ...

    async def download_fallback(self, exercise_id: str, fallback: FallbackFormat) -> bytes: ...

    async def current_weight(self) -> BodyWeight | None: ...

    def current_rate_limit(self) -> RateLimitSnapshot: ...


class AccessLinkPolarClient:
    """The only implementation, and the only connector code that touches `accesslink.client`."""

    def __init__(self, client: AccessLinkClient) -> None:
        self._client = client

    async def list_exercises(self) -> tuple[Exercise, ...]:
        return tuple(_domain(exercise) for exercise in await self._client.list_exercises())

    async def download_fit(self, exercise_id: str) -> bytes:
        return await self._client.download_fit(exercise_id)

    async def download_fallback(self, exercise_id: str, fallback: FallbackFormat) -> bytes:
        if fallback is FallbackFormat.NONE:
            raise ValueError("FALLBACK_FORMAT is 'none'; there is no fallback to download.")

        if fallback is FallbackFormat.TCX:
            return await self._client.download_tcx(exercise_id)

        return await self._client.download_gpx(exercise_id)

    async def current_weight(self) -> BodyWeight | None:
        """None when Polar has no usable weight: no physical info, no positive value, or no date."""
        return _body_weight(await self._client.get_physical_info())

    def current_rate_limit(self) -> RateLimitSnapshot:
        """What Polar reported on the most recent response, or an empty snapshot before the first."""
        return _snapshot(self._client.rate_limit)


def _domain(exercise: WireExercise) -> Exercise:
    return Exercise(
        exercise_id=exercise.exercise_id,
        start_time=exercise.start_time,
        sport=exercise.sport,
        detailed_sport=exercise.detailed_sport,
        device=exercise.device,
    )


def _body_weight(info: PhysicalInfo | None) -> BodyWeight | None:
    if info is None or info.weight is None or info.weight <= 0:
        return None

    # `modified` is when the value took effect; `created` covers a profile that never changed since.
    effective_at = info.modified or info.created
    if effective_at is None:
        return None

    return BodyWeight(kilograms=info.weight, source=info.weight_source or "", effective_at=effective_at)


def _snapshot(limit: WireRateLimit) -> RateLimitSnapshot:
    return RateLimitSnapshot(
        short_usage=limit.short_usage,
        short_limit=limit.short_limit,
        long_usage=limit.long_usage,
        long_limit=limit.long_limit,
        # The short-term window is the one that bites first, so that is the reset worth surfacing.
        reset_seconds=limit.short_reset_seconds,
    )
