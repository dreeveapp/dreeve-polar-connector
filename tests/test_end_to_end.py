"""One test that exercises the whole wiring, because every other test in this suite stubs something.

A fake Polar serves the endpoints the connector uses; `cli.run` is driven with MAX_CYCLES=1 so it
completes on its own.
"""

import asyncio
import gzip
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from dreeve_polar_connector.cli import EXIT_OK, main
from dreeve_polar_connector.ledger import LEDGER_FILENAME
from dreeve_polar_connector.tokens import Authorization, TokenStore

NOW = datetime(2026, 7, 29, 8, 0, tzinfo=UTC)
FIT_BYTES = b"\x0e\x10Q\x00.FITfake"


def _polar_app() -> web.Application:
    async def exercises(request: web.Request) -> web.Response:  # noqa: ARG001 - no query is used
        return web.json_response(
            [
                {
                    "id": "2AC312F",
                    "start_time": "2026-07-28T09:40:02",
                    "start_time_utc_offset": 120,
                    "sport": "RUNNING",
                    "detailed_sport_info": "RUNNING_TREADMILL",
                    "device": "Polar Vantage V3",
                },
                {
                    "id": "NOFIT01",
                    "start_time": "2026-07-28T10:40:02",
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

    app = web.Application()
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
