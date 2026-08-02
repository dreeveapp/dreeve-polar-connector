"""The connector's HTTP surface, split across two listeners by the trust each route needs.

`build_local_app` serves the operator: `/healthz` and `/status` for operators and Docker, `/authorize`
and `/callback` for the OAuth flow, and `/` as a landing page that doubles as a second callback target.
Its listener belongs on a LAN or on loopback. Publishing it lets anyone bind this connector to their own
Polar account, because completing the OAuth flow replaces the stored authorization.

`build_webhook_app` serves `/webhook` and nothing else. That route is the only one safe to publish: it
is HMAC-verified, size-capped, and its sole side effect is waking the sync loop. Exercise files are
fetched later from Polar's API with the stored token, so a delivery body is a notification and never a
payload.

Both share the sync loop's event loop, which is what lets the webhook handler set the sync loop's wake
event directly, with no cross-thread handoff and no lock anywhere in `status.py`.

State is held in typed `web.AppKey` handles rather than string keys so `mypy --strict` checks every
access.
"""

import asyncio
import html
import json
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
from dreeve_polar_connector.status import AuthorizationState, Status
from dreeve_polar_connector.sync import Clock
from dreeve_polar_connector.tokens import Authorization, CorruptTokenStore, StoredWebhook, TokenStore

SHUTDOWN_TIMEOUT_SECONDS = 2.0
# Polar's authorization codes expire in ten minutes, so a state older than that is useless anyway.
STATE_TTL = timedelta(minutes=10)
STATE_BYTES = 32
# The two states whose documented recovery is to open /authorize again. Every other state with a stored
# token means the connector is working, and a fresh authorization would only replace a good token.
#
# Both latch for the life of the process (`loop.py`, `_authorization_was_rejected` / `_consent_missing`),
# so a single Polar 401 or 403 — including a spurious one — reopens this window until a restart. Left
# unbounded on purpose: these routes are only on the local listener, so the window is reachable from the
# LAN and nowhere else, and time-boxing it would mean picking a bound with no data behind it. Do not add
# either state here for a listener that is published.
REAUTHORIZABLE = frozenset({AuthorizationState.REVOKED, AuthorizationState.CONSENT_REQUIRED})
# Refusals are logged at this interval after the first. An unverifiable delivery says the same thing
# every time, and the exact count lives in /status.
REJECTED_DELIVERY_LOG_INTERVAL = 100

logger = logging.getLogger(__name__)


class SecretRegistry(Protocol):
    """The sliver of `logging_.Secrets` this module needs, so the server does not depend on log setup."""

    def add(self, value: str | None) -> None: ...

    def redact(self, text: str) -> str: ...


CONFIG = web.AppKey("config", Config)
STATUS = web.AppKey("status", Status)
CLOCK: web.AppKey[Clock] = web.AppKey("clock")
TOKENS = web.AppKey("tokens", TokenStore)
SESSION = web.AppKey("session", aiohttp.ClientSession)
SECRETS: web.AppKey[SecretRegistry] = web.AppKey("secrets")
WAKE = web.AppKey("wake", asyncio.Event)
# Serialises `/callback`, so the re-authorization guard cannot be raced by two concurrent flows.
AUTHORIZING: web.AppKey[asyncio.Lock] = web.AppKey("authorizing")
# state -> when it was issued. In-memory because a single process serves both ends of the redirect.
OAUTH_STATES: web.AppKey[dict[str, datetime]] = web.AppKey("oauth_states")
# Overridable so tests can point the OAuth calls at a local fake instead of Polar. Not set by
# `build_local_app`, so `app.get(...)` falls back to the real endpoints in production.
TOKEN_URL: web.AppKey[str] = web.AppKey("token_url")
BASE_URL: web.AppKey[str] = web.AppKey("accesslink_base_url")


def build_local_app(
    *,
    config: Config,
    status: Status,
    clock: Clock,
    tokens: TokenStore,
    session: aiohttp.ClientSession,
    secrets: SecretRegistry,
    wake: asyncio.Event,
) -> web.Application:
    """The operator's routes. Reachable from a browser on the LAN, never published to the internet."""
    app = web.Application()
    app[CONFIG] = config
    app[STATUS] = status
    app[CLOCK] = clock
    app[TOKENS] = tokens
    app[SESSION] = session
    app[SECRETS] = secrets
    app[WAKE] = wake
    app[OAUTH_STATES] = {}
    app[AUTHORIZING] = asyncio.Lock()

    app.router.add_get("/", root)
    app.router.add_get("/healthz", healthz)
    app.router.add_get("/status", status_report)
    app.router.add_get("/authorize", authorize)
    app.router.add_get("/callback", callback)

    return app


def build_webhook_app(
    *,
    status: Status,
    clock: Clock,
    tokens: TokenStore,
    wake: asyncio.Event,
) -> web.Application:
    """The only application an operator ever publishes, so it holds only what `webhook` uses.

    Narrower than the local application on purpose — no `Config`, no `ClientSession`, no secret registry
    — but do not mistake that for isolation from credentials: `tokens` reads both the access token and
    the webhook signing key, and `status` carries the Polar user id and Polar's verbatim error text. The
    guarantee on this listener is the route list, nothing more. A route added here is published to the
    internet and can reach everything above, so adding one is a security decision.
    """
    app = web.Application()
    app[STATUS] = status
    app[CLOCK] = clock
    app[TOKENS] = tokens
    app[WAKE] = wake

    app.router.add_post("/webhook", webhook)

    return app


async def root(request: web.Request) -> web.Response:
    """A landing page that doubles as a second callback target.

    Not every Polar client can register `<origin>/callback`: the admin page accepts a path, but the
    authorization endpoint has been observed rejecting the resulting URL while accepting the bare
    origin - at which point Polar delivers `code` and `state` here instead. Answering both paths costs
    one route and turns that into a working browser flow rather than a dead end.

    Only a request carrying OAuth parameters is treated as a callback, so an ordinary visit does not
    consume a state or render an error page.
    """
    if request.query.keys() & {"code", "state", "error"}:
        return await callback(request)

    return _page(
        200,
        "Dreeve Polar Connector",
        'Start at <a href="/authorize">/authorize</a> to connect a Polar account, '
        'or read <a href="/status">/status</a> to see what this connector is doing.',
    )


async def healthz(request: web.Request) -> web.Response:
    """Docker's HEALTHCHECK target. 503 while unauthorized, which is a real problem and easy to miss."""
    healthy = request.app[STATUS].is_healthy(request.app[CLOCK].now())

    return web.json_response({"healthy": healthy}, status=200 if healthy else 503)


async def status_report(request: web.Request) -> web.Response:
    """Redacted on the way out, because this payload carries Polar's verbatim error text.

    `logging_.RedactingFilter` is a choke point for logs only, and `Status` records up to 200 bytes of
    whatever Polar answered with — which is exactly the kind of place a token ends up. Redacting the
    serialised payload rather than named fields means a field added to `Status` later is covered without
    anyone remembering to come back here.
    """
    payload = request.app[STATUS].to_dict(request.app[CLOCK].now())

    return web.Response(
        text=request.app[SECRETS].redact(json.dumps(payload)),
        content_type="application/json",
    )


def _refuse_authorization(request: web.Request) -> web.Response | None:
    """The page to answer with instead of authorizing, or None when the flow may go ahead.

    `TokenStore.store` replaces rather than merges, so anyone who can complete this flow becomes the
    connector's authorized user and the operator's sync stops. Neither route can authenticate its
    caller — there is no session and no credential to check — so the rule is about state instead: a
    connector that already has a usable authorization does not need another one.

    A store that cannot be read is its own answer rather than an exception, which would otherwise reach
    the client as a 500. It refuses: an unreadable file is not evidence that nobody is authorized, and
    guessing in the permissive direction is how a corrupt volume turns into a takeover.
    """
    try:
        current = request.app[TOKENS].current()
    except CorruptTokenStore as exception:
        logger.error("Cannot tell whether this connector is already authorized: %s", exception)

        return _page(
            503,
            "The stored authorization cannot be read",
            "Authorizing is refused until this is fixed, because an unreadable file is not proof that "
            "nobody is connected. Run <code>docker compose run --rm polar-connector logout</code> to "
            "clear it, restart the container, then authorize again.",
        )

    if current is None:
        return None
    if request.app[STATUS].authorization_state() in REAUTHORIZABLE:
        return None

    return _already_connected()


def _already_connected() -> web.Response:
    # The restart is not optional: `SyncLoop` keeps the `Sync` it built, so a running container goes on
    # using the token it already holds even after `logout` removes the file.
    return _page(
        409,
        "Already connected",
        "This connector is already authorized. To connect a different Polar account, run "
        "<code>docker compose run --rm polar-connector logout</code>, restart the container, then open "
        '<a href="/authorize">/authorize</a> again.',
    )


async def authorize(request: web.Request) -> web.Response:
    """Sends the browser to Polar's consent screen, remembering a `state` to check on the way back."""
    refusal = _refuse_authorization(request)
    if refusal is not None:
        return refusal

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
    """Completes the flow, one caller at a time. The authorization code is used exactly once."""
    # Held across the whole flow rather than around the check alone. Two Polar round trips separate the
    # check from the write, and checking without the lock let a callback that started while nobody was
    # authorized complete afterwards and replace the authorization that landed in between — the takeover
    # this guard exists to stop, just with a slower attacker. Serialising costs nothing here: a
    # single-user connector has one operator, and only one browser is ever mid-flow.
    async with request.app[AUTHORIZING]:
        return await _complete_callback(request)


async def _complete_callback(request: web.Request) -> web.Response:
    """The flow itself. Runs with `AUTHORIZING` held, so the state it checks cannot change under it."""
    refusal = _refuse_authorization(request)
    if refusal is not None:
        return refusal

    error = request.query.get("error")
    if error:
        return _page(
            400,
            "Polar declined",
            "Polar returned <code>{error}</code>. Nothing has been changed.",
            error=error,
        )

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
        return _page(400, "Polar would not register this user", "{detail}", detail=str(exception))

    previous = request.app[TOKENS].current()
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
    if previous is not None and previous.polar_user_id != token.x_user_id:
        logger.warning(
            "Polar user %s replaced the authorization for user %s. If you did not do this, someone else "
            "authorized this connector: run `logout`, restart the container, then authorize again and "
            "check /status.",
            token.x_user_id,
            previous.polar_user_id,
        )
    else:
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


def _page(status: int, heading: str, body: str, **values: str) -> web.Response:
    """Minimal self-contained HTML: a human is reading this one, not a machine.

    `heading` and `body` are templates written in this module, and the only place markup is allowed.
    Everything that came from outside — Polar's `error` parameter, the text of an exception — arrives as
    a keyword in `values` and is escaped before it is substituted. Escaping here rather than at the call
    sites is what stops the next page from reflecting an unescaped value because someone forgot: a call
    site cannot put outside text on the page except through `values`.
    """
    escaped = {name: html.escape(value) for name, value in values.items()}

    return web.Response(
        status=status,
        content_type="text/html",
        text=(
            "<!doctype html><meta charset=utf-8>"
            "<title>Dreeve Polar Connector</title>"
            "<style>body{font-family:system-ui,sans-serif;max-width:34rem;margin:4rem auto;padding:0 1rem;"
            "line-height:1.5}code{background:#eee;padding:.1rem .3rem;border-radius:.2rem}</style>"
            f"<h1>{html.escape(heading)}</h1><p>{body.format(**escaped) if escaped else body}</p>"
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

    stored = _stored_webhook(request)

    if request.headers.get(EVENT_HEADER, "").upper() == PING_EVENT:
        _log_ping(body, request.headers.get(SIGNATURE_HEADER, ""), stored)
        return web.json_response({"ok": True})

    if stored is None:
        if _worth_logging(request.app[STATUS].delivery_rejected()):
            logger.warning("Ignoring a webhook delivery: no signing key is stored, so nothing can be verified")
        return web.json_response({"error": "unverified"}, status=401)

    signature = request.headers.get(SIGNATURE_HEADER, "")
    if not signature_matches(body, signature, stored.signature_secret_key):
        if _worth_logging(request.app[STATUS].delivery_rejected()):
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


def _worth_logging(rejected: int) -> bool:
    """Whether this refusal gets a log line: the first one, then every hundredth.

    `/webhook` is the published route, so the rate at which this is reached is chosen by whoever can
    reach it. One line per refusal hands an anonymous caller a way to bury everything else in the log.
    The count in `/status` stays exact — only the logging thins out.
    """
    return rejected == 1 or rejected % REJECTED_DELIVERY_LOG_INTERVAL == 0


def _stored_webhook(request: web.Request) -> StoredWebhook | None:
    """The stored registration, or None — including when the file cannot be read.

    This route is the only one published to the internet, so an unreadable `webhook.json` must not
    become an unauthenticated 500. Treating it as "no key" is also the safe direction: nothing can be
    verified either way, so every delivery is refused rather than trusted. Logged at error level because
    the cause is a broken volume or a hand-edited file, and only a human can fix it.
    """
    try:
        return request.app[TOKENS].webhook()
    except CorruptTokenStore as exception:
        logger.error("Cannot verify webhook deliveries: %s", exception)

        return None


def _log_ping(body: bytes, signature: str, stored: StoredWebhook | None) -> None:
    """A ping is always answered 200; this only records how much of it could be trusted.

    Answering unconditionally is required: Polar pings during `create` — before it discloses the signing
    key — and again during `activate`, and refuses both operations unless the URL returns 200. A ping
    has no side effects, so there is nothing to protect. The activation ping does arrive with the key
    already stored, though, which makes it a free check that our HMAC construction still matches
    Polar's before a real delivery is refused for the same reason.
    """
    if stored is None:
        logger.info("Answered Polar's webhook ping; no signing key is stored yet, so it was not verified")
        return

    if signature_matches(body, signature, stored.signature_secret_key):
        logger.info("Answered Polar's webhook ping and verified its signature")
        return

    logger.warning(
        "Answered Polar's webhook ping, but its signature did not match. Real deliveries will be "
        "refused for the same reason. Check that Polar still signs the hex HMAC-SHA256 of the raw body "
        "with this webhook's key, and that the stored key belongs to the registered webhook."
    )


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
        logger.info("Serving %s on %s:%d", self._describe_routes(), self._host, self.port)

    def _describe_routes(self) -> str:
        """Named from the application's own routes, so the log cannot drift from what is served."""
        paths = sorted({resource.canonical for resource in self._app.router.resources()})

        return ", ".join(paths)

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
