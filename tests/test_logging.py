"""Redaction is tested through the logging machinery, not by calling `redact` directly: the whole point
is that no output path can bypass it."""

import json
import logging
from collections.abc import Iterator
from typing import cast

import pytest

from dreeve_polar_connector.config import LogFormat
from dreeve_polar_connector.logging_ import REDACTED, Secrets, configure_logging


@pytest.fixture(autouse=True)
def _restore_root_logger() -> Iterator[None]:
    root = logging.getLogger()
    # One test also attaches a filter directly to a named logger (not just a handler); restoring only
    # the root logger's handlers/level would let that filter leak into every later test that logs
    # through a logger named "test".
    test_logger = logging.getLogger("test")
    root_handlers, root_level = list(root.handlers), root.level
    test_filters = list(test_logger.filters)

    yield

    root.handlers = root_handlers
    root.setLevel(root_level)
    test_logger.filters = test_filters


def test_a_secret_is_replaced() -> None:
    secrets = Secrets(["a-very-secret-token"])

    assert secrets.redact("token=a-very-secret-token") == f"token={REDACTED}"


def test_a_none_secret_is_ignored() -> None:
    assert Secrets([None]).redact("nothing here") == "nothing here"


def test_a_short_value_is_not_treated_as_a_secret() -> None:
    # Anything this short is likelier to be a substring of ordinary output than a real credential.
    assert Secrets(["ab"]).redact("about") == "about"


def test_secrets_can_be_added_later() -> None:
    # The access token only exists after the OAuth exchange, so the set has to be mutable.
    secrets = Secrets()
    secrets.add("a-token-from-polar")

    assert secrets.redact("bearer a-token-from-polar") == f"bearer {REDACTED}"


def test_the_longest_secret_is_replaced_first() -> None:
    secrets = Secrets(["abcd", "abcdefgh"])

    assert secrets.redact("abcdefgh") == REDACTED


def test_a_secret_in_an_interpolated_message_is_redacted(caplog: pytest.LogCaptureFixture) -> None:
    secrets = Secrets(["a-very-secret-token"])
    configure_logging("info", LogFormat.TEXT, secrets)
    logging.getLogger("test").addFilter(_only_filter_of(logging.getLogger()))

    with caplog.at_level(logging.INFO):
        logging.getLogger("test").info("using %s", "a-very-secret-token")

    assert "a-very-secret-token" not in caplog.text


def test_a_secret_in_a_traceback_is_redacted(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("info", LogFormat.TEXT, Secrets(["a-very-secret-token"]))

    try:
        raise ValueError("failed with a-very-secret-token")
    except ValueError:
        logging.getLogger("test").exception("boom")

    captured = capsys.readouterr()
    assert "a-very-secret-token" not in captured.err
    assert REDACTED in captured.err


def test_json_format_emits_one_object_per_line(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("info", LogFormat.JSON, Secrets())

    logging.getLogger("test").info("hello")

    payload = json.loads(capsys.readouterr().err.strip())
    assert payload["message"] == "hello"
    assert payload["level"] == "info"
    assert payload["logger"] == "test"


def test_json_format_redacts_too(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("info", LogFormat.JSON, Secrets(["a-very-secret-token"]))

    logging.getLogger("test").info("using a-very-secret-token")

    assert "a-very-secret-token" not in capsys.readouterr().err


def test_configure_logging_replaces_existing_handlers() -> None:
    root = logging.getLogger()
    root.addHandler(logging.NullHandler())

    configure_logging("debug", LogFormat.TEXT, Secrets())

    assert len(root.handlers) == 1
    assert root.level == logging.DEBUG


def _only_filter_of(logger: logging.Logger) -> logging.Filter:
    # typeshed types Handler.filters more broadly than plain Filter; this is always the RedactingFilter
    # we installed via configure_logging just above.
    return cast(logging.Filter, logger.handlers[0].filters[0])
