"""UserRateLimiter, the sliding window shared by Razorpay orders, Google Play verify and reports."""

import time
import uuid

from services import user_rate_limiter
from services.assistant_payment_service import OrderRateLimiter
from services.user_rate_limiter import UserRateLimiter


def test_the_window_slides_per_user() -> None:
    limiter = UserRateLimiter(3, 60.0)
    user, other = uuid.uuid4(), uuid.uuid4()
    assert [limiter.allow(user, 0.0) for _ in range(4)] == [True, True, True, False]
    assert limiter.allow(other, 1.0)
    assert not limiter.allow(user, 59.9)
    assert limiter.allow(user, 60.1)


def test_a_refused_call_is_not_counted() -> None:
    limiter = UserRateLimiter(1, 10.0)
    user = uuid.uuid4()
    assert limiter.allow(user, 0.0)
    for moment in (1.0, 5.0, 9.0):
        assert not limiter.allow(user, moment)
    assert limiter.allow(user, 10.5)


def test_it_reads_its_own_clock_when_not_given_a_time() -> None:
    now = [100.0]
    limiter = UserRateLimiter(1, 10.0, clock=lambda: now[0])
    user = uuid.uuid4()
    assert limiter.allow(user)
    assert not limiter.allow(user)
    now[0] = 111.0
    assert limiter.allow(user)


def test_idle_users_are_forgotten_when_the_table_fills(monkeypatch: object) -> None:
    monkeypatch.setattr(user_rate_limiter, "_MAX_TRACKED_USERS", 10)  # type: ignore[attr-defined]
    limiter = UserRateLimiter(1, 10.0)
    for _ in range(10):
        limiter.allow(uuid.uuid4(), 0.0)
    busy = uuid.uuid4()
    assert limiter.allow(busy, 20.0)
    assert len(limiter._calls) == 1
    assert not limiter.allow(busy, 21.0)


def test_a_table_full_of_active_users_frees_room_in_one_go_not_on_every_call(monkeypatch: object) -> None:
    monkeypatch.setattr(user_rate_limiter, "_MAX_TRACKED_USERS", 1_000)  # type: ignore[attr-defined]
    monkeypatch.setattr(user_rate_limiter, "_FORGET_WHEN_FULL", 100)  # type: ignore[attr-defined]
    limiter = UserRateLimiter(5, 60.0)
    for _ in range(1_000):
        limiter.allow(uuid.uuid4(), 0.0)
    limiter.allow(uuid.uuid4(), 1.0)
    assert len(limiter._calls) == 901


def test_a_full_table_stays_fast() -> None:
    limiter = UserRateLimiter(5, 60.0)
    started = time.perf_counter()
    for _ in range(30_000):
        limiter.allow(uuid.uuid4(), 0.0)
    assert time.perf_counter() - started < 3.0


def test_order_limits_still_allow_five_a_minute() -> None:
    limiter = OrderRateLimiter()
    user = uuid.uuid4()
    assert [limiter.allow(user, 0.0) for _ in range(6)] == [True] * 5 + [False]
    assert limiter.allow(user, 60.5)
    assert limiter.begin_order(user) and not limiter.begin_order(user)
    limiter.end_order(user)
    assert limiter.begin_order(user)
