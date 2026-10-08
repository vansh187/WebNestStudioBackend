"""OTP guess limiting and email masking in messaging.

Persistence is replaced with in-memory fakes; the SQL the real persistence
classes build is compiled against the Postgres dialect to prove it is valid.
"""

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import OperationalError

from core.exceptions import DatabaseError, RateLimitedError, ValidationError
from core.security import OtpGenerator
from database import otp_persistence
from database.otp_persistence import FallbackAttemptCounter, OtpPersistence
from database.user_persistence import UserPersistence
from services.auth_service import (
    INVALID_OTP_MESSAGE,
    MAX_FAILED_GUESSES_PER_HOUR,
    MAX_OTP_ATTEMPTS,
    MAX_OTPS_PER_HOUR,
    AuthService,
)
from services.messaging_service import MessagingService

EMAIL = "victim@example.com"


def now() -> datetime:
    return datetime.now(timezone.utc)


class FakeOtps:
    """Mirrors OtpPersistence. `pending` models the uncommitted claim made by
    consume_if_active(commit=False); FakeUsers/FakeRefreshTokens commit it."""

    def __init__(self):
        self.rows: list[SimpleNamespace] = []
        self.failures: dict[uuid.UUID, int] = {}
        self.pending: list[SimpleNamespace] = []
        self.counter_readable = True
        self.locks_taken = 0

    def add(self, code="123456", purpose="password_reset", minutes=10, age_seconds=600, consumed=False):
        row = SimpleNamespace(
            id=uuid.uuid4(),
            email=EMAIL,
            otp_code=code,
            purpose=purpose,
            consumed=consumed,
            expires_at=now() + timedelta(minutes=minutes),
            created_at=now() - timedelta(seconds=age_seconds),
        )
        self.rows.append(row)
        return row

    def _matching(self, email, purpose):
        return [r for r in self.rows if r.email == email and r.purpose == purpose]

    async def create(self, email, otp_code, purpose, expires_at, user_id=None):
        row = self.add(code=otp_code, purpose=purpose, age_seconds=0)
        row.expires_at = expires_at
        return row

    async def issue(self, email, otp_code, purpose, expires_at, user_id=None):
        for row in self._matching(email, purpose):
            row.consumed = True
        return await self.create(email, otp_code, purpose, expires_at, user_id)

    async def lock_issuance(self, email, purpose):
        self.locks_taken += 1

    async def get_latest_active(self, email, purpose):
        live = [r for r in self._matching(email, purpose) if not r.consumed and r.expires_at > now()]
        return max(live, key=lambda r: r.created_at) if live else None

    async def latest_created_at(self, email, purpose):
        return max((r.created_at for r in self._matching(email, purpose)), default=None)

    async def count_created_since(self, email, purpose, since):
        return sum(1 for r in self._matching(email, purpose) if r.created_at >= since)

    async def failed_guesses_since(self, email, purpose, since):
        if not self.counter_readable:
            return None
        return sum(self.failures.get(r.id, 0) for r in self._matching(email, purpose) if r.created_at >= since)

    async def consume_if_active(self, otp_id, commit=True):
        for row in self.rows:
            if row.id == otp_id and not row.consumed:
                if commit:
                    row.consumed = True
                else:
                    self.pending.append(row)
                return True
        return False

    def commit_pending(self):
        for row in self.pending:
            row.consumed = True
        self.pending.clear()

    async def record_failed_attempt(self, otp_id, email, purpose, max_attempts):
        self.failures[otp_id] = self.failures.get(otp_id, 0) + 1
        if self.failures[otp_id] >= max_attempts:
            for row in self._matching(email, purpose):
                row.consumed = True
        return self.failures[otp_id]


class FakeUsers:
    def __init__(self, otps):
        self.user = SimpleNamespace(id=uuid.uuid4(), email=EMAIL, is_verified=False, password_hash="old")
        self._otps = otps

    async def get_by_email(self, email):
        return self.user if email == EMAIL else None

    async def update_password_hash(self, user, password_hash, commit=True):
        user.password_hash = password_hash
        if commit:
            self._otps.commit_pending()
        return user

    async def mark_verified(self, user):
        user.is_verified = True
        self._otps.commit_pending()
        return user


class FakeRefreshTokens:
    def __init__(self, otps, fail=False):
        self.revoked_for, self._otps, self._fail = [], otps, fail

    async def revoke_all_for_user(self, user_id):
        if self._fail:
            raise DatabaseError("connection dropped")
        self.revoked_for.append(user_id)
        self._otps.commit_pending()


class FakeHasher:
    async def hash(self, password):
        return f"hashed:{password}"


def make_auth(email_enabled=True):
    settings = SimpleNamespace(email_enabled=email_enabled, otp_expire_minutes=10)
    service = AuthService(None, settings, FakeHasher(), None, OtpGenerator(settings))
    service._otps = FakeOtps()
    service._users = FakeUsers(service._otps)
    service._refresh_tokens = FakeRefreshTokens(service._otps)
    return service


def test_correct_code_resets_password_and_is_single_use():
    service = make_auth()
    service._otps.add("123456")
    asyncio.run(service.reset_password(EMAIL, "123456", "new-password"))
    assert service._users.user.password_hash == "hashed:new-password"
    assert service._refresh_tokens.revoked_for == [service._users.user.id]
    with pytest.raises(ValidationError):
        asyncio.run(service.reset_password(EMAIL, "123456", "again"))


def test_code_locks_after_max_wrong_guesses_even_for_the_right_code():
    service = make_auth()
    service._otps.add("123456")
    for attempt in range(MAX_OTP_ATTEMPTS):
        with pytest.raises(ValidationError) as caught:
            asyncio.run(service.reset_password(EMAIL, "000000", "attacker-password"))
        if attempt == MAX_OTP_ATTEMPTS - 1:
            assert "Too many" in str(caught.value)
    with pytest.raises(ValidationError):
        asyncio.run(service.reset_password(EMAIL, "123456", "attacker-password"))
    assert service._users.user.password_hash == "old"


def test_locking_leaves_no_older_code_to_fall_back_to():
    service = make_auth()
    older = service._otps.add("111111", age_seconds=300)
    service._otps.add("222222", age_seconds=100)
    for _ in range(MAX_OTP_ATTEMPTS):
        with pytest.raises(ValidationError):
            asyncio.run(service.reset_password(EMAIL, "000000", "pw"))
    assert older.consumed
    with pytest.raises(ValidationError):
        asyncio.run(service.reset_password(EMAIL, "111111", "pw"))
    assert service._users.user.password_hash == "old"


def test_a_new_code_retires_the_earlier_ones():
    service = make_auth()
    older = service._otps.add("111111", age_seconds=120)
    new_code = asyncio.run(service.resend_otp(EMAIL, "password_reset"))
    assert older.consumed and service._otps.locks_taken == 1
    live = [row for row in service._otps.rows if not row.consumed]
    assert [row.otp_code for row in live] == [new_code]


def test_a_few_typos_do_not_lock_the_code():
    service = make_auth()
    service._otps.add("123456")
    for _ in range(MAX_OTP_ATTEMPTS - 1):
        with pytest.raises(ValidationError):
            asyncio.run(service.reset_password(EMAIL, "999999", "pw"))
    asyncio.run(service.reset_password(EMAIL, "123456", "new-password"))
    assert service._users.user.password_hash == "hashed:new-password"


def test_expired_code_is_rejected_uniformly_and_never_counted():
    service = make_auth()
    service._otps.add("123456", minutes=-1)
    for guess in ("123456", "000000", "000000", "000000", "000000", "000000", "000000"):
        with pytest.raises(ValidationError) as caught:
            asyncio.run(service.reset_password(EMAIL, guess, "pw"))
        assert str(caught.value) == INVALID_OTP_MESSAGE
    assert service._otps.failures == {}
    assert service._users.user.password_hash == "old"


def test_code_claimed_by_a_concurrent_request_is_rejected():
    service = make_auth()
    row = service._otps.add("123456")

    class RacingHasher:
        async def hash(self, password):
            row.consumed = True  # locked/used while this request was hashing
            return "hashed"

    service._password_hasher = RacingHasher()
    with pytest.raises(ValidationError):
        asyncio.run(service.reset_password(EMAIL, "123456", "pw"))
    assert service._users.user.password_hash == "old"


def test_a_failed_write_during_reset_does_not_burn_the_code():
    service = make_auth()
    row = service._otps.add("123456")
    service._refresh_tokens = FakeRefreshTokens(service._otps, fail=True)
    with pytest.raises(DatabaseError):
        asyncio.run(service.reset_password(EMAIL, "123456", "pw"))
    assert not row.consumed  # the claim was never committed

    service._otps.pending.clear()  # the real session rolls back on close
    service._refresh_tokens = FakeRefreshTokens(service._otps)
    asyncio.run(service.reset_password(EMAIL, "123456", "new-password"))
    assert row.consumed and service._users.user.password_hash == "hashed:new-password"


def test_verify_otp_counts_wrong_guesses_and_still_verifies():
    service = make_auth()
    service._otps.add("123456", purpose="signup")
    with pytest.raises(ValidationError):
        asyncio.run(service.verify_otp(EMAIL, "111111"))
    assert not service._users.user.is_verified
    user = asyncio.run(service.verify_otp(EMAIL, "123456"))
    assert user.is_verified
    assert all(row.consumed for row in service._otps.rows)

    locked = make_auth()
    locked._otps.add("123456", purpose="signup")
    for _ in range(MAX_OTP_ATTEMPTS):
        with pytest.raises(ValidationError):
            asyncio.run(locked.verify_otp(EMAIL, "111111"))
    with pytest.raises(ValidationError):
        asyncio.run(locked.verify_otp(EMAIL, "123456"))
    assert not locked._users.user.is_verified


def test_malformed_codes_never_match_or_raise_unexpectedly():
    service = make_auth()
    assert service._codes_match("123456", "123456")
    assert not service._codes_match("123456", "123457")
    assert not service._codes_match("123456", None)
    assert not service._codes_match(None, "123456")
    assert not service._codes_match("", "")
    assert not service._codes_match("123456", "١٢٣٤٥٦")


def test_requesting_codes_alone_never_blocks_the_account():
    service = make_auth()
    for _ in range(20):
        service._otps.add(age_seconds=120, consumed=True)
    code = asyncio.run(service.resend_otp(EMAIL, "password_reset"))
    assert len(code) == 6 and code.isdigit()


def test_resend_is_refused_after_too_many_wrong_guesses_in_the_hour():
    service = make_auth()
    for _ in range(MAX_FAILED_GUESSES_PER_HOUR // MAX_OTP_ATTEMPTS):
        row = service._otps.add(age_seconds=120, consumed=True)
        service._otps.failures[row.id] = MAX_OTP_ATTEMPTS
    with pytest.raises(RateLimitedError):
        asyncio.run(service.resend_otp(EMAIL, "password_reset"))

    old = make_auth()
    row = old._otps.add(age_seconds=7200, consumed=True)
    old._otps.failures[row.id] = MAX_FAILED_GUESSES_PER_HOUR
    assert asyncio.run(old.resend_otp(EMAIL, "password_reset"))


def test_resend_falls_back_to_a_code_cap_when_the_counter_is_unreadable():
    service = make_auth()
    service._otps.counter_readable = False
    for _ in range(MAX_OTPS_PER_HOUR):
        service._otps.add(age_seconds=120, consumed=True)
    with pytest.raises(RateLimitedError):
        asyncio.run(service.resend_otp(EMAIL, "password_reset"))

    fresh = make_auth()
    fresh._otps.counter_readable = False
    fresh._otps.add(age_seconds=120)
    assert asyncio.run(fresh.resend_otp(EMAIL, "password_reset"))


def test_resend_cooldown_applies_even_when_the_last_code_was_burned():
    service = make_auth()
    service._otps.add(age_seconds=5, consumed=True)
    with pytest.raises(RateLimitedError) as caught:
        asyncio.run(service.resend_otp(EMAIL, "password_reset"))
    assert "Please wait" in str(caught.value)


def test_otp_codes_are_six_digits():
    generator = OtpGenerator(SimpleNamespace(otp_expire_minutes=10))
    codes = {generator.generate_code() for _ in range(200)}
    assert all(len(code) == 6 and code.isdigit() for code in codes)
    assert len(codes) > 150
    with pytest.raises(ValueError):
        generator.generate_code(0)


# --------------------------------------------------------------------------- #
# Real persistence: statements compile, and a failing counter stays bounded
# --------------------------------------------------------------------------- #
class Savepoint:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False


class RecordingSession:
    """Stands in for AsyncSession: records statements, optionally failing the
    executes whose 1-based position is in `fail_on`."""

    def __init__(self, results=(), fail_on=()):
        self.statements, self.results = [], list(results)
        self.fail_on, self.commits, self.rollbacks, self.added = set(fail_on), 0, 0, []

    def begin_nested(self):
        return Savepoint()

    def add(self, instance):
        self.added.append(instance)

    async def refresh(self, instance):
        return None

    async def execute(self, statement):
        self.statements.append(str(statement.compile(dialect=postgresql.dialect())))
        if len(self.statements) in self.fail_on:
            raise OperationalError("stmt", {}, Exception("relation does not exist"))
        value = self.results.pop(0) if self.results else None
        return SimpleNamespace(scalar_one=lambda: value, scalar_one_or_none=lambda: value)

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


def record(session, otp_id=None):
    return asyncio.run(
        OtpPersistence(session).record_failed_attempt(otp_id or uuid.uuid4(), EMAIL, "password_reset", 5)
    )


def test_failed_attempt_is_an_atomic_upsert_and_locks_at_the_limit():
    below = RecordingSession(results=[2])
    assert record(below) == 2
    assert len(below.statements) == 1 and "ON CONFLICT" in below.statements[0]
    assert "failed_attempts + " in below.statements[0]

    at_limit = RecordingSession(results=[5])
    assert record(at_limit) == 5
    assert len(at_limit.statements) == 2 and "UPDATE otp_verifications" in at_limit.statements[1]
    # The lock covers every unused code for the email+purpose, not one row.
    assert "otp_verifications.email =" in at_limit.statements[1]
    assert "otp_verifications.id" not in at_limit.statements[1].split("WHERE")[1]
    assert at_limit.commits == 1


def test_one_typo_does_not_lock_the_code_when_the_counter_table_is_unavailable(monkeypatch):
    monkeypatch.setattr(otp_persistence, "_fallback_attempts", FallbackAttemptCounter())
    otp_id = uuid.uuid4()
    counts = []
    for _ in range(5):
        session = RecordingSession(fail_on={1})
        counts.append(record(session, otp_id))
    assert counts == [1, 2, 3, 4, 5]
    # Only the fifth miss locks, and it does so in the database.
    assert "UPDATE otp_verifications" in session.statements[-1] and session.commits == 1

    first = RecordingSession(fail_on={1})
    monkeypatch.setattr(otp_persistence, "_fallback_attempts", FallbackAttemptCounter())
    assert record(first, otp_id) == 1
    assert len(first.statements) == 1 and first.commits == 0 and first.rollbacks == 1


def test_lock_failure_surfaces_as_a_database_error(monkeypatch):
    counter = FallbackAttemptCounter()
    monkeypatch.setattr(otp_persistence, "_fallback_attempts", counter)
    otp_id = uuid.uuid4()
    for _ in range(4):
        counter.increment(otp_id)
    with pytest.raises(DatabaseError):
        record(RecordingSession(fail_on={1, 2}), otp_id)


def test_fallback_counter_is_bounded():
    counter = FallbackAttemptCounter(max_entries=3)
    ids = [uuid.uuid4() for _ in range(5)]
    for otp_id in ids:
        assert counter.increment(otp_id) == 1
    assert len(counter._counts) == 3
    assert counter.increment(ids[-1]) == 2
    counter.forget(ids[-1])
    assert counter.increment(ids[-1]) == 1


def test_consume_if_active_only_claims_an_unconsumed_code():
    otp_id = uuid.uuid4()
    claimed = RecordingSession(results=[otp_id])
    assert asyncio.run(OtpPersistence(claimed).consume_if_active(otp_id)) is True
    assert "consumed IS false" in claimed.statements[0] and "RETURNING" in claimed.statements[0]
    assert claimed.commits == 1
    assert asyncio.run(OtpPersistence(RecordingSession(results=[None])).consume_if_active(otp_id)) is False

    deferred = RecordingSession(results=[otp_id])
    assert asyncio.run(OtpPersistence(deferred).consume_if_active(otp_id, commit=False)) is True
    assert deferred.commits == 0


def test_issue_retires_earlier_codes_in_the_same_commit():
    session = RecordingSession()
    asyncio.run(OtpPersistence(session).issue(EMAIL, "123456", "password_reset", now()))
    assert "UPDATE otp_verifications" in session.statements[0]
    assert len(session.added) == 1 and session.commits == 1


def test_issuance_lock_and_guess_count_tolerate_database_errors():
    locked = RecordingSession()
    asyncio.run(OtpPersistence(locked).lock_issuance(EMAIL, "password_reset"))
    assert "pg_advisory_xact_lock" in locked.statements[0]
    asyncio.run(OtpPersistence(RecordingSession(fail_on={1})).lock_issuance(EMAIL, "password_reset"))

    counted = RecordingSession(results=[7])
    assert asyncio.run(OtpPersistence(counted).failed_guesses_since(EMAIL, "password_reset", now())) == 7
    assert "JOIN otp_verifications" in counted.statements[0]
    broken = RecordingSession(fail_on={1})
    assert asyncio.run(OtpPersistence(broken).failed_guesses_since(EMAIL, "password_reset", now())) is None


def test_latest_active_code_excludes_expired_and_cooldown_reads_all_codes():
    class ScalarsSession(RecordingSession):
        async def execute(self, statement):
            await super().execute(statement)
            return SimpleNamespace(
                scalars=lambda: SimpleNamespace(first=lambda: None), scalar_one_or_none=lambda: None
            )

    active = ScalarsSession()
    asyncio.run(OtpPersistence(active).get_latest_active(EMAIL, "signup"))
    assert "expires_at >" in active.statements[0] and "LIMIT" in active.statements[0]
    cooldown = ScalarsSession()
    asyncio.run(OtpPersistence(cooldown).latest_created_at(EMAIL, "signup"))
    assert "max(otp_verifications.created_at)" in cooldown.statements[0]
    assert "consumed" not in cooldown.statements[0]


def test_user_search_matches_names_only_unless_email_search_is_allowed():
    class ListSession(RecordingSession):
        async def execute(self, statement):
            await super().execute(statement)
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []))

    names_only = ListSession()
    asyncio.run(UserPersistence(names_only).search("jo@x.com", uuid.uuid4(), 20, match_email=False))
    where = names_only.statements[0].split("WHERE")[1].split("ORDER BY")[0]
    assert "users.full_name ILIKE" in where and "email" not in where

    with_email = ListSession()
    asyncio.run(UserPersistence(with_email).search("jo", uuid.uuid4(), 20))
    assert "users.email ILIKE" in with_email.statements[0]


def test_pending_password_change_is_not_committed_by_the_persistence():
    session = RecordingSession()
    user = SimpleNamespace(password_hash="old")
    asyncio.run(UserPersistence(session).update_password_hash(user, "new", commit=False))
    assert user.password_hash == "new" and session.commits == 0
    asyncio.run(UserPersistence(session).update_password_hash(user, "newer"))
    assert session.commits == 1


# --------------------------------------------------------------------------- #
# Messaging: people who were only added to a conversation stay masked
# --------------------------------------------------------------------------- #
def person(name, email, role="client"):
    return SimpleNamespace(id=uuid.uuid4(), full_name=name, email=email, role=role)


def conversation_of(*people):
    return SimpleNamespace(
        id=uuid.uuid4(),
        type="group",
        title="Team",
        project_id=None,
        created_by=people[0].id,
        created_at=now(),
        updated_at=now(),
        participants=[
            SimpleNamespace(user=p, user_id=p.id, role="member", joined_at=now(), last_read_at=None, left_at=None)
            for p in people
        ],
    )


def make_messaging():
    return MessagingService(None, SimpleNamespace(), SimpleNamespace())


def emails_seen(view):
    return {participant.user.full_name: participant.user.email for participant in view.participants}


def test_creator_cannot_harvest_the_emails_of_people_they_added():
    attacker = person("Attacker", "attacker@example.com")
    victims = [person("Aditya", "aditya@example.com"), person("Bina", "bina@corp.in")]
    view = make_messaging()._serialize_conversation(conversation_of(attacker, *victims), 0, attacker, None)
    assert emails_seen(view) == {
        "Attacker": "attacker@example.com",
        "Aditya": "ad****@example.com",
        "Bina": "bi**@corp.in",
    }


def test_added_person_sees_who_created_the_conversation_but_not_other_members():
    creator, me, other = (
        person("Creator", "creator@example.com"),
        person("Me", "me@example.com"),
        person("Other", "other@example.com"),
    )
    view = make_messaging()._serialize_conversation(conversation_of(creator, me, other), 0, me, None)
    assert emails_seen(view) == {
        "Creator": "creator@example.com",
        "Me": "me@example.com",
        "Other": "ot***@example.com",
    }


def test_site_admin_sees_full_participant_emails():
    admin, other = person("Admin", "admin@example.com", role="admin"), person("Other", "aditya@example.com")
    view = make_messaging()._serialize_conversation(conversation_of(other, admin), 0, admin, None)
    assert emails_seen(view)["Other"] == "aditya@example.com"


def test_people_who_wrote_a_message_keep_their_real_email_on_it():
    me, other = person("Me", "me@example.com"), person("Other", "aditya@example.com")
    replied = SimpleNamespace(id=uuid.uuid4(), sender=other, body="hi", is_deleted=False)
    message = SimpleNamespace(
        id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        sender=other,
        body="hello",
        attachments=None,
        reply_to=replied,
        is_deleted=False,
        created_at=now(),
        edited_at=None,
    )
    service = make_messaging()
    out = service._build_message_out(message, [], {}, me)
    assert out.sender.email == "aditya@example.com"
    assert out.reply_to.sender.email == "aditya@example.com"
    assert service._user_summary(None, me).email == ""
    assert service._user_summary(other, None).email == "ad****@example.com"
    assert service._user_summary(other, me).email == "ad****@example.com"


def test_search_matches_email_only_for_site_admins():
    calls = []

    class FakeUserSearch:
        async def search(self, term, exclude_id, limit, match_email=True):
            calls.append(match_email)
            return [person("Other", "aditya@example.com")]

    service = make_messaging()
    service._users = FakeUserSearch()
    result = asyncio.run(service.search_users(person("Me", "me@example.com"), "adi", 20))
    assert result.results[0].email == "ad****@example.com"
    asyncio.run(service.search_users(person("Admin", "a@example.com", role="admin"), "adi", 20))
    assert calls == [False, True]


def test_limits_are_sane():
    assert 3 <= MAX_OTP_ATTEMPTS <= 10
    assert MAX_FAILED_GUESSES_PER_HOUR >= MAX_OTP_ATTEMPTS and MAX_OTPS_PER_HOUR >= 3
