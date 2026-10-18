"""Background maintenance (plan 4.4, 6.7): best-effort jobs. Lazy paths stay
primary because Render's free tier sleeps and jobs may not run."""

import logging
import math
import uuid
from datetime import datetime, timedelta

from exams.protocols import AlertSender, AttemptCache, AttemptStore, Clock, CourseReader, JobStore, QuestionStore, UnitOfWork
from exams.records import CourseRecord
from exams.services.attempt_lifecycle import AttemptLifecycle
from exams.services.pool_service import PoolService

logger = logging.getLogger("webnest.exams.maintenance")

EXPIRE_BATCH = 100
LOST_ALERT_THRESHOLD = 3
STATS_MIN_ANSWERS = 30
QUARANTINE_BELOW_CORRECT = 0.15
RETIRE_ABOVE_CORRECT = 0.98

# Per process: at most one "attempts lost" alert per hour.
_last_lost_alert: dict[str, datetime] = {}


def item_statistics(graded: list[tuple[int, int, list[dict]]]) -> dict[str, dict]:
    """Per question: answered, correct and discrimination (point-biserial
    correlation of being right with the rest of the score)."""
    per_question: dict[str, list[tuple[int, float]]] = {}
    for score, _total, results in graded:
        for entry in results:
            qid = str(entry.get("question_id") or "")
            if not qid:
                continue
            right = 1 if entry.get("correct") else 0
            per_question.setdefault(qid, []).append((right, float(score - right)))
    stats: dict[str, dict] = {}
    for qid, pairs in per_question.items():
        n = len(pairs)
        correct = sum(r for r, _ in pairs)
        discrimination = None
        if n >= 2 and 0 < correct < n:
            mean_rest = sum(s for _, s in pairs) / n
            sd = math.sqrt(sum((s - mean_rest) ** 2 for _, s in pairs) / n)
            if sd > 0:
                mean_right = sum(s for r, s in pairs if r) / correct
                mean_wrong = sum(s for r, s in pairs if not r) / (n - correct)
                p = correct / n
                discrimination = round((mean_right - mean_wrong) / sd * math.sqrt(p * (1 - p)), 4)
        stats[qid] = {"answered": n, "correct": correct, "discrimination": discrimination}
    return stats


class MaintenanceService:
    def __init__(
        self,
        courses: CourseReader,
        questions: QuestionStore,
        attempts: AttemptStore,
        cache: AttemptCache,
        jobs: JobStore,
        pool: PoolService,
        lifecycle: AttemptLifecycle,
        alerts: AlertSender,
        uow: UnitOfWork,
        clock: Clock,
        settings,
    ) -> None:
        self._courses = courses
        self._questions = questions
        self._attempts = attempts
        self._cache = cache
        self._jobs = jobs
        self._pool = pool
        self._lifecycle = lifecycle
        self._alerts = alerts
        self._uow = uow
        self._clock = clock
        self._settings = settings

    async def expire_overdue(self) -> dict:
        """Grades every overdue attempt (SKIP LOCKED: an attempt being
        submitted right now is left to that request), then sweeps orphaned
        cache entries - in that order, so no overdue attempt loses its key."""
        now = self._clock.now()
        cutoff = now - timedelta(seconds=self._settings.exam_submit_grace_seconds)
        graded = lost = 0
        for attempt_id in await self._attempts.overdue_ids(cutoff, EXPIRE_BATCH):
            try:
                final = await self._lifecycle.grade(attempt_id, skip_locked=True)
            except Exception:
                logger.error("Could not expire attempt %s", attempt_id, exc_info=True)
                continue
            if final is not None and final.status == "lost":
                lost += 1
            elif final is not None and final.graded:
                graded += 1
        try:
            swept = await self._cache.sweep(now)
            await self._uow.commit()
        except Exception:
            await self._uow.rollback()
            raise
        await self._check_lost_rate(now)
        return {"graded": graded, "lost": lost, "swept": swept}

    async def _check_lost_rate(self, now: datetime) -> None:
        lost = await self._attempts.count_lost_since(now - timedelta(hours=1))
        if lost <= LOST_ALERT_THRESHOLD:
            return
        last = _last_lost_alert.get("lost")
        if last is not None and now - last < timedelta(hours=1):
            return
        _last_lost_alert["lost"] = now
        logger.critical("%d exam attempts were lost in the last hour", lost)
        await self._alerts.send(
            "WebNest exams: attempts are being lost",
            f"{lost} exam attempts were lost in the last hour (their questions could not be recovered). "
            "Learners can restart, but this points at a server or database problem.",
        )

    async def question_stats(self) -> dict:
        now = self._clock.now()
        quarantined = retired = updated = 0
        for course in await self._courses.list_enabled_courses():
            stats = item_statistics(await self._attempts.graded_results(course.id))
            live = {str(c.id): c.id for c in await self._questions.live_candidates(course.id)}
            rows, to_quarantine, to_retire = [], [], []
            known = {str(q) for q in await self._questions.ids_with_status(course.id, "live")}
            known |= {str(q) for q in await self._questions.ids_with_status(course.id, "quarantined")}
            known |= {str(q) for q in await self._questions.ids_with_status(course.id, "retired")}
            known |= {str(q) for q in await self._questions.ids_with_status(course.id, "voided")}
            for qid, s in stats.items():
                if qid not in known:
                    continue
                rows.append({"question_id": uuid.UUID(qid), **s})
                if qid in live and s["answered"] >= STATS_MIN_ANSWERS:
                    rate = s["correct"] / s["answered"]
                    disc = s["discrimination"]
                    if (disc is not None and disc < 0) or rate < QUARANTINE_BELOW_CORRECT:
                        to_quarantine.append(live[qid])
                    elif rate > RETIRE_ABOVE_CORRECT:
                        to_retire.append(live[qid])
            try:
                await self._questions.upsert_stats(rows, now)
                quarantined += await self._questions.set_status(to_quarantine, "quarantined", ("live",))
                retired += await self._questions.set_status(to_retire, "retired", ("live",))
                await self._uow.commit()
            except Exception:
                await self._uow.rollback()
                raise
            updated += len(rows)
        if quarantined or retired:
            logger.warning("Nightly statistics quarantined %d and retired %d questions", quarantined, retired)
        return {"updated": updated, "quarantined": quarantined, "retired": retired}

    async def courses_needing_fill(self) -> list[CourseRecord]:
        now = self._clock.now()
        due = []
        for course in await self._courses.list_enabled_courses():
            latest = await self._jobs.latest_for(course.id)
            if latest is not None and latest.status == "paused" and latest.paused_until and latest.paused_until > now:
                continue
            if latest is not None and latest.status == "running":
                continue
            if not (await self._pool.state(course.id)).full:
                due.append(course)
        return due

    async def weekly_report(self) -> str:
        lines = ["WebNest course exams - weekly report", ""]
        for course in await self._courses.list_enabled_courses():
            state = await self._pool.state(course.id)
            counts = await self._questions.status_counts(course.id)
            totals: dict[str, int] = {}
            for per_status in counts.values():
                for status, n in per_status.items():
                    totals[status] = totals.get(status, 0) + n
            created = totals.get("live", 0) + totals.get("quarantined", 0) + totals.get("voided", 0) + totals.get("retired", 0)
            reject_rate = totals.get("rejected", 0) / max(1, created + totals.get("rejected", 0))
            latest = await self._jobs.latest_for(course.id)
            lines.append(
                f"{course.slug}: live {state.total_live}/{state.total_target}, quarantined {totals.get('quarantined', 0)}, "
                f"voided {totals.get('voided', 0)}, retired {totals.get('retired', 0)}, "
                f"reject rate {reject_rate:.0%}"
                + (f", refill paused until {latest.paused_until:%Y-%m-%d %H:%M} UTC" if latest and latest.status == "paused" and latest.paused_until else "")
                + (", rejected by check " + str(latest.rejected) if latest and latest.rejected else "")
            )
        report = "\n".join(lines)
        await self._alerts.send("WebNest exams weekly report", report)
        return report

