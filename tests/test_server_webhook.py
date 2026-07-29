"""This endpoint is unauthenticated by construction, so the tests are mostly about what it refuses."""

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import aiohttp
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
from dreeve_polar_connector.server import build_app
from dreeve_polar_connector.status import Status
from dreeve_polar_connector.tokens import Authorization, StoredWebhook, TokenStore

NOW = datetime(2026, 7, 29, 8, 0, tzinfo=UTC)
SECRET = "abe1f3ae-fd33-11e8-8eb2-f2801f1b9fd1"
POLAR_USER = 475


class _Secrets:
    def add(self, _value: str | None) -> None:
        return None


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

    async with aiohttp.ClientSession() as session:
        app = build_app(
            config=config,
            status=built.status,
            clock=StubClock(NOW),
            tokens=tokens,
            session=session,
            secrets=_Secrets(),
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
    async with aiohttp.ClientSession() as session:
        app = build_app(
            config=config,
            status=Status(config.poll_interval, NOW, authorize_url=None),
            clock=StubClock(NOW),
            tokens=TokenStore(config.tokens_dir),
            session=session,
            secrets=_Secrets(),
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
        finally:
            await client.close()
