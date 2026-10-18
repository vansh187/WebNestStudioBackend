"""Grades an attempt from its frozen cache key (plan section 4.5) and regrades
graded attempts from their compact results (section 6.7)."""

import uuid
from dataclasses import dataclass, field


@dataclass(frozen=True)
class KeyEntry:
    """One served question's grading key, as frozen in the attempt cache."""

    question_id: str
    module_title: str
    permutation: tuple[int, ...]
    correct_index: int


@dataclass
class GradeOutcome:
    score: int
    total: int
    passed: bool
    module_breakdown: list[dict] = field(default_factory=list)
    # [{question_id, module_title, answered, correct}] - no text, options or key.
    results: list[dict] = field(default_factory=list)

    @property
    def percent(self) -> int:
        return percent_of(self.score, self.total)


def percent_of(score: int, total: int) -> int:
    if total <= 0:
        return 0
    return int((score * 100 + total // 2) // total)  # half rounds up, no float drift


def is_pass(score: int, total: int, pass_percent: int) -> bool:
    return total > 0 and score * 100 >= pass_percent * total


def _breakdown(results: list[dict]) -> list[dict]:
    order: list[str] = []
    totals: dict[str, list[int]] = {}
    for entry in results:
        title = str(entry.get("module_title") or "General")
        if title not in totals:
            order.append(title)
            totals[title] = [0, 0]
        totals[title][1] += 1
        if entry.get("correct"):
            totals[title][0] += 1
    return [{"module_title": t, "correct": totals[t][0], "total": totals[t][1]} for t in order]


def grade(key: list[KeyEntry], answers: dict[str, int], pass_percent: int) -> GradeOutcome:
    """`answers` maps question id -> displayed option index. Unanswered or
    out-of-range answers are wrong."""
    results: list[dict] = []
    score = 0
    for entry in key:
        displayed = answers.get(entry.question_id)
        answered = isinstance(displayed, int) and not isinstance(displayed, bool) and 0 <= displayed < len(entry.permutation)
        correct = bool(answered and entry.permutation[displayed] == entry.correct_index)
        if correct:
            score += 1
        results.append(
            {
                "question_id": entry.question_id,
                "module_title": entry.module_title,
                "answered": bool(answered),
                "correct": correct,
            }
        )
    total = len(key)
    return GradeOutcome(
        score=score,
        total=total,
        passed=is_pass(score, total, pass_percent),
        module_breakdown=_breakdown(results),
        results=results,
    )


def regrade(results: list[dict], voided: set[uuid.UUID] | set[str], pass_percent: int) -> GradeOutcome:
    """Recomputes a graded attempt with voided questions removed from the total.
    The stored results themselves are kept unchanged."""
    voided_ids = {str(v) for v in voided}
    kept = [r for r in results if str(r.get("question_id")) not in voided_ids]
    score = sum(1 for r in kept if r.get("correct"))
    total = len(kept)
    return GradeOutcome(
        score=score,
        total=total,
        passed=is_pass(score, total, pass_percent),
        module_breakdown=_breakdown(kept),
        results=list(results),
    )
