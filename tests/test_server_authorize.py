"""The CSRF guard and the single-use code are the two things worth asserting here. A replayed state or a
replayed code both have real consequences — the second one deletes every token for the account."""

import asyncio
import logging
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
from dreeve_polar_connector.server import BASE_URL, OAUTH_STATES, TOKEN_URL, build_local_app
from dreeve_polar_connector.status import AuthorizationState, Status
from dreeve_polar_connector.tokens import Authorization, TokenStore
from tests.stubs import StubClock

NOW = datetime(2026, 7, 29, 8, 0, tzinfo=UTC)


class _FakePolar:
    """A local stand-in for polarremote.com and www.polaraccesslink.com."""

    def __init__(self) -> None:
        self.token_requests = 0
        self.registrations: list[str] = []
        self.token_status = 200
        # Per-code responses and delays, so one flow can be held mid-exchange while another finishes —
        # which is the only way to test that the two cannot interleave.
        self.tokens_by_code: dict[str, tuple[int, str]] = {}
        self.delay_by_code: dict[str, float] = {}
        self.registration_status = 200
        self.registration_body = ""

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_post("/v2/oauth2/token", self._token)
        app.router.add_post("/v3/users", self._register)
        return app

    async def _token(self, request: web.Request) -> web.Response:
        self.token_requests += 1
        code = (await request.post()).get("code", "")
        delay = self.delay_by_code.get(str(code))
        if delay:
            await asyncio.sleep(delay)
        if self.token_status != 200:
            return web.json_response({"error": "invalid_grant"}, status=self.token_status)

        user_id, access_token = self.tokens_by_code.get(str(code), (475, "a-token"))

        return web.json_response({"access_token": access_token, "token_type": "bearer", "x_user_id": user_id})

    async def _register(self, request: web.Request) -> web.Response:
        body = await request.json()
        if self.registration_status != 200:
            return web.Response(status=self.registration_status, text=self.registration_body)
        self.registrations.append(body["member-id"])
        return web.json_response({"polar-user-id": 475, "member-id": body["member-id"]})


class _Secrets:
    def __init__(self) -> None:
        self.added: list[str] = []

    def add(self, value: str | None) -> None:
        if value:
            self.added.append(value)

    def redact(self, text: str) -> str:
        for value in self.added:
            text = text.replace(value, "***")

        return text


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
    built.app = build_local_app(
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
    # Driven through a failed exchange so no authorization lands: with one stored, `_may_authorize`
    # would refuse the replay before `_claim_state` ever ran, and this would assert the guard rather
    # than the single-use state that protects a leaked callback URL.
    harness.polar.token_status = 400
    state = await _authorize_then_state(harness)
    await harness.client.get("/callback", params={"code": "a-code", "state": state})

    response = await harness.client.get("/callback", params={"code": "another-code", "state": state})

    assert response.status == 400
    assert harness.polar.token_requests == 1
    assert harness.tokens.current() is None


@pytest.mark.asyncio
async def test_a_replayed_state_is_refused_once_an_authorization_exists(harness: _Harness) -> None:
    # The same replay after a successful flow, which the guard now refuses ahead of the state check.
    # Both layers reject it; this records which one answers first.
    state = await _authorize_then_state(harness)
    await harness.client.get("/callback", params={"code": "a-code", "state": state})

    response = await harness.client.get("/callback", params={"code": "another-code", "state": state})

    assert response.status == 409
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


async def _store_authorization(harness: _Harness, polar_user_id: int = 475) -> None:
    await harness.tokens.store(
        Authorization(
            polar_user_id=polar_user_id,
            access_token="an-existing-token",
            member_id=f"dreeve-{polar_user_id}",
            authorized_at=NOW,
        )
    )


@pytest.mark.asyncio
async def test_authorize_is_refused_once_an_authorization_exists(harness: _Harness) -> None:
    # Completing the flow replaces the stored token, so an uninvited party could otherwise bind this
    # connector to their own Polar account and take over the sync.
    await _store_authorization(harness)
    harness.status.authorized(475)

    response = await harness.client.get("/authorize", allow_redirects=False)

    assert response.status == 409
    assert "logout" in await response.text()


@pytest.mark.asyncio
async def test_callback_cannot_replace_an_existing_authorization(harness: _Harness) -> None:
    state = await _authorize_then_state(harness)
    await _store_authorization(harness)
    harness.status.authorized(475)

    response = await harness.client.get("/callback", params={"code": "a-code", "state": state})

    assert response.status == 409
    stored = harness.tokens.current()
    assert stored is not None
    assert stored.access_token == "an-existing-token"
    assert harness.polar.token_requests == 0


@pytest.mark.asyncio
async def test_the_root_callback_target_is_guarded_too(harness: _Harness) -> None:
    state = await _authorize_then_state(harness)
    await _store_authorization(harness)
    harness.status.authorized(475)

    response = await harness.client.get("/", params={"code": "a-code", "state": state})

    assert response.status == 409
    assert harness.polar.token_requests == 0


@pytest.mark.asyncio
async def test_authorize_is_open_when_the_token_was_revoked(harness: _Harness) -> None:
    # Documented recovery: /status says "revoked", the operator opens /authorize again.
    await _store_authorization(harness)
    harness.status.authorization_revoked("Polar refused the stored token")

    response = await harness.client.get("/authorize", allow_redirects=False)

    assert response.status == 302


@pytest.mark.asyncio
async def test_authorize_is_open_when_consent_is_required(harness: _Harness) -> None:
    await _store_authorization(harness)
    harness.status.consent_required("the user has not accepted all mandatory consents")

    response = await harness.client.get("/authorize", allow_redirects=False)

    assert response.status == 302


@pytest.mark.asyncio
async def test_a_revoked_authorization_can_be_replaced_through_the_callback(harness: _Harness) -> None:
    await _store_authorization(harness, polar_user_id=999)
    harness.status.authorization_revoked("Polar refused the stored token")
    state = await _authorize_then_state(harness)

    response = await harness.client.get("/callback", params={"code": "a-code", "state": state})

    assert response.status == 200
    stored = harness.tokens.current()
    assert stored is not None
    assert stored.access_token == "a-token"
    assert stored.polar_user_id == 475


@pytest.mark.asyncio
async def test_authorize_is_open_when_nothing_is_stored(harness: _Harness) -> None:
    # The first-run flow, unchanged.
    response = await harness.client.get("/authorize", allow_redirects=False)

    assert response.status == 302


@pytest.mark.asyncio
async def test_replacing_one_polar_user_with_another_is_logged_as_a_warning(
    harness: _Harness, caplog: pytest.LogCaptureFixture
) -> None:
    # Reachable legitimately after a revocation, and it is the one event a takeover would produce, so
    # it must not read like a routine first-time authorization.
    await _store_authorization(harness, polar_user_id=999)
    harness.status.authorization_revoked("Polar refused the stored token")
    state = await _authorize_then_state(harness)

    with caplog.at_level(logging.WARNING):
        response = await harness.client.get("/callback", params={"code": "a-code", "state": state})

    assert response.status == 200
    assert "replaced" in caplog.text
    assert "999" in caplog.text


@pytest.mark.asyncio
async def test_a_first_authorization_is_not_logged_as_a_replacement(
    harness: _Harness, caplog: pytest.LogCaptureFixture
) -> None:
    state = await _authorize_then_state(harness)

    with caplog.at_level(logging.INFO):
        await harness.client.get("/callback", params={"code": "a-code", "state": state})

    assert "replaced" not in caplog.text
    assert "the next sync starts now" in caplog.text


@pytest.mark.asyncio
async def test_reauthorizing_the_same_user_is_not_a_replacement(
    harness: _Harness, caplog: pytest.LogCaptureFixture
) -> None:
    await _store_authorization(harness, polar_user_id=475)
    harness.status.authorization_revoked("Polar refused the stored token")
    state = await _authorize_then_state(harness)

    with caplog.at_level(logging.INFO):
        await harness.client.get("/callback", params={"code": "a-code", "state": state})

    assert "replaced" not in caplog.text


@pytest.mark.asyncio
async def test_concurrent_callbacks_cannot_both_write(harness: _Harness) -> None:
    # The guard is checked once, and two Polar round trips separate that check from the write. Unless the
    # flow is serialised, both callers pass the check and both store, so the second silently replaces the
    # first — the takeover this guard exists to stop, reached by a slower attacker instead of an
    # unguarded route. Whoever gets there first wins; the other must be refused, not queued behind it.
    harness.polar.tokens_by_code = {"first-code": (475, "first-token"), "second-code": (666, "second-token")}
    harness.polar.delay_by_code = {"first-code": 0.3}
    first_state = await _authorize_then_state(harness)
    second_state = await _authorize_then_state(harness)

    first = asyncio.create_task(harness.client.get("/callback", params={"code": "first-code", "state": first_state}))
    await asyncio.sleep(0.05)
    second = await harness.client.get("/callback", params={"code": "second-code", "state": second_state})
    first_response = await first

    # The slow flow holds the lock, so it completes and the one that arrived mid-exchange is refused.
    assert first_response.status == 200
    assert second.status == 409
    stored = harness.tokens.current()
    assert stored is not None
    assert stored.polar_user_id == 475
    assert stored.access_token == "first-token"
    # The refused caller never reached Polar: the guard answered before the exchange, so its code is
    # still unspent and it can retry after a logout.
    assert harness.polar.token_requests == 1


@pytest.mark.asyncio
async def test_a_callback_that_loses_the_race_does_not_overwrite_the_winner(harness: _Harness) -> None:
    # The reverse ordering, to pin that the outcome follows who finishes first rather than which request
    # happened to be slower. Either way exactly one authorization exists at the end.
    harness.polar.tokens_by_code = {"quick-code": (475, "quick-token"), "slow-code": (666, "slow-token")}
    harness.polar.delay_by_code = {"slow-code": 0.3}
    quick_state = await _authorize_then_state(harness)
    slow_state = await _authorize_then_state(harness)

    quick = await harness.client.get("/callback", params={"code": "quick-code", "state": quick_state})
    slow = await harness.client.get("/callback", params={"code": "slow-code", "state": slow_state})

    assert quick.status == 200
    assert slow.status == 409
    stored = harness.tokens.current()
    assert stored is not None
    assert stored.polar_user_id == 475
    assert stored.access_token == "quick-token"


@pytest.mark.asyncio
async def test_polars_error_parameter_is_escaped_on_the_page(harness: _Harness) -> None:
    # Reflected straight from the query string onto a page on this connector's own origin, so markup in
    # it has to arrive as text. The origin serves no cookies, but script here can read /status and drive
    # the OAuth flow from a browser the operator trusts.
    response = await harness.client.get("/callback", params={"error": "<script>alert(1)</script>"})
    body = await response.text()

    assert response.status == 400
    assert "<script>" not in body
    assert "&lt;script&gt;" in body


@pytest.mark.asyncio
async def test_an_attribute_breaking_error_parameter_is_escaped(harness: _Harness) -> None:
    # The payload's text survives as text — that is the point. What must not survive is its structure:
    # no tag to hang the handler on, and no quote to break out of an attribute with.
    response = await harness.client.get("/callback", params={"error": '"><img src=x onerror=alert(1)>'})
    body = await response.text()

    assert "<img" not in body
    assert '">' not in body
    assert "&lt;img" in body
    assert "&quot;&gt;" in body


@pytest.mark.asyncio
async def test_polars_registration_error_is_escaped_on_the_page(harness: _Harness) -> None:
    # The other route outside text reaches the page: up to 200 bytes of Polar's response body.
    harness.polar.registration_status = 400
    harness.polar.registration_body = "<script>alert(document.domain)</script>"
    state = await _authorize_then_state(harness)

    response = await harness.client.get("/callback", params={"code": "a-code", "state": state})
    body = await response.text()

    assert response.status == 400
    assert "<script>" not in body


@pytest.mark.asyncio
async def test_the_landing_page_keeps_its_links(harness: _Harness) -> None:
    # The templates in this module are trusted markup; only outside values are escaped. If escaping ever
    # widens to the whole body, this catches the links turning into visible text.
    response = await harness.client.get("/")
    body = await response.text()

    assert '<a href="/authorize">' in body
    assert '<a href="/status">' in body


@pytest.mark.asyncio
async def test_an_unreadable_token_store_refuses_instead_of_crashing(harness: _Harness) -> None:
    # An unreadable file is not evidence that nobody is authorized, so guessing permissively here would
    # turn a broken volume into a takeover. 503 rather than 500: it names the fix.
    harness.tokens.tokens_path.parent.mkdir(parents=True, exist_ok=True)
    harness.tokens.tokens_path.write_text("{not json", encoding="utf-8")

    authorize = await harness.client.get("/authorize", allow_redirects=False)
    callback = await harness.client.get("/callback", params={"code": "a-code", "state": "any"})

    assert authorize.status == 503
    assert callback.status == 503
    assert "logout" in await authorize.text()
    assert harness.polar.token_requests == 0


@pytest.mark.asyncio
async def test_a_stored_token_refuses_before_the_first_cycle_has_run(harness: _Harness) -> None:
    # Straight after a restart Status is REQUIRED while a token exists, because nothing has tried to use
    # it yet. That combination must refuse: REQUIRED is not a re-authorization window.
    await _store_authorization(harness)

    assert harness.status.authorization_state() is AuthorizationState.REQUIRED

    response = await harness.client.get("/authorize", allow_redirects=False)

    assert response.status == 409
