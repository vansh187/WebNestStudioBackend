"""POST /api/assistant/payments/google-play/verify, through the real router and service, with a
fake Google Play and in-memory stores. Each status is the one the Android app depends on."""

import asyncio
import logging
import uuid
from datetime import timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.assistant_payment_router import router as payment_router
from api.assistant_router import get_assistant_user_id
from core.dependencies import get_google_play_payment_service
from core.error_handlers import register_error_handlers
from core.exceptions import DatabaseError
from database.assistant_payment_persistence import AccessState
from services import google_play_payment_service
from services.assistant_payment_service import AssistantPaymentService, OrderRateLimiter
from services.google_play_client import GooglePlayUnavailableError, PlayAcknowledgeRefusedError, PlayPurchaseNotFoundError
from services.google_play_payment_service import GooglePlayPaymentService
from services.user_rate_limiter import UserRateLimiter
from tests.google_play_fakes import PRODUCT, FakeGooglePlay, FakePlayStore, play_settings, purchase
from tests.test_assistant_payments import LATER, OTHER_USER, USER, FakePaymentStore, FakeRazorpay, settings

URL = "/api/assistant/payments/google-play/verify"
TOKEN = "opaque.TOKEN_from-google:" + "x" * 120


class FakeGoogleSettledLater(FakeGooglePlay):
    """The first read is the purchase being bought; later reads show it as the app left it."""

    settled: Any = None

    async def get_purchase(self, product_id: str, token: str) -> Any:
        first = await super().get_purchase(product_id, token)
        return first if len(self.reads) == 1 else self.settled


class Setup:
    def __init__(self, google: FakeGooglePlay | None = None, access: AccessState | None = None, limit: int = 10, **changes: Any) -> None:
        self.settings = play_settings(settings(**changes))
        self.payments = FakePaymentStore(access)
        self.store = FakePlayStore(self.payments.access)
        self.google = google if google is not None else FakeGooglePlay(purchase(USER))
        access_service = AssistantPaymentService(self.settings, self.payments, FakeRazorpay(), OrderRateLimiter())  # type: ignore[arg-type]
        self.service = GooglePlayPaymentService(self.settings, self.store, self.google, access_service, UserRateLimiter(limit, 60.0))  # type: ignore[arg-type]

    def client(self, user: uuid.UUID | None = USER) -> TestClient:
        app = FastAPI()
        app.include_router(payment_router)
        register_error_handlers(app)
        app.dependency_overrides[get_google_play_payment_service] = lambda: self.service
        if user is not None:
            app.dependency_overrides[get_assistant_user_id] = lambda: user
        return TestClient(app, raise_server_exceptions=False)

    def verify(self, user: uuid.UUID = USER, **changes: Any) -> Any:
        return self.client(user).post(URL, json=body(**changes))


def body(**changes: Any) -> dict[str, Any]:
    return {
        "product_id": PRODUCT,
        "purchase_token": TOKEN,
        "order_id": "GPA.1234-5678-9012-34567",
        "terms_version": "2026-11-05",
        "terms_accepted_at": "2026-11-05T10:15:30.000Z",
        **changes,
    }


# ---- statuses the app reads as "not verified" or "try again" ---------------------------


def test_login_is_required() -> None:
    response = Setup().client(user=None).post(URL, json=body())
    assert response.status_code == 401


@pytest.mark.parametrize("product", ["webnest_prime_365d", "", None])
def test_an_unknown_product_is_a_404_and_google_is_not_asked(product: str | None) -> None:
    setup = Setup()
    response = setup.verify(product_id=product)
    assert response.status_code == 404
    assert setup.google.reads == []


def test_paywall_off_is_a_503() -> None:
    setup = Setup(assistant_paywall_enabled=False)
    assert setup.verify().status_code == 503
    assert setup.google.reads == []


def test_missing_google_credentials_are_a_503() -> None:
    setup = Setup(FakeGooglePlay(purchase(USER), configured=False))
    assert setup.verify().status_code == 503


@pytest.mark.parametrize("token", [None, "", "has spaces", "bad/slash", "x" * 4097, "nul\x00byte"])
def test_a_missing_or_impossible_token_is_a_400_and_google_is_not_asked(token: str | None) -> None:
    setup = Setup()
    assert setup.verify(purchase_token=token).status_code == 400
    assert setup.google.reads == []


@pytest.mark.parametrize("raw", [b"not json", b"[]", b'{"product_id": 5}', b""])
def test_an_unreadable_body_is_a_400(raw: bytes) -> None:
    response = Setup().client().post(URL, content=raw, headers={"content-type": "application/json"})
    assert response.status_code == 400
    assert response.json() == {"detail": "This payment could not be verified."}


def test_a_huge_body_is_refused_before_it_is_read_whole() -> None:
    assert Setup().verify(purchase_token="x" * 20_000).status_code == 413


def test_google_not_knowing_the_token_is_a_400() -> None:
    setup = Setup(FakeGooglePlay(error=PlayPurchaseNotFoundError("status 404")))
    response = setup.verify()
    assert response.status_code == 400
    assert response.json() == {"detail": "This purchase could not be verified."}
    assert setup.store.purchases == {}


def test_google_refusing_our_credentials_is_a_503_never_a_400() -> None:
    setup = Setup(FakeGooglePlay(error=GooglePlayUnavailableError("status 401")))
    response = setup.verify()
    assert response.status_code == 503
    assert response.json() == {"detail": "Payments are not available right now. Please try again later."}


def test_google_timing_out_is_a_503(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(google_play_payment_service, "_GOOGLE_SECONDS", 0.05)
    setup = Setup(FakeGooglePlay(purchase(USER), delay=1.0))
    assert setup.verify().status_code == 503
    assert setup.store.purchases == {}


def test_a_pending_payment_is_a_202_with_no_access_and_nothing_granted() -> None:
    setup = Setup(FakeGooglePlay(purchase(USER, purchase_state=2)))
    response = setup.verify()
    assert response.status_code == 202
    assert response.json() == {"detail": "Payment is still processing."}
    assert setup.store.purchases == {}
    assert USER not in setup.payments.access


@pytest.mark.parametrize("state", [1, None, 7])
def test_a_cancelled_or_unknown_purchase_state_is_a_400(state: int | None) -> None:
    setup = Setup(FakeGooglePlay(purchase(USER, purchase_state=state)))
    assert setup.verify().status_code == 400
    assert setup.store.purchases == {}


@pytest.mark.parametrize("account", [str(OTHER_USER), None, "not-a-uuid", ""])
def test_a_purchase_for_another_or_no_account_is_a_403(account: str | None) -> None:
    setup = Setup(FakeGooglePlay(purchase(USER, account_id=account)))
    response = setup.verify()
    assert response.status_code == 403
    assert response.json() == {"detail": "This purchase belongs to a different WebNest account."}
    assert setup.store.purchases == {}


def test_the_account_id_match_ignores_letter_case() -> None:
    setup = Setup(FakeGooglePlay(purchase(USER, account_id=str(USER).upper())))
    assert setup.verify().status_code == 200


# ---- granting ---------------------------------------------------------------------------


def test_a_valid_purchase_grants_once_returns_active_access_and_is_acknowledged() -> None:
    setup = Setup()
    response = setup.verify()
    assert response.status_code == 200
    access = response.json()["access"]
    assert access["is_prime"] is True and access["active"] is True
    assert access["tokens_limit"] == 2_000_000
    assert setup.store.claims == 1
    assert setup.google.acknowledged == [(PRODUCT, TOKEN)]
    assert TOKEN in setup.store.acknowledged


def test_only_googles_answer_is_saved_never_the_apps_order_id() -> None:
    setup = Setup()
    setup.verify(order_id="GPA.fake-from-app")
    saved = setup.store.purchases[TOKEN]
    assert saved.google_order_id == "GPA.3301-1111-2222-33333"
    assert (saved.product_id, saved.pass_days, saved.pass_tokens, saved.region_code) == (PRODUCT, 30, 2_000_000, "IN")


def test_the_same_token_from_the_same_user_again_is_200_with_no_second_grant() -> None:
    setup = Setup()
    assert setup.verify().status_code == 200
    second = setup.verify()
    assert second.status_code == 200
    assert second.json()["access"]["active"] is True
    assert setup.store.claims == 1
    assert len(setup.google.reads) == 1


def test_the_same_token_from_another_user_is_a_409_and_google_is_not_asked() -> None:
    setup = Setup()
    assert setup.verify().status_code == 200
    response = setup.verify(user=OTHER_USER)
    assert response.status_code == 409
    assert len(setup.google.reads) == 1
    assert OTHER_USER not in setup.payments.access


def test_two_requests_for_one_token_at_the_same_moment_grant_once() -> None:
    setup = Setup(FakeGooglePlay(purchase(USER), delay=0.05))
    payload = google_play_payment_service.GooglePlayVerifyRequest.model_validate(body())

    async def both() -> list[Any]:
        return await asyncio.gather(setup.service.verify(USER, payload), setup.service.verify(USER, payload))

    results = asyncio.run(both())
    assert all(result is not None and result.active for result in results)
    assert setup.store.claims == 1


def test_buying_again_while_a_pass_runs_is_allowed_and_granted() -> None:
    setup = Setup(access=AccessState(LATER, 2_000_000, 1_900_000))
    response = setup.verify()
    assert response.status_code == 200
    assert setup.store.claims == 1


def test_an_already_consumed_purchase_still_grants_the_first_time() -> None:
    setup = Setup(FakeGooglePlay(purchase(USER, consumption_state=1, acknowledgement_state=1)))
    assert setup.verify().status_code == 200
    assert setup.store.claims == 1
    # Consuming acknowledged it, so the server doesn't ask Google again.
    assert setup.google.acknowledged == []
    assert TOKEN in setup.store.acknowledged


def test_terms_are_saved_when_sent() -> None:
    setup = Setup()
    setup.verify()
    saved = setup.store.purchases[TOKEN]
    assert saved.terms_version == "2026-11-05"
    assert saved.terms_accepted_at is not None and saved.terms_accepted_at.utcoffset() == timedelta(0)


def test_a_resumed_purchase_without_terms_still_works() -> None:
    setup = Setup()
    payload = {"product_id": PRODUCT, "purchase_token": TOKEN}
    assert setup.client().post(URL, json=payload).status_code == 200
    saved = setup.store.purchases[TOKEN]
    assert (saved.terms_version, saved.terms_accepted_at) == (None, None)


@pytest.mark.parametrize("accepted_at", ["yesterday", "2026-11-05T10:15:30", "x" * 50])
def test_an_unusable_terms_time_is_dropped_not_fatal(accepted_at: str) -> None:
    setup = Setup()
    assert setup.verify(terms_accepted_at=accepted_at, terms_version="v" * 65).status_code == 200
    saved = setup.store.purchases[TOKEN]
    assert (saved.terms_version, saved.terms_accepted_at) == (None, None)


def test_unknown_fields_are_ignored() -> None:
    assert Setup().verify(amount=1, owner=str(OTHER_USER), success=True).status_code == 200


@pytest.mark.parametrize("error", [GooglePlayUnavailableError("status 400"), TimeoutError(), DatabaseError("down")])
def test_an_acknowledge_failure_still_returns_200_and_is_retried_on_the_next_verify(error: Exception) -> None:
    setup = Setup(FakeGooglePlay(purchase(USER), acknowledge_error=error))
    assert setup.verify().status_code == 200
    assert TOKEN not in setup.store.acknowledged
    setup.google.acknowledge_error = None
    assert setup.verify().status_code == 200
    assert TOKEN in setup.store.acknowledged
    assert setup.store.claims == 1


@pytest.mark.parametrize(("acknowledged", "consumed"), [(1, 0), (0, 1)])
def test_a_refused_acknowledge_of_a_purchase_the_app_settled_is_recorded_and_not_retried(acknowledged: int, consumed: int) -> None:
    google = FakeGoogleSettledLater(purchase(USER), acknowledge_error=PlayAcknowledgeRefusedError("status 400"))
    google.settled = purchase(USER, acknowledgement_state=acknowledged, consumption_state=consumed)
    setup = Setup(google)
    assert setup.verify().status_code == 200
    assert TOKEN in setup.store.acknowledged
    setup.verify()
    setup.verify()
    assert len(google.acknowledged) == 1


def test_a_refused_acknowledge_of_an_unsettled_purchase_is_tried_again_later() -> None:
    google = FakeGoogleSettledLater(purchase(USER), acknowledge_error=PlayAcknowledgeRefusedError("status 400"))
    google.settled = purchase(USER)
    setup = Setup(google)
    assert setup.verify().status_code == 200
    assert TOKEN not in setup.store.acknowledged
    assert setup.verify().status_code == 200
    assert len(google.acknowledged) == 2


def test_an_acknowledged_purchase_is_not_acknowledged_again_on_a_repeat_verify() -> None:
    setup = Setup()
    setup.verify()
    setup.verify()
    assert len(setup.google.acknowledged) == 1


def test_more_than_ten_attempts_a_minute_is_a_429() -> None:
    setup = Setup(FakeGooglePlay(error=GooglePlayUnavailableError("down")))
    statuses = [setup.verify().status_code for _ in range(11)]
    assert statuses == [503] * 10 + [429]


def test_logs_never_hold_the_whole_token(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    setup = Setup()
    setup.verify()
    setup.verify(user=OTHER_USER)
    Setup(FakeGooglePlay(error=PlayPurchaseNotFoundError("status 404"))).verify()
    assert caplog.records
    assert all(TOKEN not in record.getMessage() for record in caplog.records)
    assert any(TOKEN[-8:] in record.getMessage() for record in caplog.records)
