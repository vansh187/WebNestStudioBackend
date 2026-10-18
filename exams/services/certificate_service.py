"""Certificates (plan section 4.6/4.7): issue inside the grading transaction,
public lookup and PDF, the learner's list, admin search / revoke / reissue."""

import asyncio
import logging
import re
import uuid
from collections import OrderedDict
from datetime import datetime

from exams.domain.certificate_id import EXAM_CODE_PATTERN, build_certificate_id, normalize_certificate_id
from exams.domain.validation import clean_recipient_name
from exams.errors import (
    CertificateIdTaken,
    CertificateNotFound,
    CertificateRevoked,
    ExamConflict,
    ExamInternalError,
    ExamValidationError,
    RateLimited,
    ValidCertificateExists,
)
from exams.protocols import CertificateStore, Clock, IdGenerator, PdfRenderer, RateLimiter, UnitOfWork
from exams.records import AttemptRecord, CertificateRecord, CourseRecord
from exams.services.common import audit, guarded

logger = logging.getLogger("webnest.exams.certificates")

ID_ATTEMPTS = 5
PUBLIC_LIMIT_PER_MINUTE = 60
PDF_LIMIT_PER_MINUTE = 20


class PdfCache:
    """LRU of rendered PDFs keyed by (id, status): a certificate only changes
    by revoke/reissue, which changes the key."""

    def __init__(self, max_entries: int = 64) -> None:
        self._max_entries = max_entries
        self._entries: OrderedDict[tuple[str, str], bytes] = OrderedDict()

    def get(self, key: tuple[str, str]) -> bytes | None:
        value = self._entries.get(key)
        if value is not None:
            self._entries.move_to_end(key)
        return value

    def put(self, key: tuple[str, str], value: bytes) -> None:
        self._entries[key] = value
        self._entries.move_to_end(key)
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)


def fallback_exam_code(slug: str) -> str:
    """Used only if an enabled course somehow has no exam code."""
    letters = re.sub(r"[^A-Z0-9]", "", (slug or "").upper())
    if not letters or not letters[0].isalpha():
        letters = "C" + letters
    code = letters[:12]
    return code if EXAM_CODE_PATTERN.fullmatch(code) else "COURSE"


class CertificateService:
    def __init__(
        self,
        certificates: CertificateStore,
        uow: UnitOfWork,
        clock: Clock,
        ids: IdGenerator,
        renderer: PdfRenderer,
        pdf_cache: PdfCache,
        limiter: RateLimiter,
        settings,
    ) -> None:
        self._certificates = certificates
        self._uow = uow
        self._clock = clock
        self._ids = ids
        self._renderer = renderer
        self._pdf_cache = pdf_cache
        self._limiter = limiter
        self._settings = settings

    def verify_url(self, certificate_id: str) -> str:
        return f"{self._settings.certificate_verify_base_url.rstrip('/')}/{certificate_id}"

    def _limit(self, key: str, limit: int) -> None:
        retry_after = self._limiter.hit(key, limit, 60)
        if retry_after:
            raise RateLimited(retry_after)

    async def issue_in_transaction(
        self, attempt: AttemptRecord, course: CourseRecord, score: int, total: int, issued_at: datetime
    ) -> str:
        """Called by the grader inside its transaction (no commit here). Each
        insert runs in a savepoint so an ID collision doesn't abort the grade."""
        existing = await self._certificates.valid_for(attempt.user_id, course.id)
        if existing is not None:
            logger.warning("User already holds a valid certificate for course %s; not issuing another", course.id)
            return existing.certificate_id
        code = course.exam_code if course.exam_code and EXAM_CODE_PATTERN.fullmatch(course.exam_code) else fallback_exam_code(course.slug)
        for _ in range(ID_ATTEMPTS):
            certificate_id = build_certificate_id(code, issued_at.year, self._ids.choice)
            record = CertificateRecord(
                certificate_id=certificate_id,
                user_id=attempt.user_id,
                course_id=course.id,
                attempt_id=attempt.id,
                recipient_name=attempt.recipient_name,
                course_title=course.title,
                course_slug=course.slug,
                score=score,
                total=total,
                issued_at=issued_at,
                status="valid",
            )
            try:
                async with self._uow.savepoint():
                    await self._certificates.insert(record)
                logger.info("Issued certificate %s for attempt %s", certificate_id, attempt.id)
                return certificate_id
            except CertificateIdTaken:
                continue
            except ValidCertificateExists:
                existing = await self._certificates.valid_for(attempt.user_id, course.id)
                existing = existing or await self._certificates.valid_for_attempt(attempt.id)
                if existing is not None:
                    return existing.certificate_id
                raise ExamInternalError()
        logger.error("Could not find a free certificate ID after %d tries", ID_ATTEMPTS)
        raise ExamInternalError()

    async def _get_or_404(self, raw_id: str) -> CertificateRecord:
        certificate_id = normalize_certificate_id(raw_id)
        if certificate_id is None:
            raise CertificateNotFound()
        record = await self._certificates.get(certificate_id)
        if record is None:
            raise CertificateNotFound()
        return record

    @guarded("get public certificate")
    async def get_public(self, raw_id: str, client_key: str) -> CertificateRecord:
        self._limit(f"cert:{client_key}", PUBLIC_LIMIT_PER_MINUTE)
        return await self._get_or_404(raw_id)

    @guarded("render certificate pdf")
    async def get_pdf(self, raw_id: str, client_key: str) -> tuple[str, bytes]:
        self._limit(f"pdf:{client_key}", PDF_LIMIT_PER_MINUTE)
        record = await self._get_or_404(raw_id)
        if record.status != "valid":
            raise CertificateRevoked()
        key = (record.certificate_id, record.status)
        cached = self._pdf_cache.get(key)
        if cached is None:
            # CPU-bound: keep it off the event loop.
            cached = await asyncio.to_thread(self._renderer.render, record, self.verify_url(record.certificate_id))
            self._pdf_cache.put(key, cached)
        return record.certificate_id, cached

    @guarded("list my certificates")
    async def list_mine(self, user_id: uuid.UUID) -> list[CertificateRecord]:
        return await self._certificates.list_for_user(user_id)

    @guarded("search certificates")
    async def search(self, query: str | None) -> list[CertificateRecord]:
        return await self._certificates.search(query, 200)

    async def _with_email(self, certificate_id: str) -> CertificateRecord:
        for record in await self._certificates.search(certificate_id, 5):
            if record.certificate_id == certificate_id:
                return record
        record = await self._certificates.get(certificate_id)
        if record is None:
            raise CertificateNotFound()
        return record

    @guarded("revoke certificate")
    async def revoke(self, raw_id: str, reason: str, admin_id: uuid.UUID) -> CertificateRecord:
        certificate_id = normalize_certificate_id(raw_id)
        if certificate_id is None:
            raise CertificateNotFound()
        reason = " ".join((reason or "").split())
        if not 1 <= len(reason) <= 500:
            raise ExamValidationError("reason must be 1-500 characters")
        try:
            record = await self._certificates.get_for_update(certificate_id)
            if record is None:
                raise CertificateNotFound()
            if record.status == "valid":
                await self._certificates.revoke(certificate_id, reason, self._clock.now(), None)
                await self._uow.commit()
                audit("certificate_revoke", admin_id, certificate=certificate_id)
            else:
                await self._uow.rollback()
        except Exception:
            await self._uow.rollback()
            raise
        return await self._with_email(certificate_id)

    @guarded("reissue certificate")
    async def reissue(self, raw_id: str, recipient_name: str, admin_id: uuid.UUID) -> CertificateRecord:
        certificate_id = normalize_certificate_id(raw_id)
        if certificate_id is None:
            raise CertificateNotFound()
        try:
            name = clean_recipient_name(recipient_name)
        except ValueError as exc:
            raise ExamValidationError(str(exc)) from exc
        try:
            old = await self._certificates.get_for_update(certificate_id)
            if old is None:
                raise CertificateNotFound()
            if old.status != "valid":
                raise ExamConflict("Only a valid certificate can be reissued.")
            code = certificate_id.split("-")[1]
            now = self._clock.now()
            for _ in range(ID_ATTEMPTS):
                new_id = build_certificate_id(code, old.issued_at.year, self._ids.choice)
                if new_id == certificate_id:
                    continue
                try:
                    async with self._uow.savepoint():
                        # Revoke first so the partial unique indexes free up.
                        await self._certificates.revoke(certificate_id, "reissued", now, new_id)
                        await self._certificates.insert(
                            CertificateRecord(
                                certificate_id=new_id,
                                user_id=old.user_id,
                                course_id=old.course_id,
                                attempt_id=old.attempt_id,
                                recipient_name=name,
                                course_title=old.course_title,
                                course_slug=old.course_slug,
                                score=old.score,
                                total=old.total,
                                issued_at=old.issued_at,
                                status="valid",
                            )
                        )
                    break
                except CertificateIdTaken:
                    continue
            else:
                raise ExamInternalError()
            await self._uow.commit()
        except Exception:
            await self._uow.rollback()
            raise
        audit("certificate_reissue", admin_id, old=certificate_id, new=new_id)
        return await self._with_email(new_id)
