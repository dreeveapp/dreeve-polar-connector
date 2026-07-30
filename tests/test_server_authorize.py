"""The CSRF guard and the single-use code are the two things worth asserting here. A replayed state or a
replayed code both have real consequences — the second one deletes every token for the account."""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import aiohttp
import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from dreeve_polar_connector.config import Config
from dreeve_polar_connector.server import BASE_URL, OAUTH_STATES, TOKEN_URL, build_app
from dreeve_polar_connector.status import AuthorizationState, Status
from dreeve_polar_connector.tokens import TokenStore
from tests.stubs import StubClock

NOW = datetime(2026, 7, 29, 8, 0, tzinfo=UTC)


class _FakePolar:
    """A local stand-in for polarremote.com and www.polaraccesslink.com."""

    def __init__(self) -> None:
        self.token_requests = 0
        self.registrations: list[str] = []
        self.token_status = 200

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_post("/v2/oauth2/token", self._token)
        app.router.add_post("/v3/users", self._register)
        return app

    async def _token(self, request: web.Request) -> web.Response:  # noqa: ARG002 - body is irrelevant
        self.token_requests += 1
        if self.token_status != 200:
            return web.json_response({"error": "invalid_grant"}, status=self.token_status)
        return web.json_response({"access_token": "a-token", "token_type": "bearer", "x_user_id": 475})

    async def _register(self, request: web.Request) -> web.Response:
        body = await request.json()
        self.registrations.append(body["member-id"])
        return web.json_response({"polar-user-id": 475, "member-id": body["member-id"]})


class _Secrets:
    def __init__(self) -> None:
        self.added: list[str] = []

    def add(self, value: str | None) -> None:
        if value:
            self.added.append(value)


class _Harness:
    def __init__(self) -> None:
        self.client: TestClient[web.Request, web.Application]
        self.status: Status
        self.tokens: TokenStore
        self.secrets: _Secrets
        self.wake: asyncio.Event
        self.polar: _FakePolar
        self.app: web.Application
        self._sessions: list[aiohttp.ClientSession] = []
        self._servers: list[TestClient[web.Request, web.Application]] = []

    async def close(self) -> None:
        for client in self._servers:
            await client.close()
        for session in self._sessions:
            await session.close()


@pytest_asyncio.fixture
async def harness(tmp_path: Path) -> AsyncIterator[_Harness]:
    """Wires the connector's app against a local fake Polar, so no network is involved."""
    built = _Harness()
    built.polar = _FakePolar()

    polar_client = TestClient(TestServer(built.polar.app()))
    await polar_client.start_server()
    built._servers.append(polar_client)
    polar_base = f"http://127.0.0.1:{polar_client.port}"

    config = Config.from_env(
        {
            "POLAR_CLIENT_ID": "a-client",
            "POLAR_CLIENT_SECRET": "a-secret",
            "WATCH_DIR": str(tmp_path / "watch"),
            "STATE_DIR": str(tmp_path / "state"),
            "POLAR_TOKENS": str(tmp_path / "tokens"),
            "PUBLIC_URL": "http://nas.local:8080",
        }
    )

    session = aiohttp.ClientSession()
    built._sessions.append(session)

    built.status = Status(config.poll_interval, NOW, authorize_url="http://nas.local:8080/authorize")
    built.tokens = TokenStore(config.tokens_dir)
    built.secrets = _Secrets()
    built.wake = asyncio.Event()
    built.app = build_app(
        config=config,
        status=built.status,
        clock=StubClock(NOW),
        tokens=built.tokens,
        session=session,
        secrets=built.secrets,
        wake=built.wake,
    )
    # Point the OAuth calls at the fake rather than at Polar.
    built.app[TOKEN_URL] = f"{polar_base}/v2/oauth2/token"
    built.app[BASE_URL] = polar_base

    connector_client = TestClient(TestServer(built.app))
    await connector_client.start_server()
    built._servers.append(connector_client)
    built.client = connector_client

    yield built

    await built.close()


async def _authorize_then_state(harness: _Harness) -> str:
    response = await harness.client.get("/authorize", allow_redirects=False)
    location = response.headers["Location"]

    return parse_qs(urlparse(location).query)["state"][0]


@pytest.mark.asyncio
async def test_authorize_redirects_to_polar(harness: _Harness) -> None:
    response = await harness.client.get("/authorize", allow_redirects=False)

    assert response.status == 302
    assert response.headers["Location"].startswith("https://flow.polar.com/oauth2/authorization")


@pytest.mark.asyncio
async def test_authorize_includes_the_configured_redirect_uri(harness: _Harness) -> None:
    response = await harness.client.get("/authorize", allow_redirects=False)
    query = parse_qs(urlparse(response.headers["Location"]).query)

    assert query["redirect_uri"] == ["http://nas.local:8080/callback"]


@pytest.mark.asyncio
async def test_authorize_issues_a_fresh_state_each_time(harness: _Harness) -> None:
    first = await _authorize_then_state(harness)
    second = await _authorize_then_state(harness)

    assert first != second


@pytest.mark.asyncio
async def test_a_completed_callback_stores_the_token_and_registers_the_user(harness: _Harness) -> None:
    state = await _authorize_then_state(harness)

    response = await harness.client.get("/callback", params={"code": "a-code", "state": state})

    assert response.status == 200
    assert "text/html" in response.content_type
    stored = harness.tokens.current()
    assert stored is not None
    assert stored.access_token == "a-token"
    assert stored.polar_user_id == 475
    assert harness.polar.registrations == ["dreeve-475"]


@pytest.mark.asyncio
async def test_a_completed_callback_reports_success_and_wakes_the_loop(harness: _Harness) -> None:
    state = await _authorize_then_state(harness)

    await harness.client.get("/callback", params={"code": "a-code", "state": state})

    assert harness.status.to_dict(NOW)["authorization"] == AuthorizationState.OK
    assert harness.wake.is_set()


@pytest.mark.asyncio
async def test_the_new_token_is_registered_for_redaction(harness: _Harness) -> None:
    state = await _authorize_then_state(harness)

    await harness.client.get("/callback", params={"code": "a-code", "state": state})

    assert "a-token" in harness.secrets.added


@pytest.mark.asyncio
async def test_a_callback_without_a_state_is_rejected(harness: _Harness) -> None:
    response = await harness.client.get("/callback", params={"code": "a-code"})

    assert response.status == 400
    assert harness.polar.token_requests == 0


@pytest.mark.asyncio
async def test_a_callback_with_an_unknown_state_is_rejected(harness: _Harness) -> None:
    response = await harness.client.get("/callback", params={"code": "a-code", "state": "not-one-we-issued"})

    assert response.status == 400
    assert harness.polar.token_requests == 0


@pytest.mark.asyncio
async def test_a_state_cannot_be_reused(harness: _Harness) -> None:
    state = await _authorize_then_state(harness)
    await harness.client.get("/callback", params={"code": "a-code", "state": state})

    response = await harness.client.get("/callback", params={"code": "another-code", "state": state})

    assert response.status == 400
    assert harness.polar.token_requests == 1


@pytest.mark.asyncio
async def test_an_expired_state_is_rejected(harness: _Harness) -> None:
    state = await _authorize_then_state(harness)
    harness.app[OAUTH_STATES][state] = NOW - timedelta(hours=1)

    response = await harness.client.get("/callback", params={"code": "a-code", "state": state})

    assert response.status == 400
    assert harness.polar.token_requests == 0


@pytest.mark.asyncio
async def test_a_callback_carrying_polars_error_is_shown_without_an_exchange(harness: _Harness) -> None:
    response = await harness.client.get("/callback", params={"error": "access_denied"})

    assert response.status == 400
    assert "access_denied" in await response.text()
    assert harness.polar.token_requests == 0


@pytest.mark.asyncio
async def test_a_callback_without_a_code_is_rejected(harness: _Harness) -> None:
    state = await _authorize_then_state(harness)

    response = await harness.client.get("/callback", params={"state": state})

    assert response.status == 400
    assert harness.polar.token_requests == 0


@pytest.mark.asyncio
async def test_a_rejected_exchange_is_reported_and_not_replayed(harness: _Harness) -> None:
    # The code is spent whatever happens, so the page has to tell the user to start again.
    harness.polar.token_status = 400
    state = await _authorize_then_state(harness)

    response = await harness.client.get("/callback", params={"code": "a-code", "state": state})

    assert response.status == 400
    assert "/authorize" in await response.text()
    assert harness.polar.token_requests == 1
    assert harness.tokens.current() is None


@pytest.mark.asyncio
async def test_the_root_path_completes_a_callback_delivered_there(harness: _Harness) -> None:
    # Polar delivers here when the client's registered redirect URL is a bare origin, which its
    # authorization endpoint accepts in cases where it rejects the same origin plus '/callback'.
    state = await _authorize_then_state(harness)

    response = await harness.client.get("/", params={"code": "a-code", "state": state})

    assert response.status == 200
    stored = harness.tokens.current()
    assert stored is not None
    assert stored.access_token == "a-token"
    assert harness.polar.registrations == ["dreeve-475"]


@pytest.mark.asyncio
async def test_the_root_path_rejects_an_unknown_state_like_the_callback_does(harness: _Harness) -> None:
    response = await harness.client.get("/", params={"code": "a-code", "state": "never-issued"})

    assert response.status == 400
    assert harness.polar.token_requests == 0


@pytest.mark.asyncio
async def test_a_plain_visit_to_the_root_path_is_a_landing_page(harness: _Harness) -> None:
    # Without OAuth parameters nothing is claimed or spent, so a stray visit cannot break a flow in
    # progress or render an error page at the connector's front door.
    response = await harness.client.get("/")

    assert response.status == 200
    assert "/authorize" in await response.text()
    assert harness.polar.token_requests == 0
