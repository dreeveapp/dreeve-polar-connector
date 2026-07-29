"""Where the access token and the webhook signing key live.

Both are credentials, so both go under POLAR_TOKENS at mode 0600 — the volume the documentation tells
people to back up and never share. Polar's tokens do not expire unless revoked, so keeping this volume
means never authorizing twice.

The file is keyed by polar-user-id from v1 (Decision 3) while the API here is single-user. That way
adding multi-user support later is a change to this module and nothing else.
"""

import asyncio
import json
import logging
import os
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pydantic.alias_generators import to_camel

TOKENS_FILENAME = "tokens.json"
WEBHOOK_FILENAME = "webhook.json"
STORE_VERSION = 1
# Owner only. Anything wider and a shared bind mount leaks account access.
STORE_FILE_MODE = 0o600
TEMP_SUFFIX = ".tmp"

logger = logging.getLogger(__name__)

# Every model inlines its own ConfigDict(...) literal rather than sharing a module-level constant.
# The pydantic mypy plugin only reads `frozen=True` — and only marks fields read-only for mypy —
# off a literal ConfigDict(...) call written directly in the class body. Sharing a constant, or
# merging two with `|`, silently loses that. Verified against mypy --strict.


class CorruptTokenStore(Exception):
    """Reported rather than treated as "no token": the fix is to inspect it, not to re-authorize blindly."""


class Authorization(BaseModel):
    """One authorized Polar user. `polar_user_id` is the file's key, so it is not stored in the value."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, frozen=True)

    # Defaulted, not just excluded: `_TokenFile.users` is a dict[str, Authorization], so pydantic
    # reconstructs each Authorization straight from its JSON value on load, which never contains
    # `polarUserId` since it is excluded from serialisation. Without a default that validation fails
    # with "Field required" on every round trip. `current()` overwrites it with the real key regardless.
    polar_user_id: int = Field(default=0, exclude=True)
    access_token: str
    member_id: str
    authorized_at: datetime


class StoredWebhook(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, frozen=True)

    webhook_id: str
    signature_secret_key: str
    url: str


class _TokenFile(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    version: int = STORE_VERSION
    users: dict[str, Authorization] = Field(default_factory=dict)


class _WebhookFile(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    version: int = STORE_VERSION
    webhook: StoredWebhook | None = None


class TokenStore:
    def __init__(self, directory: Path) -> None:
        self._directory = directory

    @property
    def tokens_path(self) -> Path:
        return self._directory / TOKENS_FILENAME

    @property
    def webhook_path(self) -> Path:
        return self._directory / WEBHOOK_FILENAME

    def current(self) -> Authorization | None:
        """The single authorization, or None when nobody has completed /authorize yet."""
        contents = self._read(self.tokens_path, _TokenFile)
        if contents is None:
            return None

        for polar_user_id, authorization in contents.users.items():
            return authorization.model_copy(update={"polar_user_id": int(polar_user_id)})

        return None

    async def store(self, authorization: Authorization) -> None:
        """Replaces whatever was there. Single-user behaviour over a multi-user file shape."""
        contents = _TokenFile(users={str(authorization.polar_user_id): authorization})
        await asyncio.to_thread(self._write, self.tokens_path, contents)
        logger.info("Stored the Polar authorization for user %s in %s", authorization.polar_user_id, self.tokens_path)

    def clear(self) -> None:
        self.tokens_path.unlink(missing_ok=True)

    def webhook(self) -> StoredWebhook | None:
        contents = self._read(self.webhook_path, _WebhookFile)

        return contents.webhook if contents else None

    async def store_webhook(self, webhook: StoredWebhook) -> None:
        await asyncio.to_thread(self._write, self.webhook_path, _WebhookFile(webhook=webhook))
        logger.info("Stored the webhook registration in %s", self.webhook_path)

    def clear_webhook(self) -> None:
        self.webhook_path.unlink(missing_ok=True)

    def secrets(self) -> Iterator[str]:
        """Everything in here that must never reach a log line, for the `Secrets` redactor."""
        authorization = self.current()
        if authorization is not None:
            yield authorization.access_token

        webhook = self.webhook()
        if webhook is not None:
            yield webhook.signature_secret_key

    def _read[T: BaseModel](self, path: Path, model: type[T]) -> T | None:
        if not path.exists():
            return None

        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exception:
            raise CorruptTokenStore(
                f"{path} is not valid JSON: {exception}. Inspect it, or delete it and authorize again."
            ) from exception
        except OSError as exception:
            raise CorruptTokenStore(f"{path} could not be read: {exception}.") from exception

        if not isinstance(raw, dict):
            raise CorruptTokenStore(f"{path} should contain an object, got {type(raw).__name__}.")

        version = raw.get("version")
        if version != STORE_VERSION:
            raise CorruptTokenStore(
                f"{path} is version {version!r}, this build reads version {STORE_VERSION}. "
                f"It was most likely written by a newer connector; upgrade rather than downgrade."
            )

        try:
            return model.model_validate(raw)
        except ValidationError as exception:
            raise CorruptTokenStore(f"{path} could not be read: {exception}.") from exception

    def _write(self, path: Path, contents: BaseModel) -> None:
        """Same atomic sequence as the ledger, plus a mode set before the file has any content."""
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_name(path.name + TEMP_SUFFIX)

        payload: dict[str, Any] = contents.model_dump(by_alias=True, mode="json")
        # Opened at 0600 rather than chmod-ed afterwards: a token must never exist as a wider file,
        # not even for the microsecond in between.
        descriptor = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, STORE_FILE_MODE)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

        temp_path.replace(path)
