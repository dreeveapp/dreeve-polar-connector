"""The hierarchy is load-bearing: `loop.py` catches these by type to decide whether to retry, back
off, or stop and wait for a human. A wrong base class silently changes daemon behaviour."""

import pytest

from dreeve_polar_connector.accesslink.exceptions import (
    AccessLinkError,
    AuthenticationFailed,
    ConnectionFailed,
    ConsentRequired,
    NotFound,
    RateLimited,
    UnexpectedResponse,
)

EVERY_ERROR = [
    RateLimited,
    AuthenticationFailed,
    ConsentRequired,
    NotFound,
    ConnectionFailed,
    UnexpectedResponse,
]


@pytest.mark.parametrize("error", EVERY_ERROR)
def test_every_error_derives_from_the_base(error: type[AccessLinkError]) -> None:
    assert issubclass(error, AccessLinkError)


def test_consent_required_is_not_an_authentication_failure() -> None:
    # Fixed in Polar Flow by accepting consents, not by re-authorizing. Conflating the two sends
    # the user down the wrong path.
    assert not issubclass(ConsentRequired, AuthenticationFailed)


def test_rate_limited_is_not_a_connection_failure() -> None:
    # ConnectionFailed is the only exception tenacity retries. A rate limit must escape to the loop.
    assert not issubclass(RateLimited, ConnectionFailed)
