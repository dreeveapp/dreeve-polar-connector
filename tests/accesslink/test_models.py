"""Timestamp handling gets more attention than anything else in this file on purpose: a silent
three-hour error surfaces months later as mysteriously mis-ordered activities, and nothing in the
rest of the system can detect it."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from dreeve_polar_connector.accesslink.models import CreatedWebhook, Exercise, ExerciseEvent, RateLimit, WebhookInfo

MINIMAL_PAYLOAD = {
    "id": "2AC312F",
    "start_time": "2008-10-13T10:40:02",
    "start_time_utc_offset": 180,
}


def test_start_time_is_the_local_time_shifted_by_the_offset() -> None:
    exercise = Exercise.model_validate(MINIMAL_PAYLOAD)

    # Local 10:40:02 at UTC+3 is 07:40:02 UTC.
    assert exercise.start_time == datetime(2008, 10, 13, 7, 40, 2, tzinfo=UTC)


def test_a_negative_offset_shifts_the_other_way() -> None:
    exercise = Exercise.model_validate({**MINIMAL_PAYLOAD, "start_time_utc_offset": -300})

    assert exercise.start_time == datetime(2008, 10, 13, 15, 40, 2, tzinfo=UTC)


def test_a_zero_offset_is_utc() -> None:
    exercise = Exercise.model_validate({**MINIMAL_PAYLOAD, "start_time_utc_offset": 0})

    assert exercise.start_time == datetime(2008, 10, 13, 10, 40, 2, tzinfo=UTC)


def test_a_missing_offset_is_rejected_rather_than_assumed_to_be_utc() -> None:
    payload = {"id": "2AC312F", "start_time": "2008-10-13T10:40:02"}

    with pytest.raises(ValidationError, match="start_time_utc_offset"):
        Exercise.model_validate(payload)


def test_a_non_integer_offset_is_rejected() -> None:
    with pytest.raises(ValidationError, match="start_time_utc_offset"):
        Exercise.model_validate({**MINIMAL_PAYLOAD, "start_time_utc_offset": "three hours"})


def test_an_unparseable_start_time_is_rejected() -> None:
    with pytest.raises(ValidationError, match="start_time"):
        Exercise.model_validate({**MINIMAL_PAYLOAD, "start_time": "last tuesday"})


def test_an_already_aware_start_time_is_converted_rather_than_shifted_twice() -> None:
    # Not something Polar sends, but revalidating our own output must be a no-op.
    exercise = Exercise.model_validate({**MINIMAL_PAYLOAD, "start_time": "2008-10-13T10:40:02+02:00"})

    assert exercise.start_time == datetime(2008, 10, 13, 8, 40, 2, tzinfo=UTC)


def test_the_id_is_read_from_polars_field_name() -> None:
    assert Exercise.model_validate(MINIMAL_PAYLOAD).exercise_id == "2AC312F"


def test_optional_fields_default_rather_than_failing() -> None:
    exercise = Exercise.model_validate(MINIMAL_PAYLOAD)

    assert exercise.sport == ""
    assert exercise.detailed_sport == ""
    assert exercise.device == ""
    assert exercise.upload_time is None


def test_full_payload_is_read() -> None:
    exercise = Exercise.model_validate(
        {
            **MINIMAL_PAYLOAD,
            "upload_time": "2008-10-13T10:40:02.000Z",
            "sport": "RUNNING",
            "detailed_sport_info": "RUNNING_TREADMILL",
            "device": "Polar M400",
            "has_route": True,
        }
    )

    assert exercise.sport == "RUNNING"
    assert exercise.detailed_sport == "RUNNING_TREADMILL"
    assert exercise.device == "Polar M400"
    assert exercise.has_route is True
    assert exercise.upload_time == datetime(2008, 10, 13, 10, 40, 2, tzinfo=UTC)


def test_unknown_fields_are_ignored_so_a_new_polar_field_does_not_break_the_sync() -> None:
    exercise = Exercise.model_validate({**MINIMAL_PAYLOAD, "something_polar_added_last_week": 42})

    assert exercise.exercise_id == "2AC312F"


def test_the_model_is_frozen() -> None:
    exercise = Exercise.model_validate(MINIMAL_PAYLOAD)

    with pytest.raises(ValidationError):
        exercise.sport = "CYCLING"  # type: ignore[misc]


def test_rate_limit_reads_the_short_and_long_term_pairs() -> None:
    limit = RateLimit.from_headers(
        {
            "RateLimit-Usage": "12,340",
            "RateLimit-Limit": "520,5100",
            "RateLimit-Reset": "640,80000",
        }
    )

    assert (limit.short_usage, limit.long_usage) == (12, 340)
    assert (limit.short_limit, limit.long_limit) == (520, 5100)
    assert (limit.short_reset_seconds, limit.long_reset_seconds) == (640, 80000)


def test_rate_limit_tolerates_whitespace_around_the_pair() -> None:
    limit = RateLimit.from_headers({"RateLimit-Usage": " 12 , 340 "})

    assert (limit.short_usage, limit.long_usage) == (12, 340)


def test_missing_headers_leave_everything_unknown() -> None:
    limit = RateLimit.from_headers({})

    assert limit.short_usage is None
    assert limit.worst_fraction_used is None


def test_a_malformed_header_degrades_instead_of_failing_the_response() -> None:
    limit = RateLimit.from_headers({"RateLimit-Usage": "plenty,left"})

    assert (limit.short_usage, limit.long_usage) == (None, None)


def test_a_single_value_header_fills_only_the_short_term_slot() -> None:
    limit = RateLimit.from_headers({"RateLimit-Usage": "12"})

    assert (limit.short_usage, limit.long_usage) == (12, None)


def test_worst_fraction_used_takes_the_more_urgent_of_the_two_budgets() -> None:
    limit = RateLimit.from_headers({"RateLimit-Usage": "260,102", "RateLimit-Limit": "520,5100"})

    # Short term is at 50%, long term at 2%. The short one is what will bite.
    assert limit.worst_fraction_used == pytest.approx(0.5)


def test_worst_fraction_used_ignores_a_zero_limit_rather_than_dividing_by_it() -> None:
    limit = RateLimit.from_headers({"RateLimit-Usage": "1,102", "RateLimit-Limit": "0,5100"})

    assert limit.worst_fraction_used == pytest.approx(102 / 5100)


def test_created_webhook_carries_the_signing_key() -> None:
    created = CreatedWebhook.model_validate(
        {
            "id": "abdf33",
            "events": ["EXERCISE"],
            "url": "https://example.test/webhook",
            "signature_secret_key": "abe1f3ae-fd33-11e8-8eb2-f2801f1b9fd1",
        }
    )

    assert created.webhook_id == "abdf33"
    assert created.signature_secret_key == "abe1f3ae-fd33-11e8-8eb2-f2801f1b9fd1"
    assert created.events == ("EXERCISE",)


def test_webhook_info_defaults_to_active() -> None:
    info = WebhookInfo.model_validate({"id": "abdf33", "events": ["EXERCISE"], "url": "https://example.test/webhook"})

    assert info.active is True


def test_webhook_info_reads_a_deactivated_webhook() -> None:
    info = WebhookInfo.model_validate({"id": "abdf33", "url": "https://example.test/webhook", "active": False})

    assert info.active is False


def test_exercise_event_reads_the_payload_polar_posts() -> None:
    event = ExerciseEvent.model_validate(
        {
            "event": "EXERCISE",
            "user_id": 475,
            "entity_id": "aQlC83",
            "timestamp": "2018-05-15T14:22:24Z",
            "url": "https://www.polaraccesslink.com/v3/exercises/aQlC83",
        }
    )

    assert event.user_id == 475
    assert event.entity_id == "aQlC83"


def test_exercise_event_requires_the_fields_the_handler_acts_on() -> None:
    with pytest.raises(ValidationError):
        ExerciseEvent.model_validate({"event": "EXERCISE"})
