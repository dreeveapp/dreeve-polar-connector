"""The user-token half of AccessLink's HTTP: exercises, their files, and user registration.

Everything here funnels through `_send`, which means the retry policy exists exactly once. The
status-to-exception mapping itself lives in `_http.py`, shared with `webhooks.py`. `tenacity` retries
`ConnectionFailed` and nothing else: replaying a rate-limited request lengthens the ban, and replaying
a rejected token is what gets accounts locked.

The session is injected rather than created. A library that owns a connection pool is one you fight in
tests and in shutdown.
"""

import gzip
import json
import logging

import aiohttp
from pydantic import ValidationError
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from dreeve_polar_connector.accesslink import _http
from dreeve_polar_connector.accesslink._http import error_detail, raise_for_status
from dreeve_polar_connector.accesslink.exceptions import ConnectionFailed, ConsentRequired, UnexpectedResponse
from dreeve_polar_connector.accesslink.models import Exercise, RateLimit

GZIP_MAGIC = b"\x1f\x8b"
RETRY_ATTEMPTS = 3

logger = logging.getLogger(__name__)


class AccessLinkClient:
    """The user-scoped half of AccessLink: exercises, their files, and user registration.

    Webhook and OAuth calls use client credentials rather than a user token, so they live in
    `webhooks.py` and `oauth.py` instead of here.
    """

    def __init__(self, session: aiohttp.ClientSession, token: str, base_url: str | None = None) -> None:
        self._session = session
        self._token = token
        # Resolved here rather than bound as a default argument, and read off the `_http` module object
        # rather than through a `from ... import ACCESSLINK_BASE_URL` name: either one would be fixed
        # at import time, which would make it impossible to redirect at a fake server in tests, or at
        # Polar's sandbox in a real deployment.
        self._base_url = (base_url or _http.ACCESSLINK_BASE_URL).rstrip("/")
        self._rate_limit = RateLimit()

    @property
    def rate_limit(self) -> RateLimit:
        """What Polar said about our budget on the most recent response."""
        return self._rate_limit

    async def list_exercises(self) -> tuple[Exercise, ...]:
        """Every exercise Polar still holds: the last 30 days, and only since this client was
        connected. There are no date parameters to narrow it — a caller that wants incremental sync
        has to track what it has already seen."""
        _, body = await self._send("GET", "/v3/exercises")

        return _exercises_from(body)

    async def register_user(self, member_id: str) -> bool:
        """Registers this token's user with the client. Returns False when they already were.

        A 409 means "already registered, or duplicate member-id". Both are fine: the token in hand is
        what grants access, and the registration is idempotent from our side. The body is kept and
        logged rather than discarded — the two causes are only distinguishable by reading it, and a
        genuine misconfiguration deserves better evidence than a reassuring log line.
        """
        status, body = await self._send(
            "POST",
            "/v3/users",
            json_body={"member-id": member_id},
            allow_status=(200, 409),
        )
        if status == 409:
            logger.info("This Polar user is already registered with this client: %s", error_detail(body))
            return False

        return True

    async def download_fit(self, exercise_id: str) -> bytes:
        _, body = await self._send("GET", f"/v3/exercises/{exercise_id}/fit", accept="*/*")

        return body

    async def download_tcx(self, exercise_id: str) -> bytes:
        """Polar serves TCX gzip-compressed as content, so the body is sniffed rather than trusted."""
        _, body = await self._send("GET", f"/v3/exercises/{exercise_id}/tcx", accept="*/*")

        return _decompressed(body)

    async def download_gpx(self, exercise_id: str) -> bytes:
        _, body = await self._send("GET", f"/v3/exercises/{exercise_id}/gpx", accept="*/*")

        return body

    @retry(
        retry=retry_if_exception_type(ConnectionFailed),
        stop=stop_after_attempt(RETRY_ATTEMPTS),
        wait=wait_exponential(multiplier=0.2, max=2),
        reraise=True,
    )
    async def _send(
        self,
        method: str,
        path: str,
        *,
        accept: str = "application/json",
        json_body: dict[str, str] | None = None,
        allow_status: tuple[int, ...] = (),
    ) -> tuple[int, bytes]:
        headers = {"Authorization": f"Bearer {self._token}", "Accept": accept}
        try:
            async with self._session.request(
                method, f"{self._base_url}{path}", headers=headers, json=json_body
            ) as response:
                body = await response.read()
                self._rate_limit = RateLimit.from_headers(response.headers)
                status = response.status
        except aiohttp.ClientError as exception:
            raise ConnectionFailed(f"Could not reach Polar for {path}: {exception}.") from exception
        except TimeoutError as exception:
            raise ConnectionFailed(f"Polar timed out on {path}: {exception}.") from exception

        self._warn_if_nearly_out_of_budget()
        if status not in allow_status:
            raise_for_status(status, path, body, forbidden=ConsentRequired)

        return status, body

    def _warn_if_nearly_out_of_budget(self) -> None:
        used = self._rate_limit.worst_fraction_used
        if used is not None and used >= 0.8:
            logger.warning(
                "Polar rate-limit budget is %.0f%% used (15min %s/%s, 24h %s/%s)",
                used * 100,
                self._rate_limit.short_usage,
                self._rate_limit.short_limit,
                self._rate_limit.long_usage,
                self._rate_limit.long_limit,
            )


def _decompressed(payload: bytes) -> bytes:
    """Sniffs the magic bytes. The documentation says gzip; trusting it would break the day it stops."""
    if not payload.startswith(GZIP_MAGIC):
        return payload

    try:
        return gzip.decompress(payload)
    except (OSError, EOFError, gzip.BadGzipFile) as exception:
        # A truncated download looks exactly like this, and the same request is worth making again —
        # so this is ConnectionFailed rather than UnexpectedResponse.
        raise ConnectionFailed(f"The TCX body was gzipped but arrived incomplete: {exception}.") from exception


def _exercises_from(body: bytes) -> tuple[Exercise, ...]:
    """An empty body is an empty list, not an error: Polar answers 204 when there is nothing."""
    if not body.strip():
        return ()

    try:
        payload = json.loads(body)
    except ValueError as exception:
        raise UnexpectedResponse(f"Polar's exercise list was not valid JSON: {exception}.") from exception

    if not isinstance(payload, list):
        raise UnexpectedResponse(f"Expected a list of exercises, got {type(payload).__name__}.")

    try:
        return tuple(Exercise.model_validate(item) for item in payload)
    except ValidationError as exception:
        raise UnexpectedResponse(
            f"Polar returned an exercise this connector cannot read: {exception}. "
            f"This usually means the API changed; check for a newer connector."
        ) from exception
