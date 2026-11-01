"""What the container says about itself, over HTTP and to Docker's healthcheck.

A connector nobody has authorized is by far the likeliest real failure here, and it is silent: the
container keeps running, the logs go quiet, and nothing arrives in the watch folder. So authorization
state is reported first-class, together with the URL to open, rather than left in a log line.

There is deliberately no lock. Both HTTP listeners and the sync loop share one event loop, so nothing
in here is ever touched concurrently — including `delivery_rejected`, which is called from the webhook
listener while `to_dict` is served from the local one.
"""

from datetime import date, datetime, timedelta
from enum import StrEnum
from typing import Any

from dreeve_polar_connector.config import WeightUnit
from dreeve_polar_connector.ledger import ExerciseStatus
from dreeve_polar_connector.sync import CycleResult
from dreeve_polar_connector.weight import WeightOutcome, WeightResult

HEARTBEAT_FILENAME = "heartbeat"
# Three intervals without a completed cycle: one missed cycle is noise, three is a problem.
UNHEALTHY_AFTER_CYCLES = 3


class AuthorizationState(StrEnum):
    OK = "ok"
    REQUIRED = "required"
    REVOKED = "revoked"
    CONSENT_REQUIRED = "consent-required"


class Status:
    """Written by the loop and both listeners, read by `/status`. Same event loop throughout."""

    def __init__(
        self,
        poll_interval: int,
        started_at: datetime,
        authorize_url: str | None,
        weight_unit: WeightUnit | None = None,
    ) -> None:
        self._poll_interval = poll_interval
        self._started_at = started_at
        self._authorize_url = authorize_url
        self._cycles = 0
        self._last_successful_sync: datetime | None = None
        self._last_cycle: CycleResult | None = None
        self._next_run_at: datetime | None = None
        self._backoff_seconds = 0.0
        # Starts REQUIRED, so a connector nobody has authorized is unhealthy from the first moment.
        self._authorization = AuthorizationState.REQUIRED
        self._authorization_error: str | None = None
        self._polar_user: int | None = None
        self._last_error: str | None = None
        self._counts: dict[ExerciseStatus, int] = {}
        self._webhook_id: str | None = None
        self._webhook_active = False
        self._webhook_last_event: datetime | None = None
        self._rejected_deliveries = 0
        self._rate_limit: dict[str, int | None] | None = None
        # None means weight sync is off, and `/status` then says nothing about weight at all.
        self._weight_unit = weight_unit
        self._weight: WeightResult | None = None
        self._weight_on: date | None = None
        self._weight_value: float | None = None

    def authorized(self, polar_user: int) -> None:
        self._authorization = AuthorizationState.OK
        self._authorization_error = None
        self._polar_user = polar_user

    def authorization_required(self, message: str) -> None:
        """Nobody has authorized yet. Cheap to re-check every cycle, so this is not terminal."""
        self._authorization = AuthorizationState.REQUIRED
        self._authorization_error = message

    def authorization_revoked(self, message: str) -> None:
        self._authorization = AuthorizationState.REVOKED
        self._authorization_error = message

    def consent_required(self, message: str) -> None:
        self._authorization = AuthorizationState.CONSENT_REQUIRED
        self._authorization_error = message

    def cycle_succeeded(self, result: CycleResult, counts: dict[ExerciseStatus, int], at: datetime) -> None:
        self._cycles += 1
        self._last_successful_sync = at
        self._last_cycle = result
        self._counts = counts
        self._backoff_seconds = 0.0
        self._last_error = None
        if result.weight is not None:
            self._weight_seen(result.weight)

    def _weight_seen(self, result: WeightResult) -> None:
        """Kept apart from `_last_error` and from health: exercise sync is what healthy means, and a
        Dreeve that is down must not make Docker restart a connector that is delivering fine."""
        self._weight = result
        # UNCHANGED carries what Dreeve already has, so this is filled in after a restart too.
        if result.outcome in (WeightOutcome.PUSHED, WeightOutcome.UNCHANGED):
            self._weight_on = result.on
            self._weight_value = result.value

    def cycle_failed(self, message: str) -> None:
        self._cycles += 1
        self._last_error = message

    def rate_limited(self, backoff_seconds: float, message: str) -> None:
        self._cycles += 1
        self._backoff_seconds = backoff_seconds
        self._last_error = message

    def sleeping_until(self, at: datetime) -> None:
        self._next_run_at = at

    def webhook_registered(self, *, webhook_id: str, active: bool) -> None:
        self._webhook_id = webhook_id
        self._webhook_active = active

    def webhook_event_received(self, at: datetime) -> None:
        self._webhook_last_event = at

    def delivery_rejected(self) -> int:
        """A delivery answered with 401 — unverifiable, not merely uninteresting. Returns the new total.

        Counted because the alternative symptom of a signing-key or scheme problem is silence: Polar
        keeps delivering, this connector keeps refusing, and only the latency changes. The total is
        returned so the caller can log the first refusal and then only every hundredth: this route is
        published, so whoever reaches it decides how often this is called.
        """
        self._rejected_deliveries += 1

        return self._rejected_deliveries

    def rate_limit_seen(
        self,
        *,
        short_usage: int | None,
        short_limit: int | None,
        long_usage: int | None,
        long_limit: int | None,
        reset_seconds: int | None,
    ) -> None:
        self._rate_limit = {
            "shortUsage": short_usage,
            "shortLimit": short_limit,
            "longUsage": long_usage,
            "longLimit": long_limit,
            "resetSeconds": reset_seconds,
        }

    def is_healthy(self, now: datetime) -> bool:
        """Unauthorized is unhealthy, immediately and on purpose.

        A fresh install has no token, so it reports unhealthy until someone completes `/authorize` —
        that is the whole point, because the alternative failure mode is silent: the container runs,
        the logs go quiet, and the watch folder stays empty. Docker's own `--start-period` covers the
        few seconds of genuine startup.
        """
        if self._authorization is not AuthorizationState.OK:
            return False

        # Once authorized, the first cycle gets the same grace as every later one, so a slow start
        # is not reported as a failure.
        since = self._last_successful_sync or self._started_at

        return now - since <= timedelta(seconds=self._poll_interval * UNHEALTHY_AFTER_CYCLES)

    def authorization_state(self) -> AuthorizationState:
        """Read by the server to decide whether starting a fresh authorization is legitimate."""
        return self._authorization

    def to_dict(self, now: datetime) -> dict[str, Any]:
        return {
            "healthy": self.is_healthy(now),
            "startedAt": self._started_at.isoformat(),
            "cycles": self._cycles,
            "lastSuccessfulSync": _instant(self._last_successful_sync),
            "nextRunAt": _instant(self._next_run_at),
            "backoffSeconds": self._backoff_seconds,
            "authorization": self._authorization,
            # Only shown when it is actionable, so a healthy /status stays quiet.
            "authorizeUrl": self._authorize_url if self._authorization is not AuthorizationState.OK else None,
            "authorizationError": self._authorization_error,
            "polarUser": self._polar_user,
            "lastError": self._last_error,
            "lastCycle": _cycle(self._last_cycle),
            "backlog": self._last_cycle.backlog if self._last_cycle else 0,
            "exercises": {status.value: count for status, count in self._counts.items()},
            "webhook": {
                "configured": self._webhook_id is not None,
                "active": self._webhook_active,
                "id": self._webhook_id,
                "lastEventAt": _instant(self._webhook_last_event),
                "rejectedDeliveries": self._rejected_deliveries,
            },
            "rateLimit": self._rate_limit,
        } | self._weight_dict()

    def _weight_dict(self) -> dict[str, Any]:
        if self._weight_unit is None:
            return {}

        return {
            "weight": {
                "unit": self._weight_unit.value,
                "lastOutcome": self._weight.outcome.value if self._weight else None,
                "lastRecordedOn": self._weight_on.isoformat() if self._weight_on else None,
                "lastRecordedValue": self._weight_value,
                "lastError": self._weight.error if self._weight else None,
            }
        }


def _instant(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _cycle(result: CycleResult | None) -> dict[str, int] | None:
    if result is None:
        return None

    return {
        "listed": result.listed,
        "delivered": result.delivered,
        "failed": result.failed,
        "skipped": result.skipped,
        "withoutFile": result.without_file,
        "backlog": result.backlog,
    }
