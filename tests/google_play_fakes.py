"""Fakes for the Google Play tests: a Google Play client, a purchase store and a real RSA
service-account key made at test time. Never calls Google."""

import asyncio
import base64
import json
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from functools import cache
from types import SimpleNamespace
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from database.assistant_payment_persistence import AccessState, NewPlayPurchase, PlayPurchaseRecord
from services.google_play_client import PlayPurchase

PRODUCT = "webnest_prime_30d"
PACKAGE = "com.webneststudio.app"
TOKEN_URI = "https://oauth2.googleapis.com/token"
SERVICE_EMAIL = "play-verifier@webnest-test.iam.gserviceaccount.com"


@cache
def private_key_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()


def public_key_pem() -> bytes:
    key = serialization.load_pem_private_key(private_key_pem().encode(), password=None)
    return key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)


def service_account_json(**changes: Any) -> str:
    """The key as Render holds it: the JSON Google gives you, base64-encoded."""
    info = {
        "type": "service_account",
        "client_email": SERVICE_EMAIL,
        "private_key": private_key_pem(),
        "private_key_id": "test-key-id",
        "token_uri": TOKEN_URI,
        **changes,
    }
    return base64.b64encode(json.dumps(info).encode()).decode()


def play_settings(base: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(
        **{
            **vars(base),
            "assistant_google_play_package_name": PACKAGE,
            "assistant_google_play_product_id": PRODUCT,
            "assistant_google_play_service_account_json": service_account_json(),
        }
    )


def purchase(user_id: uuid.UUID | None, **changes: Any) -> PlayPurchase:
    """A completed purchase made by this user, as Google reports it."""
    made = PlayPurchase(
        purchase_state=0,
        consumption_state=0,
        acknowledgement_state=0,
        order_id="GPA.3301-1111-2222-33333",
        account_id=str(user_id) if user_id is not None else None,
        region_code="IN",
        purchase_time=datetime(2026, 11, 5, 10, 16, tzinfo=timezone.utc),
        purchase_type=None,
    )
    return replace(made, **changes)


class FakeGooglePlay:
    def __init__(
        self,
        answer: PlayPurchase | None = None,
        error: Exception | None = None,
        acknowledge_error: Exception | None = None,
        configured: bool = True,
        delay: float = 0.0,
    ) -> None:
        self.answer = answer
        self.error = error
        self.acknowledge_error = acknowledge_error
        self._configured = configured
        self.delay = delay
        self.reads: list[tuple[str, str]] = []
        self.acknowledged: list[tuple[str, str]] = []

    @property
    def configured(self) -> bool:
        return self._configured

    async def get_purchase(self, product_id: str, token: str) -> PlayPurchase:
        self.reads.append((product_id, token))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        assert self.answer is not None, "the test gave Google no answer"
        return self.answer

    async def acknowledge(self, product_id: str, token: str) -> None:
        self.acknowledged.append((product_id, token))
        if self.acknowledge_error is not None:
            raise self.acknowledge_error

    async def aclose(self) -> None:
        pass


class FakePlayStore:
    """Tokens in memory. A claim checks and records with no await in between, which is what the
    real unique index guarantees, and grants through the payment store's access."""

    def __init__(self, access: dict[uuid.UUID, AccessState]) -> None:
        self.access = access
        self.purchases: dict[str, NewPlayPurchase] = {}
        self.acknowledged: set[str] = set()
        self.claims = 0

    async def find_play_purchase(self, purchase_token: str) -> PlayPurchaseRecord | None:
        saved = self.purchases.get(purchase_token)
        return None if saved is None else PlayPurchaseRecord(saved.user_id, purchase_token in self.acknowledged)

    async def claim_play_purchase(self, purchase: NewPlayPurchase) -> bool:
        if purchase.purchase_token in self.purchases:
            return False
        self.claims += 1
        self.purchases[purchase.purchase_token] = purchase
        if purchase.acknowledged:
            self.acknowledged.add(purchase.purchase_token)
        self.access[purchase.user_id] = AccessState(datetime.now(timezone.utc) + timedelta(days=purchase.pass_days), purchase.pass_tokens, 0)
        return True

    async def mark_play_acknowledged(self, purchase_token: str) -> None:
        self.acknowledged.add(purchase_token)
