"""The two Razorpay API calls we need. Its raw error messages stay here: callers only ever
see RazorpayError, and the logs only ever get a status code."""

import logging
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from core.config import Settings

logger = logging.getLogger("webnest.assistant.payments")

RAZORPAY_API_URL = "https://api.razorpay.com"
_TIMEOUT = httpx.Timeout(10.0, connect=5.0)
_LIMITS = httpx.Limits(max_connections=10, max_keepalive_connections=5, keepalive_expiry=60.0)


class RazorpayError(Exception):
    """Razorpay is unavailable or gave an unusable answer."""


@dataclass(frozen=True)
class RazorpayPayment:
    payment_id: str
    status: str
    amount: int | None
    currency: str | None


class RazorpayClient(Protocol):
    async def create_order(self, amount: int, currency: str, receipt: str, notes: dict[str, str]) -> str: ...

    async def order_payments(self, order_id: str) -> list[RazorpayPayment]: ...

    async def aclose(self) -> None: ...


class HttpRazorpayClient:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._key_id = settings.assistant_razorpay_key_id
        self._key_secret = settings.assistant_razorpay_key_secret
        self._transport = transport
        self._client: httpx.AsyncClient | None = None

    async def create_order(self, amount: int, currency: str, receipt: str, notes: dict[str, str]) -> str:
        body = await self._request("POST", "/v1/orders", {"amount": amount, "currency": currency, "receipt": receipt, "notes": notes})
        order_id = body.get("id")
        if not isinstance(order_id, str) or not order_id:
            raise RazorpayError("no order id")
        return order_id

    async def order_payments(self, order_id: str) -> list[RazorpayPayment]:
        body = await self._request("GET", f"/v1/orders/{order_id}/payments", None)
        items = body.get("items")
        return [_payment(item) for item in items if isinstance(item, dict)] if isinstance(items, list) else []

    async def aclose(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=RAZORPAY_API_URL,
                auth=(self._key_id, self._key_secret),
                timeout=_TIMEOUT,
                limits=_LIMITS,
                transport=self._transport,
            )
        return self._client

    async def _request(self, method: str, path: str, body: dict[str, Any] | None) -> dict[str, Any]:
        try:
            response = await self._get_client().request(method, path, json=body)
        except httpx.HTTPError as exc:
            logger.warning("Razorpay request failed: %s", type(exc).__name__)
            raise RazorpayError("unreachable") from exc
        if response.status_code >= 400:
            logger.warning("Razorpay refused a request: status=%s", response.status_code)
            raise RazorpayError(f"status {response.status_code}")
        try:
            parsed = response.json()
        except ValueError as exc:
            raise RazorpayError("not json") from exc
        if not isinstance(parsed, dict):
            raise RazorpayError("not an object")
        return parsed


def _payment(item: dict[str, Any]) -> RazorpayPayment:
    amount = item.get("amount")
    currency = item.get("currency")
    return RazorpayPayment(
        payment_id=str(item.get("id") or ""),
        status=str(item.get("status") or ""),
        amount=amount if isinstance(amount, int) else None,
        currency=currency if isinstance(currency, str) else None,
    )
