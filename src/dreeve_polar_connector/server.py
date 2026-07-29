"""The connector's HTTP surface.

Five routes on one `aiohttp.web` application, sharing the sync loop's event loop: `/healthz` and
`/status` for operators and Docker, `/authorize` and `/callback` for the one-time OAuth flow, and
`/webhook` for Polar's deliveries.

Sharing the loop is what lets the webhook handler set the sync loop's wake event directly, with no
cross-thread handoff and no lock anywhere in `status.py`.

State is held in typed `web.AppKey` handles rather than string keys so `mypy --strict` checks every
access.
"""

import asyncio
import logging
import secrets as secrets_module
from datetime import datetime, timedelta
from typing import Protocol

import aiohttp
from aiohttp import web

from dreeve_polar_connector.accesslink import oauth
from dreeve_polar_connector.accesslink._http import ACCESSLINK_BASE_URL
from dreeve_polar_connector.accesslink.client import AccessLinkClient
from dreeve_polar_connector.accesslink.exceptions import AccessLinkError
from dreeve_polar_connector.accesslink.webhooks import (
    EVENT_HEADER,
    MAX_BODY_BYTES,
    PING_EVENT,
    SIGNATURE_HEADER,
    parse_exercise_event,
    signature_matches,
)
from dreeve_polar_connector.config import Config
from dreeve_polar_connector.status import Status
from dreeve_polar_connector.sync import Clock
from dreeve_polar_connector.tokens import Authorization, TokenStore

SHUTDOWN_TIMEOUT_SECONDS = 2.0
# Polar's authorization codes expire in ten minutes, so a state older than that is useless anyway.
STATE_TTL = timedelta(minutes=10)
STATE_BYTES = 32

logger = logging.getLogger(__name__)


class SecretRegistry(Protocol):
    """The sliver of `logging_.Secrets` this module needs, so the server does not depend on log setup."""

    def add(self, value: str | None) -> None: ...


CONFIG = web.AppKey("config", Config)
STATUS = web.AppKey("status", Status)
CLOCK: web.AppKey[Clock] = web.AppKey("clock")
TOKENS = web.AppKey("tokens", TokenStore)
SESSION = web.AppKey("session", aiohttp.ClientSession)
SECRETS: web.AppKey[SecretRegistry] = web.AppKey("secrets")
WAKE = web.AppKey("wake", asyncio.Event)
# state -> when it was issued. In-memory because a single process serves both ends of the redirect.
OAUTH_STATES: web.AppKey[dict[str, datetime]] = web.AppKey("oauth_states")
# Overridable so tests can point the OAuth calls at a local fake instead of Polar. Not set by
# `build_app`, so `app.get(...)` falls back to the real endpoints in production.
TOKEN_URL: web.AppKey[str] = web.AppKey("token_url")
BASE_URL: web.AppKey[str] = web.AppKey("accesslink_base_url")


def build_app(
    *,
    config: Config,
    status: Status,
    clock: Clock,
    tokens: TokenStore,
    session: aiohttp.ClientSession,
    secrets: SecretRegistry,
    wake: asyncio.Event,
) -> web.Application:
    app = web.Application()
    app[CONFIG] = config
    app[STATUS] = status
    app[CLOCK] = clock
    app[TOKENS] = tokens
    app[SESSION] = session
    app[SECRETS] = secrets
    app[WAKE] = wake
    app[OAUTH_STATES] = {}

    app.router.add_get("/healthz", healthz)
    app.router.add_get("/status", status_report)
    app.router.add_get("/authorize", authorize)
    app.router.add_get("/callback", callback)
    app.router.add_post("/webhook", webhook)

    return app


async def healthz(request: web.Request) -> web.Response:
    """Docker's HEALTHCHECK target. 503 while unauthorized, which is a real problem and easy to miss."""
    healthy = request.app[STATUS].is_healthy(request.app[CLOCK].now())

    return web.json_response({"healthy": healthy}, status=200 if healthy else 503)


async def status_report(request: web.Request) -> web.Response:
    return web.json_response(request.app[STATUS].to_dict(request.app[CLOCK].now()))


async def authorize(request: web.Request) -> web.Response:
    """Sends the browser to Polar's consent screen, remembering a `state` to check on the way back."""
    config = request.app[CONFIG]
    state = secrets_module.token_urlsafe(STATE_BYTES)

    issued = request.app[OAUTH_STATES]
    now = request.app[CLOCK].now()
    # Housekeeping while we are here; nothing else ever prunes this dict.
    for old, at in list(issued.items()):
        if now - at > STATE_TTL:
            del issued[old]
    issued[state] = now

    # Raised rather than returned: aiohttp deprecates returning an HTTPException as a response.
    raise web.HTTPFound(
        oauth.authorization_url(client_id=config.client_id, state=state, redirect_uri=config.redirect_uri)
    )


async def callback(request: web.Request) -> web.Response:
    """Completes the flow. The authorization code is used exactly once, whatever the outcome."""
    error = request.query.get("error")
    if error:
        return _page(400, "Polar declined", f"Polar returned <code>{error}</code>. Nothing has been changed.")

    state = request.query.get("state")
    if not _claim_state(request, state):
        return _page(
            400,
            "That link is not valid",
            "The <code>state</code> parameter is missing, unknown or expired. "
            'Start again from <a href="/authorize">/authorize</a>.',
        )

    code = request.query.get("code")
    if not code:
        return _page(400, "No authorization code", 'Start again from <a href="/authorize">/authorize</a>.')

    config = request.app[CONFIG]
    session = request.app[SESSION]
    token_url = request.app.get(TOKEN_URL, oauth.EXCHANGE_URL)
    base_url = request.app.get(BASE_URL, ACCESSLINK_BASE_URL)

    try:
        token = await oauth.exchange_code(
            session,
            client_id=config.client_id,
            client_secret=config.client_secret,
            code=code,
            redirect_uri=config.redirect_uri,
            token_url=token_url,
        )
    except AccessLinkError as exception:
        logger.error("The token exchange failed: %s", exception)
        return _page(
            400,
            "Polar would not issue a token",
            "The authorization code has been used and cannot be retried. "
            'Start again from <a href="/authorize">/authorize</a>.',
        )

    # Registered for redaction before anything else can log it.
    request.app[SECRETS].add(token.access_token)

    member_id = f"dreeve-{token.x_user_id}"
    try:
        await AccessLinkClient(session, token.access_token, base_url=base_url).register_user(member_id)
    except AccessLinkError as exception:
        logger.error("Polar issued a token but would not register the user: %s", exception)
        return _page(400, "Polar would not register this user", str(exception))

    await request.app[TOKENS].store(
        Authorization(
            polar_user_id=token.x_user_id,
            access_token=token.access_token,
            member_id=member_id,
            authorized_at=request.app[CLOCK].now(),
        )
    )
    request.app[STATUS].authorized(token.x_user_id)
    # The loop is waiting on this; without it the first sync would be up to POLL_INTERVAL away.
    request.app[WAKE].set()
    logger.info("Authorized Polar user %s; the next sync starts now", token.x_user_id)

    return _page(
        200,
        "Connected",
        "This connector is now authorized and will start syncing immediately. You can close this page.",
    )


def _claim_state(request: web.Request, state: str | None) -> bool:
    """Single-use: a state is removed as it is checked, so a replayed callback fails."""
    if not state:
        return False

    issued = request.app[OAUTH_STATES]
    at = issued.pop(state, None)
    if at is None:
        return False

    return request.app[CLOCK].now() - at <= STATE_TTL


def _page(status: int, heading: str, body: str) -> web.Response:
    """Minimal self-contained HTML: a human is reading this one, not a machine."""
    return web.Response(
        status=status,
        content_type="text/html",
        text=(
            "<!doctype html><meta charset=utf-8>"
            "<title>Dreeve Polar Connector</title>"
            "<style>body{font-family:system-ui,sans-serif;max-width:34rem;margin:4rem auto;padding:0 1rem;"
            "line-height:1.5}code{background:#eee;padding:.1rem .3rem;border-radius:.2rem}</style>"
            f"<h1>{heading}</h1><p>{body}</p>"
        ),
    )


async def webhook(request: web.Request) -> web.Response:
    """Verify, record, wake, return. Nothing else happens here, deliberately.

    No download: Polar counts a slow or non-200 answer as a delivery failure, and seven days of failures
    deactivates the webhook. No ledger write either — `sync.py` stays its single writer, which is what
    keeps the atomic flush meaningful.
    """
    if (request.content_length or 0) > MAX_BODY_BYTES:
        return web.json_response({"error": "body too large"}, status=413)

    body = await request.content.read(MAX_BODY_BYTES + 1)
    if len(body) > MAX_BODY_BYTES:
        return web.json_response({"error": "body too large"}, status=413)

    if request.headers.get(EVENT_HEADER, "").upper() == PING_EVENT:
        # Unverifiable by construction: this arrives during `create`, before Polar discloses the key.
        logger.info("Answered Polar's webhook ping")
        return web.json_response({"ok": True})

    stored = request.app[TOKENS].webhook()
    if stored is None:
        logger.warning("Ignoring a webhook delivery: no signing key is stored, so nothing can be verified")
        return web.json_response({"error": "unverified"}, status=401)

    signature = request.headers.get(SIGNATURE_HEADER, "")
    if not signature_matches(body, signature, stored.signature_secret_key):
        logger.warning("Rejected a webhook delivery whose signature did not match")
        return web.json_response({"error": "bad signature"}, status=401)

    event = parse_exercise_event(body)
    if event is None:
        # Verified but not an exercise event. 200 keeps Polar's delivery record clean.
        return web.json_response({"ok": True})

    authorization = request.app[TOKENS].current()
    if authorization is None or event.user_id != authorization.polar_user_id:
        logger.info("Ignoring a webhook event for Polar user %s, which is not the authorized one", event.user_id)
        return web.json_response({"ok": True})

    request.app[STATUS].webhook_event_received(request.app[CLOCK].now())
    request.app[WAKE].set()
    logger.info("Webhook reported exercise %s; syncing now instead of at the next interval", event.entity_id)

    return web.json_response({"ok": True})


class HttpServer:
    """Runs the application on a port, and shuts it down without leaving the loop hanging."""

    def __init__(self, address: str, app: web.Application) -> None:
        self._app = app
        host, _, port = address.rpartition(":")
        self._host = host
        self._port = int(port)
        self._runner: web.AppRunner | None = None

    async def start(self) -> None:
        self._runner = web.AppRunner(self._app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self._host, self._port)
        await site.start()
        logger.info("Serving /healthz, /status, /authorize, /callback and /webhook on %s:%d", self._host, self.port)

    @property
    def port(self) -> int:
        """The port actually bound, which differs from the configured one when that was 0."""
        if self._runner is not None and self._runner.addresses:
            bound = self._runner.addresses[0]
            if isinstance(bound, tuple):
                return int(bound[1])

        return self._port

    async def stop(self) -> None:
        if self._runner is None:
            return

        await asyncio.wait_for(self._runner.cleanup(), timeout=SHUTDOWN_TIMEOUT_SECONDS)
        self._runner = None
