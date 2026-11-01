"""`__all__` is functional here, not decorative: `mypy --strict` implies `--no-implicit-reexport`, so a
name a consumer needs but that isn't listed here is a mypy error at the call site, not just an
inconvenience. A package meant to be lifted out and depended on should let a caller write
`from ...accesslink import AccessLinkClient, Exercise` without knowing the internal module layout."""

import dreeve_polar_connector.accesslink as accesslink

EXPECTED_NOUNS = {
    "AccessLinkClient",
    "Exercise",
    "RateLimit",
    "Token",
    "CreatedWebhook",
    "WebhookInfo",
    "ExerciseEvent",
    "PhysicalInfo",
}

EXPECTED_FUNCTIONS = {
    "authorization_url",
    "exchange_code",
    "client_credentials_header",
}

EXPECTED_EXCEPTIONS = {
    "AccessLinkError",
    "AuthenticationFailed",
    "ConnectionFailed",
    "ConsentRequired",
    "NotFound",
    "RateLimited",
    "UnexpectedResponse",
    "WebhookAlreadyExists",
}


def test_all_lists_the_nouns_functions_and_exceptions() -> None:
    assert set(accesslink.__all__) == EXPECTED_NOUNS | EXPECTED_FUNCTIONS | EXPECTED_EXCEPTIONS


def test_every_listed_name_is_actually_importable_from_the_top_level() -> None:
    for name in accesslink.__all__:
        assert hasattr(accesslink, name), f"{name} is in __all__ but not actually bound on the package"


def test_webhook_verbs_are_deliberately_not_flattened_to_the_top_level() -> None:
    # create/read/activate/delete are far too generic at package scope; webhooks.create(...) reads
    # better than accesslink.create(...) and avoids clobbering builtins-adjacent names.
    for verb in ("create", "read", "activate", "delete"):
        assert verb not in accesslink.__all__
