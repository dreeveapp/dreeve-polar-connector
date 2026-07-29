"""The shared status-to-exception mapping used by both `client.py` (a user token) and `webhooks.py`
(client credentials). Both callers reach this through their own modules too, but it is asserted
directly here because it is the one place the mapping exists — get a branch wrong here and both
callers inherit the mistake."""

import base64

import pytest

from dreeve_polar_connector.accesslink._http import ACCESSLINK_BASE_URL, client_credentials_header, raise_for_status
from dreeve_polar_connector.accesslink.exceptions import (
    AccessLinkError,
    AuthenticationFailed,
    ConnectionFailed,
    ConsentRequired,
    NotFound,
    RateLimited,
    UnexpectedResponse,
)

BOTH_PROFILES = (ConsentRequired, AuthenticationFailed)


def test_base_url_is_the_documented_accesslink_host() -> None:
    assert ACCESSLINK_BASE_URL == "https://www.polaraccesslink.com"


def test_client_credentials_header_is_http_basic_over_the_pair() -> None:
    header = client_credentials_header("a-client", "a-secret")

    assert header.startswith("Basic ")
    assert base64.b64decode(header.removeprefix("Basic ")).decode("ascii") == "a-client:a-secret"


def test_a_status_below_400_does_not_raise() -> None:
    raise_for_status(200, "/v3/exercises", b"{}", forbidden=ConsentRequired)


@pytest.mark.parametrize("forbidden", BOTH_PROFILES)
def test_429_raises_rate_limited_regardless_of_profile(forbidden: type[AccessLinkError]) -> None:
    with pytest.raises(RateLimited, match="slow down"):
        raise_for_status(429, "/v3/exercises", b"slow down", forbidden=forbidden)


def test_401_for_a_user_token_points_at_reauthorizing() -> None:
    with pytest.raises(AuthenticationFailed, match="Re-authorize"):
        raise_for_status(401, "/v3/exercises", b"expired", forbidden=ConsentRequired)


def test_401_for_client_credentials_points_at_the_env_vars() -> None:
    with pytest.raises(AuthenticationFailed, match="POLAR_CLIENT_ID"):
        raise_for_status(401, "/v3/webhooks", b"bad creds", forbidden=AuthenticationFailed)


def test_403_for_a_user_token_raises_consent_required() -> None:
    with pytest.raises(ConsentRequired, match="Polar Flow"):
        raise_for_status(403, "/v3/exercises", b"no consent", forbidden=ConsentRequired)


def test_403_for_client_credentials_raises_authentication_failed() -> None:
    with pytest.raises(AuthenticationFailed, match="POLAR_CLIENT_ID"):
        raise_for_status(403, "/v3/webhooks", b"bad creds", forbidden=AuthenticationFailed)


@pytest.mark.parametrize("forbidden", BOTH_PROFILES)
def test_404_raises_not_found_regardless_of_profile(forbidden: type[AccessLinkError]) -> None:
    with pytest.raises(NotFound):
        raise_for_status(404, "/v3/exercises/x", b"", forbidden=forbidden)


@pytest.mark.parametrize("forbidden", BOTH_PROFILES)
def test_5xx_raises_connection_failed_regardless_of_profile(forbidden: type[AccessLinkError]) -> None:
    with pytest.raises(ConnectionFailed):
        raise_for_status(503, "/v3/exercises", b"", forbidden=forbidden)


def test_an_unmapped_status_raises_unexpected_response() -> None:
    with pytest.raises(UnexpectedResponse, match="418"):
        raise_for_status(418, "/v3/exercises", b"", forbidden=ConsentRequired)
