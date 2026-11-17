from services.login_rate_limiter import ACCOUNT_ATTEMPT_LIMIT, LoginRateLimiter


def test_reservations_enforce_limit_before_concurrent_authentication_finishes() -> None:
    limiter = LoginRateLimiter(clock=lambda: 100.0)
    reservations = []

    for _ in range(ACCOUNT_ATTEMPT_LIMIT):
        reservation, retry_after = limiter.begin_attempt("jane@example.com")
        assert reservation is not None and retry_after is None
        reservations.append(reservation)

    reservation, retry_after = limiter.begin_attempt("jane@example.com")
    assert reservation is None and retry_after == 900

    limiter.finish_failure("jane@example.com", reservations[0])
    limiter.finish_success("jane@example.com", reservations[1])
    # A successful attempt clears the failure while preserving other in-flight requests.
    reservation, retry_after = limiter.begin_attempt("jane@example.com")
    assert reservation is not None and retry_after is None
    limiter.release_attempt("jane@example.com", reservation)

    # The remaining reservations still occupy their slots.
    for _ in range(ACCOUNT_ATTEMPT_LIMIT - 8):
        reservation, retry_after = limiter.begin_attempt("jane@example.com")
        assert reservation is not None and retry_after is None
    reservation, retry_after = limiter.begin_attempt("jane@example.com")
    assert reservation is None and retry_after == 900


def test_success_clears_completed_failures_and_non_auth_errors_release_reservations() -> None:
    now = [0.0]
    limiter = LoginRateLimiter(clock=lambda: now[0])
    first, _ = limiter.begin_attempt("jane@example.com")
    assert first is not None
    limiter.finish_failure("jane@example.com", first)
    success, _ = limiter.begin_attempt("jane@example.com")
    assert success is not None
    limiter.finish_success("jane@example.com", success)

    for _ in range(ACCOUNT_ATTEMPT_LIMIT - 1):
        reservation, retry_after = limiter.begin_attempt("jane@example.com")
        assert reservation is not None and retry_after is None
        limiter.release_attempt("jane@example.com", reservation)

    reservation, retry_after = limiter.begin_attempt("jane@example.com")
    assert reservation is not None and retry_after is None


def test_retry_after_tracks_the_oldest_attempt_in_the_sliding_window() -> None:
    now = [10.0]
    limiter = LoginRateLimiter(clock=lambda: now[0])
    for _ in range(ACCOUNT_ATTEMPT_LIMIT):
        limiter.begin_attempt("jane@example.com")

    now[0] += 60
    reservation, retry_after = limiter.begin_attempt("jane@example.com")
    assert reservation is None and retry_after == 840


def test_distinct_login_identifiers_keep_distinct_buckets() -> None:
    limiter = LoginRateLimiter(clock=lambda: 0.0)
    for _ in range(ACCOUNT_ATTEMPT_LIMIT):
        reservation, _ = limiter.begin_attempt("Jane@example.com")
        assert reservation is not None

    reservation, retry_after = limiter.begin_attempt("jane@example.com")
    assert reservation is not None and retry_after is None
