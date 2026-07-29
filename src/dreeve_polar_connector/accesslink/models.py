"""Pydantic models of the AccessLink wire format.

Two jobs beyond type safety. Validation errors name the offending field, which is what makes an
`UnexpectedResponse` actionable instead of a bare `KeyError`. And normalisation happens here, once,
rather than in every caller — `start_time` being the case that matters.
"""

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Exercise(BaseModel):
    """One training session, as AccessLink reports it, with the start time already in UTC."""

    # Inlined rather than shared through a module-level constant: the pydantic mypy plugin only
    # reads `frozen=True` (and marks fields read-only for mypy) off a literal ConfigDict(...) call
    # written directly in the class body.
    model_config = ConfigDict(populate_by_name=True, extra="ignore", frozen=True)

    exercise_id: str = Field(alias="id")
    start_time: datetime
    upload_time: datetime | None = None
    sport: str = ""
    detailed_sport: str = Field(default="", alias="detailed_sport_info")
    device: str = ""
    has_route: bool = False

    @model_validator(mode="before")
    @classmethod
    def _start_time_to_utc(cls, data: Any) -> Any:
        """Polar sends '2008-10-13T10:40:02' plus 'start_time_utc_offset': 180. Both, or neither."""
        if not isinstance(data, dict):
            return data

        raw = data.get("start_time")
        if raw is None or isinstance(raw, datetime):
            return data

        try:
            local = datetime.fromisoformat(str(raw))
        except (TypeError, ValueError) as exception:
            raise ValueError(f"start_time is not an ISO 8601 timestamp: {raw!r}") from exception

        if local.tzinfo is not None:
            return {**data, "start_time": local.astimezone(UTC)}

        offset = data.get("start_time_utc_offset")
        # bool is an int in Python, and `True` minutes is not an offset anybody meant.
        if not isinstance(offset, int) or isinstance(offset, bool):
            raise ValueError(
                f"start_time_utc_offset must be a whole number of minutes, got {offset!r}. "
                f"Without it a start time cannot be placed on the clock at all."
            )

        return {**data, "start_time": local.replace(tzinfo=timezone(timedelta(minutes=offset))).astimezone(UTC)}


class RateLimit(BaseModel):
    """Polar's budget, as reported on every response.

    Each header holds two comma-separated values: the 15-minute figure, then the 24-hour one. Reading
    only the first number, or reading a differently named header, produces a client that believes it
    has unlimited quota — which is how a daemon walks into a 429 it could have seen coming.
    """

    model_config = ConfigDict(frozen=True)

    short_usage: int | None = None
    long_usage: int | None = None
    short_limit: int | None = None
    long_limit: int | None = None
    short_reset_seconds: int | None = None
    long_reset_seconds: int | None = None

    @classmethod
    def from_headers(cls, headers: Mapping[str, str]) -> "RateLimit":
        short_usage, long_usage = _pair(headers.get("RateLimit-Usage"))
        short_limit, long_limit = _pair(headers.get("RateLimit-Limit"))
        short_reset, long_reset = _pair(headers.get("RateLimit-Reset"))

        return cls(
            short_usage=short_usage,
            long_usage=long_usage,
            short_limit=short_limit,
            long_limit=long_limit,
            short_reset_seconds=short_reset,
            long_reset_seconds=long_reset,
        )

    @property
    def worst_fraction_used(self) -> float | None:
        """How close the more urgent of the two budgets is to exhaustion, or None if unknown."""
        fractions = [
            usage / limit
            for usage, limit in ((self.short_usage, self.short_limit), (self.long_usage, self.long_limit))
            if usage is not None and limit
        ]

        return max(fractions) if fractions else None


def _pair(raw: str | None) -> tuple[int | None, int | None]:
    """Reads 'short,long'. Anything unreadable becomes None rather than failing a good response."""
    values: list[int | None] = []
    for part in (raw or "").split(",")[:2]:
        try:
            values.append(int(part.strip()))
        except ValueError:
            values.append(None)

    while len(values) < 2:
        values.append(None)

    return values[0], values[1]


class WebhookInfo(BaseModel):
    """An existing webhook. `active` is the field that matters: Polar deactivates after seven days of
    failed deliveries, and a deactivated webhook looks exactly like a working one until you ask."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore", frozen=True)

    webhook_id: str = Field(alias="id")
    events: tuple[str, ...] = ()
    url: str = ""
    active: bool = True


class CreatedWebhook(BaseModel):
    """The create response, and the only time Polar ever discloses `signature_secret_key`."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore", frozen=True)

    webhook_id: str = Field(alias="id")
    events: tuple[str, ...] = ()
    url: str = ""
    signature_secret_key: str


class ExerciseEvent(BaseModel):
    """The body Polar POSTs for an EXERCISE event."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore", frozen=True)

    event: str
    user_id: int
    entity_id: str
    timestamp: datetime | None = None
    url: str = ""
