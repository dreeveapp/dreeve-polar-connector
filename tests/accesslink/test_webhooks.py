"""The signature check is the only authentication this endpoint can have, so it is tested against
tampering directly rather than through the server."""

import hashlib
import hmac
import json
from collections.abc import Awaitable, Callable
from typing import TypedDict

import aiohttp
import pytest
from aiohttp import web

from dreeve_polar_connector.accesslink.exceptions import (
    AuthenticationFailed,
    ConnectionFailed,
    RateLimited,
    UnexpectedResponse,
    WebhookAlreadyExists,
)
from dreeve_polar_connector.accesslink.webhooks import (
    EXERCISE_EVENT,
    PING_EVENT,
    activate,
    create,
    delete,
    parse_exercise_event,
    read,
    signature_for,
    signature_matches,
)
from tests.accesslink.conftest import FakeAccessLink, unused_closed_port

Factory = Callable[[dict[tuple[str, str], object]], Awaitable[FakeAccessLink]]


class _Credentials(TypedDict):
    """A TypedDict rather than a plain dict: `**CREDENTIALS` only type-checks each keyword argument
    against its own parameter type (mypy --strict) when the dict's shape is this precise."""

    client_id: str
    client_secret: str


CREDENTIALS: _Credentials = {"client_id": "a-client", "client_secret": "a-secret"}

SECRET = "abe1f3ae-fd33-11e8-8eb2-f2801f1b9fd1"
BODY = json.dumps({"event": "EXERCISE", "user_id": 475, "entity_id": "aQlC83"}).encode("utf-8")


def test_a_signature_we_generate_verifies() -> None:
    assert signature_matches(BODY, signature_for(BODY, SECRET), SECRET)


def test_the_signature_is_hmac_sha256_hex_over_the_raw_body() -> None:
    expected = hmac.new(SECRET.encode("utf-8"), BODY, hashlib.sha256).hexdigest()

    assert signature_for(BODY, SECRET) == expected


def test_a_tampered_body_does_not_verify() -> None:
    signature = signature_for(BODY, SECRET)
    tampered = BODY.replace(b"475", b"476")

    assert not signature_matches(tampered, signature, SECRET)


def test_reserialised_json_does_not_verify() -> None:
    # The reason the handler must hash the raw bytes: re-encoding changes whitespace and key order.
    signature = signature_for(BODY, SECRET)
    reserialised = json.dumps(json.loads(BODY), indent=2).encode("utf-8")

    assert not signature_matches(reserialised, signature, SECRET)


def test_the_wrong_secret_does_not_verify() -> None:
    assert not signature_matches(BODY, signature_for(BODY, SECRET), "a-different-secret")


def test_an_empty_signature_does_not_verify() -> None:
    assert not signature_matches(BODY, "", SECRET)


def test_a_signature_is_compared_case_insensitively() -> None:
    assert signature_matches(BODY, signature_for(BODY, SECRET).upper(), SECRET)


def test_a_non_hex_signature_does_not_verify_and_does_not_raise() -> None:
    assert not signature_matches(BODY, "not-a-signature", SECRET)


def test_an_exercise_event_is_parsed() -> None:
    event = parse_exercise_event(BODY)

    assert event is not None
    assert event.user_id == 475
    assert event.entity_id == "aQlC83"


def test_a_body_that_is_not_an_exercise_event_parses_to_none() -> None:
    body = json.dumps({"event": "SLEEP", "user_id": 475, "entity_id": "x"}).encode("utf-8")

    assert parse_exercise_event(body) is None


def test_an_unreadable_body_parses_to_none_rather_than_raising() -> None:
    # The handler must answer 200 regardless: a non-200 counts as a delivery failure, and seven days
    # of those deactivates the webhook.
    assert parse_exercise_event(b"not json at all") is None


def test_an_exercise_event_missing_its_entity_id_parses_to_none() -> None:
    body = json.dumps({"event": "EXERCISE", "user_id": 475}).encode("utf-8")

    assert parse_exercise_event(body) is None


def test_the_event_names_are_the_ones_polar_sends() -> None:
    assert (PING_EVENT, EXERCISE_EVENT) == ("PING", "EXERCISE")


@pytest.mark.asyncio
async def test_create_returns_the_signing_key(polar: Factory) -> None:
    async def handler(request: web.Request) -> web.Response:
        assert request.headers["Authorization"].startswith("Basic ")
        assert await request.json() == {"events": ["EXERCISE"], "url": "https://example.test/webhook"}
        return web.json_response(
            {
                "data": {
                    "id": "abdf33",
                    "events": ["EXERCISE"],
                    "url": "https://example.test/webhook",
                    "signature_secret_key": SECRET,
                }
            },
            status=201,
        )

    server = await polar({("POST", "/v3/webhooks"): handler})

    created = await create(server.session, url="https://example.test/webhook", base_url=server.base_url, **CREDENTIALS)

    assert created.webhook_id == "abdf33"
    assert created.signature_secret_key == SECRET


@pytest.mark.asyncio
async def test_create_reports_an_existing_webhook_distinctly(polar: Factory) -> None:
    # One webhook per client. The caller needs to tell "already there" from "rejected".
    server = await polar({("POST", "/v3/webhooks"): _status(409, '{"error": "webhook already exists"}')})

    with pytest.raises(WebhookAlreadyExists):
        await create(server.session, url="https://example.test/webhook", base_url=server.base_url, **CREDENTIALS)


@pytest.mark.asyncio
async def test_create_fails_when_polars_ping_could_not_reach_us(polar: Factory) -> None:
    server = await polar({("POST", "/v3/webhooks"): _status(400, '{"error": "ping failed"}')})

    with pytest.raises(UnexpectedResponse, match="ping"):
        await create(server.session, url="https://example.test/webhook", base_url=server.base_url, **CREDENTIALS)


@pytest.mark.asyncio
async def test_read_returns_the_configured_webhook(polar: Factory) -> None:
    async def handler(request: web.Request) -> web.Response:  # noqa: ARG001 - the request is irrelevant here
        return web.json_response(
            {"data": [{"id": "abdf33", "events": ["EXERCISE"], "url": "https://example.test/webhook", "active": False}]}
        )

    server = await polar({("GET", "/v3/webhooks"): handler})

    info = await read(server.session, base_url=server.base_url, **CREDENTIALS)

    assert info is not None
    assert info.webhook_id == "abdf33"
    assert info.active is False


@pytest.mark.asyncio
async def test_read_returns_none_when_there_is_no_webhook(polar: Factory) -> None:
    async def handler(request: web.Request) -> web.Response:  # noqa: ARG001 - the request is irrelevant here
        return web.json_response({"data": []})

    server = await polar({("GET", "/v3/webhooks"): handler})

    assert await read(server.session, base_url=server.base_url, **CREDENTIALS) is None


@pytest.mark.asyncio
async def test_read_treats_204_as_no_webhook(polar: Factory) -> None:
    async def handler(request: web.Request) -> web.Response:  # noqa: ARG001 - the request is irrelevant here
        return web.Response(status=204)

    server = await polar({("GET", "/v3/webhooks"): handler})

    assert await read(server.session, base_url=server.base_url, **CREDENTIALS) is None


@pytest.mark.asyncio
async def test_activate_succeeds_on_204(polar: Factory) -> None:
    async def handler(request: web.Request) -> web.Response:  # noqa: ARG001 - the request is irrelevant here
        return web.Response(status=204)

    server = await polar({("POST", "/v3/webhooks/activate"): handler})

    await activate(server.session, base_url=server.base_url, **CREDENTIALS)

    assert server.count_of("POST", "/v3/webhooks/activate") == 1


@pytest.mark.asyncio
async def test_delete_targets_the_webhook_id(polar: Factory) -> None:
    async def handler(request: web.Request) -> web.Response:
        assert request.match_info["webhook_id"] == "abdf33"
        return web.Response(status=204)

    server = await polar({("DELETE", "/v3/webhooks/{webhook_id}"): handler})

    await delete(server.session, webhook_id="abdf33", base_url=server.base_url, **CREDENTIALS)

    assert server.count_of("DELETE", "/v3/webhooks/abdf33") == 1


@pytest.mark.asyncio
async def test_bad_client_credentials_raise_authentication_failed(polar: Factory) -> None:
    server = await polar({("GET", "/v3/webhooks"): _status(401)})

    with pytest.raises(AuthenticationFailed):
        await read(server.session, base_url=server.base_url, **CREDENTIALS)


@pytest.mark.asyncio
async def test_a_rejected_client_credential_403_also_raises_authentication_failed(polar: Factory) -> None:
    # Client credentials have no "consent" concept: a 403 here means the credentials themselves were
    # rejected, same as a 401, unlike a user token where 403 means a missing consent instead.
    server = await polar({("GET", "/v3/webhooks"): _status(403)})

    with pytest.raises(AuthenticationFailed):
        await read(server.session, base_url=server.base_url, **CREDENTIALS)


@pytest.mark.asyncio
async def test_a_429_from_a_webhook_endpoint_raises_rate_limited(polar: Factory) -> None:
    # Previously this fell through to UnexpectedResponse: a daemon reading that would fail loudly
    # instead of backing off, on what is actually a routine rate limit.
    server = await polar({("GET", "/v3/webhooks"): _status(429, '{"error": "slow down"}')})

    with pytest.raises(RateLimited, match="slow down"):
        await read(server.session, base_url=server.base_url, **CREDENTIALS)


@pytest.mark.asyncio
async def test_a_5xx_from_a_webhook_endpoint_raises_connection_failed(polar: Factory) -> None:
    # Previously this fell through to UnexpectedResponse too: a transient 500 would have been read
    # as "the API contract changed" instead of "retry later".
    server = await polar({("GET", "/v3/webhooks"): _status(500)})

    with pytest.raises(ConnectionFailed):
        await read(server.session, base_url=server.base_url, **CREDENTIALS)


@pytest.mark.asyncio
async def test_a_genuine_connection_failure_raises_connection_failed() -> None:
    # No status code involved: nothing is listening on this port, so aiohttp raises a real
    # ClientError, exercising the except (aiohttp.ClientError, TimeoutError) branch in _request.
    port = unused_closed_port()
    async with aiohttp.ClientSession() as session:
        with pytest.raises(ConnectionFailed):
            await read(session, base_url=f"http://127.0.0.1:{port}", **CREDENTIALS)


def _status(status: int, body: str = "{}") -> Callable[[web.Request], Awaitable[web.Response]]:
    async def handler(request: web.Request) -> web.Response:  # noqa: ARG001 - the request is irrelevant here
        return web.Response(status=status, text=body, content_type="application/json")

    return handler
