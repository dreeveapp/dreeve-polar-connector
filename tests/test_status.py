"""Health and the authorization report. The `authorizeUrl` field is the whole point of this module: the
answer to "why is nothing arriving?" should be one line of /status, not a documentation search."""

from datetime import UTC, date, datetime, timedelta

from dreeve_polar_connector.config import WeightUnit
from dreeve_polar_connector.ledger import ExerciseStatus
from dreeve_polar_connector.status import UNHEALTHY_AFTER_CYCLES, AuthorizationState, Status
from dreeve_polar_connector.sync import CycleResult
from dreeve_polar_connector.weight import WeightOutcome, WeightResult

STARTED = datetime(2026, 7, 29, 8, 0, tzinfo=UTC)
POLL_INTERVAL = 900


def _status(authorize_url: str | None = "http://nas.local:8080/authorize") -> Status:
    return Status(poll_interval=POLL_INTERVAL, started_at=STARTED, authorize_url=authorize_url)


def test_a_fresh_connector_is_unhealthy_until_someone_authorizes() -> None:
    # The spec is explicit that this is correct: an unauthorized connector is useless, and saying so
    # loudly beats the silent alternative where the container runs and nothing ever arrives.
    assert not _status().is_healthy(STARTED)


def test_an_authorized_connector_gets_grace_before_its_first_cycle() -> None:
    # Once authorized, a slow first cycle is not a failure — the same grace every later cycle gets.
    status = _status()
    status.authorized(475)

    assert status.is_healthy(STARTED + timedelta(seconds=POLL_INTERVAL))


def test_a_connector_that_has_not_synced_for_three_intervals_is_unhealthy() -> None:
    # One missed cycle is noise; three is a problem.
    stale = STARTED + timedelta(seconds=POLL_INTERVAL * UNHEALTHY_AFTER_CYCLES + 1)

    assert not _status().is_healthy(stale)


def test_a_successful_cycle_resets_the_clock() -> None:
    status = _status()
    later = STARTED + timedelta(seconds=POLL_INTERVAL * 2)
    status.authorized(475)
    status.cycle_succeeded(CycleResult(listed=1, delivered=1), {}, later)

    assert status.is_healthy(later + timedelta(seconds=POLL_INTERVAL))


def test_an_unauthorized_connector_is_unhealthy_immediately() -> None:
    status = _status()

    status.authorization_required("nobody has authorized yet")

    assert not status.is_healthy(STARTED)


def test_an_unauthorized_connector_reports_the_url_to_open() -> None:
    status = _status()
    status.authorization_required("nobody has authorized yet")

    payload = status.to_dict(STARTED)

    assert payload["authorization"] == AuthorizationState.REQUIRED
    assert payload["authorizeUrl"] == "http://nas.local:8080/authorize"


def test_an_authorized_connector_does_not_advertise_the_url() -> None:
    status = _status()
    status.authorized(475)

    payload = status.to_dict(STARTED)

    assert payload["authorization"] == AuthorizationState.OK
    assert payload["authorizeUrl"] is None
    assert payload["polarUser"] == 475


def test_a_revoked_token_is_distinct_from_never_having_authorized() -> None:
    status = _status()
    status.authorization_revoked("Polar rejected the token")

    assert status.to_dict(STARTED)["authorization"] == AuthorizationState.REVOKED


def test_a_missing_consent_is_reported_as_its_own_state() -> None:
    # Fixed in Polar Flow, not by re-authorizing, so it must not read as "authorize again".
    status = _status()
    status.consent_required("not all consents accepted")

    assert status.to_dict(STARTED)["authorization"] == AuthorizationState.CONSENT_REQUIRED


def test_there_is_no_authorize_url_when_the_server_is_off() -> None:
    status = _status(authorize_url=None)
    status.authorization_required("nobody has authorized yet")

    assert status.to_dict(STARTED)["authorizeUrl"] is None


def test_a_successful_cycle_is_reported() -> None:
    status = _status()
    status.authorized(475)
    status.cycle_succeeded(CycleResult(listed=3, delivered=2, backlog=1), {ExerciseStatus.DELIVERED: 2}, STARTED)

    payload = status.to_dict(STARTED)

    assert payload["cycles"] == 1
    assert payload["lastCycle"]["delivered"] == 2
    assert payload["backlog"] == 1
    assert payload["exercises"]["delivered"] == 2
    assert payload["lastError"] is None


def test_a_failed_cycle_records_the_error_without_clearing_authorization() -> None:
    status = _status()
    status.authorized(475)
    status.cycle_failed("the watch folder went away")

    payload = status.to_dict(STARTED)

    assert payload["lastError"] == "the watch folder went away"
    assert payload["authorization"] == AuthorizationState.OK


def test_a_rate_limit_reports_the_backoff() -> None:
    status = _status()
    status.rate_limited(1800.0, "429 from Polar")

    payload = status.to_dict(STARTED)

    assert payload["backoffSeconds"] == 1800.0
    assert payload["lastError"] == "429 from Polar"


def test_the_next_run_is_reported() -> None:
    status = _status()
    next_run = STARTED + timedelta(seconds=POLL_INTERVAL)
    status.sleeping_until(next_run)

    assert status.to_dict(STARTED)["nextRunAt"] == next_run.isoformat()


def test_the_webhook_is_reported_when_registered() -> None:
    status = _status()
    status.webhook_registered(webhook_id="abdf33", active=True)

    payload = status.to_dict(STARTED)

    assert payload["webhook"] == {
        "configured": True,
        "active": True,
        "id": "abdf33",
        "lastEventAt": None,
        "rejectedDeliveries": 0,
    }


def test_the_webhook_reports_the_last_event_it_received() -> None:
    status = _status()
    status.webhook_registered(webhook_id="abdf33", active=True)
    status.webhook_event_received(STARTED)

    assert status.to_dict(STARTED)["webhook"]["lastEventAt"] == STARTED.isoformat()


def test_an_unconfigured_webhook_is_reported_as_such() -> None:
    assert _status().to_dict(STARTED)["webhook"] == {
        "configured": False,
        "active": False,
        "id": None,
        "lastEventAt": None,
        "rejectedDeliveries": 0,
    }


def test_rejected_deliveries_start_at_zero() -> None:
    assert _status().to_dict(STARTED)["webhook"]["rejectedDeliveries"] == 0


def test_rejected_deliveries_are_counted() -> None:
    # A rising count means deliveries are arriving but cannot be verified, which is the one symptom
    # that distinguishes a signing-key problem from Polar simply being quiet.
    status = _status()

    status.delivery_rejected()
    status.delivery_rejected()

    assert status.to_dict(STARTED)["webhook"]["rejectedDeliveries"] == 2


def test_the_rate_limit_budget_is_reported() -> None:
    status = _status()
    status.rate_limit_seen(short_usage=12, short_limit=520, long_usage=340, long_limit=5100, reset_seconds=640)

    assert status.to_dict(STARTED)["rateLimit"] == {
        "shortUsage": 12,
        "shortLimit": 520,
        "longUsage": 340,
        "longLimit": 5100,
        "resetSeconds": 640,
    }


def test_the_authorization_state_is_readable() -> None:
    # The server has to branch on this, and reaching into a private attribute from another module is
    # how that branch silently rots.
    status = _status()

    assert status.authorization_state() is AuthorizationState.REQUIRED

    status.authorized(475)

    assert status.authorization_state() is AuthorizationState.OK


def test_delivery_rejected_reports_the_running_total() -> None:
    # The server thins out its logging by this number, so it has to be the real count.
    status = _status()

    assert status.delivery_rejected() == 1
    assert status.delivery_rejected() == 2


PUSHED = WeightResult(WeightOutcome.PUSHED, on=date(2026, 10, 30), value=80.4)


def _weighing_status() -> Status:
    status = Status(poll_interval=POLL_INTERVAL, started_at=STARTED, authorize_url=None, weight_unit=WeightUnit.KG)
    status.authorized(475)
    return status


def test_weight_is_not_mentioned_while_weight_sync_is_off() -> None:
    assert "weight" not in _status().to_dict(STARTED)


def test_weight_sync_is_reported_before_its_first_cycle() -> None:
    assert _weighing_status().to_dict(STARTED)["weight"] == {
        "unit": "kg",
        "lastOutcome": None,
        "lastRecordedOn": None,
        "lastRecordedValue": None,
        "lastError": None,
    }


def test_a_recorded_weight_is_reported() -> None:
    status = _weighing_status()
    status.cycle_succeeded(CycleResult(weight=PUSHED), {}, STARTED)

    weight = status.to_dict(STARTED)["weight"]

    assert weight["lastOutcome"] == "pushed"
    assert weight["lastRecordedOn"] == "2026-10-30"
    assert weight["lastRecordedValue"] == 80.4


def test_an_unchanged_weight_still_says_what_dreeve_has() -> None:
    # After a restart nothing is pushed until the weight changes; /status should not go blank meanwhile.
    status = _weighing_status()
    status.cycle_succeeded(
        CycleResult(weight=WeightResult(WeightOutcome.UNCHANGED, on=date(2026, 10, 30), value=80.4)), {}, STARTED
    )

    assert status.to_dict(STARTED)["weight"]["lastRecordedOn"] == "2026-10-30"


def test_a_failed_weight_keeps_the_last_recorded_one_and_reports_the_error() -> None:
    status = _weighing_status()
    status.cycle_succeeded(CycleResult(weight=PUSHED), {}, STARTED)
    status.cycle_succeeded(CycleResult(weight=WeightResult(WeightOutcome.FAILED, error="Dreeve is down")), {}, STARTED)

    weight = status.to_dict(STARTED)["weight"]

    assert weight["lastOutcome"] == "failed"
    assert weight["lastError"] == "Dreeve is down"
    assert weight["lastRecordedOn"] == "2026-10-30"


def test_a_failed_weight_is_neither_the_connectors_error_nor_its_health() -> None:
    # Exercise sync is what healthy means; Docker must not restart a connector that is delivering fine.
    status = _weighing_status()
    status.cycle_succeeded(CycleResult(weight=WeightResult(WeightOutcome.FAILED, error="Dreeve is down")), {}, STARTED)

    report = status.to_dict(STARTED)

    assert report["lastError"] is None
    assert report["healthy"] is True


def test_the_weight_error_clears_once_a_weight_is_recorded() -> None:
    status = _weighing_status()
    status.cycle_succeeded(CycleResult(weight=WeightResult(WeightOutcome.FAILED, error="Dreeve is down")), {}, STARTED)
    status.cycle_succeeded(CycleResult(weight=PUSHED), {}, STARTED)

    assert status.to_dict(STARTED)["weight"]["lastError"] is None
