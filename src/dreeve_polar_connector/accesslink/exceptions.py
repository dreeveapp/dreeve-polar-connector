"""What can go wrong at the Polar boundary, split by what the caller should do about it.

These are not interchangeable. One means back off for a while, another means stop and ask a human, a
third means try the same request again shortly, a fourth means never try this one again. Losing those
distinctions is how a daemon either loses activities or hammers an API that is saying no.
"""


class AccessLinkError(Exception):
    """Base for everything this package raises, so nothing from aiohttp or pydantic leaks past it."""


class RateLimited(AccessLinkError):
    """Back off. Not for this one request — for everything, for a while."""


class AuthenticationFailed(AccessLinkError):
    """The token is dead or revoked. Retrying cannot help; a human has to authorize again."""


class ConsentRequired(AccessLinkError):
    """The user has not accepted all mandatory consents. Fixed in Polar Flow, not by re-authorizing."""


class NotFound(AccessLinkError):
    """Polar has nothing here. For an exercise that means deleted, or aged past the 30-day window."""


class ConnectionFailed(AccessLinkError):
    """Transient. The only exception this package retries, and the only one it is safe to retry."""


class UnexpectedResponse(AccessLinkError):
    """Polar answered with something we do not recognise. Worth failing loudly over, not guessing at."""


class WebhookAlreadyExists(AccessLinkError):
    """A client may have exactly one webhook. Distinct from a rejection: the caller can read it instead."""
