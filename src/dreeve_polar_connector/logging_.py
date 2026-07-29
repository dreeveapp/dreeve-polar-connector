"""Logging setup and secret redaction.

Redaction is deliberately a single choke point rather than something call sites remember to do. The
client secret, every access token, the webhook signing key and any authorization code pass through code
that logs, and a credential pasted into a stack trace is a real leak.
"""

import json
import logging
import sys
import traceback
from collections.abc import Iterable
from typing import Any

from dreeve_polar_connector.config import LogFormat

REDACTED = "***"
TEXT_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
TEXT_DATE_FORMAT = "%Y-%m-%dT%H:%M:%S%z"
# Anything shorter is more likely to be a substring of ordinary log output than a real secret.
MINIMUM_SECRET_LENGTH = 4


class Secrets:
    """The values that must never reach a log line. Mutable: the access token only exists after OAuth."""

    def __init__(self, values: Iterable[str | None] = ()) -> None:
        self._values: set[str] = set()
        for value in values:
            self.add(value)

    def add(self, value: str | None) -> None:
        if value and len(value) >= MINIMUM_SECRET_LENGTH:
            self._values.add(value)

    def redact(self, text: str) -> str:
        # Longest first, so a secret containing another secret is not partially replaced.
        for value in sorted(self._values, key=len, reverse=True):
            text = text.replace(value, REDACTED)

        return text


class RedactingFilter(logging.Filter):
    """Renders and redacts the record before any formatter sees it, so no output path can bypass it."""

    def __init__(self, secrets: Secrets) -> None:
        super().__init__()
        self._secrets = secrets

    def filter(self, record: logging.LogRecord) -> bool:
        # Merging args into the message first catches secrets that only appear once interpolated.
        record.msg = self._secrets.redact(record.getMessage())
        record.args = None

        if record.exc_info and record.exc_text is None:
            _, exception, _ = record.exc_info
            if exception is not None:
                record.exc_text = self._secrets.redact("".join(traceback.format_exception(exception)))

        if record.stack_info:
            record.stack_info = self._secrets.redact(record.stack_info)

        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": self.formatTime(record, TEXT_DATE_FORMAT),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_text:
            payload["exception"] = record.exc_text
        if record.stack_info:
            payload["stack"] = record.stack_info

        return json.dumps(payload)


def configure_logging(level: str, log_format: LogFormat, secrets: Secrets) -> None:
    """Installs a single stderr handler: in a container, logs are the container's stdout/stderr."""
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        JsonFormatter() if log_format is LogFormat.JSON else logging.Formatter(TEXT_FORMAT, datefmt=TEXT_DATE_FORMAT)
    )
    handler.addFilter(RedactingFilter(secrets))

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level.upper())
