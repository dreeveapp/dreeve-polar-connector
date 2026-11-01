"""The Dreeve client is one POST, so these tests are about what each answer means: what is worth trying
again next cycle, what needs a human, and that the API key never leaks into a message."""

import json
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import date
from typing import Protocol

import aiohttp
import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestServer

from dreeve_polar_connector.dreeve import DreeveClient, DreeveRejected, DreeveUnreachable
from tests.accesslink.conftest import unused_closed_port

API_KEY = "drv_secret-key-value"
ON = date(2026, 10, 30)

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


class Start(Protocol):
    def __call__(self, handler: Handler, *, base_path: str = "") -> Awaitable[DreeveClient]: ...


@pytest_asyncio.fixture
async def dreeve() -> AsyncIterator[Start]:
    """Yields a factory: pass the handler for the weights endpoint, get a client pointed at it.

    `base_path` is appended to the server's origin in the client's base URL, as a DREEVE_URL with a
    path (or a trailing slash) would be.
    """
    servers: list[TestServer] = []
    sessions: list[aiohttp.ClientSession] = []

    async def start(handler: Handler, *, base_path: str = "") -> DreeveClient:
        app = web.Application()
        app.router.add_route("*", "/api/v1/athlete/weights", handler)
        server = TestServer(app)
        await server.start_server()
        servers.append(server)
        session = aiohttp.ClientSession()
        sessions.append(session)

        return DreeveClient(session, f"http://127.0.0.1:{server.port}{base_path}", API_KEY)

    yield start

    for session in sessions:
        await session.close()
    for server in servers:
        await server.close()


def _answering(status: int, payload: object | None = None) -> Handler:
    async def handler(request: web.Request) -> web.Response:  # noqa: ARG001 - the request is irrelevant here
        if payload is None:
            return web.Response(status=status)
        return web.json_response(payload, status=status)

    return handler


@pytest.mark.asyncio
async def test_a_weight_is_posted_as_dreeve_expects_it(dreeve: Start) -> None:
    seen: dict[str, object] = {}

    async def handler(request: web.Request) -> web.Response:
        seen["method"] = request.method
        seen["authorization"] = request.headers.get("Authorization")
        seen["content_type"] = request.content_type
        seen["body"] = json.loads(await request.read())
        return web.json_response({"on": "2026-10-30", "weight": 80.4})

    client = await dreeve(handler)
    await client.record_weight(ON, 80.4)

    assert seen == {
        "method": "POST",
        "authorization": f"Bearer {API_KEY}",
        "content_type": "application/json",
        "body": {"weight": 80.4, "on": "2026-10-30"},
    }


@pytest.mark.asyncio
async def test_a_rejected_key_says_which_key_belongs_there(dreeve: Start) -> None:
    client = await dreeve(_answering(401, {"error": "unauthorized", "message": "Invalid API key."}))

    with pytest.raises(DreeveRejected, match="DREEVE_API_KEY") as raised:
        await client.record_weight(ON, 80.4)

    assert raised.value.status == 401
    assert "drv_" in str(raised.value)


@pytest.mark.asyncio
async def test_a_refused_weight_carries_dreeves_own_message(dreeve: Start) -> None:
    client = await dreeve(_answering(400, {"error": "bad_request", "message": '"weight" must be a positive number.'}))

    with pytest.raises(DreeveRejected, match="must be a positive number") as raised:
        await client.record_weight(ON, 80.4)

    assert raised.value.status == 400


@pytest.mark.asyncio
async def test_a_missing_endpoint_points_at_the_url_and_the_dreeve_version(dreeve: Start) -> None:
    # Aimed at a path that does not exist, as a DREEVE_URL with a stray path would be.
    client = await dreeve(_answering(404), base_path="/dreeve")

    with pytest.raises(DreeveRejected, match="DREEVE_URL") as raised:
        await client.record_weight(ON, 80.4)

    assert raised.value.status == 404
    assert "v5.3.3" in str(raised.value)


@pytest.mark.asyncio
async def test_a_redirect_is_refused_rather_than_followed(dreeve: Start) -> None:
    # Following it would turn the POST into a GET of the weight list, which answers 200: a push that
    # looks successful and records nothing.
    async def handler(request: web.Request) -> web.Response:  # noqa: ARG001 - the request is irrelevant here
        raise web.HTTPFound("https://dreeve.example/api/v1/athlete/weights")

    client = await dreeve(handler)

    with pytest.raises(DreeveRejected, match=re.escape("https://dreeve.example/api/v1/athlete/weights")) as raised:
        await client.record_weight(ON, 80.4)

    assert raised.value.status == 302


@pytest.mark.asyncio
async def test_a_server_error_is_worth_trying_again(dreeve: Start) -> None:
    client = await dreeve(_answering(503))

    with pytest.raises(DreeveUnreachable, match="503"):
        await client.record_weight(ON, 80.4)


@pytest.mark.asyncio
async def test_nothing_listening_is_worth_trying_again() -> None:
    async with aiohttp.ClientSession() as session:
        client = DreeveClient(session, f"http://127.0.0.1:{unused_closed_port()}", API_KEY)

        with pytest.raises(DreeveUnreachable, match="Could not reach Dreeve"):
            await client.record_weight(ON, 80.4)


@pytest.mark.asyncio
async def test_a_trailing_slash_on_the_base_url_does_not_double_up(dreeve: Start) -> None:
    client = await dreeve(_answering(200, {"on": "2026-10-30", "weight": 80.4}), base_path="/")

    await client.record_weight(ON, 80.4)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 401, 404, 302, 503])
async def test_the_api_key_never_appears_in_an_error(dreeve: Start, status: int) -> None:
    async def handler(request: web.Request) -> web.Response:
        # A server that echoes the Authorization header back must not get the key into the message.
        if status == 302:
            raise web.HTTPFound("https://dreeve.example/")
        return web.json_response({"message": request.headers.get("Authorization", "")}, status=status)

    client = await dreeve(handler)

    with pytest.raises((DreeveRejected, DreeveUnreachable)) as raised:
        await client.record_weight(ON, 80.4)

    assert API_KEY not in str(raised.value)
