"""certificates and exam_question_reports."""

import uuid
from datetime import datetime

from sqlalchemy import func, insert, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from database.models import User
from exams.errors import CertificateIdTaken, ExamStoreUnavailable, ValidCertificateExists
from exams.infra.sql_base import SqlStore, constraint_name, logger
from exams.models import Certificate, ExamQuestionReport
from exams.records import CertificateRecord

_COLUMNS = (
    Certificate.certificate_id, Certificate.user_id, Certificate.course_id, Certificate.attempt_id,
    Certificate.recipient_name, Certificate.course_title, Certificate.course_slug, Certificate.score,
    Certificate.total, Certificate.issued_at, Certificate.status, Certificate.revoked_reason,
    Certificate.revoked_at, Certificate.replaced_by,
)  # fmt: skip


def _record(row, email: str | None = None) -> CertificateRecord:
    return CertificateRecord(*row[: len(_COLUMNS)], user_email=email)


def _like_pattern(query: str) -> str:
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


class SqlCertificateStore(SqlStore):
    async def _first(self, statement, context: str) -> CertificateRecord | None:
        row = (await self._execute(statement, context)).first()
        return _record(row) if row is not None else None

    async def get(self, certificate_id: str) -> CertificateRecord | None:
        return await self._first(
            select(*_COLUMNS).where(Certificate.certificate_id == certificate_id), "get certificate"
        )

    async def get_for_update(self, certificate_id: str) -> CertificateRecord | None:
        return await self._first(
            select(*_COLUMNS).where(Certificate.certificate_id == certificate_id).with_for_update(),
            "lock certificate",
        )

    async def valid_for(self, user_id: uuid.UUID, course_id: uuid.UUID) -> CertificateRecord | None:
        return await self._first(
            select(*_COLUMNS).where(
                Certificate.user_id == user_id, Certificate.course_id == course_id, Certificate.status == "valid"
            ),
            "get valid certificate",
        )

    async def valid_for_attempt(self, attempt_id: uuid.UUID) -> CertificateRecord | None:
        return await self._first(
            select(*_COLUMNS).where(Certificate.attempt_id == attempt_id, Certificate.status == "valid"),
            "get certificate of attempt",
        )

    async def insert(self, certificate: CertificateRecord) -> None:
        values = {column.key: getattr(certificate, column.key) for column in _COLUMNS}
        try:
            await self._session.execute(insert(Certificate).values(**values))
        except IntegrityError as exc:
            name = constraint_name(exc)
            if "certificates_pkey" in name:
                raise CertificateIdTaken() from exc
            if "uq_certificates_user_course_valid" in name or "uq_certificates_attempt_valid" in name:
                raise ValidCertificateExists() from exc
            logger.error("Certificate insert violated a constraint: %s", name)
            raise ExamStoreUnavailable() from exc
        except SQLAlchemyError as exc:
            logger.error("Certificate insert failed", exc_info=True)
            raise ExamStoreUnavailable() from exc

    async def list_for_user(self, user_id: uuid.UUID) -> list[CertificateRecord]:
        result = await self._execute(
            select(*_COLUMNS).where(Certificate.user_id == user_id).order_by(Certificate.issued_at.desc()),
            "list certificates of user",
        )
        return [_record(row) for row in result.all()]

    async def search(self, query: str | None, limit: int) -> list[CertificateRecord]:
        statement = select(*_COLUMNS, User.email).join(User, User.id == Certificate.user_id)
        if query and query.strip():
            pattern = _like_pattern(query.strip())
            statement = statement.where(
                or_(
                    Certificate.certificate_id.ilike(pattern, escape="\\"),
                    Certificate.recipient_name.ilike(pattern, escape="\\"),
                    User.email.ilike(pattern, escape="\\"),
                    Certificate.course_title.ilike(pattern, escape="\\"),
                    Certificate.course_slug.ilike(pattern, escape="\\"),
                )
            )
        statement = statement.order_by(Certificate.issued_at.desc()).limit(max(1, min(limit, 500)))
        result = await self._execute(statement, "search certificates")
        return [_record(row, row[-1]) for row in result.all()]

    async def revoke(self, certificate_id: str, reason: str, now: datetime, replaced_by: str | None) -> None:
        await self._execute(
            update(Certificate)
            .where(Certificate.certificate_id == certificate_id)
            .values(status="revoked", revoked_reason=reason, revoked_at=now, replaced_by=replaced_by),
            "revoke certificate",
        )


class SqlReportStore(SqlStore):
    async def add(
        self, question_id: uuid.UUID, attempt_id: uuid.UUID, user_id: uuid.UUID, reason: str, comment: str
    ) -> bool:
        result = await self._execute(
            pg_insert(ExamQuestionReport)
            .values(question_id=question_id, attempt_id=attempt_id, user_id=user_id, reason=reason, comment=comment)
            .on_conflict_do_nothing(constraint="uq_exam_question_reports_attempt_question")
            .returning(ExamQuestionReport.id),
            "add question report",
        )
        return result.first() is not None

    async def distinct_open_reporters(self, question_id: uuid.UUID) -> int:
        result = await self._execute(
            select(func.count(func.distinct(ExamQuestionReport.user_id))).where(
                ExamQuestionReport.question_id == question_id, ExamQuestionReport.resolved_at.is_(None)
            ),
            "count reporters",
        )
        return int(result.scalar_one() or 0)

    async def resolve_for_question(self, question_id: uuid.UUID, admin_id: uuid.UUID | None, now: datetime) -> None:
        await self._execute(
            update(ExamQuestionReport)
            .where(ExamQuestionReport.question_id == question_id, ExamQuestionReport.resolved_at.is_(None))
            .values(resolved_at=now, resolved_by=admin_id),
            "resolve reports",
        )
