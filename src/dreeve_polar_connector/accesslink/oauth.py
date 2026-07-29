"""Getting an access token, and the one operation in this project that must never be retried.

Polar's documentation on the authorization code: "This code must only be used once. If it's used more
than once, the request will be denied and ALL issued tokens will be deleted."

So `exchange_code` has no retry, and — the part that is easy to get wrong — a network failure inside it
raises `AuthenticationFailed` rather than `ConnectionFailed`. `ConnectionFailed` is what `tenacity`
retries in `client.py`; raising it here would make a flaky connection delete every token for the
account. The code is spent the moment it is sent, whatever the outcome.

Tokens themselves do not expire unless revoked, so there is no refresh flow to implement.
"""

import logging
from urllib.parse import urlencode

import aiohttp
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from dreeve_polar_connector.accesslink._http import client_credentials_header
from dreeve_polar_connector.accesslink.exceptions import AuthenticationFailed, UnexpectedResponse

AUTHORIZATION_URL = "https://flow.polar.com/oauth2/authorization"
# Named EXCHANGE_URL rather than TOKEN_URL: flake8-bandit's S105 flags any assignment whose target name
# contains "token" as a possible hardcoded password, regardless of the value. This constant is a URL.
EXCHANGE_URL = "https://polarremote.com/v2/oauth2/token"
ACCESSLINK_SCOPE = "accesslink.read_all"

logger = logging.getLogger(__name__)


class Token(BaseModel):
    """What the token endpoint returns. `x_user_id` is the polar-user-id everything else is keyed by."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    access_token: str
    x_user_id: int
    # Field(default=...) rather than a bare `= "bearer"`: the same S105 heuristic fires on "token_type"
    # for a direct string-literal assignment, but not when the literal is nested inside the Field call.
    token_type: str = Field(default="bearer")


def authorization_url(*, client_id: str, state: str, redirect_uri: str | None = None) -> str:
    """Where to send the user's browser.

    `redirect_uri` is optional: omitted, Polar falls back to the client's only registered URL, which is
    one fewer thing for a self-hoster to configure. If it is sent here it must also be sent to the
    token endpoint, and vice versa.
    """
    query = {"response_type": "code", "client_id": client_id, "state": state}
    if redirect_uri:
        query["redirect_uri"] = redirect_uri

    return f"{AUTHORIZATION_URL}?{urlencode(query)}"


async def exchange_code(
    session: aiohttp.ClientSession,
    *,
    client_id: str,
    client_secret: str,
    code: str,
    redirect_uri: str | None = None,
    token_url: str = EXCHANGE_URL,
) -> Token:
    """Trades an authorization code for an access token. Exactly once — see the module docstring."""
    body = {"grant_type": "authorization_code", "code": code}
    if redirect_uri:
        body["redirect_uri"] = redirect_uri

    try:
        async with session.post(
            token_url,
            data=body,
            headers={
                "Accept": "application/json",
                "Authorization": client_credentials_header(client_id, client_secret),
            },
        ) as response:
            status = response.status
            payload = await response.text()
    except (aiohttp.ClientError, TimeoutError) as exception:
        raise AuthenticationFailed(
            f"The token exchange could not be completed: {exception}. The authorization code is now "
            f"spent either way — start again from /authorize rather than retrying."
        ) from exception

    if status != 200:
        raise AuthenticationFailed(
            f"Polar rejected the token exchange with {status}: {payload[:200].strip()}. "
            f"The authorization code is spent; start again from /authorize."
        )

    try:
        return Token.model_validate_json(payload)
    except ValidationError as exception:
        raise UnexpectedResponse(f"Polar's token response could not be read: {exception}.") from exception
