"""HttpGooglePlayClient against a mocked network: the signed service-account token request,
token caching, the purchase and acknowledge calls, and how each of Google's answers maps to
an error. Never calls Google."""

import asyncio
import base64
import json
import logging
from collections.abc import Callable
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest
from google.auth import jwt

from services.google_play_client import (
    ANDROID_PUBLISHER_URL,
    SCOPE,
    GooglePlayUnavailableError,
    HttpGooglePlayClient,
    PlayAcknowledgeRefusedError,
    PlayPurchaseNotFoundError,
    parse_service_account,
)
from tests.google_play_fakes import PACKAGE, PRODUCT, SERVICE_EMAIL, TOKEN_URI, private_key_pem, public_key_pem, service_account_json

TOKEN = "opaque.token/with+odd=chars"
PURCHASE = {
    "kind": "androidpublisher#productPurchase",
    "purchaseTimeMillis": "1793873762000",
    "purchaseState": 0,
    "consumptionState": 0,
    "orderId": "GPA.3301-1111-2222-33333",
    "purchaseType": 0,
    "acknowledgementState": 0,
    "obfuscatedExternalAccountId": "8d6a1e0e-3c2f-4f7e-9d1b-2b2f1a7c9e10",
    "regionCode": "IN",
}


class FakeGoogle:
    """Answers the token endpoint and the purchase API, and records what was asked."""

    def __init__(self, purchase_status: int = 200, purchase_body: Any = None, token_status: int = 200, ack_status: int = 204) -> None:
        self.purchase_status = purchase_status
        self.purchase_body = PURCHASE if purchase_body is None else purchase_body
        self.token_status = token_status
        self.ack_status = ack_status
        self.token_requests: list[dict[str, list[str]]] = []
        self.api_requests: list[httpx.Request] = []
        self.issued = 0

    def handle(self, request: httpx.Request) -> httpx.Response:
        if str(request.url) == TOKEN_URI:
            self.token_requests.append(parse_qs(request.content.decode()))
            self.issued += 1
            return httpx.Response(self.token_status, json={"access_token": f"access-{self.issued}", "expires_in": 3599, "token_type": "Bearer"})
        self.api_requests.append(request)
        if request.method == "POST":
            return httpx.Response(self.ack_status)
        if isinstance(self.purchase_body, bytes):
            return httpx.Response(self.purchase_status, content=self.purchase_body)
        return httpx.Response(self.purchase_status, json=self.purchase_body)


def client_for(google: FakeGoogle | Callable[[httpx.Request], httpx.Response], clock: Callable[[], float] | None = None, **changes: Any) -> HttpGooglePlayClient:
    values = {"assistant_google_play_package_name": PACKAGE, "assistant_google_play_service_account_json": service_account_json()}
    settings = SimpleNamespace(**{**values, **changes})
    handler = google.handle if isinstance(google, FakeGoogle) else google
    kwargs: dict[str, Any] = {"transport": httpx.MockTransport(handler)}
    if clock is not None:
        kwargs["clock"] = clock
    return HttpGooglePlayClient(settings, **kwargs)  # type: ignore[arg-type]


def run(call: Any) -> Any:
    return asyncio.run(call)


async def read(client: HttpGooglePlayClient, times: int = 1) -> Any:
    try:
        for _ in range(times):
            result = await client.get_purchase(PRODUCT, TOKEN)
        return result
    finally:
        await client.aclose()


# ---- the service-account setting ----------------------------------------------------------


def test_the_key_is_read_from_base64_or_plain_json() -> None:
    raw = json.dumps({"client_email": SERVICE_EMAIL, "private_key": private_key_pem(), "token_uri": TOKEN_URI})
    for value in (base64.b64encode(raw.encode()).decode(), raw, f"  {raw}\n"):
        account = parse_service_account(value)
        assert account is not None and account.email == SERVICE_EMAIL and account.token_uri == TOKEN_URI


@pytest.mark.parametrize(
    "value",
    ["", "   ", "not base64 !!", base64.b64encode(b"not json").decode(), base64.b64encode(b"[1]").decode(), '{"client_email": "a@b"}', "{bad json"],
)
def test_an_unusable_key_turns_the_client_off(value: str) -> None:
    assert parse_service_account(value) is None
    assert client_for(FakeGoogle(), assistant_google_play_service_account_json=value).configured is False


def test_a_token_uri_that_is_not_https_falls_back_to_googles() -> None:
    account = parse_service_account(service_account_json(token_uri="http://evil.example/token"))
    assert account is not None and account.token_uri == "https://oauth2.googleapis.com/token"


def test_a_set_but_broken_key_is_logged_without_its_content(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.ERROR)
    client_for(FakeGoogle(), assistant_google_play_service_account_json="eyJicm9rZW4")
    assert "not a usable service-account key" in caplog.text
    assert "eyJicm9rZW4" not in caplog.text


def test_no_package_name_means_not_configured() -> None:
    assert client_for(FakeGoogle(), assistant_google_play_package_name="  ").configured is False


# ---- reading a purchase -------------------------------------------------------------------


def test_reading_a_purchase_signs_a_token_request_and_calls_the_right_url() -> None:
    google = FakeGoogle()
    purchase = run(read(client_for(google)))
    assert purchase.purchase_state == 0 and purchase.order_id == "GPA.3301-1111-2222-33333"
    assert purchase.account_id == PURCHASE["obfuscatedExternalAccountId"]
    assert purchase.purchase_time == datetime.fromtimestamp(1793873762, tz=timezone.utc)
    assert (purchase.region_code, purchase.purchase_type, purchase.acknowledgement_state) == ("IN", 0, 0)

    sent = google.token_requests[0]
    assert sent["grant_type"] == ["urn:ietf:params:oauth:grant-type:jwt-bearer"]
    claims = jwt.decode(sent["assertion"][0], certs=public_key_pem(), audience=TOKEN_URI)
    assert claims["iss"] == SERVICE_EMAIL and claims["scope"] == SCOPE
    assert claims["exp"] - claims["iat"] == 3600

    request = google.api_requests[0]
    assert request.method == "GET"
    assert str(request.url) == f"{ANDROID_PUBLISHER_URL}/applications/{PACKAGE}/purchases/products/{PRODUCT}/tokens/opaque.token%2Fwith%2Bodd%3Dchars"
    assert request.headers["Authorization"] == "Bearer access-1"


def test_the_access_token_is_reused_until_it_nears_expiry() -> None:
    now = [1_000_000.0]
    google = FakeGoogle()
    client = client_for(google, clock=lambda: now[0])

    async def main() -> None:
        try:
            await client.get_purchase(PRODUCT, TOKEN)
            now[0] += 3000
            await client.get_purchase(PRODUCT, TOKEN)
            assert google.issued == 1
            now[0] += 400
            await client.get_purchase(PRODUCT, TOKEN)
            assert google.issued == 2
        finally:
            await client.aclose()

    asyncio.run(main())
    assert [request.headers["Authorization"] for request in google.api_requests] == ["Bearer access-1", "Bearer access-1", "Bearer access-2"]


def test_parallel_first_calls_share_one_token_request() -> None:
    google = FakeGoogle()
    client = client_for(google)

    async def main() -> None:
        try:
            await asyncio.gather(*(client.get_purchase(PRODUCT, TOKEN) for _ in range(5)))
        finally:
            await client.aclose()

    asyncio.run(main())
    assert google.issued == 1
    assert len(google.api_requests) == 5


@pytest.mark.parametrize("status", [400, 404, 410])
def test_google_not_knowing_the_purchase_is_not_found(status: int) -> None:
    with pytest.raises(PlayPurchaseNotFoundError):
        run(read(client_for(FakeGoogle(purchase_status=status, purchase_body={"error": {"message": "secret detail"}}))))


@pytest.mark.parametrize("status", [401, 403])
def test_refused_credentials_are_unavailable_and_logged_as_an_error(status: int, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.ERROR)
    with pytest.raises(GooglePlayUnavailableError):
        run(read(client_for(FakeGoogle(purchase_status=status, purchase_body={"error": {"message": "secret detail"}}))))
    assert "refused our service account" in caplog.text
    assert "secret detail" not in caplog.text


def test_a_revoked_token_is_replaced_and_the_call_retried_once() -> None:
    google = FakeGoogle()
    refused_once = [False]

    def handle(request: httpx.Request) -> httpx.Response:
        if str(request.url) != TOKEN_URI and not refused_once[0]:
            refused_once[0] = True
            google.api_requests.append(request)
            return httpx.Response(401)
        return google.handle(request)

    purchase = run(read(client_for(handle)))
    assert purchase.purchase_state == 0
    assert google.issued == 2
    assert [request.headers["Authorization"] for request in google.api_requests] == ["Bearer access-1", "Bearer access-2"]


def test_a_key_google_keeps_refusing_is_unavailable_after_one_retry() -> None:
    google = FakeGoogle(purchase_status=401)
    with pytest.raises(GooglePlayUnavailableError):
        run(read(client_for(google)))
    assert google.issued == 2
    assert len(google.api_requests) == 2


@pytest.mark.parametrize(("status", "body"), [(500, {}), (503, {}), (429, {}), (200, b"not json"), (200, [1, 2])])
def test_a_server_problem_or_unusable_answer_is_unavailable(status: int, body: Any) -> None:
    with pytest.raises(GooglePlayUnavailableError):
        run(read(client_for(FakeGoogle(purchase_status=status, purchase_body=body))))


def test_a_network_failure_is_unavailable() -> None:
    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("slow", request=request)

    with pytest.raises(GooglePlayUnavailableError):
        run(read(client_for(broken)))


@pytest.mark.parametrize("status", [400, 401, 500])
def test_a_refused_token_request_is_unavailable(status: int) -> None:
    google = FakeGoogle(token_status=status)
    with pytest.raises(GooglePlayUnavailableError):
        run(read(client_for(google)))
    assert google.api_requests == []


@pytest.mark.parametrize("millis", ["99999999999999999999", str(10**30), -(10**20)])
def test_a_purchase_time_too_big_for_a_date_is_none_not_a_crash(millis: Any) -> None:
    purchase = run(read(client_for(FakeGoogle(purchase_body={**PURCHASE, "purchaseTimeMillis": millis}))))
    assert purchase.purchase_time is None
    assert purchase.purchase_state == 0


def test_odd_values_in_googles_answer_become_none() -> None:
    odd = {**PURCHASE, "purchaseState": True, "purchaseTimeMillis": "soon", "orderId": "a\x00b", "regionCode": 5}
    purchase = run(read(client_for(FakeGoogle(purchase_body=odd))))
    assert (purchase.purchase_state, purchase.purchase_time, purchase.order_id, purchase.region_code) == (None, None, None, None)


# ---- acknowledging ------------------------------------------------------------------------


def test_acknowledge_posts_to_the_acknowledge_url() -> None:
    google = FakeGoogle()
    client = client_for(google)

    async def main() -> None:
        try:
            await client.acknowledge(PRODUCT, TOKEN)
        finally:
            await client.aclose()

    asyncio.run(main())
    request = google.api_requests[0]
    assert request.method == "POST"
    assert str(request.url).endswith("/tokens/opaque.token%2Fwith%2Bodd%3Dchars:acknowledge")


def acknowledge_with(status: int) -> None:
    client = client_for(FakeGoogle(ack_status=status))

    async def main() -> None:
        try:
            await client.acknowledge(PRODUCT, TOKEN)
        finally:
            await client.aclose()

    asyncio.run(main())


@pytest.mark.parametrize("status", [400, 409, 410])
def test_google_refusing_to_acknowledge_is_its_own_error(status: int) -> None:
    with pytest.raises(PlayAcknowledgeRefusedError):
        acknowledge_with(status)


@pytest.mark.parametrize("status", [403, 500, 503])
def test_a_credentials_or_server_problem_on_acknowledge_is_unavailable_not_refused(status: int) -> None:
    with pytest.raises(GooglePlayUnavailableError) as raised:
        acknowledge_with(status)
    assert not isinstance(raised.value, PlayAcknowledgeRefusedError)


def test_httpx_request_logs_never_hold_the_whole_token(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="httpx")
    token = "a1b2c3d4.secret-part-of-the-token_" + "q" * 60 + "TAILabcd"
    client = client_for(FakeGoogle())

    async def main() -> None:
        try:
            await client.get_purchase(PRODUCT, token)
            await client.acknowledge(PRODUCT, token)
        finally:
            await client.aclose()

    asyncio.run(main())
    httpx_lines = [record.getMessage() for record in caplog.records if record.name == "httpx"]
    assert len(httpx_lines) >= 3
    assert all("secret-part-of-the-token" not in line for line in httpx_lines)
    assert sum("/tokens/...TAILabcd" in line for line in httpx_lines) == 2


def test_closing_twice_is_safe() -> None:
    client = client_for(FakeGoogle())

    async def main() -> None:
        await client.get_purchase(PRODUCT, TOKEN)
        await client.aclose()
        await client.aclose()

    asyncio.run(main())
