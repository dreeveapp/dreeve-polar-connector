"""Two things matter here beyond round-tripping: the files are not world-readable, and a corrupt store
is reported rather than silently treated as "no token", which would trigger a pointless re-authorize."""

import json
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest

from dreeve_polar_connector.tokens import (
    TOKENS_FILENAME,
    WEBHOOK_FILENAME,
    Authorization,
    CorruptTokenStore,
    StoredWebhook,
    TokenStore,
)

NOW = datetime(2026, 7, 29, 8, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_a_fresh_store_has_no_authorization(tmp_path: Path) -> None:
    assert TokenStore(tmp_path).current() is None


@pytest.mark.asyncio
async def test_an_authorization_round_trips(tmp_path: Path) -> None:
    store = TokenStore(tmp_path)

    await store.store(
        Authorization(polar_user_id=475, access_token="a-token", member_id="dreeve-475", authorized_at=NOW)
    )
    current = TokenStore(tmp_path).current()

    assert current is not None
    assert current.polar_user_id == 475
    assert current.access_token == "a-token"
    assert current.member_id == "dreeve-475"


@pytest.mark.asyncio
async def test_the_token_file_is_not_world_readable(tmp_path: Path) -> None:
    store = TokenStore(tmp_path)

    await store.store(
        Authorization(polar_user_id=475, access_token="a-token", member_id="dreeve-475", authorized_at=NOW)
    )

    mode = (tmp_path / TOKENS_FILENAME).stat().st_mode
    assert not mode & stat.S_IROTH
    assert not mode & stat.S_IRGRP


@pytest.mark.asyncio
async def test_the_user_id_is_the_key_and_is_not_repeated_in_the_value(tmp_path: Path) -> None:
    store = TokenStore(tmp_path)

    await store.store(
        Authorization(polar_user_id=475, access_token="a-token", member_id="dreeve-475", authorized_at=NOW)
    )

    raw = json.loads((tmp_path / TOKENS_FILENAME).read_text(encoding="utf-8"))
    assert list(raw["users"]) == ["475"]
    assert "polarUserId" not in raw["users"]["475"]


@pytest.mark.asyncio
async def test_storing_a_second_authorization_replaces_the_first(tmp_path: Path) -> None:
    # Single-user behaviour: re-authorizing swaps the token rather than accumulating users.
    store = TokenStore(tmp_path)
    await store.store(Authorization(polar_user_id=475, access_token="old", member_id="dreeve-475", authorized_at=NOW))

    await store.store(Authorization(polar_user_id=476, access_token="new", member_id="dreeve-476", authorized_at=NOW))

    current = store.current()
    assert current is not None
    assert current.polar_user_id == 476
    assert len(json.loads((tmp_path / TOKENS_FILENAME).read_text(encoding="utf-8"))["users"]) == 1


@pytest.mark.asyncio
async def test_clearing_removes_the_token(tmp_path: Path) -> None:
    store = TokenStore(tmp_path)
    await store.store(
        Authorization(polar_user_id=475, access_token="a-token", member_id="dreeve-475", authorized_at=NOW)
    )

    store.clear()

    assert store.current() is None


def test_a_corrupt_token_file_is_reported_not_treated_as_empty(tmp_path: Path) -> None:
    (tmp_path / TOKENS_FILENAME).write_text("{ not json", encoding="utf-8")

    with pytest.raises(CorruptTokenStore, match="not valid JSON"):
        TokenStore(tmp_path).current()


def test_a_token_file_from_a_newer_version_is_reported(tmp_path: Path) -> None:
    (tmp_path / TOKENS_FILENAME).write_text(json.dumps({"version": 99, "users": {}}), encoding="utf-8")

    with pytest.raises(CorruptTokenStore, match="upgrade"):
        TokenStore(tmp_path).current()


@pytest.mark.asyncio
async def test_a_webhook_registration_round_trips(tmp_path: Path) -> None:
    store = TokenStore(tmp_path)

    await store.store_webhook(
        StoredWebhook(webhook_id="abdf33", signature_secret_key="a-key", url="https://example.test/webhook")
    )
    stored = TokenStore(tmp_path).webhook()

    assert stored is not None
    assert stored.webhook_id == "abdf33"
    assert stored.signature_secret_key == "a-key"


@pytest.mark.asyncio
async def test_the_webhook_file_is_not_world_readable(tmp_path: Path) -> None:
    store = TokenStore(tmp_path)

    await store.store_webhook(
        StoredWebhook(webhook_id="abdf33", signature_secret_key="a-key", url="https://example.test/webhook")
    )

    assert not (tmp_path / WEBHOOK_FILENAME).stat().st_mode & stat.S_IROTH


@pytest.mark.asyncio
async def test_clearing_the_webhook_removes_it(tmp_path: Path) -> None:
    store = TokenStore(tmp_path)
    await store.store_webhook(
        StoredWebhook(webhook_id="abdf33", signature_secret_key="a-key", url="https://example.test/webhook")
    )

    store.clear_webhook()

    assert store.webhook() is None


@pytest.mark.asyncio
async def test_every_secret_in_the_store_can_be_listed_for_redaction(tmp_path: Path) -> None:
    # `Secrets` needs these before anything logs, and it must not have to know the file layout.
    store = TokenStore(tmp_path)
    await store.store(
        Authorization(polar_user_id=475, access_token="a-token", member_id="dreeve-475", authorized_at=NOW)
    )
    await store.store_webhook(
        StoredWebhook(webhook_id="abdf33", signature_secret_key="a-key", url="https://example.test/webhook")
    )

    assert set(store.secrets()) == {"a-token", "a-key"}
