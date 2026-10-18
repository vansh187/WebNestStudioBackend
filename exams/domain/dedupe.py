"""Duplicate rules (plan section 6.5), cheapest first: exact hash, wording
(token Jaccard), public lesson-quiz prompts, concept count, then meaning
(embedding cosine). A draft is compared with every non-rejected question of
its course and with the drafts already accepted from its own batch."""

import math
import uuid
from dataclasses import dataclass

from exams.domain.validation import jaccard


@dataclass(frozen=True)
class PoolEntry:
    id: uuid.UUID | None
    lesson_id: uuid.UUID
    concept_key: str
    prompt_hash: str
    tokens: frozenset[str]
    embedding: tuple[float, ...] | None = None


@dataclass(frozen=True)
class SemanticMatch:
    nearest_id: uuid.UUID | None
    similarity: float
    duplicate: bool


def concept_key(concept: str) -> str:
    return " ".join((concept or "").lower().split())


def textual_duplicate(
    candidate: PoolEntry,
    pool: list[PoolEntry],
    avoid_token_sets: list[frozenset[str]],
    jaccard_threshold: float,
) -> str | None:
    """Reason code for a hash / wording / public-quiz duplicate, else None."""
    for entry in pool:
        if entry.prompt_hash == candidate.prompt_hash:
            return "duplicate_exact"
    for entry in pool:
        if jaccard(candidate.tokens, entry.tokens) >= jaccard_threshold:
            return "duplicate_wording"
    for avoid in avoid_token_sets:
        if avoid and jaccard(candidate.tokens, avoid) >= jaccard_threshold:
            return "duplicate_public_quiz"
    return None


def concept_is_full(candidate: PoolEntry, pool: list[PoolEntry], max_per_concept: int) -> bool:
    used = sum(
        1 for entry in pool if entry.lesson_id == candidate.lesson_id and entry.concept_key == candidate.concept_key
    )
    return used >= max_per_concept


def cosine(a: tuple[float, ...] | list[float], b: tuple[float, ...] | list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def semantic_duplicate(
    candidate: PoolEntry,
    pool: list[PoolEntry],
    threshold: float,
    same_lesson_threshold: float,
) -> SemanticMatch:
    """>= threshold with any question, or >= same_lesson_threshold within the
    same lesson, is a duplicate. Entries without an embedding are skipped."""
    if candidate.embedding is None:
        return SemanticMatch(nearest_id=None, similarity=0.0, duplicate=False)
    best_id: uuid.UUID | None = None
    best = 0.0
    duplicate = False
    for entry in pool:
        if entry.embedding is None:
            continue
        similarity = cosine(candidate.embedding, entry.embedding)
        if similarity > best:
            best, best_id = similarity, entry.id
        limit = same_lesson_threshold if entry.lesson_id == candidate.lesson_id else threshold
        if similarity >= limit:
            duplicate = True
    return SemanticMatch(nearest_id=best_id, similarity=round(best, 4), duplicate=duplicate)
