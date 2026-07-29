"""The status-to-exception mapping and the retry scope. Both decide daemon behaviour, so both are
asserted directly rather than inferred from higher-level tests."""

import gzip
from collections.abc import Awaitable, Callable

import aiohttp
import pytest
from aiohttp import web

from dreeve_polar_connector.accesslink.client import AccessLinkClient
from dreeve_polar_connector.accesslink.exceptions import (
    AuthenticationFailed,
    ConnectionFailed,
    ConsentRequired,
    NotFound,
    RateLimited,
    UnexpectedResponse,
)
from tests.accesslink.conftest import FakeAccessLink, unused_closed_port

Factory = Callable[[dict[tuple[str, str], object]], Awaitable[FakeAccessLink]]


def _responding(status: int, body: str = "{}") -> Callable[[web.Request], Awaitable[web.Response]]:
    async def handler(request: web.Request) -> web.Response:  # noqa: ARG001 - the request is irrelevant here
        return web.Response(status=status, text=body, content_type="application/json")

    return handler


def _client(polar: FakeAccessLink) -> AccessLinkClient:
    return AccessLinkClient(session=polar.session, token="a-token", base_url=polar.base_url)


@pytest.mark.asyncio
async def test_the_bearer_token_is_sent(polar: Factory) -> None:
    seen: dict[str, str] = {}

    async def handler(request: web.Request) -> web.Response:
        seen.update(request.headers)
        return web.json_response([])

    server = await polar({("GET", "/v3/exercises"): handler})
    await _client(server).list_exercises()

    assert seen["Authorization"] == "Bearer a-token"


@pytest.mark.asyncio
async def test_429_raises_rate_limited(polar: Factory) -> None:
    server = await polar({("GET", "/v3/exercises"): _responding(429, '{"error": "slow down"}')})

    with pytest.raises(RateLimited, match="slow down"):
        await _client(server).list_exercises()


@pytest.mark.asyncio
async def test_429_is_not_retried(polar: Factory) -> None:
    # tenacity must retry ConnectionFailed and nothing else. Replaying a rate-limited request
    # lengthens the ban.
    server = await polar({("GET", "/v3/exercises"): _responding(429)})

    with pytest.raises(RateLimited):
        await _client(server).list_exercises()

    assert server.count_of("GET", "/v3/exercises") == 1


@pytest.mark.asyncio
async def test_401_raises_authentication_failed(polar: Factory) -> None:
    server = await polar({("GET", "/v3/exercises"): _responding(401)})

    with pytest.raises(AuthenticationFailed):
        await _client(server).list_exercises()


@pytest.mark.asyncio
async def test_401_is_not_retried(polar: Factory) -> None:
    server = await polar({("GET", "/v3/exercises"): _responding(401)})

    with pytest.raises(AuthenticationFailed):
        await _client(server).list_exercises()

    assert server.count_of("GET", "/v3/exercises") == 1


@pytest.mark.asyncio
async def test_403_raises_consent_required_and_says_where_to_fix_it(polar: Factory) -> None:
    server = await polar({("GET", "/v3/exercises"): _responding(403)})

    with pytest.raises(ConsentRequired, match="Polar Flow"):
        await _client(server).list_exercises()


@pytest.mark.asyncio
async def test_404_raises_not_found(polar: Factory) -> None:
    server = await polar({("GET", "/v3/exercises/nope/fit"): _responding(404)})

    with pytest.raises(NotFound):
        await _client(server).download_fit("nope")


@pytest.mark.asyncio
async def test_500_raises_connection_failed_and_is_retried(polar: Factory) -> None:
    server = await polar({("GET", "/v3/exercises"): _responding(500)})

    with pytest.raises(ConnectionFailed):
        await _client(server).list_exercises()

    assert server.count_of("GET", "/v3/exercises") == 3


@pytest.mark.asyncio
async def test_an_unexpected_4xx_raises_unexpected_response(polar: Factory) -> None:
    server = await polar({("GET", "/v3/exercises"): _responding(418)})

    with pytest.raises(UnexpectedResponse, match="418"):
        await _client(server).list_exercises()


@pytest.mark.asyncio
async def test_a_genuine_connection_failure_raises_connection_failed_after_retrying() -> None:
    # No status code involved: nothing is listening on this port, so aiohttp raises a real
    # ClientError. _send retries ConnectionFailed three times (tenacity), which is correct - so this
    # is allowed to take a moment - but it must still end in ConnectionFailed, not UnexpectedResponse.
    port = unused_closed_port()
    async with aiohttp.ClientSession() as session:
        client = AccessLinkClient(session=session, token="a-token", base_url=f"http://127.0.0.1:{port}")
        with pytest.raises(ConnectionFailed):
            await client.list_exercises()


@pytest.mark.asyncio
async def test_rate_limit_headers_are_recorded_from_a_successful_response(polar: Factory) -> None:
    async def handler(request: web.Request) -> web.Response:  # noqa: ARG001 - the request is irrelevant here
        return web.json_response([], headers={"RateLimit-Usage": "7,90", "RateLimit-Limit": "520,5100"})

    server = await polar({("GET", "/v3/exercises"): handler})
    client = _client(server)
    await client.list_exercises()

    assert client.rate_limit.short_usage == 7
    assert client.rate_limit.long_limit == 5100


EXERCISE_PAYLOAD = {
    "id": "2AC312F",
    "start_time": "2026-07-28T09:40:02",
    "start_time_utc_offset": 120,
    "sport": "RUNNING",
    "detailed_sport_info": "RUNNING_TREADMILL",
    "device": "Polar Vantage V3",
}


def _json_list(payload: list[dict[str, object]]) -> Callable[[web.Request], Awaitable[web.Response]]:
    async def handler(request: web.Request) -> web.Response:  # noqa: ARG001 - the request is irrelevant here
        return web.json_response(payload)

    return handler


@pytest.mark.asyncio
async def test_exercises_are_parsed(polar: Factory) -> None:
    server = await polar({("GET", "/v3/exercises"): _json_list([EXERCISE_PAYLOAD])})

    exercises = await _client(server).list_exercises()

    assert len(exercises) == 1
    assert exercises[0].exercise_id == "2AC312F"
    assert exercises[0].sport == "RUNNING"


@pytest.mark.asyncio
async def test_an_empty_list_is_not_an_error(polar: Factory) -> None:
    server = await polar({("GET", "/v3/exercises"): _json_list([])})

    assert await _client(server).list_exercises() == ()


@pytest.mark.asyncio
async def test_an_empty_body_is_read_as_no_exercises(polar: Factory) -> None:
    # Polar answers 204 with no body when there is nothing to report.
    async def handler(request: web.Request) -> web.Response:  # noqa: ARG001 - the request is irrelevant here
        return web.Response(status=204)

    server = await polar({("GET", "/v3/exercises"): handler})

    assert await _client(server).list_exercises() == ()


@pytest.mark.asyncio
async def test_an_object_instead_of_a_list_fails_loudly(polar: Factory) -> None:
    async def handler(request: web.Request) -> web.Response:  # noqa: ARG001 - the request is irrelevant here
        return web.json_response({"exercises": []})

    server = await polar({("GET", "/v3/exercises"): handler})

    with pytest.raises(UnexpectedResponse, match="Expected a list"):
        await _client(server).list_exercises()


@pytest.mark.asyncio
async def test_a_malformed_exercise_fails_loudly_and_suggests_the_cause(polar: Factory) -> None:
    # Missing the offset, so the start time cannot be placed on the clock.
    server = await polar(
        {("GET", "/v3/exercises"): _json_list([{"id": "2AC312F", "start_time": "2026-07-28T09:40:02"}])}
    )

    with pytest.raises(UnexpectedResponse, match="the API changed"):
        await _client(server).list_exercises()


@pytest.mark.asyncio
async def test_a_non_json_body_fails_loudly(polar: Factory) -> None:
    async def handler(request: web.Request) -> web.Response:  # noqa: ARG001 - the request is irrelevant here
        return web.Response(text="<html>maintenance</html>", content_type="text/html")

    server = await polar({("GET", "/v3/exercises"): handler})

    with pytest.raises(UnexpectedResponse, match="not valid JSON"):
        await _client(server).list_exercises()


FIT_BYTES = b"\x0e\x10Q\x00.FIT\x00\x00fake fit payload"
TCX_XML = b'<?xml version="1.0"?><TrainingCenterDatabase/>'


def _binary(body: bytes, content_type: str) -> Callable[[web.Request], Awaitable[web.Response]]:
    async def handler(request: web.Request) -> web.Response:  # noqa: ARG001 - the request is irrelevant here
        return web.Response(body=body, content_type=content_type)

    return handler


@pytest.mark.asyncio
async def test_fit_is_returned_verbatim(polar: Factory) -> None:
    server = await polar({("GET", "/v3/exercises/2AC312F/fit"): _binary(FIT_BYTES, "application/octet-stream")})

    assert await _client(server).download_fit("2AC312F") == FIT_BYTES


@pytest.mark.asyncio
async def test_a_gzipped_tcx_body_is_decompressed(polar: Factory) -> None:
    compressed = gzip.compress(TCX_XML)
    server = await polar({("GET", "/v3/exercises/2AC312F/tcx"): _binary(compressed, "application/vnd.garmin.tcx+xml")})

    assert await _client(server).download_tcx("2AC312F") == TCX_XML


@pytest.mark.asyncio
async def test_a_plain_tcx_body_is_left_alone(polar: Factory) -> None:
    # The docs say gzipped. Sniffing rather than trusting means both behaviours are correct.
    server = await polar({("GET", "/v3/exercises/2AC312F/tcx"): _binary(TCX_XML, "application/vnd.garmin.tcx+xml")})

    assert await _client(server).download_tcx("2AC312F") == TCX_XML


@pytest.mark.asyncio
async def test_a_truncated_gzip_body_is_treated_as_a_failed_transfer(polar: Factory) -> None:
    # A half-arrived download looks exactly like this, and the right answer is to fetch it again.
    truncated = gzip.compress(TCX_XML)[:12]
    server = await polar({("GET", "/v3/exercises/2AC312F/tcx"): _binary(truncated, "application/vnd.garmin.tcx+xml")})

    with pytest.raises(ConnectionFailed, match="incomplete"):
        await _client(server).download_tcx("2AC312F")


@pytest.mark.asyncio
async def test_gpx_is_returned_verbatim(polar: Factory) -> None:
    gpx = b'<?xml version="1.0"?><gpx/>'
    server = await polar({("GET", "/v3/exercises/2AC312F/gpx"): _binary(gpx, "application/gpx+xml")})

    assert await _client(server).download_gpx("2AC312F") == gpx


@pytest.mark.asyncio
async def test_a_missing_file_raises_not_found_and_is_not_retried(polar: Factory) -> None:
    server = await polar({("GET", "/v3/exercises/2AC312F/fit"): _responding(404)})

    with pytest.raises(NotFound):
        await _client(server).download_fit("2AC312F")

    assert server.count_of("GET", "/v3/exercises/2AC312F/fit") == 1


@pytest.mark.asyncio
async def test_registering_a_new_user_reports_it_as_new(polar: Factory) -> None:
    async def handler(request: web.Request) -> web.Response:
        assert await request.json() == {"member-id": "dreeve-475"}
        return web.json_response({"polar-user-id": 475, "member-id": "dreeve-475"})

    server = await polar({("POST", "/v3/users"): handler})

    assert await _client(server).register_user("dreeve-475") is True


@pytest.mark.asyncio
async def test_an_already_registered_user_is_success_not_failure(polar: Factory) -> None:
    server = await polar({("POST", "/v3/users"): _responding(409, '{"error": "already registered"}')})

    assert await _client(server).register_user("dreeve-475") is False


@pytest.mark.asyncio
async def test_a_409_bodys_detail_is_kept_rather_than_discarded(
    polar: Factory, caplog: pytest.LogCaptureFixture
) -> None:
    # Polar's 409 covers both "already registered" and "duplicate member-id". Discarding the body
    # before logging would report a genuine misconfiguration as reassuring news.
    server = await polar({("POST", "/v3/users"): _responding(409, '{"error": "duplicate member-id"}')})

    with caplog.at_level("INFO"):
        await _client(server).register_user("dreeve-475")

    assert "duplicate member-id" in caplog.text


@pytest.mark.asyncio
async def test_a_rejected_registration_still_raises(polar: Factory) -> None:
    server = await polar({("POST", "/v3/users"): _responding(403)})

    with pytest.raises(ConsentRequired):
        await _client(server).register_user("dreeve-475")
