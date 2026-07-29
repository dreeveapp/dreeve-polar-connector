"""Shared HTTP plumbing between `client.py` (a user token, sent as Bearer) and `webhooks.py`
(client credentials, sent as Basic): the one base URL, the one way to build a Basic-auth header, and
the one status-to-exception mapping.

The mapping has exactly one axis of variation, because a 401 or 403 means something different
depending on which credential was rejected. For a user token, 401 means the token itself is dead and
403 means a mandatory consent tied to that token is missing — two different repairs, re-authorize
versus accept a consent in Polar Flow. For client credentials there is no consent concept at all:
401 and 403 both mean the partner credentials were rejected outright, repaired the same way either
time — check POLAR_CLIENT_ID and POLAR_CLIENT_SECRET. `forbidden` is what a 403 raises, and since
only a user-token caller ever passes `ConsentRequired`, it doubles as the signal for which of the two
401 messages applies. There is no third profile to plan for: `client.py` and `webhooks.py` are the
only two callers this package has, so branching on `forbidden is ConsentRequired` is a closed
choice, not an open-ended special case.
"""

import base64

from dreeve_polar_connector.accesslink.exceptions import (
    AccessLinkError,
    AuthenticationFailed,
    ConnectionFailed,
    ConsentRequired,
    NotFound,
    RateLimited,
    UnexpectedResponse,
)

ACCESSLINK_BASE_URL = "https://www.polaraccesslink.com"
# Enough of the body to make an error message useful, not enough to paste a whole payload into a log.
ERROR_DETAIL_BYTES = 200


def client_credentials_header(client_id: str, client_secret: str) -> str:
    """HTTP Basic for the client (partner) credentials, as `/v3/webhooks` and the token endpoint want.

    Built by hand rather than with `aiohttp.BasicAuth`: both it and the `auth=` request parameter are
    deprecated in aiohttp 3.x and removed in 4.0, and they emit a DeprecationWarning on every call.
    """
    encoded = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode("ascii")

    return f"Basic {encoded}"


def error_detail(body: bytes) -> str:
    """The leading slice of an error body, decoded leniently: enough to be useful in a log or an
    exception message, without assuming the body is valid UTF-8 or trusting its size."""
    return body[:ERROR_DETAIL_BYTES].decode("utf-8", errors="replace").strip()


def raise_for_status(status: int, path: str, body: bytes, *, forbidden: type[AccessLinkError]) -> None:
    """Each branch means something different to the caller; see `exceptions.py`.

    `forbidden` is raised for a 403, and is also what tells this function which of the two 401
    messages to use: pass `ConsentRequired` for a user-token caller, `AuthenticationFailed` for a
    client-credential caller.
    """
    if status < 400:
        return

    detail = error_detail(body)
    is_user_token = forbidden is ConsentRequired

    if status == 429:
        raise RateLimited(f"Polar rate-limited {path}: {detail}")
    if status == 401:
        if is_user_token:
            raise AuthenticationFailed(
                f"Polar rejected the access token on {path}: {detail}. Re-authorize to get a fresh one."
            )
        raise _client_credentials_rejected(path, detail, AuthenticationFailed)
    if status == 403:
        if is_user_token:
            raise forbidden(
                f"Polar refused {path}: the user has not accepted all mandatory consents. "
                f"Accept them in Polar Flow — re-authorizing will not help. {detail}"
            )
        raise _client_credentials_rejected(path, detail, forbidden)
    if status == 404:
        raise NotFound(f"Polar has nothing at {path}: {detail}")
    if status >= 500:
        # Deliberately ConnectionFailed and not UnexpectedResponse: a 5xx is the one server-side
        # answer that is worth sending the very same request again.
        raise ConnectionFailed(f"Polar returned {status} for {path}: {detail}")

    raise UnexpectedResponse(f"Polar returned {status} for {path}: {detail}")


def _client_credentials_rejected(path: str, detail: str, exception: type[AccessLinkError]) -> AccessLinkError:
    return exception(
        f"Polar rejected the client credentials on {path}: {detail}. Check POLAR_CLIENT_ID and POLAR_CLIENT_SECRET."
    )
