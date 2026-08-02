"""Webhook registration, and verifying that a delivery really came from Polar.

The receiving endpoint is unauthenticated by construction — Polar cannot send credentials — so this
HMAC is the only authentication it has. It is computed over the raw request body: re-serialising the
JSON first changes whitespace and key order, and a correct signature would then fail.

The scheme, per https://www.polar.com/accesslink-api/ : header `Polar-Webhook-Signature`, HMAC-SHA256
over the payload, hex-encoded, keyed with the `signature_secret_key` that is disclosed exactly once
when the webhook is created. Every payload is signed, pings included.

This is Polar Electro's AccessLink API. It is *not* Polar.sh, the payments company, whose webhooks use
Standard Webhooks — `webhook-id`/`webhook-timestamp`/`webhook-signature`, base64, a `v1,` prefix, svix
libraries — and which dominates search results for "Polar webhook signature". Implementing that here
would reject every genuine delivery.

Registration uses client credentials (HTTP Basic), not a user token, which is why these are module
functions rather than methods on `AccessLinkClient`.
"""

import hashlib
import hmac
import json
import logging

import aiohttp
from pydantic import BaseModel, ValidationError

from dreeve_polar_connector.accesslink import _http
from dreeve_polar_connector.accesslink._http import client_credentials_header, raise_for_status
from dreeve_polar_connector.accesslink.exceptions import (
    AuthenticationFailed,
    ConnectionFailed,
    UnexpectedResponse,
    WebhookAlreadyExists,
)
from dreeve_polar_connector.accesslink.models import CreatedWebhook, ExerciseEvent, WebhookInfo

PING_EVENT = "PING"
EXERCISE_EVENT = "EXERCISE"
EVENT_HEADER = "Polar-Webhook-Event"
SIGNATURE_HEADER = "Polar-Webhook-Signature"
# Polar's payloads are a few hundred bytes. A cap keeps an unauthenticated endpoint from being a
# memory-exhaustion lever.
MAX_BODY_BYTES = 64 * 1024
HEX_DIGITS = frozenset("0123456789abcdef")

logger = logging.getLogger(__name__)


def signature_for(body: bytes, secret: str) -> str:
    """HMAC-SHA256 of the raw body, hex-encoded — the form Polar puts in `Polar-Webhook-Signature`."""
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def signature_matches(body: bytes, signature: str, secret: str) -> bool:
    """Constant-time comparison. Never raises: a malformed signature is simply not a match.

    Anything that is not ASCII hexadecimal is rejected before the comparison. `hmac.compare_digest`
    raises TypeError on a non-ASCII `str`, and this value arrives in a header on an unauthenticated
    endpoint, so letting it through would turn a crafted request into a 500.
    """
    candidate = signature.strip().lower()
    if not candidate or not HEX_DIGITS.issuperset(candidate):
        return False

    return hmac.compare_digest(signature_for(body, secret), candidate)


def parse_exercise_event(body: bytes) -> ExerciseEvent | None:
    """The event, or None for anything that is not a readable EXERCISE event.

    None rather than an exception on purpose: the handler has to answer 200 either way, because a
    non-200 counts as a delivery failure and seven days of those deactivates the webhook.
    """
    try:
        event = ExerciseEvent.model_validate_json(body)
    except ValidationError as exception:
        logger.warning("Ignoring a webhook body this connector cannot read: %s", exception)
        return None

    if event.event != EXERCISE_EVENT:
        logger.debug("Ignoring a %s webhook event; only %s is acted on", event.event, EXERCISE_EVENT)
        return None

    return event


async def create(
    session: aiohttp.ClientSession,
    *,
    client_id: str,
    client_secret: str,
    url: str,
    events: tuple[str, ...] = (EXERCISE_EVENT,),
    base_url: str | None = None,
) -> CreatedWebhook:
    """Registers the client's one webhook.

    Polar pings `url` during this call and refuses to create the webhook unless it answers 200, so the
    server has to be listening and publicly reachable before this runs. The returned
    `signature_secret_key` is disclosed exactly once — persist it before doing anything else.
    """
    status, body = await _request(
        session,
        "POST",
        "/v3/webhooks",
        client_id=client_id,
        client_secret=client_secret,
        base_url=base_url,
        json_body={"events": list(events), "url": url},
        allow_status=(201, 409),
    )

    if status == 409:
        raise WebhookAlreadyExists(
            "This Polar client already has a webhook. A client may only have one; read it or delete it first."
        )

    return _model(CreatedWebhook, _unwrap(body), "the created webhook")


async def read(
    session: aiohttp.ClientSession,
    *,
    client_id: str,
    client_secret: str,
    base_url: str | None = None,
) -> WebhookInfo | None:
    """The client's webhook, or None. Check `active`: Polar deactivates after seven days of failures."""
    _, body = await _request(
        session,
        "GET",
        "/v3/webhooks",
        client_id=client_id,
        client_secret=client_secret,
        base_url=base_url,
        allow_status=(200, 204),
    )

    entries = _unwrap(body)
    if not entries:
        return None
    if not isinstance(entries, list):
        raise UnexpectedResponse(f"Expected a list of webhooks, got {type(entries).__name__}.")

    return _model(WebhookInfo, entries[0], "the configured webhook")


async def activate(
    session: aiohttp.ClientSession,
    *,
    client_id: str,
    client_secret: str,
    base_url: str | None = None,
) -> None:
    """Revives an auto-deactivated webhook. Polar re-pings the URL, so it must be reachable again."""
    await _request(
        session,
        "POST",
        "/v3/webhooks/activate",
        client_id=client_id,
        client_secret=client_secret,
        base_url=base_url,
        allow_status=(200, 204),
    )


async def delete(
    session: aiohttp.ClientSession,
    *,
    client_id: str,
    client_secret: str,
    webhook_id: str,
    base_url: str | None = None,
) -> None:
    await _request(
        session,
        "DELETE",
        f"/v3/webhooks/{webhook_id}",
        client_id=client_id,
        client_secret=client_secret,
        base_url=base_url,
        allow_status=(200, 204),
    )


async def _request(
    session: aiohttp.ClientSession,
    method: str,
    path: str,
    *,
    client_id: str,
    client_secret: str,
    base_url: str | None,
    json_body: dict[str, object] | None = None,
    allow_status: tuple[int, ...] = (200,),
) -> tuple[int, bytes]:
    """Client-credential requests. No retry: creating a webhook twice is not idempotent.

    `base_url` is resolved here rather than bound as a default argument on each public function, and
    read off the `_http` module object rather than through a `from ... import ACCESSLINK_BASE_URL`
    name: either one would be fixed at import time, which would make it impossible to redirect at a
    fake server in tests, or at Polar's sandbox in a real deployment.
    """
    resolved_base_url = base_url or _http.ACCESSLINK_BASE_URL
    try:
        async with session.request(
            method,
            f"{resolved_base_url.rstrip('/')}{path}",
            headers={
                "Accept": "application/json",
                "Authorization": client_credentials_header(client_id, client_secret),
            },
            json=json_body,
        ) as response:
            status = response.status
            body = await response.read()
    except (aiohttp.ClientError, TimeoutError) as exception:
        raise ConnectionFailed(f"Could not reach Polar for {path}: {exception}.") from exception

    if status in allow_status:
        return status, body

    raise_for_status(status, path, body, forbidden=AuthenticationFailed)

    return status, body


def _unwrap(body: bytes) -> object:
    """Webhook responses wrap their payload in a `data` key; an empty body means nothing configured."""
    if not body.strip():
        return None

    try:
        payload = json.loads(body)
    except ValueError as exception:
        raise UnexpectedResponse(f"Polar's webhook response was not valid JSON: {exception}.") from exception

    if not isinstance(payload, dict):
        raise UnexpectedResponse(f"Expected a webhook object, got {type(payload).__name__}.")

    return payload.get("data")


def _model[T: BaseModel](model: type[T], payload: object, what: str) -> T:
    try:
        return model.model_validate(payload)
    except ValidationError as exception:
        raise UnexpectedResponse(f"Polar's description of {what} could not be read: {exception}.") from exception
