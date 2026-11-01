"""The Dreeve side of weight sync: one call to Dreeve's `/api/v1`, recording a weight for a date.

Deliberately not part of `accesslink`, which must stay ignorant of Dreeve. Only one request exists, so
there is no retry policy here: the next cycle is the retry. What matters is what each answer means,
and the two exceptions split them that way — `DreeveUnreachable` is worth trying again as-is,
`DreeveRejected` needs a human to change something first.

The session is injected, as it is for `AccessLinkClient`: the connector already owns one.
"""

import json
from datetime import date

import aiohttp

WEIGHTS_PATH = "/api/v1/athlete/weights"
# Dreeve is on the same network as the connector; a request that takes longer than this is not going to
# succeed by waiting, and it would hold up the cycle that runs it.
TIMEOUT = aiohttp.ClientTimeout(total=10)
DETAIL_LIMIT = 200


class DreeveError(Exception):
    """Anything that kept a weight from being recorded."""


class DreeveUnreachable(DreeveError):
    """No answer, or a 5xx: nothing to fix on this side, so the same request is worth making next cycle."""


class DreeveRejected(DreeveError):
    """Dreeve answered and said no. Repeating the request unchanged will get the same answer."""

    def __init__(self, status: int, message: str) -> None:
        self.status = status
        super().__init__(message)


class DreeveClient:
    def __init__(self, session: aiohttp.ClientSession, base_url: str, api_key: str) -> None:
        self._session = session
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key

    async def record_weight(self, on: date, weight: float) -> None:
        """Records `weight`, in Dreeve's configured unit, for `on`. Dreeve replaces any value that date had."""
        url = f"{self._base_url}{WEIGHTS_PATH}"
        try:
            async with self._session.post(
                url,
                json={"weight": weight, "on": on.isoformat()},
                headers={"Authorization": f"Bearer {self._api_key}", "Accept": "application/json"},
                timeout=TIMEOUT,
                # A redirect would be followed as a GET of the weight list, which answers 200: a push that
                # looks successful and records nothing. Refused instead, naming where it pointed.
                allow_redirects=False,
            ) as response:
                status = response.status
                body = await response.read()
                location = response.headers.get("Location", "")
        except aiohttp.ClientError as exception:
            raise DreeveUnreachable(f"Could not reach Dreeve at {self._base_url}: {exception}.") from exception
        except TimeoutError as exception:
            raise DreeveUnreachable(f"Dreeve at {self._base_url} did not answer in time.") from exception

        if 200 <= status < 300:
            return

        raise self._failure(status, body, location, url)

    def _failure(self, status: int, body: bytes, location: str, url: str) -> DreeveError:
        if status >= 500:
            return DreeveUnreachable(f"Dreeve answered {status} while recording a weight{self._detail(body)}.")

        if status in (401, 403):
            return DreeveRejected(
                status,
                f"Dreeve rejected DREEVE_API_KEY ({status}). It must be an API key (drv_...) generated in "
                f"Dreeve's admin panel under Settings, Security; an MCP key (drvm_...) does not work here.",
            )

        if 300 <= status < 400:
            return DreeveRejected(
                status,
                f"Dreeve redirected the request ({status}) to {location or 'an unnamed location'}. Set "
                f"DREEVE_URL to the address it redirects to.",
            )

        if status == 404:
            return DreeveRejected(
                status,
                f"Dreeve has no weight API at {url} (404). DREEVE_URL must be Dreeve's base URL, and "
                f"recording weights needs Dreeve v5.3.3 or newer.",
            )

        return DreeveRejected(status, f"Dreeve refused the weight ({status}){self._detail(body)}.")

    def _detail(self, body: bytes) -> str:
        """Dreeve's own message when it sent one. Never anything carrying the API key."""
        text = body.decode("utf-8", errors="replace").strip()
        try:
            payload = json.loads(text)
        except ValueError:
            payload = None
        if isinstance(payload, dict):
            message = payload.get("message") or payload.get("error")
            if isinstance(message, str) and message.strip():
                text = message.strip()

        text = text.replace(self._api_key, "***")[:DETAIL_LIMIT]

        return f": {text}" if text else ""
