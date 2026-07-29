"""A client for the Polar AccessLink API v3.

Self-contained on purpose: nothing in here knows about Dreeve, watch folders or ledgers, so the whole
package can be lifted into a standalone library. `tests/accesslink/test_import_boundary.py` enforces
that rather than trusting it.

`__all__` below is functional, not decorative: `mypy --strict` implies `--no-implicit-reexport`, so a
consumer can write `from ...accesslink import AccessLinkClient, Exercise` without knowing this package's
internal module layout. `webhooks.py`'s verbs (`create`, `read`, `activate`, `delete`) are deliberately
not flattened in here — they are far too generic at package scope, and `webhooks.create(...)` reads
better than a bare `create(...)` would.
"""

from dreeve_polar_connector.accesslink._http import client_credentials_header
from dreeve_polar_connector.accesslink.client import AccessLinkClient
from dreeve_polar_connector.accesslink.exceptions import (
    AccessLinkError,
    AuthenticationFailed,
    ConnectionFailed,
    ConsentRequired,
    NotFound,
    RateLimited,
    UnexpectedResponse,
    WebhookAlreadyExists,
)
from dreeve_polar_connector.accesslink.models import CreatedWebhook, Exercise, ExerciseEvent, RateLimit, WebhookInfo
from dreeve_polar_connector.accesslink.oauth import Token, authorization_url, exchange_code

__all__ = [
    "AccessLinkClient",
    "AccessLinkError",
    "AuthenticationFailed",
    "ConnectionFailed",
    "ConsentRequired",
    "CreatedWebhook",
    "Exercise",
    "ExerciseEvent",
    "NotFound",
    "RateLimit",
    "RateLimited",
    "Token",
    "UnexpectedResponse",
    "WebhookAlreadyExists",
    "WebhookInfo",
    "authorization_url",
    "client_credentials_header",
    "exchange_code",
]
