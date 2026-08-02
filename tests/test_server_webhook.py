"""This endpoint is unauthenticated by construction, so the tests are mostly about what it refuses."""

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from dreeve_polar_connector.accesslink.webhooks import (
    EVENT_HEADER,
    EXERCISE_EVENT,
    MAX_BODY_BYTES,
    PING_EVENT,
    SIGNATURE_HEADER,
    signature_for,
)
from dreeve_polar_connector.config import Config
from dreeve_polar_connector.ledger import LEDGER_FILENAME
from dreeve_polar_connector.server import build_webhook_app
from dreeve_polar_connector.status import Status
from dreeve_polar_connector.tokens import Authorization, StoredWebhook, TokenStore

NOW = datetime(2026, 7, 29, 8, 0, tzinfo=UTC)
SECRET = "abe1f3ae-fd33-11e8-8eb2-f2801f1b9fd1"
POLAR_USER = 475


class _Harness:
    client: TestClient[web.Request, web.Application]
    status: Status
    wake: asyncio.Event
    tokens: TokenStore
    state_dir: Path


@pytest_asyncio.fixture
async def harness(tmp_path: Path) -> AsyncIterator[_Harness]:
    from tests.stubs import StubClock

    config = Config.from_env(
        {
            "POLAR_CLIENT_ID": "a-client",
            "POLAR_CLIENT_SECRET": "a-secret",
            "WATCH_DIR": str(tmp_path / "watch"),
            "STATE_DIR": str(tmp_path / "state"),
            "POLAR_TOKENS": str(tmp_path / "tokens"),
        }
    )
    tokens = TokenStore(config.tokens_dir)
    await tokens.store(
        Authorization(polar_user_id=POLAR_USER, access_token="a-token", member_id="dreeve-475", authorized_at=NOW)
    )
    await tokens.store_webhook(
        StoredWebhook(webhook_id="abdf33", signature_secret_key=SECRET, url="https://example.test/webhook")
    )

    built = _Harness()
    built.status = Status(config.poll_interval, NOW, authorize_url=None)
    built.wake = asyncio.Event()
    built.tokens = tokens
    built.state_dir = config.state_dir

    app = build_webhook_app(
        status=built.status,
        clock=StubClock(NOW),
        tokens=tokens,
        wake=built.wake,
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    built.client = client

    yield built

    await client.close()


def _exercise_body(user_id: int = POLAR_USER) -> bytes:
    return json.dumps({"event": EXERCISE_EVENT, "user_id": user_id, "entity_id": "aQlC83"}).encode("utf-8")


@pytest.mark.asyncio
async def test_a_ping_is_answered_without_a_signature(harness: _Harness) -> None:
    # It has to be: the signing key only arrives in the create response, after this ping.
    response = await harness.client.post("/webhook", data=b"{}", headers={EVENT_HEADER: PING_EVENT})

    assert response.status == 200


@pytest.mark.asyncio
async def test_a_signed_exercise_event_wakes_the_loop(harness: _Harness) -> None:
    body = _exercise_body()

    response = await harness.client.post(
        "/webhook",
        data=body,
        headers={EVENT_HEADER: EXERCISE_EVENT, SIGNATURE_HEADER: signature_for(body, SECRET)},
    )

    assert response.status == 200
    assert harness.wake.is_set()


@pytest.mark.asyncio
async def test_a_signed_exercise_event_is_recorded_in_status(harness: _Harness) -> None:
    body = _exercise_body()

    await harness.client.post(
        "/webhook",
        data=body,
        headers={EVENT_HEADER: EXERCISE_EVENT, SIGNATURE_HEADER: signature_for(body, SECRET)},
    )

    assert harness.status.to_dict(NOW)["webhook"]["lastEventAt"] == NOW.isoformat()


@pytest.mark.asyncio
async def test_the_handler_never_touches_the_ledger(harness: _Harness) -> None:
    # Single-writer discipline: sync.py owns the ledger, so an atomic flush stays atomic.
    body = _exercise_body()

    await harness.client.post(
        "/webhook",
        data=body,
        headers={EVENT_HEADER: EXERCISE_EVENT, SIGNATURE_HEADER: signature_for(body, SECRET)},
    )

    assert not (harness.state_dir / LEDGER_FILENAME).exists()


@pytest.mark.asyncio
async def test_a_tampered_body_is_rejected_and_does_not_wake_the_loop(harness: _Harness) -> None:
    body = _exercise_body()
    signature = signature_for(body, SECRET)

    response = await harness.client.post(
        "/webhook",
        data=body.replace(b"aQlC83", b"aQlC84"),
        headers={EVENT_HEADER: EXERCISE_EVENT, SIGNATURE_HEADER: signature},
    )

    assert response.status == 401
    assert not harness.wake.is_set()


@pytest.mark.asyncio
async def test_an_unsigned_exercise_event_is_rejected(harness: _Harness) -> None:
    response = await harness.client.post("/webhook", data=_exercise_body(), headers={EVENT_HEADER: EXERCISE_EVENT})

    assert response.status == 401
    assert not harness.wake.is_set()


@pytest.mark.asyncio
async def test_an_event_for_another_user_is_accepted_and_ignored(harness: _Harness) -> None:
    # 200 so Polar's delivery record stays clean; no wake, because it is not our data.
    body = _exercise_body(user_id=999)

    response = await harness.client.post(
        "/webhook",
        data=body,
        headers={EVENT_HEADER: EXERCISE_EVENT, SIGNATURE_HEADER: signature_for(body, SECRET)},
    )

    assert response.status == 200
    assert not harness.wake.is_set()


@pytest.mark.asyncio
async def test_a_non_exercise_event_is_accepted_and_ignored(harness: _Harness) -> None:
    body = json.dumps({"event": "SLEEP", "user_id": POLAR_USER, "entity_id": "x"}).encode("utf-8")

    response = await harness.client.post(
        "/webhook",
        data=body,
        headers={EVENT_HEADER: "SLEEP", SIGNATURE_HEADER: signature_for(body, SECRET)},
    )

    assert response.status == 200
    assert not harness.wake.is_set()


@pytest.mark.asyncio
async def test_an_oversized_body_is_refused_before_it_is_read(harness: _Harness) -> None:
    oversized = b"x" * (MAX_BODY_BYTES + 1)

    response = await harness.client.post(
        "/webhook",
        data=oversized,
        headers={EVENT_HEADER: EXERCISE_EVENT, SIGNATURE_HEADER: signature_for(oversized, SECRET)},
    )

    assert response.status == 413


@pytest.mark.asyncio
async def test_a_non_ascii_signature_is_refused_rather_than_crashing(harness: _Harness) -> None:
    # An unauthenticated 500 is a defect in itself; this header is chosen by the caller.
    body = _exercise_body()

    response = await harness.client.post(
        "/webhook",
        data=body,
        headers={EVENT_HEADER: EXERCISE_EVENT, SIGNATURE_HEADER: "ü" * 64},
    )

    assert response.status == 401
    assert not harness.wake.is_set()


@pytest.mark.asyncio
async def test_an_event_arriving_before_registration_is_refused(tmp_path: Path) -> None:
    from tests.stubs import StubClock

    config = Config.from_env(
        {
            "POLAR_CLIENT_ID": "a-client",
            "POLAR_CLIENT_SECRET": "a-secret",
            "WATCH_DIR": str(tmp_path / "watch"),
            "STATE_DIR": str(tmp_path / "state"),
            "POLAR_TOKENS": str(tmp_path / "tokens"),
        }
    )
    status = Status(config.poll_interval, NOW, authorize_url=None)
    app = build_webhook_app(
        status=status,
        clock=StubClock(NOW),
        tokens=TokenStore(config.tokens_dir),
        wake=asyncio.Event(),
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        body = _exercise_body()
        response = await client.post(
            "/webhook",
            data=body,
            headers={EVENT_HEADER: EXERCISE_EVENT, SIGNATURE_HEADER: signature_for(body, SECRET)},
        )

        # No stored key means nothing can be verified, so the only safe answer is to refuse.
        assert response.status == 401
        assert status.to_dict(NOW)["webhook"]["rejectedDeliveries"] == 1
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_a_ping_with_a_stored_key_and_a_good_signature_is_verified(
    harness: _Harness, caplog: pytest.LogCaptureFixture
) -> None:
    body = b'{"event":"PING"}'

    with caplog.at_level(logging.INFO):
        response = await harness.client.post(
            "/webhook",
            data=body,
            headers={EVENT_HEADER: PING_EVENT, SIGNATURE_HEADER: signature_for(body, SECRET)},
        )

    assert response.status == 200
    assert "verified its signature" in caplog.text


@pytest.mark.asyncio
async def test_a_ping_whose_signature_does_not_match_is_answered_and_warned_about(
    harness: _Harness, caplog: pytest.LogCaptureFixture
) -> None:
    # 200 regardless: refusing a ping fails webhook creation and re-activation. The warning is the
    # point — it says our HMAC construction disagrees with Polar's before deliveries start failing.
    with caplog.at_level(logging.WARNING):
        response = await harness.client.post(
            "/webhook",
            data=b'{"event":"PING"}',
            headers={EVENT_HEADER: PING_EVENT, SIGNATURE_HEADER: "a" * 64},
        )

    assert response.status == 200
    assert "did not match" in caplog.text


@pytest.mark.asyncio
async def test_an_unsigned_ping_before_registration_is_answered_without_a_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from tests.stubs import StubClock

    # The create-time ping arrives before Polar discloses the key, so it cannot be verified at all.
    config = Config.from_env(
        {
            "POLAR_CLIENT_ID": "a-client",
            "POLAR_CLIENT_SECRET": "a-secret",
            "WATCH_DIR": str(tmp_path / "watch"),
            "STATE_DIR": str(tmp_path / "state"),
            "POLAR_TOKENS": str(tmp_path / "tokens"),
        }
    )
    app = build_webhook_app(
        status=Status(config.poll_interval, NOW, authorize_url=None),
        clock=StubClock(NOW),
        tokens=TokenStore(config.tokens_dir),
        wake=asyncio.Event(),
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        with caplog.at_level(logging.WARNING):
            response = await client.post("/webhook", data=b"{}", headers={EVENT_HEADER: PING_EVENT})

        assert response.status == 200
        assert "did not match" not in caplog.text
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_a_rejected_delivery_is_counted_in_status(harness: _Harness) -> None:
    body = _exercise_body()

    await harness.client.post(
        "/webhook",
        data=body,
        headers={EVENT_HEADER: EXERCISE_EVENT, SIGNATURE_HEADER: "b" * 64},
    )

    assert harness.status.to_dict(NOW)["webhook"]["rejectedDeliveries"] == 1


@pytest.mark.asyncio
async def test_an_accepted_delivery_is_not_counted_as_rejected(harness: _Harness) -> None:
    body = _exercise_body()

    await harness.client.post(
        "/webhook",
        data=body,
        headers={EVENT_HEADER: EXERCISE_EVENT, SIGNATURE_HEADER: signature_for(body, SECRET)},
    )

    assert harness.status.to_dict(NOW)["webhook"]["rejectedDeliveries"] == 0


@pytest.mark.asyncio
async def test_an_oversized_body_is_not_counted_as_a_verification_failure(harness: _Harness) -> None:
    # 413 says nothing about signing, so counting it would muddy the one signal this counter carries.
    oversized = b"x" * (MAX_BODY_BYTES + 1)

    await harness.client.post(
        "/webhook",
        data=oversized,
        headers={EVENT_HEADER: EXERCISE_EVENT, SIGNATURE_HEADER: signature_for(oversized, SECRET)},
    )

    assert harness.status.to_dict(NOW)["webhook"]["rejectedDeliveries"] == 0


@pytest.mark.asyncio
async def test_an_unreadable_store_refuses_a_delivery_instead_of_crashing(harness: _Harness) -> None:
    # This is the only route published to the internet, so an unreadable webhook.json must not become
    # an unauthenticated 500 — the defect class this branch set out to remove.
    harness.tokens.webhook_path.write_text("{not json", encoding="utf-8")
    body = _exercise_body()

    response = await harness.client.post(
        "/webhook",
        data=body,
        headers={EVENT_HEADER: EXERCISE_EVENT, SIGNATURE_HEADER: signature_for(body, SECRET)},
    )

    assert response.status == 401
    assert not harness.wake.is_set()
    assert harness.status.to_dict(NOW)["webhook"]["rejectedDeliveries"] == 1


@pytest.mark.asyncio
async def test_an_unreadable_store_still_answers_a_ping(harness: _Harness) -> None:
    # Polar refuses to create or activate a webhook whose URL does not answer 200, so a broken store
    # must not also cost the operator the ability to register one.
    harness.tokens.webhook_path.write_text('{"version": 99}', encoding="utf-8")

    response = await harness.client.post("/webhook", data=b"{}", headers={EVENT_HEADER: PING_EVENT})

    assert response.status == 200


@pytest.mark.asyncio
async def test_an_unreadable_store_is_reported_as_an_error(harness: _Harness, caplog: pytest.LogCaptureFixture) -> None:
    # Distinguishable from "no key stored yet": one is a broken volume a human must fix, the other is
    # the ordinary state before registration.
    harness.tokens.webhook_path.write_text("[]", encoding="utf-8")

    with caplog.at_level(logging.ERROR):
        await harness.client.post("/webhook", data=b"{}", headers={EVENT_HEADER: PING_EVENT})

    assert "Cannot verify webhook deliveries" in caplog.text


@pytest.mark.asyncio
async def test_repeated_refusals_stop_filling_the_log(harness: _Harness, caplog: pytest.LogCaptureFixture) -> None:
    # This route is published, so the rate of refusals is chosen by whoever can reach it. The count must
    # stay exact; the log must not grow one line per attempt.
    body = _exercise_body()

    with caplog.at_level(logging.WARNING):
        for _ in range(12):
            await harness.client.post(
                "/webhook",
                data=body,
                headers={EVENT_HEADER: EXERCISE_EVENT, SIGNATURE_HEADER: "c" * 64},
            )

    warnings = [record for record in caplog.records if "signature did not match" in record.message]
    assert len(warnings) == 1
    assert harness.status.to_dict(NOW)["webhook"]["rejectedDeliveries"] == 12
