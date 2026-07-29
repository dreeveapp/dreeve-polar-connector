"""A real local AccessLink stand-in.

Mocking the transport would let a wrong assumption about gzip or headers survive all the way to
production. A real server on a real port costs milliseconds and tests the actual client.
"""

import socket
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field

import aiohttp
import pytest_asyncio
from aiohttp import web

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


def unused_closed_port() -> int:
    """A TCP port guaranteed to have nothing listening on it, for forcing a genuine
    `aiohttp.ClientError` rather than mocking one. Binding to port 0 asks the OS for a free port;
    closing the socket immediately hands it back without anything ever accepting a connection on it.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@dataclass
class FakeAccessLink:
    """Routes registered per test, plus a log of what was actually requested."""

    base_url: str
    session: aiohttp.ClientSession
    requests: list[tuple[str, str]] = field(default_factory=list)

    def count_of(self, method: str, path: str) -> int:
        return self.requests.count((method, path))


@pytest_asyncio.fixture
async def polar() -> AsyncIterator[Callable[[dict[tuple[str, str], Handler]], Awaitable[FakeAccessLink]]]:
    """Yields a factory: pass a {(method, path): handler} mapping and get a live server plus session."""
    runners: list[web.AppRunner] = []
    sessions: list[aiohttp.ClientSession] = []
    log: list[tuple[str, str]] = []

    async def start(routes: dict[tuple[str, str], Handler]) -> FakeAccessLink:
        app = web.Application()

        @web.middleware
        async def record(request: web.Request, handler: Handler) -> web.StreamResponse:
            log.append((request.method, request.path))
            return await handler(request)

        app.middlewares.append(record)
        for (method, path), handler in routes.items():
            app.router.add_route(method, path, handler)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        runners.append(runner)

        port = runner.addresses[0][1]
        session = aiohttp.ClientSession()
        sessions.append(session)

        return FakeAccessLink(base_url=f"http://127.0.0.1:{port}", session=session, requests=log)

    yield start

    for session in sessions:
        await session.close()
    for runner in runners:
        await runner.cleanup()
