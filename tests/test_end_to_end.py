"""One test that exercises the whole wiring, because every other test in this suite stubs something.

A fake Polar serves the endpoints the connector uses; `cli.run` is driven with MAX_CYCLES=1 so it
completes on its own.
"""

import asyncio
import gzip
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from dreeve_polar_connector.accesslink.webhooks import signature_for
from dreeve_polar_connector.cli import EXIT_OK, main
from dreeve_polar_connector.ledger import LEDGER_FILENAME
from dreeve_polar_connector.server import HttpServer
from dreeve_polar_connector.tokens import Authorization, StoredWebhook, TokenStore
from dreeve_polar_connector.weight_state import WEIGHT_STATE_FILENAME

NOW = datetime(2026, 7, 29, 8, 0, tzinfo=UTC)
FIT_BYTES = b"\x0e\x10Q\x00.FITfake"
SIGNING_KEY = "abe1f3ae-fd33-11e8-8eb2-f2801f1b9fd1"


def _local_start_time(hours_ago: int) -> str:
    """A start time inside `SINCE=-30d` whenever the suite runs.

    Relative to the real clock, not `NOW`: `run` resolves SINCE against the system clock, so a fixed date
    here silently falls out of the window 30 days later and the cycle skips every exercise.
    Naive and local, the way Polar sends it next to `start_time_utc_offset`.
    """
    return (datetime.now(UTC) - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%S")


def _polar_app(physical_info: dict[str, object] | None = None) -> web.Application:
    """`physical_info` is what /v3/users/physical-info answers; None answers 404, as for an unknown user.

    A dict rather than a value so a test can change the weight between runs."""

    async def exercises(request: web.Request) -> web.Response:  # noqa: ARG001 - no query is used
        return web.json_response(
            [
                {
                    "id": "2AC312F",
                    "start_time": _local_start_time(hours_ago=24),
                    "start_time_utc_offset": 120,
                    "sport": "RUNNING",
                    "detailed_sport_info": "RUNNING_TREADMILL",
                    "device": "Polar Vantage V3",
                },
                {
                    "id": "NOFIT01",
                    "start_time": _local_start_time(hours_ago=23),
                    "start_time_utc_offset": 120,
                    "sport": "STRENGTH_TRAINING",
                    "detailed_sport_info": "STRENGTH_TRAINING",
                    "device": "Polar Vantage V3",
                },
            ],
            headers={"RateLimit-Usage": "3,9", "RateLimit-Limit": "520,5100", "RateLimit-Reset": "600,80000"},
        )

    async def fit(request: web.Request) -> web.Response:
        if request.match_info["exercise_id"] == "NOFIT01":
            return web.Response(status=404, text="{}", content_type="application/json")
        return web.Response(body=FIT_BYTES, content_type="application/octet-stream")

    async def tcx(request: web.Request) -> web.Response:  # noqa: ARG001 - the id is irrelevant
        # Gzipped, as Polar documents it, so the sniffing path is exercised end to end.
        return web.Response(
            body=gzip.compress(b"<TrainingCenterDatabase/>"), content_type="application/vnd.garmin.tcx+xml"
        )

    async def physical(request: web.Request) -> web.Response:  # noqa: ARG001 - no query is used
        if physical_info is None:
            return web.Response(status=404, text="{}", content_type="application/json")
        return web.json_response(physical_info)

    app = web.Application()
    app.router.add_get("/v3/users/physical-info", physical)
    app.router.add_get("/v3/exercises", exercises)
    app.router.add_get("/v3/exercises/{exercise_id}/fit", fit)
    app.router.add_get("/v3/exercises/{exercise_id}/tcx", tcx)

    return app


@pytest.mark.asyncio
async def test_a_full_cycle_delivers_a_fit_and_a_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    polar = TestServer(_polar_app())
    await polar.start_server()
    try:
        monkeypatch.setattr(
            "dreeve_polar_connector.accesslink._http.ACCESSLINK_BASE_URL",
            f"http://127.0.0.1:{polar.port}",
        )

        tokens = TokenStore(tmp_path / "tokens")
        await tokens.store(
            Authorization(polar_user_id=475, access_token="a-token", member_id="dreeve-475", authorized_at=NOW)
        )

        monkeypatch.setattr(
            "os.environ",
            {
                "POLAR_CLIENT_ID": "a-client",
                "POLAR_CLIENT_SECRET": "a-secret",
                "WATCH_DIR": str(tmp_path / "watch"),
                "STATE_DIR": str(tmp_path / "state"),
                "POLAR_TOKENS": str(tmp_path / "tokens"),
                "SINCE": "-30d",
                "DOWNLOAD_DELAY_SECONDS": "0",
                "MAX_CYCLES": "1",
                # Port 0 lets the OS pick, so the test never collides with a busy 8080.
                "HTTP_ADDR": "127.0.0.1:0",
            },
        )

        exit_code = await asyncio.to_thread(main, ["run"])

        assert exit_code == EXIT_OK
        assert (tmp_path / "watch" / "2AC312F.fit").read_bytes() == FIT_BYTES
        assert (tmp_path / "watch" / "NOFIT01.tcx").read_bytes() == b"<TrainingCenterDatabase/>"

        ledger = json.loads((tmp_path / "state" / LEDGER_FILENAME).read_text(encoding="utf-8"))
        assert ledger["exercises"]["2AC312F"]["status"] == "delivered"
        assert ledger["exercises"]["NOFIT01"]["status"] == "delivered"
        assert ledger["exercises"]["2AC312F"]["polarUserId"] == 475
    finally:
        await polar.close()


@pytest.mark.asyncio
async def test_run_without_an_authorization_stays_up_and_reports_the_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A missing authorization must never be fatal to `run`: no token is stored here at all, yet the
    container still completes its one cycle cleanly and tells the operator exactly where to go.

    capsys rather than caplog: `main()` calls `configure_logging`, which tears down every existing root
    handler (including one caplog installed for this test) and installs its own; the message still
    lands on the real stderr stream that handler writes to, which capsys reads directly.
    """
    monkeypatch.setattr(
        "os.environ",
        {
            "POLAR_CLIENT_ID": "a-client",
            "POLAR_CLIENT_SECRET": "a-secret",
            "WATCH_DIR": str(tmp_path / "watch"),
            "STATE_DIR": str(tmp_path / "state"),
            "POLAR_TOKENS": str(tmp_path / "tokens"),
            "MAX_CYCLES": "1",
            # Port 0 lets the OS pick for the real listening socket; the logged URL only ever quotes
            # back the configured address, so it stays predictable regardless of which port that is.
            "HTTP_ADDR": "127.0.0.1:0",
        },
    )

    exit_code = await asyncio.to_thread(main, ["run"])

    assert exit_code == EXIT_OK
    stderr = capsys.readouterr().err
    assert "http://127.0.0.1:0/authorize" in stderr
    assert not (tmp_path / "watch").exists() or not list((tmp_path / "watch").glob("*"))


@pytest.mark.asyncio
async def test_a_signed_delivery_reaches_the_webhook_listener_of_a_running_connector(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one property no other test covers: a genuine signed delivery, arriving on the second listener
    of a real `run()`, against the real token store.

    Everything else about the split is asserted in isolation — that the applications serve the routes they
    should, that `run` starts both. This is the wiring between them.
    """
    polar = TestServer(_polar_app())
    await polar.start_server()
    try:
        monkeypatch.setattr(
            "dreeve_polar_connector.accesslink._http.ACCESSLINK_BASE_URL", f"http://127.0.0.1:{polar.port}"
        )
        tokens = TokenStore(tmp_path / "tokens")
        await tokens.store(
            Authorization(polar_user_id=475, access_token="a-token", member_id="dreeve-475", authorized_at=NOW)
        )
        await tokens.store_webhook(
            StoredWebhook(webhook_id="wh-1", signature_secret_key=SIGNING_KEY, url="http://nas.local:8081/webhook")
        )

        # The bound ports are chosen by the OS, so they are read back off the servers as they start.
        ports: list[int] = []
        real_start = HttpServer.start

        async def _record(self: HttpServer) -> None:
            await real_start(self)
            ports.append(self.port)

        monkeypatch.setattr(HttpServer, "start", _record)
        monkeypatch.setattr(
            "os.environ",
            {
                "POLAR_CLIENT_ID": "a-client",
                "POLAR_CLIENT_SECRET": "a-secret",
                "WATCH_DIR": str(tmp_path / "watch"),
                "STATE_DIR": str(tmp_path / "state"),
                "POLAR_TOKENS": str(tmp_path / "tokens"),
                "DOWNLOAD_DELAY_SECONDS": "0",
                "MAX_CYCLES": "2",
                "POLL_INTERVAL": "2",
                "HTTP_ADDR": "127.0.0.1:0",
                "WEBHOOK_ADDR": "127.0.0.1:0",
            },
        )

        running = asyncio.create_task(asyncio.to_thread(main, ["run"]))
        for _ in range(200):
            if len(ports) == 2:
                break
            await asyncio.sleep(0.01)

        assert len(ports) == 2, "both listeners should have started"
        local_port, webhook_port = ports
        assert local_port != webhook_port

        body = json.dumps({"event": "EXERCISE", "user_id": 475, "entity_id": "2AC312F"}).encode("utf-8")
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"http://127.0.0.1:{webhook_port}/webhook",
                data=body,
                headers={
                    "Polar-Webhook-Event": "EXERCISE",
                    "Polar-Webhook-Signature": signature_for(body, SIGNING_KEY),
                },
            ) as delivery:
                assert delivery.status == 200

            # The operator's routes are not on that port, and Polar's are not on the operator's.
            async with session.get(f"http://127.0.0.1:{webhook_port}/status") as wrong_listener:
                assert wrong_listener.status == 404
            async with session.post(f"http://127.0.0.1:{local_port}/webhook", data=b"{}") as wrong_port:
                assert wrong_port.status == 404

        assert await running == EXIT_OK
    finally:
        await polar.close()


DREEVE_KEY = "drv_end-to-end-key"


@pytest.mark.asyncio
async def test_weight_changes_reach_dreeve_once_each(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    physical_info: dict[str, object] = {
        "weight": 80.4,
        "weight_source": "SOURCE_MEASURED",
        "modified": "2026-10-30T07:12:00Z",
    }
    recorded: list[tuple[str | None, dict[str, object]]] = []

    async def weights(request: web.Request) -> web.Response:
        body = await request.json()
        recorded.append((request.headers.get("Authorization"), body))
        return web.json_response(body)

    dreeve_app = web.Application()
    dreeve_app.router.add_post("/api/v1/athlete/weights", weights)

    polar = TestServer(_polar_app(physical_info))
    dreeve = TestServer(dreeve_app)
    await polar.start_server()
    await dreeve.start_server()
    try:
        monkeypatch.setattr(
            "dreeve_polar_connector.accesslink._http.ACCESSLINK_BASE_URL", f"http://127.0.0.1:{polar.port}"
        )
        tokens = TokenStore(tmp_path / "tokens")
        await tokens.store(
            Authorization(polar_user_id=475, access_token="a-token", member_id="dreeve-475", authorized_at=NOW)
        )
        monkeypatch.setattr(
            "os.environ",
            {
                "POLAR_CLIENT_ID": "a-client",
                "POLAR_CLIENT_SECRET": "a-secret",
                "WATCH_DIR": str(tmp_path / "watch"),
                "STATE_DIR": str(tmp_path / "state"),
                "POLAR_TOKENS": str(tmp_path / "tokens"),
                "DOWNLOAD_DELAY_SECONDS": "0",
                "MAX_CYCLES": "1",
                "HTTP_ADDR": "127.0.0.1:0",
                "WEIGHT_SYNC": "true",
                "DREEVE_URL": f"http://127.0.0.1:{dreeve.port}",
                "DREEVE_API_KEY": DREEVE_KEY,
            },
        )

        # First run: recorded. Second: unchanged, nothing sent. Third: a new weight, recorded again.
        assert await asyncio.to_thread(main, ["run"]) == EXIT_OK
        assert await asyncio.to_thread(main, ["run"]) == EXIT_OK
        physical_info.update(weight=79.8, modified="2026-11-06T06:30:00Z")
        assert await asyncio.to_thread(main, ["run"]) == EXIT_OK

        assert recorded == [
            (f"Bearer {DREEVE_KEY}", {"weight": 80.4, "on": "2026-10-30"}),
            (f"Bearer {DREEVE_KEY}", {"weight": 79.8, "on": "2026-11-06"}),
        ]
        state = json.loads((tmp_path / "state" / WEIGHT_STATE_FILENAME).read_text(encoding="utf-8"))
        assert state["lastPushed"]["kilograms"] == 79.8
        # Exercise sync ran alongside, untouched.
        assert (tmp_path / "watch" / "2AC312F.fit").read_bytes() == FIT_BYTES
        assert DREEVE_KEY not in capsys.readouterr().err
    finally:
        await polar.close()
        await dreeve.close()
