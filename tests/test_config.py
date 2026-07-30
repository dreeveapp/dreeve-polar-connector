"""Configuration is the first thing a misconfigured container hits, so the messages matter as much as
the parsing. Every test that asserts a failure also asserts the message names the variable."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from dreeve_polar_connector.config import (
    Config,
    ConflictPolicy,
    FallbackFormat,
    InvalidConfiguration,
    LogFormat,
)

MINIMAL = {"POLAR_CLIENT_ID": "a-client", "POLAR_CLIENT_SECRET": "a-secret"}


def test_the_minimal_environment_is_enough() -> None:
    config = Config.from_env(MINIMAL)

    assert config.client_id == "a-client"
    assert config.client_secret == "a-secret"


def test_defaults_match_the_documented_ones() -> None:
    config = Config.from_env(MINIMAL)

    assert config.watch_dir == Path("/watch")
    assert config.state_dir == Path("/state")
    assert config.tokens_dir == Path("/tokens")
    assert config.poll_interval == 900
    assert config.poll_jitter_pct == 10
    assert config.max_downloads_per_cycle == 25
    assert config.download_delay_seconds == 1.0
    assert config.max_attempts == 5
    assert config.max_backoff_seconds == 21600
    assert config.fallback_format is FallbackFormat.TCX
    assert config.on_conflict is ConflictPolicy.SKIP
    assert config.http_addr == "0.0.0.0:8080"
    assert config.webhook_url is None
    assert config.public_url is None
    assert config.max_cycles == 0
    assert config.log_format is LogFormat.TEXT
    assert config.dry_run is False
    assert config.sports == ()


def test_a_missing_client_id_is_reported() -> None:
    with pytest.raises(InvalidConfiguration, match="POLAR_CLIENT_ID"):
        Config.from_env({"POLAR_CLIENT_SECRET": "a-secret"})


def test_every_problem_is_reported_at_once() -> None:
    with pytest.raises(InvalidConfiguration) as raised:
        Config.from_env({"POLL_INTERVAL": "soon", "MAX_ATTEMPTS": "-1"})

    message = str(raised.value)
    assert "POLAR_CLIENT_ID" in message
    assert "POLAR_CLIENT_SECRET" in message
    assert "POLL_INTERVAL" in message
    assert "MAX_ATTEMPTS" in message


def test_a_secret_can_come_from_a_file(tmp_path: Path) -> None:
    secret_file = tmp_path / "secret"
    # A file written with `echo` ends in a newline, which would otherwise land in the auth header.
    secret_file.write_text("a-secret\n", encoding="utf-8")

    config = Config.from_env({"POLAR_CLIENT_ID": "a-client", "POLAR_CLIENT_SECRET_FILE": str(secret_file)})

    assert config.client_secret == "a-secret"


def test_setting_both_a_secret_and_its_file_is_an_error(tmp_path: Path) -> None:
    secret_file = tmp_path / "secret"
    secret_file.write_text("a-secret", encoding="utf-8")

    with pytest.raises(InvalidConfiguration, match="both"):
        Config.from_env({**MINIMAL, "POLAR_CLIENT_SECRET_FILE": str(secret_file)})


def test_an_unreadable_secret_file_is_reported() -> None:
    with pytest.raises(InvalidConfiguration, match="could not be read"):
        Config.from_env({"POLAR_CLIENT_ID": "a-client", "POLAR_CLIENT_SECRET_FILE": "/nope/secret"})


def test_an_empty_secret_file_is_reported(tmp_path: Path) -> None:
    secret_file = tmp_path / "secret"
    secret_file.write_text("   ", encoding="utf-8")

    with pytest.raises(InvalidConfiguration, match="empty"):
        Config.from_env({"POLAR_CLIENT_ID": "a-client", "POLAR_CLIENT_SECRET_FILE": str(secret_file)})


@pytest.mark.parametrize("raw,expected", [("true", True), ("1", True), ("yes", True), ("off", False), ("0", False)])
def test_booleans_accept_the_usual_spellings(raw: str, expected: bool) -> None:
    assert Config.from_env({**MINIMAL, "DRY_RUN": raw}).dry_run is expected


def test_a_nonsense_boolean_is_reported() -> None:
    with pytest.raises(InvalidConfiguration, match="DRY_RUN"):
        Config.from_env({**MINIMAL, "DRY_RUN": "perhaps"})


def test_sports_are_lowercased_and_split() -> None:
    config = Config.from_env({**MINIMAL, "SPORTS": "Running, CYCLING ,"})

    assert config.sports == ("running", "cycling")


def test_since_accepts_a_relative_offset() -> None:
    config = Config.from_env({**MINIMAL, "SINCE": "-14d"})

    assert config.since.value == timedelta(days=14)


def test_since_accepts_hours() -> None:
    config = Config.from_env({**MINIMAL, "SINCE": "720h"})

    assert config.since.value == timedelta(hours=720)


def test_since_accepts_now() -> None:
    config = Config.from_env({**MINIMAL, "SINCE": "now"})

    assert config.since.value == timedelta(0)


def test_since_accepts_a_bare_date_and_reads_it_as_utc() -> None:
    config = Config.from_env({**MINIMAL, "SINCE": "2026-01-01"})

    assert config.since.value == datetime(2026, 1, 1, tzinfo=UTC)


def test_since_defaults_to_the_whole_window_polar_offers() -> None:
    # Polar keeps 30 days, so this is "everything available" rather than an arbitrary choice.
    config = Config.from_env(MINIMAL)

    assert config.since.value == timedelta(days=30)


def test_a_nonsense_since_is_reported() -> None:
    with pytest.raises(InvalidConfiguration, match="SINCE"):
        Config.from_env({**MINIMAL, "SINCE": "whenever"})


def test_since_resolves_a_relative_offset_against_now() -> None:
    config = Config.from_env({**MINIMAL, "SINCE": "-2d"})
    now = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)

    assert config.since.resolve(now) == datetime(2026, 7, 27, 12, 0, tzinfo=UTC)


def test_since_resolves_an_absolute_instant_to_itself() -> None:
    config = Config.from_env({**MINIMAL, "SINCE": "2026-01-01"})
    now = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)

    assert config.since.resolve(now) == datetime(2026, 1, 1, tzinfo=UTC)


def test_http_addr_can_be_switched_off() -> None:
    assert Config.from_env({**MINIMAL, "HTTP_ADDR": "off"}).http_addr is None


def test_an_http_addr_without_a_port_is_reported() -> None:
    with pytest.raises(InvalidConfiguration, match="HTTP_ADDR"):
        Config.from_env({**MINIMAL, "HTTP_ADDR": "0.0.0.0"})


def test_an_out_of_range_port_is_reported() -> None:
    with pytest.raises(InvalidConfiguration, match="port"):
        Config.from_env({**MINIMAL, "HTTP_ADDR": "0.0.0.0:70000"})


def test_a_webhook_without_a_server_to_receive_it_is_reported() -> None:
    # Polar POSTs to the connector's own HTTP server. With HTTP_ADDR off there is nothing listening,
    # so the webhook would be registered and then fail every delivery for seven days.
    with pytest.raises(InvalidConfiguration, match="WEBHOOK_URL"):
        Config.from_env({**MINIMAL, "HTTP_ADDR": "off", "WEBHOOK_URL": "https://example.test/webhook"})


def test_a_public_url_loses_its_trailing_slash() -> None:
    # It is concatenated with '/callback', and '//callback' would not match the registered redirect URI.
    config = Config.from_env({**MINIMAL, "PUBLIC_URL": "http://nas.local:8080/"})

    assert config.public_url == "http://nas.local:8080"


def test_the_redirect_uri_is_derived_from_the_public_url() -> None:
    config = Config.from_env({**MINIMAL, "PUBLIC_URL": "http://nas.local:8080"})

    assert config.redirect_uri == "http://nas.local:8080/callback"


def test_there_is_no_redirect_uri_without_a_public_url() -> None:
    # Polar then falls back to the client's only registered URL.
    assert Config.from_env(MINIMAL).redirect_uri is None


def test_an_explicit_redirect_uri_overrides_the_derived_one() -> None:
    # For clients whose registered URL is not '<public_url>/callback' - Polar's admin page takes a path
    # but its authorization endpoint has been seen to accept only the bare origin.
    config = Config.from_env(
        {**MINIMAL, "PUBLIC_URL": "http://nas.local:8080", "REDIRECT_URI": "http://nas.local:8080"}
    )

    assert config.redirect_uri == "http://nas.local:8080"


def test_an_explicit_redirect_uri_works_without_a_public_url() -> None:
    config = Config.from_env({**MINIMAL, "REDIRECT_URI": "http://nas.local:8080"})

    assert config.redirect_uri == "http://nas.local:8080"


def test_an_explicit_redirect_uri_keeps_its_trailing_slash() -> None:
    # Unlike PUBLIC_URL, this value is not tidied up: Polar matches it byte for byte against what is
    # registered, and a registered URL may well end in a slash.
    config = Config.from_env({**MINIMAL, "REDIRECT_URI": "http://nas.local:8080/"})

    assert config.redirect_uri == "http://nas.local:8080/"


def test_a_jitter_above_one_hundred_percent_is_reported() -> None:
    with pytest.raises(InvalidConfiguration, match="POLL_JITTER_PCT"):
        Config.from_env({**MINIMAL, "POLL_JITTER_PCT": "150"})


def test_an_unknown_fallback_format_is_reported() -> None:
    with pytest.raises(InvalidConfiguration, match="FALLBACK_FORMAT"):
        Config.from_env({**MINIMAL, "FALLBACK_FORMAT": "csv"})


def test_an_unknown_log_level_is_reported() -> None:
    with pytest.raises(InvalidConfiguration, match="LOG_LEVEL"):
        Config.from_env({**MINIMAL, "LOG_LEVEL": "chatty"})
