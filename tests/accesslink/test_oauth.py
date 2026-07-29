"""The exchange must never be replayed. Two tests here exist solely to assert that, because the
failure mode is not a bug report — it is every token for the account being deleted."""

from collections.abc import Awaitable, Callable
from urllib.parse import parse_qs, urlparse

import aiohttp
import pytest
from aiohttp import web

from dreeve_polar_connector.accesslink.exceptions import AuthenticationFailed, ConnectionFailed, UnexpectedResponse
from dreeve_polar_connector.accesslink.oauth import AUTHORIZATION_URL, authorization_url, exchange_code
from tests.accesslink.conftest import FakeAccessLink, unused_closed_port

Factory = Callable[[dict[tuple[str, str], object]], Awaitable[FakeAccessLink]]


def test_authorization_url_carries_the_mandatory_parameters() -> None:
    url = authorization_url(client_id="a-client", state="a-state")
    query = parse_qs(urlparse(url).query)

    assert url.startswith(AUTHORIZATION_URL)
    assert query["response_type"] == ["code"]
    assert query["client_id"] == ["a-client"]
    assert query["state"] == ["a-state"]


def test_authorization_url_omits_the_redirect_when_none_is_configured() -> None:
    # Polar then falls back to the client's only registered URL, which is one fewer thing to configure.
    query = parse_qs(urlparse(authorization_url(client_id="a-client", state="a-state")).query)

    assert "redirect_uri" not in query


def test_authorization_url_includes_the_redirect_when_one_is_configured() -> None:
    url = authorization_url(client_id="a-client", state="a-state", redirect_uri="http://nas.local:8080/callback")
    query = parse_qs(urlparse(url).query)

    assert query["redirect_uri"] == ["http://nas.local:8080/callback"]


@pytest.mark.asyncio
async def test_a_successful_exchange_returns_the_token_and_the_user_id(polar: Factory) -> None:
    async def handler(request: web.Request) -> web.Response:
        assert request.headers["Authorization"].startswith("Basic ")
        assert await request.text() == "grant_type=authorization_code&code=a-code"
        return web.json_response(
            {"access_token": "a-token", "token_type": "bearer", "expires_in": 31535999, "x_user_id": 475}
        )

    server = await polar({("POST", "/v2/oauth2/token"): handler})

    token = await exchange_code(
        server.session,
        client_id="a-client",
        client_secret="a-secret",
        code="a-code",
        token_url=f"{server.base_url}/v2/oauth2/token",
    )

    assert token.access_token == "a-token"
    assert token.x_user_id == 475


@pytest.mark.asyncio
async def test_the_redirect_uri_is_sent_when_it_was_sent_to_the_authorize_endpoint(polar: Factory) -> None:
    # Polar requires the two to match, so they are passed together or not at all.
    seen: dict[str, str] = {}

    async def handler(request: web.Request) -> web.Response:
        seen["body"] = await request.text()
        return web.json_response({"access_token": "a-token", "x_user_id": 475})

    server = await polar({("POST", "/v2/oauth2/token"): handler})
    await exchange_code(
        server.session,
        client_id="a-client",
        client_secret="a-secret",
        code="a-code",
        redirect_uri="http://nas.local:8080/callback",
        token_url=f"{server.base_url}/v2/oauth2/token",
    )

    assert "redirect_uri=http%3A%2F%2Fnas.local%3A8080%2Fcallback" in seen["body"]


@pytest.mark.asyncio
async def test_a_rejected_code_raises_authentication_failed(polar: Factory) -> None:
    server = await polar(
        {
            ("POST", "/v2/oauth2/token"): _responding(
                400, '{"error": "invalid_grant", "error_description": "code expired"}'
            )
        }
    )

    with pytest.raises(AuthenticationFailed, match="invalid_grant"):
        await exchange_code(
            server.session,
            client_id="a-client",
            client_secret="a-secret",
            code="a-code",
            token_url=f"{server.base_url}/v2/oauth2/token",
        )


@pytest.mark.asyncio
async def test_a_failed_exchange_is_attempted_exactly_once(polar: Factory) -> None:
    server = await polar({("POST", "/v2/oauth2/token"): _responding(500)})

    with pytest.raises(AuthenticationFailed):
        await exchange_code(
            server.session,
            client_id="a-client",
            client_secret="a-secret",
            code="a-code",
            token_url=f"{server.base_url}/v2/oauth2/token",
        )

    assert server.count_of("POST", "/v2/oauth2/token") == 1


@pytest.mark.asyncio
async def test_a_server_error_is_not_reported_as_a_retryable_failure(polar: Factory) -> None:
    # ConnectionFailed is what tenacity retries elsewhere. The code is spent either way, so the
    # exchange must never raise it.
    server = await polar({("POST", "/v2/oauth2/token"): _responding(503)})

    with pytest.raises(AuthenticationFailed) as raised:
        await exchange_code(
            server.session,
            client_id="a-client",
            client_secret="a-secret",
            code="a-code",
            token_url=f"{server.base_url}/v2/oauth2/token",
        )

    assert not isinstance(raised.value, ConnectionFailed)


@pytest.mark.asyncio
async def test_a_token_response_without_a_token_fails_loudly(polar: Factory) -> None:
    async def handler(request: web.Request) -> web.Response:  # noqa: ARG001 - the request is irrelevant here
        return web.json_response({"token_type": "bearer"})

    server = await polar({("POST", "/v2/oauth2/token"): handler})

    with pytest.raises(UnexpectedResponse):
        await exchange_code(
            server.session,
            client_id="a-client",
            client_secret="a-secret",
            code="a-code",
            token_url=f"{server.base_url}/v2/oauth2/token",
        )


@pytest.mark.asyncio
async def test_a_genuine_network_failure_raises_authentication_failed_not_connection_failed() -> None:
    # No status code involved at all here: nothing is listening on this port, so aiohttp raises a
    # real ClientError. ConnectionFailed is exactly what tenacity retries in client.py, and retrying
    # a spent authorization code is what deletes every token for the account - so this must never be
    # ConnectionFailed, whatever else it is.
    port = unused_closed_port()
    async with aiohttp.ClientSession() as session:
        with pytest.raises(AuthenticationFailed) as raised:
            await exchange_code(
                session,
                client_id="a-client",
                client_secret="a-secret",
                code="a-code",
                token_url=f"http://127.0.0.1:{port}/v2/oauth2/token",
            )

        assert not isinstance(raised.value, ConnectionFailed)


def _responding(status: int, body: str = "{}") -> Callable[[web.Request], Awaitable[web.Response]]:
    async def handler(request: web.Request) -> web.Response:  # noqa: ARG001 - the request is irrelevant here
        return web.Response(status=status, text=body, content_type="application/json")

    return handler
