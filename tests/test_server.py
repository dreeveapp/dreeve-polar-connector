"""Driven through a real aiohttp test client, so route wiring, status codes and content types are all
exercised rather than assumed."""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path

import aiohttp
import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from dreeve_polar_connector.config import Config
from dreeve_polar_connector.server import build_app
from dreeve_polar_connector.status import Status
from dreeve_polar_connector.tokens import TokenStore
from tests.stubs import StubClock

NOW = datetime(2026, 7, 29, 8, 0, tzinfo=UTC)

# The factory's return type, spelled out once so every test signature below stays readable.
ClientFactory = Callable[..., Awaitable["TestClient[web.Request, web.Application]"]]


def _config(tmp_path: Path, **overrides: str) -> Config:
    return Config.from_env(
        {
            "POLAR_CLIENT_ID": "a-client",
            "POLAR_CLIENT_SECRET": "a-secret",
            "WATCH_DIR": str(tmp_path / "watch"),
            "STATE_DIR": str(tmp_path / "state"),
            "POLAR_TOKENS": str(tmp_path / "tokens"),
            **overrides,
        }
    )


@pytest_asyncio.fixture
async def make_client(tmp_path: Path) -> AsyncIterator[ClientFactory]:
    """A factory, so the aiohttp session and the test server outlive the call that creates them.

    Building the client inside an `async with aiohttp.ClientSession()` and returning it would close the
    session before the test ran — harmless for /healthz, but broken the moment a route uses it.
    """
    sessions: list[aiohttp.ClientSession] = []
    clients: list[TestClient[web.Request, web.Application]] = []

    async def build(status: Status, **overrides: str) -> TestClient[web.Request, web.Application]:
        config = _config(tmp_path, **overrides)
        session = aiohttp.ClientSession()
        sessions.append(session)

        app = build_app(
            config=config,
            status=status,
            clock=StubClock(NOW),
            tokens=TokenStore(config.tokens_dir),
            session=session,
            secrets=_Secrets(),
            wake=asyncio.Event(),
        )
        client = TestClient(TestServer(app))
        await client.start_server()
        clients.append(client)

        return client

    yield build

    for client in clients:
        await client.close()
    for session in sessions:
        await session.close()


class _Secrets:
    """Stands in for `logging_.Secrets` so the server tests do not depend on logging setup."""

    def __init__(self) -> None:
        self.added: list[str] = []

    def add(self, value: str | None) -> None:
        if value:
            self.added.append(value)


@pytest.fixture
def status() -> Status:
    return Status(poll_interval=900, started_at=NOW, authorize_url="http://nas.local:8080/authorize")


@pytest.mark.asyncio
async def test_healthz_reports_503_before_anyone_has_authorized(make_client: ClientFactory, status: Status) -> None:
    client = await make_client(status)

    response = await client.get("/healthz")

    assert response.status == 503
    assert (await response.json())["healthy"] is False


@pytest.mark.asyncio
async def test_healthz_reports_200_once_authorized(make_client: ClientFactory, status: Status) -> None:
    status.authorized(475)
    client = await make_client(status)

    response = await client.get("/healthz")

    assert response.status == 200
    assert (await response.json())["healthy"] is True


@pytest.mark.asyncio
async def test_status_returns_the_report_as_json(make_client: ClientFactory, status: Status) -> None:
    status.authorized(475)
    client = await make_client(status)

    response = await client.get("/status")
    payload = await response.json()

    assert response.status == 200
    assert response.content_type == "application/json"
    assert payload["polarUser"] == 475
    assert payload["authorization"] == "ok"


@pytest.mark.asyncio
async def test_status_tells_an_unauthorized_operator_what_to_open(make_client: ClientFactory, status: Status) -> None:
    status.authorization_required("nobody has authorized yet")
    client = await make_client(status)

    payload = await (await client.get("/status")).json()

    assert payload["authorizeUrl"] == "http://nas.local:8080/authorize"


@pytest.mark.asyncio
async def test_an_unknown_path_is_a_json_404(make_client: ClientFactory, status: Status) -> None:
    client = await make_client(status)

    response = await client.get("/wat")

    assert response.status == 404
