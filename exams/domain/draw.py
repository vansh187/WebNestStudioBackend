"""Draws one attempt's questions from a course pool (plan section 4.2).

Rules, in priority order:
1. Only the examined course's questions (raises CrossCourseQuestion otherwise).
2. Distinct question ids, and at most one question per concept while that is
   possible (relaxed only as a last resort so a learner is never refused).
3. Questions this learner has not seen before, while any exist; after that the
   ones seen longest ago.
4. Module quotas by lesson count (largest remainder, at least 1 per module
   that has questions); a module's shortfall moves to the modules with the most
   spare questions.
5. Inside a module, 30% easy / 50% medium / 20% hard, falling back to any
   difficulty.
Then the order is shuffled and each question's 4 options are permuted.
"""

import random
import uuid
from dataclasses import dataclass
from datetime import datetime

from exams.errors import CrossCourseQuestion, PoolNotReady

OPTION_COUNT = 4
DIFFICULTY_MIX = (("easy", 0.3), ("medium", 0.5), ("hard", 0.2))


@dataclass(frozen=True)
class DrawCandidate:
    id: uuid.UUID
    course_id: uuid.UUID
    module_id: uuid.UUID
    difficulty: str
    concept: str


@dataclass(frozen=True)
class DrawModule:
    id: uuid.UUID
    lesson_count: int


@dataclass(frozen=True)
class DrawnQuestion:
    question_id: uuid.UUID
    module_id: uuid.UUID
    # permutation[displayed_index] = original option index
    permutation: tuple[int, ...]


def allocate_quotas(weights: dict, availability: dict, count: int) -> dict:
    """Splits `count` across the modules that have availability, in proportion
    to their weights (largest remainder, minimum 1 each), then moves any quota
    a module can't fill to the modules with the most spare. The result never
    exceeds a module's availability and sums to min(count, total availability)."""
    modules = sorted((m for m, available in availability.items() if available > 0), key=str)
    if not modules or count <= 0:
        return {}
    total_available = sum(availability[m] for m in modules)
    target = min(count, total_available)

    weight = {m: max(1, int(weights.get(m, 1) or 1)) for m in modules}
    if len(modules) >= target:
        # More modules than slots: one each for the heaviest modules.
        ranked = sorted(modules, key=lambda m: (-weight[m], str(m)))
        return {m: (1 if m in set(ranked[:target]) else 0) for m in modules}

    total_weight = sum(weight.values())
    shares = {m: target * weight[m] / total_weight for m in modules}
    quotas = {m: max(1, int(shares[m])) for m in modules}
    diff = target - sum(quotas.values())
    while diff > 0:
        for m in sorted(modules, key=lambda m: (-(shares[m] - quotas[m]), str(m))):
            if diff == 0:
                break
            quotas[m] += 1
            diff -= 1
    while diff < 0:
        reducible = [m for m in modules if quotas[m] > 1]
        m = max(reducible, key=lambda m: (quotas[m] - shares[m], str(m)))
        quotas[m] -= 1
        diff += 1

    shortfall = 0
    for m in modules:
        if quotas[m] > availability[m]:
            shortfall += quotas[m] - availability[m]
            quotas[m] = availability[m]
    while shortfall > 0:
        spare = {m: availability[m] - quotas[m] for m in modules}
        best = max(modules, key=lambda m: (spare[m], str(m)))
        if spare[best] <= 0:
            break
        quotas[best] += 1
        shortfall -= 1
    return quotas


def _difficulty_targets(quota: int) -> list[tuple[str, int]]:
    easy = round(quota * DIFFICULTY_MIX[0][1])
    medium = round(quota * DIFFICULTY_MIX[1][1])
    if easy + medium > quota:
        medium = max(0, quota - easy)
    return [("easy", easy), ("medium", medium), ("hard", max(0, quota - easy - medium))]


def _concept_key(concept: str) -> str:
    return " ".join((concept or "").lower().split())


def draw(
    course_id: uuid.UUID,
    modules: list[DrawModule],
    candidates: list[DrawCandidate],
    seen: dict[uuid.UUID, datetime],
    count: int,
    rng: random.Random,
) -> list[DrawnQuestion]:
    """`seen` maps question ids this learner saw in earlier attempts of this
    course to when they last saw them."""
    module_ids = {m.id for m in modules}
    for candidate in candidates:
        if candidate.course_id != course_id or candidate.module_id not in module_ids:
            raise CrossCourseQuestion(
                "A question outside the examined course reached the draw.",
            )

    unique: dict[uuid.UUID, DrawCandidate] = {}
    for candidate in sorted(candidates, key=lambda c: str(c.id)):
        unique.setdefault(candidate.id, candidate)
    pool = list(unique.values())
    if count <= 0 or len(pool) < count:
        raise PoolNotReady()

    unseen = [c for c in pool if c.id not in seen]
    rng.shuffle(unseen)
    seen_pool = [c for c in pool if c.id in seen]
    rng.shuffle(seen_pool)
    seen_pool.sort(key=lambda c: seen[c.id])  # stable: oldest first, random ties

    weights = {m.id: m.lesson_count for m in modules}

    def availability(items: list[DrawCandidate]) -> dict:
        counts: dict = {}
        for c in items:
            counts[c.module_id] = counts.get(c.module_id, 0) + 1
        return counts

    distinct_unseen_concepts = len({_concept_key(c.concept) for c in unseen})
    if min(len(unseen), distinct_unseen_concepts) >= count:
        quotas = allocate_quotas(weights, availability(unseen), count)
    else:
        quotas = allocate_quotas(weights, availability(pool), count)

    picked: list[DrawCandidate] = []
    picked_ids: set[uuid.UUID] = set()
    used_concepts: set[str] = set()
    per_module: dict = {}

    def take(c: DrawCandidate, unique_concept: bool) -> bool:
        if c.id in picked_ids:
            return False
        key = _concept_key(c.concept)
        if unique_concept and key in used_concepts:
            return False
        picked.append(c)
        picked_ids.add(c.id)
        used_concepts.add(key)
        per_module[c.module_id] = per_module.get(c.module_id, 0) + 1
        return True

    def fill(tier: list[DrawCandidate], respect_quota: bool, unique_concept: bool, by_difficulty: bool) -> None:
        for module_id in sorted(quotas, key=str) if respect_quota else [None]:
            module_items = [c for c in tier if module_id is None or c.module_id == module_id]
            if by_difficulty and module_id is not None:
                for difficulty, wanted in _difficulty_targets(quotas[module_id]):
                    taken = 0
                    for c in module_items:
                        if taken >= wanted or per_module.get(module_id, 0) >= quotas[module_id]:
                            break
                        if c.difficulty == difficulty and take(c, unique_concept):
                            taken += 1
                continue
            for c in module_items:
                if len(picked) >= count:
                    return
                if respect_quota and per_module.get(c.module_id, 0) >= quotas.get(c.module_id, 0):
                    continue
                take(c, unique_concept)

    # Unseen first (by quota and difficulty, then by quota, then anywhere),
    # then seen-longest-ago, then - only if concepts collide - relax concepts.
    passes = (
        (unseen, True, True, True),
        (unseen, True, True, False),
        (unseen, False, True, False),
        (seen_pool, True, True, False),
        (seen_pool, False, True, False),
        (unseen, False, False, False),
        (seen_pool, False, False, False),
    )
    for tier, respect_quota, unique_concept, by_difficulty in passes:
        if len(picked) >= count:
            break
        fill(tier, respect_quota, unique_concept, by_difficulty)

    if len(picked) < count:
        raise PoolNotReady()
    picked = picked[:count]
    rng.shuffle(picked)
    return [
        DrawnQuestion(
            question_id=c.id,
            module_id=c.module_id,
            permutation=tuple(rng.sample(range(OPTION_COUNT), OPTION_COUNT)),
        )
        for c in picked
    ]
