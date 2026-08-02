"""Argument handling, the paste-flow login, and the two commands operators run by hand."""

import asyncio
import json
import logging
import os
import time
from argparse import Namespace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from dreeve_polar_connector.accesslink import oauth
from dreeve_polar_connector.cli import (
    EXIT_FAILED,
    EXIT_OK,
    authorization_stamp_for,
    authorize_url_for,
    ensure_webhook,
    login,
    main,
    run,
    webhook_action,
)
from dreeve_polar_connector.config import Config
from dreeve_polar_connector.logging_ import Secrets
from dreeve_polar_connector.loop import heartbeat_path
from dreeve_polar_connector.server import HttpServer
from dreeve_polar_connector.status import Status
from dreeve_polar_connector.tokens import Authorization, StoredWebhook, TokenStore

NOW = datetime(2026, 7, 29, 8, 0, tzinfo=UTC)


class _FakePolarWebhooks:
    """A local stand-in for the webhook endpoints of www.polaraccesslink.com."""

    def __init__(self) -> None:
        self.create_status = 201
        self.existing: dict[str, object] | None = None
        self.activated = 0
        self.deleted: list[str] = []
        self.delete_status = 200

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_post("/v3/webhooks", self._create)
        app.router.add_get("/v3/webhooks", self._read)
        app.router.add_post("/v3/webhooks/activate", self._activate)
        app.router.add_delete("/v3/webhooks/{webhook_id}", self._delete)
        return app

    async def _create(self, request: web.Request) -> web.Response:
        if self.create_status == 409:
            return web.json_response({}, status=409)
        body = await request.json()
        payload = {"id": "wh-1", "signature_secret_key": "s3cr3t", "url": body["url"], "events": ["EXERCISE"]}
        return web.json_response({"data": payload}, status=201)

    async def _read(self, request: web.Request) -> web.Response:  # noqa: ARG002 - nothing in the request is used
        return web.json_response({"data": [self.existing] if self.existing else None})

    async def _activate(self, request: web.Request) -> web.Response:  # noqa: ARG002
        self.activated += 1
        return web.json_response({}, status=200)

    async def _delete(self, request: web.Request) -> web.Response:
        if self.delete_status != 200:
            return web.json_response({}, status=self.delete_status)
        self.deleted.append(request.match_info["webhook_id"])
        return web.json_response({}, status=200)


def _environment(tmp_path: Path, **overrides: str) -> dict[str, str]:
    return {
        "POLAR_CLIENT_ID": "a-client",
        "POLAR_CLIENT_SECRET": "a-secret",
        "WATCH_DIR": str(tmp_path / "watch"),
        "STATE_DIR": str(tmp_path / "state"),
        "POLAR_TOKENS": str(tmp_path / "tokens"),
        **overrides,
    }


def _config(tmp_path: Path, **overrides: str) -> Config:
    return Config.from_env(_environment(tmp_path, **overrides))


def test_a_missing_client_id_fails_before_anything_else(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("os.environ", {"STATE_DIR": str(tmp_path)})

    assert main(["sync-once"]) == EXIT_FAILED
    assert "POLAR_CLIENT_ID" in capsys.readouterr().err


def test_an_unknown_command_is_rejected_by_argparse() -> None:
    with pytest.raises(SystemExit):
        main(["teleport"])


def test_no_command_is_rejected() -> None:
    with pytest.raises(SystemExit):
        main([])


def test_sync_once_without_an_authorization_fails_with_instructions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # capsys rather than caplog: `main()` calls `configure_logging`, which (correctly, per
    # test_logging.py::test_configure_logging_replaces_existing_handlers) tears down every existing
    # root handler, including the one caplog attached for this test. The message still lands on the
    # real stderr stream that the freshly installed handler writes to, so capsys reads it there instead.
    monkeypatch.setattr("os.environ", _environment(tmp_path))

    assert main(["sync-once"]) == EXIT_FAILED
    assert "/authorize" in capsys.readouterr().err


def test_the_authorize_url_uses_the_public_url_when_configured(tmp_path: Path) -> None:
    config = _config(tmp_path, PUBLIC_URL="http://nas.local:8080")

    assert authorize_url_for(config) == "http://nas.local:8080/authorize"


def test_the_authorize_url_falls_back_to_the_bound_address(tmp_path: Path) -> None:
    # 0.0.0.0 is an address to listen on, not one to open in a browser.
    config = _config(tmp_path, HTTP_ADDR="0.0.0.0:9000")

    assert authorize_url_for(config) == "http://127.0.0.1:9000/authorize"


def test_there_is_no_authorize_url_when_the_server_is_off(tmp_path: Path) -> None:
    config = _config(tmp_path, HTTP_ADDR="off")

    assert authorize_url_for(config) is None


def test_there_is_no_authorization_stamp_before_anyone_authorizes(tmp_path: Path) -> None:
    assert authorization_stamp_for(TokenStore(tmp_path / "tokens")) is None


@pytest.mark.asyncio
async def test_the_authorization_stamp_identifies_the_stored_token(tmp_path: Path) -> None:
    tokens = TokenStore(tmp_path / "tokens")
    await tokens.store(
        Authorization(polar_user_id=475, access_token="a-token", member_id="dreeve-475", authorized_at=NOW)
    )

    assert authorization_stamp_for(tokens) == (475, NOW)


@pytest.mark.asyncio
async def test_the_authorization_stamp_changes_when_a_new_token_is_stored(tmp_path: Path) -> None:
    # This is the whole signal the loop uses to recover from a rejected token, so it is worth pinning:
    # re-authorizing must produce a stamp that differs from the one before it.
    tokens = TokenStore(tmp_path / "tokens")
    await tokens.store(
        Authorization(polar_user_id=475, access_token="rejected", member_id="dreeve-475", authorized_at=NOW)
    )
    before = authorization_stamp_for(tokens)

    await tokens.store(
        Authorization(
            polar_user_id=475,
            access_token="fresh",
            member_id="dreeve-475",
            authorized_at=NOW + timedelta(minutes=5),
        )
    )

    assert authorization_stamp_for(tokens) != before


@pytest.mark.asyncio
async def test_the_authorization_stamp_holds_no_token_material(tmp_path: Path) -> None:
    # The stamp is compared and logged around; it must never be a way for a token to leak.
    tokens = TokenStore(tmp_path / "tokens")
    await tokens.store(
        Authorization(polar_user_id=475, access_token="a-secret-token", member_id="dreeve-475", authorized_at=NOW)
    )

    assert "a-secret-token" not in str(authorization_stamp_for(tokens))


def test_healthcheck_without_a_server_reads_the_heartbeat(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("os.environ", _environment(tmp_path, HTTP_ADDR="off"))
    heartbeat = heartbeat_path(tmp_path / "state")
    heartbeat.parent.mkdir(parents=True, exist_ok=True)
    heartbeat.touch()

    assert main(["healthcheck"]) == EXIT_OK


def test_healthcheck_fails_when_there_is_no_heartbeat_yet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("os.environ", _environment(tmp_path, HTTP_ADDR="off"))

    assert main(["healthcheck"]) == EXIT_FAILED
    assert "heartbeat" in capsys.readouterr().err


def test_healthcheck_fails_when_the_heartbeat_is_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # POLL_INTERVAL=1 makes UNHEALTHY_AFTER_CYCLES * POLL_INTERVAL a 3-second window, so a heartbeat
    # backdated 10 real seconds is unambiguously stale without the test itself waiting on a clock.
    monkeypatch.setattr("os.environ", _environment(tmp_path, HTTP_ADDR="off", POLL_INTERVAL="1"))
    heartbeat = heartbeat_path(tmp_path / "state")
    heartbeat.parent.mkdir(parents=True, exist_ok=True)
    heartbeat.touch()
    stale_at = time.time() - 10
    os.utime(heartbeat, (stale_at, stale_at))

    assert main(["healthcheck"]) == EXIT_FAILED
    assert "more than" in capsys.readouterr().err


def test_status_without_a_server_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("os.environ", _environment(tmp_path, HTTP_ADDR="off"))

    assert main(["status"]) == EXIT_FAILED
    assert "HTTP_ADDR" in capsys.readouterr().err


def test_login_prints_the_authorization_url_and_reads_a_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("os.environ", _environment(tmp_path, HTTP_ADDR="off"))
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: "")

    # An empty code aborts before any request is made, which is the behaviour under test.
    assert main(["login"]) == EXIT_FAILED
    assert "flow.polar.com/oauth2/authorization" in capsys.readouterr().out


def test_login_refuses_without_a_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # capsys rather than caplog: see the comment on the sync-once test above for why.
    monkeypatch.setattr("os.environ", _environment(tmp_path, HTTP_ADDR="off"))
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)

    assert main(["login"]) == EXIT_FAILED
    assert "terminal" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_login_stores_the_token_after_a_successful_exchange(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _register(request: web.Request) -> web.Response:
        body = await request.json()
        return web.json_response({"polar-user-id": 475, "member-id": body["member-id"]})

    fake = web.Application()
    fake.router.add_post("/v3/users", _register)
    polar = TestServer(fake)
    await polar.start_server()
    try:
        monkeypatch.setattr(
            "dreeve_polar_connector.accesslink._http.ACCESSLINK_BASE_URL", f"http://127.0.0.1:{polar.port}"
        )

        async def _exchange_code(*args: object, **kwargs: object) -> oauth.Token:  # noqa: ARG001 - a stand-in
            return oauth.Token(access_token="a-token", x_user_id=475, token_type="bearer")

        monkeypatch.setattr(oauth, "exchange_code", _exchange_code)
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda _: "a-code")

        config = _config(tmp_path, HTTP_ADDR="off")
        secrets = Secrets([config.client_secret])
        tokens = TokenStore(config.tokens_dir)

        exit_code = await login(config, secrets, tokens, Namespace())

        assert exit_code == EXIT_OK
        stored = tokens.current()
        assert stored is not None
        assert stored.access_token == "a-token"
        assert stored.polar_user_id == 475
    finally:
        await polar.close()


@pytest.mark.asyncio
async def test_run_with_http_addr_off_completes_without_a_server(tmp_path: Path) -> None:
    config = _config(tmp_path, HTTP_ADDR="off", MAX_CYCLES="1")
    secrets = Secrets([config.client_secret])
    tokens = TokenStore(config.tokens_dir)

    exit_code = await run(config, secrets, tokens, Namespace())

    assert exit_code == EXIT_OK


def test_webhook_show_without_a_registration_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("os.environ", _environment(tmp_path))

    assert main(["webhook", "show"]) == EXIT_OK
    assert json.loads(capsys.readouterr().out) == {"configured": False}


def test_webhook_show_with_a_registration_prints_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("os.environ", _environment(tmp_path))
    tokens_dir = tmp_path / "tokens"
    _store_webhook_sync(tokens_dir)

    assert main(["webhook", "show"]) == EXIT_OK
    printed = json.loads(capsys.readouterr().out)
    assert printed == {"configured": True, "id": "wh-1", "url": "https://example.test/webhook"}


def _store_webhook_sync(tokens_dir: Path) -> None:
    """A synchronous helper so `TokenStore.store_webhook` can be seeded outside an async test body."""
    asyncio.run(
        TokenStore(tokens_dir).store_webhook(
            StoredWebhook(webhook_id="wh-1", signature_secret_key="s3cr3t", url="https://example.test/webhook")
        )
    )


@pytest.mark.asyncio
async def test_webhook_register_stores_the_webhook_and_its_signing_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    polar = TestServer(_FakePolarWebhooks().app())
    await polar.start_server()
    try:
        monkeypatch.setattr(
            "dreeve_polar_connector.accesslink._http.ACCESSLINK_BASE_URL", f"http://127.0.0.1:{polar.port}"
        )
        config = _config(tmp_path, WEBHOOK_URL="http://example.test/webhook", WEBHOOK_ADDR="127.0.0.1:0")
        tokens = TokenStore(config.tokens_dir)

        exit_code = await webhook_action(config, Secrets(), tokens, Namespace(action="register"))

        assert exit_code == EXIT_OK
        stored = tokens.webhook()
        assert stored is not None
        assert stored.webhook_id == "wh-1"
        assert stored.signature_secret_key == "s3cr3t"
    finally:
        await polar.close()


@pytest.mark.asyncio
async def test_webhook_register_without_a_webhook_url_fails(tmp_path: Path) -> None:
    config = _config(tmp_path)
    tokens = TokenStore(config.tokens_dir)

    assert await webhook_action(config, Secrets(), tokens, Namespace(action="register")) == EXIT_FAILED


@pytest.mark.asyncio
async def test_webhook_register_reports_registration_as_the_failed_verb(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    fake = _FakePolarWebhooks()
    fake.create_status = 409
    polar = TestServer(fake.app())
    await polar.start_server()
    try:
        monkeypatch.setattr(
            "dreeve_polar_connector.accesslink._http.ACCESSLINK_BASE_URL", f"http://127.0.0.1:{polar.port}"
        )
        config = _config(tmp_path, WEBHOOK_URL="http://example.test/webhook", WEBHOOK_ADDR="127.0.0.1:0")
        tokens = TokenStore(config.tokens_dir)

        with caplog.at_level(logging.ERROR):
            exit_code = await webhook_action(config, Secrets(), tokens, Namespace(action="register"))

        assert exit_code == EXIT_FAILED
        # 409 is `WebhookAlreadyExists`, a distinct message from the generic `AccessLinkError` one below;
        # covered here so `register`'s error path (not just `delete`'s) has a test of its own.
        assert "already has a webhook" in caplog.text
    finally:
        await polar.close()


@pytest.mark.asyncio
async def test_webhook_delete_removes_the_local_registration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    polar = TestServer(_FakePolarWebhooks().app())
    await polar.start_server()
    try:
        monkeypatch.setattr(
            "dreeve_polar_connector.accesslink._http.ACCESSLINK_BASE_URL", f"http://127.0.0.1:{polar.port}"
        )
        config = _config(tmp_path)
        tokens = TokenStore(config.tokens_dir)
        await tokens.store_webhook(
            StoredWebhook(webhook_id="wh-1", signature_secret_key="s3cr3t", url="https://example.test/webhook")
        )

        exit_code = await webhook_action(config, Secrets(), tokens, Namespace(action="delete"))

        assert exit_code == EXIT_OK
        assert tokens.webhook() is None
    finally:
        await polar.close()


@pytest.mark.asyncio
async def test_webhook_delete_without_a_local_registration_says_so(tmp_path: Path) -> None:
    config = _config(tmp_path)
    tokens = TokenStore(config.tokens_dir)

    assert await webhook_action(config, Secrets(), tokens, Namespace(action="delete")) == EXIT_OK


@pytest.mark.asyncio
async def test_webhook_delete_reports_deletion_as_the_failed_verb_not_registration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # Fix 3: `register` and `delete` used to share one `except AccessLinkError` clause that always
    # logged "Could not register the webhook", so a failed `delete` was misreported as a failed
    # registration.
    fake = _FakePolarWebhooks()
    fake.delete_status = 401
    polar = TestServer(fake.app())
    await polar.start_server()
    try:
        monkeypatch.setattr(
            "dreeve_polar_connector.accesslink._http.ACCESSLINK_BASE_URL", f"http://127.0.0.1:{polar.port}"
        )
        config = _config(tmp_path)
        tokens = TokenStore(config.tokens_dir)
        await tokens.store_webhook(
            StoredWebhook(webhook_id="wh-1", signature_secret_key="s3cr3t", url="https://example.test/webhook")
        )

        with caplog.at_level(logging.ERROR):
            exit_code = await webhook_action(config, Secrets(), tokens, Namespace(action="delete"))

        assert exit_code == EXIT_FAILED
        assert "Could not delete the webhook" in caplog.text
        assert "Could not register the webhook" not in caplog.text
    finally:
        await polar.close()


@pytest.mark.asyncio
async def test_ensure_webhook_registers_and_persists_when_nothing_is_stored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    polar = TestServer(_FakePolarWebhooks().app())
    await polar.start_server()
    try:
        monkeypatch.setattr(
            "dreeve_polar_connector.accesslink._http.ACCESSLINK_BASE_URL", f"http://127.0.0.1:{polar.port}"
        )
        config = _config(tmp_path, WEBHOOK_URL="http://example.test/webhook", WEBHOOK_ADDR="127.0.0.1:0")
        tokens = TokenStore(config.tokens_dir)
        status = Status(config.poll_interval, NOW, authorize_url=None)

        async with aiohttp.ClientSession() as session:
            await ensure_webhook(config, Secrets(), tokens, session, status)

        stored = tokens.webhook()
        assert stored is not None
        assert stored.webhook_id == "wh-1"
        assert stored.signature_secret_key == "s3cr3t"
        assert status.to_dict(NOW)["webhook"] == {
            "configured": True,
            "active": True,
            "id": "wh-1",
            "lastEventAt": None,
            "rejectedDeliveries": 0,
        }
    finally:
        await polar.close()


@pytest.mark.asyncio
async def test_ensure_webhook_without_a_webhook_url_does_nothing(tmp_path: Path) -> None:
    config = _config(tmp_path)
    tokens = TokenStore(config.tokens_dir)
    status = Status(config.poll_interval, NOW, authorize_url=None)

    async with aiohttp.ClientSession() as session:
        await ensure_webhook(config, Secrets(), tokens, session, status)

    assert tokens.webhook() is None


@pytest.mark.asyncio
async def test_ensure_webhook_warns_without_crashing_when_polar_reports_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # A webhook is stored locally, but the fake below reports none: Polar's side of the pairing is
    # gone (deleted through the admin page, say), and this must be a warning, not a crash.
    polar = TestServer(_FakePolarWebhooks().app())
    await polar.start_server()
    try:
        monkeypatch.setattr(
            "dreeve_polar_connector.accesslink._http.ACCESSLINK_BASE_URL", f"http://127.0.0.1:{polar.port}"
        )
        config = _config(tmp_path, WEBHOOK_URL="http://example.test/webhook", WEBHOOK_ADDR="127.0.0.1:0")
        tokens = TokenStore(config.tokens_dir)
        await tokens.store_webhook(
            StoredWebhook(webhook_id="wh-1", signature_secret_key="s3cr3t", url="https://example.test/webhook")
        )
        status = Status(config.poll_interval, NOW, authorize_url=None)

        with caplog.at_level(logging.WARNING):
            async with aiohttp.ClientSession() as session:
                await ensure_webhook(config, Secrets(), tokens, session, status)

        assert "Polar has none" in caplog.text
        assert status.to_dict(NOW)["webhook"]["configured"] is False
    finally:
        await polar.close()


@pytest.mark.asyncio
async def test_ensure_webhook_activates_a_webhook_polar_reports_as_inactive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _FakePolarWebhooks()
    fake.existing = {"id": "wh-1", "active": False, "url": "https://example.test/webhook"}
    polar = TestServer(fake.app())
    await polar.start_server()
    try:
        monkeypatch.setattr(
            "dreeve_polar_connector.accesslink._http.ACCESSLINK_BASE_URL", f"http://127.0.0.1:{polar.port}"
        )
        config = _config(tmp_path, WEBHOOK_URL="http://example.test/webhook", WEBHOOK_ADDR="127.0.0.1:0")
        tokens = TokenStore(config.tokens_dir)
        await tokens.store_webhook(
            StoredWebhook(webhook_id="wh-1", signature_secret_key="s3cr3t", url="https://example.test/webhook")
        )
        status = Status(config.poll_interval, NOW, authorize_url=None)

        async with aiohttp.ClientSession() as session:
            await ensure_webhook(config, Secrets(), tokens, session, status)

        assert fake.activated == 1
        assert status.to_dict(NOW)["webhook"] == {
            "configured": True,
            "active": True,
            "id": "wh-1",
            "lastEventAt": None,
            "rejectedDeliveries": 0,
        }
    finally:
        await polar.close()


@pytest.mark.asyncio
async def test_ensure_webhook_leaves_an_active_webhook_alone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakePolarWebhooks()
    fake.existing = {"id": "wh-1", "active": True, "url": "https://example.test/webhook"}
    polar = TestServer(fake.app())
    await polar.start_server()
    try:
        monkeypatch.setattr(
            "dreeve_polar_connector.accesslink._http.ACCESSLINK_BASE_URL", f"http://127.0.0.1:{polar.port}"
        )
        config = _config(tmp_path, WEBHOOK_URL="http://example.test/webhook", WEBHOOK_ADDR="127.0.0.1:0")
        tokens = TokenStore(config.tokens_dir)
        await tokens.store_webhook(
            StoredWebhook(webhook_id="wh-1", signature_secret_key="s3cr3t", url="https://example.test/webhook")
        )
        status = Status(config.poll_interval, NOW, authorize_url=None)

        async with aiohttp.ClientSession() as session:
            await ensure_webhook(config, Secrets(), tokens, session, status)

        assert fake.activated == 0
    finally:
        await polar.close()


@pytest.mark.asyncio
async def test_ensure_webhook_logs_the_remediation_when_polar_already_has_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # Nothing is stored locally, but Polar reports a 409: some earlier registration succeeded and this
    # connector never got, or has since lost, the signing key. Must not crash; must say what to do.
    fake = _FakePolarWebhooks()
    fake.create_status = 409
    polar = TestServer(fake.app())
    await polar.start_server()
    try:
        monkeypatch.setattr(
            "dreeve_polar_connector.accesslink._http.ACCESSLINK_BASE_URL", f"http://127.0.0.1:{polar.port}"
        )
        config = _config(tmp_path, WEBHOOK_URL="http://example.test/webhook", WEBHOOK_ADDR="127.0.0.1:0")
        tokens = TokenStore(config.tokens_dir)
        status = Status(config.poll_interval, NOW, authorize_url=None)

        with caplog.at_level(logging.ERROR):
            async with aiohttp.ClientSession() as session:
                await ensure_webhook(config, Secrets(), tokens, session, status)

        assert "webhook delete" in caplog.text
        assert tokens.webhook() is None
    finally:
        await polar.close()


async def _run_with_a_broken_signing_key_persist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, polar_port: int | None
) -> int:
    """Registers fine against the fake Polar below, but `TokenStore.store_webhook` always fails —
    simulating `POLAR_TOKENS` going unwritable between `webhooks.create` succeeding and the one-time
    signing key being persisted."""
    monkeypatch.setattr("dreeve_polar_connector.accesslink._http.ACCESSLINK_BASE_URL", f"http://127.0.0.1:{polar_port}")

    async def _boom(self: TokenStore, webhook: StoredWebhook) -> None:  # noqa: ARG001 - matches the real signature
        raise OSError("no space left on device")

    monkeypatch.setattr(TokenStore, "store_webhook", _boom)

    config = _config(
        tmp_path,
        WEBHOOK_URL="http://example.test/webhook",
        WEBHOOK_ADDR="127.0.0.1:0",
        HTTP_ADDR="127.0.0.1:0",
        MAX_CYCLES="1",
    )
    secrets = Secrets([config.client_secret])
    tokens = TokenStore(config.tokens_dir)

    return await run(config, secrets, tokens, Namespace())


@pytest.mark.asyncio
async def test_a_signing_key_that_cannot_be_persisted_does_not_crash_run_and_stops_the_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Before the fix, `ensure_webhook` let a raw `OSError` from `store_webhook` propagate. It was
    # called before `run`'s `try/finally`, so the exception crashed `run` outright and the `HttpServer`
    # it had already started was never stopped.
    #
    # Both HTTP_ADDR and WEBHOOK_ADDR are set below, so two listeners start and both must stop.
    polar = TestServer(_FakePolarWebhooks().app())
    await polar.start_server()

    stopped = []
    original_stop = HttpServer.stop

    async def _tracking_stop(self: HttpServer) -> None:
        stopped.append(True)
        await original_stop(self)

    monkeypatch.setattr(HttpServer, "stop", _tracking_stop)

    try:
        exit_code = await _run_with_a_broken_signing_key_persist(tmp_path, monkeypatch, polar.port)
    finally:
        await polar.close()

    assert exit_code == EXIT_OK
    assert stopped == [True, True]


@pytest.mark.asyncio
async def test_a_signing_key_that_cannot_be_persisted_is_reported_with_its_remediation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # A generic "could not set up the webhook" is not good enough here: Polar has already disclosed the
    # one-time signing key and it is gone for good, so the operator has to be told exactly what to do.
    polar = TestServer(_FakePolarWebhooks().app())
    await polar.start_server()

    try:
        with caplog.at_level(logging.ERROR):
            exit_code = await _run_with_a_broken_signing_key_persist(tmp_path, monkeypatch, polar.port)
    finally:
        await polar.close()

    assert exit_code == EXIT_OK
    assert "webhook delete" in caplog.text
    assert "gone" in caplog.text.lower()


async def _run_and_record_served_routes(
    config: Config, tokens: TokenStore, monkeypatch: pytest.MonkeyPatch
) -> tuple[int, list[str]]:
    """Runs one cycle and reports what each started listener serves, keyed off the real router."""
    served: list[str] = []
    real_start = HttpServer.start

    async def _record(self: HttpServer) -> None:
        await real_start(self)
        served.append(self._describe_routes())

    monkeypatch.setattr(HttpServer, "start", _record)
    exit_code = await run(config, Secrets([config.client_secret]), tokens, Namespace())

    return exit_code, served


@pytest.mark.asyncio
async def test_run_serves_the_webhook_on_its_own_listener(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Port 0 lets the OS pick, so this cannot collide with whatever else is listening.
    config = _config(tmp_path, HTTP_ADDR="127.0.0.1:0", WEBHOOK_ADDR="127.0.0.1:0", MAX_CYCLES="1")

    exit_code, served = await _run_and_record_served_routes(config, TokenStore(config.tokens_dir), monkeypatch)

    assert exit_code == EXIT_OK
    assert len(served) == 2
    assert "/webhook" in served[1]
    assert "/webhook" not in served[0]
    assert "/authorize" in served[0]


@pytest.mark.asyncio
async def test_run_starts_only_the_local_listener_when_webhooks_are_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, HTTP_ADDR="127.0.0.1:0", MAX_CYCLES="1")

    exit_code, served = await _run_and_record_served_routes(config, TokenStore(config.tokens_dir), monkeypatch)

    assert exit_code == EXIT_OK
    assert served == ["/, /authorize, /callback, /healthz, /status"]


def test_logout_clears_the_authorization_and_keeps_the_webhook(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The signing key is disclosed exactly once, so clearing it would force a re-registration nobody
    # asked for.
    monkeypatch.setattr("os.environ", _environment(tmp_path))
    tokens_dir = tmp_path / "tokens"
    _store_webhook_sync(tokens_dir)
    asyncio.run(
        TokenStore(tokens_dir).store(
            Authorization(polar_user_id=475, access_token="a-token", member_id="dreeve-475", authorized_at=NOW)
        )
    )

    assert main(["logout"]) == EXIT_OK

    tokens = TokenStore(tokens_dir)
    assert tokens.current() is None
    assert tokens.webhook() is not None


def test_logout_without_an_authorization_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # capsys rather than caplog: see the comment on the sync-once test above for why — `main()` tears
    # down the handler caplog attached before the test body ever runs.
    monkeypatch.setattr("os.environ", _environment(tmp_path))

    assert main(["logout"]) == EXIT_OK

    assert "no authorization is stored" in capsys.readouterr().err


def test_logout_asks_for_a_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A running container keeps the Sync it built, so it goes on using the token it loaded. Clearing the
    # file is not enough, and the message is the only place an operator learns that.
    monkeypatch.setattr("os.environ", _environment(tmp_path))
    asyncio.run(
        TokenStore(tmp_path / "tokens").store(
            Authorization(polar_user_id=475, access_token="a-token", member_id="dreeve-475", authorized_at=NOW)
        )
    )

    assert main(["logout"]) == EXIT_OK

    assert "Restart the connector" in capsys.readouterr().err


def test_logout_clears_a_store_it_cannot_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # /authorize refuses while it cannot tell who is connected, and logout is the documented way out of
    # that. Refusing to run here would leave deleting the file by hand as the only route.
    monkeypatch.setattr("os.environ", _environment(tmp_path))
    tokens_dir = tmp_path / "tokens"
    tokens_dir.mkdir(parents=True, exist_ok=True)
    (tokens_dir / "tokens.json").write_text("{not json", encoding="utf-8")

    assert main(["logout"]) == EXIT_OK

    assert not (tokens_dir / "tokens.json").exists()
    assert "Restart the connector" in capsys.readouterr().err


def test_a_command_other_than_logout_still_refuses_an_unreadable_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Everything else needs a token it cannot read — and one it cannot read is one it cannot redact.
    monkeypatch.setattr("os.environ", _environment(tmp_path))
    tokens_dir = tmp_path / "tokens"
    tokens_dir.mkdir(parents=True, exist_ok=True)
    (tokens_dir / "tokens.json").write_text("{not json", encoding="utf-8")

    assert main(["sync-once"]) == EXIT_FAILED
    assert "tokens.json" in capsys.readouterr().err
    assert (tokens_dir / "tokens.json").exists()


@pytest.mark.asyncio
async def test_ensure_webhook_reports_a_registration_pointing_at_the_old_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # The migration this release requires invites exactly this: move WEBHOOK_URL to the new port, forget
    # `webhook delete`, and Polar keeps delivering to the old address while /status looks healthy.
    fake = _FakePolarWebhooks()
    fake.existing = {"id": "wh-1", "events": ["EXERCISE"], "url": "http://old.test:8080/webhook", "active": True}
    polar = TestServer(fake.app())
    await polar.start_server()
    try:
        monkeypatch.setattr(
            "dreeve_polar_connector.accesslink._http.ACCESSLINK_BASE_URL", f"http://127.0.0.1:{polar.port}"
        )
        config = _config(
            tmp_path, WEBHOOK_URL="http://new.test:8081/webhook", WEBHOOK_ADDR="127.0.0.1:0", HTTP_ADDR="127.0.0.1:0"
        )
        tokens = TokenStore(config.tokens_dir)
        await tokens.store_webhook(
            StoredWebhook(webhook_id="wh-1", signature_secret_key="s3cr3t", url="http://old.test:8080/webhook")
        )
        status = Status(config.poll_interval, NOW, authorize_url=None)

        async with aiohttp.ClientSession() as session:
            with caplog.at_level(logging.ERROR):
                await ensure_webhook(config, Secrets(), tokens, session, status)

        assert "http://old.test:8080/webhook" in caplog.text
        assert "webhook delete" in caplog.text
    finally:
        await polar.close()
