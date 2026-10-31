"""Environment configuration.

Everything the connector can be told to do arrives as an environment variable, and every one of them is
validated here, at construction. A misconfigured container must fail on boot with a readable message
rather than halfway through its first sync cycle — and it must report every problem at once, because
fixing environment variables one boot at a time is misery.
"""

import ipaddress
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path

SECONDS_PER_UNIT = {"h": 3600, "d": 86400}
RELATIVE_SINCE_PATTERN = re.compile(r"^-?(?P<amount>\d+)(?P<unit>[hd])$")
BOOLEANS = {"true": True, "1": True, "yes": True, "on": True, "false": False, "0": False, "no": False, "off": False}
HTTP_ADDR_DISABLED = "off"
# Polar returns exercises uploaded in the last 30 days and no earlier, so this is "everything there is".
DEFAULT_SINCE = "-30d"
CALLBACK_PATH = "/callback"


class InvalidConfiguration(Exception):
    """Raised with every problem found, not just the first one."""

    def __init__(self, errors: Sequence[str]) -> None:
        self.errors = tuple(errors)
        super().__init__("\n".join(["Invalid configuration:", *(f"  - {error}" for error in errors)]))


class FallbackFormat(StrEnum):
    TCX = "tcx"
    GPX = "gpx"
    NONE = "none"


class ConflictPolicy(StrEnum):
    SKIP = "skip"
    OVERWRITE = "overwrite"


class LogFormat(StrEnum):
    TEXT = "text"
    JSON = "json"


class WeightUnit(StrEnum):
    """The unit Dreeve reads a weight in: its Appearance unit system, which its API does not expose."""

    KG = "kg"
    LB = "lb"


@dataclass(frozen=True)
class Since:
    """The lower bound of the very first sync, either an absolute instant or an offset from "now".

    It stays unresolved on purpose: `SINCE=now` must resolve exactly once, on first boot, and then live
    in the ledger as `resolvedSince`. Resolving it here would silently skip every exercise recorded
    while the container was down.
    """

    value: datetime | timedelta

    def resolve(self, now: datetime) -> datetime:
        if isinstance(self.value, timedelta):
            return now - self.value

        return self.value


@dataclass(frozen=True)
class Config:
    client_id: str
    client_secret: str
    public_url: str | None
    redirect_uri_override: str | None
    watch_dir: Path
    state_dir: Path
    tokens_dir: Path
    since: Since
    sports: tuple[str, ...]
    fallback_format: FallbackFormat
    on_conflict: ConflictPolicy
    max_attempts: int
    poll_interval: int
    poll_jitter_pct: int
    max_downloads_per_cycle: int
    download_delay_seconds: float
    max_backoff_seconds: int
    webhook_url: str | None
    webhook_addr: str | None
    http_addr: str | None
    max_cycles: int
    log_level: str
    log_format: LogFormat
    dry_run: bool
    weight_sync: bool = False
    dreeve_url: str | None = None
    dreeve_api_key: str | None = None
    dreeve_weight_unit: WeightUnit = WeightUnit.KG

    @property
    def redirect_uri(self) -> str | None:
        """Where Polar sends the browser back. None means "use the client's only registered URL".

        `REDIRECT_URI` wins over the value derived from `PUBLIC_URL`. It exists because a client's
        registered URL is not always `<public_url>/callback`: Polar's admin page accepts a path, but its
        authorization endpoint has been observed rejecting the result, leaving the bare origin as the
        only value it will honour. The server answers the callback at `/` as well for that reason, so
        `REDIRECT_URI=http://host:8080` completes the flow.
        """
        if self.redirect_uri_override:
            return self.redirect_uri_override

        return f"{self.public_url}{CALLBACK_PATH}" if self.public_url else None

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "Config":
        reader = _EnvReader(env)

        http_addr = reader.http_addr("HTTP_ADDR", default="0.0.0.0:8080")
        webhook_addr = reader.http_addr("WEBHOOK_ADDR", default=HTTP_ADDR_DISABLED)
        webhook_url = reader.text("WEBHOOK_URL")
        if webhook_url and webhook_addr is None:
            reader.errors.append(
                "WEBHOOK_URL is set but WEBHOOK_ADDR is 'off', so nothing would be listening for Polar's "
                "deliveries. Polar deactivates a webhook after seven days of failures."
            )
        if _listeners_collide(http_addr, webhook_addr):
            reader.errors.append(
                f"HTTP_ADDR ('{http_addr}') and WEBHOOK_ADDR ('{webhook_addr}') want the same port, so the "
                f"second listener could not bind. Give the webhook listener its own port, conventionally "
                f"0.0.0.0:8081."
            )

        # Opt-in. The Dreeve settings are read either way, so they can be staged before the switch is
        # flipped, but only required and checked once it is on.
        weight_sync = reader.flag("WEIGHT_SYNC", default=False)
        dreeve_url = reader.url("DREEVE_URL")
        dreeve_api_key = reader.optional_secret("DREEVE_API_KEY")
        if weight_sync:
            if dreeve_url is None:
                reader.errors.append(
                    "WEIGHT_SYNC is on but DREEVE_URL is not set. Point it at Dreeve's base URL, "
                    "e.g. http://dreeve:8080."
                )
            elif not dreeve_url.startswith(("http://", "https://")):
                reader.errors.append(f"DREEVE_URL must start with http:// or https://, got '{dreeve_url}'.")
            # None means unset; "" means a DREEVE_API_KEY_FILE problem that is already reported.
            if dreeve_api_key is None:
                reader.errors.append(
                    "WEIGHT_SYNC is on but DREEVE_API_KEY is not set. Generate a key in Dreeve's admin "
                    "panel under Settings, Security, or point DREEVE_API_KEY_FILE at a file containing it."
                )

        config = cls(
            client_id=reader.required_secret("POLAR_CLIENT_ID"),
            client_secret=reader.required_secret("POLAR_CLIENT_SECRET"),
            public_url=reader.url("PUBLIC_URL"),
            # Verbatim, unlike PUBLIC_URL: Polar compares a redirect URI byte for byte against what is
            # registered, so a trailing slash this reader helpfully stripped would fail the match.
            redirect_uri_override=reader.text("REDIRECT_URI"),
            watch_dir=reader.path("WATCH_DIR", default="/watch"),
            state_dir=reader.path("STATE_DIR", default="/state"),
            tokens_dir=reader.path("POLAR_TOKENS", default="/tokens"),
            since=reader.since("SINCE", default=DEFAULT_SINCE),  # never None; see `_EnvReader.since`
            sports=reader.csv("SPORTS"),
            fallback_format=reader.choice("FALLBACK_FORMAT", FallbackFormat, default=FallbackFormat.TCX),
            on_conflict=reader.choice("ON_CONFLICT", ConflictPolicy, default=ConflictPolicy.SKIP),
            max_attempts=reader.integer("MAX_ATTEMPTS", default=5, minimum=1),
            poll_interval=reader.integer("POLL_INTERVAL", default=900, minimum=1),
            poll_jitter_pct=reader.integer("POLL_JITTER_PCT", default=10, minimum=0, maximum=100),
            max_downloads_per_cycle=reader.integer("MAX_DOWNLOADS_PER_CYCLE", default=25, minimum=1),
            download_delay_seconds=reader.number("DOWNLOAD_DELAY_SECONDS", default=1.0, minimum=0.0),
            max_backoff_seconds=reader.integer("MAX_BACKOFF_SECONDS", default=21600, minimum=0),
            webhook_url=webhook_url,
            webhook_addr=webhook_addr,
            http_addr=http_addr,
            max_cycles=reader.integer("MAX_CYCLES", default=0, minimum=0),
            log_level=reader.log_level("LOG_LEVEL", default="info"),
            log_format=reader.choice("LOG_FORMAT", LogFormat, default=LogFormat.TEXT),
            dry_run=reader.flag("DRY_RUN", default=False),
            weight_sync=weight_sync,
            dreeve_url=dreeve_url,
            dreeve_api_key=dreeve_api_key or None,
            dreeve_weight_unit=reader.choice("DREEVE_WEIGHT_UNIT", WeightUnit, default=WeightUnit.KG),
        )

        if reader.errors:
            raise InvalidConfiguration(reader.errors)

        return config


def _listeners_collide(http_addr: str | None, webhook_addr: str | None) -> bool:
    """Whether the two listeners would fight over one port.

    Reported here rather than left to `bind`, which raises `OSError` from inside `run` once the first
    listener is already up — a traceback halfway through startup, which is exactly what validating
    configuration on boot exists to avoid. Port 0 is exempt: it means "any free port", so two of them
    cannot collide.
    """
    if http_addr is None or webhook_addr is None:
        return False

    local_host, _, local_port = http_addr.rpartition(":")
    webhook_host, _, webhook_port = webhook_addr.rpartition(":")
    if local_port != webhook_port or local_port == "0":
        return False

    # Identical hosts obviously clash, and so does a wildcard against anything: binding 0.0.0.0:8080
    # takes the port on every interface, so 127.0.0.1:8080 can no longer have it.
    return local_host == webhook_host or _every_interface(local_host) or _every_interface(webhook_host)


def _every_interface(host: str) -> bool:
    """True for '0.0.0.0', '::' and '' — the addresses that claim a port on every interface."""
    if not host:
        return True
    try:
        return ipaddress.ip_address(host).is_unspecified
    except ValueError:
        return False


class _EnvReader:
    """Reads and validates one variable at a time, collecting failures instead of raising on the first."""

    def __init__(self, env: Mapping[str, str]) -> None:
        self._env = env
        self.errors: list[str] = []

    def text(self, key: str) -> str | None:
        return self._env.get(key, "").strip() or None

    def required_secret(self, key: str) -> str:
        """Supports the `_FILE` convention so credentials can come from a Docker secret."""
        value = self.optional_secret(key)
        if value is None:
            self.errors.append(
                f"{key} is required. Create a client at https://admin.polaraccesslink.com and set it, "
                f"or point {key}_FILE at a file containing it."
            )
            return ""

        return value

    def optional_secret(self, key: str) -> str | None:
        """None when neither `key` nor `key_FILE` is set; "" when one is set but unusable (reported)."""
        file_key = f"{key}_FILE"
        inline = self._env.get(key, "").strip()
        secret_file = self._env.get(file_key, "").strip()

        if inline and secret_file:
            self.errors.append(f"{key} and {file_key} are both set; use one or the other.")
            return ""

        if secret_file:
            try:
                # A file written by `echo` ends in a newline, which would end up in the auth header.
                contents = Path(secret_file).read_text(encoding="utf-8").strip()
            except OSError as exception:
                self.errors.append(f"{file_key} points at '{secret_file}', which could not be read: {exception}.")
                return ""
            if not contents:
                self.errors.append(f"{file_key} points at '{secret_file}', which is empty.")
                return ""
            return contents

        return inline or None

    def url(self, key: str) -> str | None:
        """A trailing slash would produce '//callback', which will not match a registered redirect URI."""
        raw = self.text(key)

        return raw.rstrip("/") if raw else None

    def flag(self, key: str, *, default: bool) -> bool:
        raw = self._env.get(key, "").strip().lower()
        if not raw:
            return default
        if raw not in BOOLEANS:
            self.errors.append(f"{key} must be a boolean (true/false), got '{raw}'.")
            return default

        return BOOLEANS[raw]

    def integer(self, key: str, *, default: int, minimum: int | None = None, maximum: int | None = None) -> int:
        raw = self._env.get(key, "").strip()
        if not raw:
            return default
        try:
            value = int(raw)
        except ValueError:
            self.errors.append(f"{key} must be a whole number, got '{raw}'.")
            return default

        return self._within_bounds(key, value, minimum, maximum, default)

    def number(self, key: str, *, default: float, minimum: float | None = None) -> float:
        raw = self._env.get(key, "").strip()
        if not raw:
            return default
        try:
            value = float(raw)
        except ValueError:
            self.errors.append(f"{key} must be a number, got '{raw}'.")
            return default

        return self._within_bounds(key, value, minimum, None, default)

    def _within_bounds[T: (int, float)](
        self, key: str, value: T, minimum: T | None, maximum: T | None, default: T
    ) -> T:
        if minimum is not None and value < minimum:
            self.errors.append(f"{key} must be {minimum} or higher, got {value}.")
            return default
        if maximum is not None and value > maximum:
            self.errors.append(f"{key} must be {maximum} or lower, got {value}.")
            return default

        return value

    def choice[E: StrEnum](self, key: str, options: type[E], *, default: E) -> E:
        raw = self._env.get(key, "").strip().lower()
        if not raw:
            return default
        try:
            return options(raw)
        except ValueError:
            allowed = "|".join(option.value for option in options)
            self.errors.append(f"{key} must be one of {allowed}, got '{raw}'.")
            return default

    def csv(self, key: str) -> tuple[str, ...]:
        raw = self._env.get(key, "")

        return tuple(item.strip().lower() for item in raw.split(",") if item.strip())

    def path(self, key: str, *, default: str) -> Path:
        return Path(self._env.get(key, "").strip() or default)

    def log_level(self, key: str, *, default: str) -> str:
        raw = self._env.get(key, "").strip().upper()
        if not raw:
            return default.upper()
        if raw not in logging.getLevelNamesMapping():
            allowed = "|".join(("debug", "info", "warning", "error", "critical"))
            self.errors.append(f"{key} must be one of {allowed}, got '{raw.lower()}'.")
            return default.upper()

        return raw

    def http_addr(self, key: str, *, default: str) -> str | None:
        raw = self._env.get(key, "").strip() or default
        if raw.lower() == HTTP_ADDR_DISABLED:
            return None

        host, separator, port = raw.rpartition(":")
        if not separator or not host:
            self.errors.append(f"{key} must be 'host:port' or '{HTTP_ADDR_DISABLED}', got '{raw}'.")
            return default
        # 0 is a legitimate request to bind: "let the OS pick a free port", which is exactly what an
        # end-to-end test needs to avoid colliding with whatever else is already listening on 8080.
        if not port.isdigit() or not 0 <= int(port) <= 65535:
            self.errors.append(f"{key} must carry a port between 0 and 65535, got '{raw}'.")
            return default

        return raw

    def since(self, key: str, *, default: str) -> Since:
        """Never returns None. On a parse failure the error is recorded, so the fallback never escapes
        `from_env` — which spares every caller a `None` check on a value that always has a default."""
        raw = self._env.get(key, "").strip() or default

        if raw.lower() == "now":
            return Since(timedelta(0))

        relative = RELATIVE_SINCE_PATTERN.match(raw.lower())
        if relative:
            amount = int(relative.group("amount"))
            return Since(timedelta(seconds=amount * SECONDS_PER_UNIT[relative.group("unit")]))

        try:
            instant = datetime.fromisoformat(raw)
        except ValueError:
            self.errors.append(
                f"{key} must be a date (2026-01-01), an ISO instant, a relative offset (-30d, 720h) "
                f"or 'now', got '{raw}'."
            )
            return Since(timedelta(days=30))

        # A bare date or a naive instant is read as UTC; every comparison in this project is in UTC.
        return Since(instant if instant.tzinfo else instant.replace(tzinfo=UTC))
