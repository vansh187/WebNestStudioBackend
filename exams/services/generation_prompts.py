"""Prompts for drafting and cross-checking exam questions, and defensive
parsing of what the models return."""

import json
import random
import re

from exams.domain.validation import DraftQuestion

# Version baselines the questions are written against (contract section 8).
COURSE_BASELINES = {
    "java-core": "Java 17",
    "advanced-java": "Java 17",
    "spring-framework": "Spring Framework 6 on Java 17",
    "spring-boot": "Spring Boot 4.1 on Java 17",
    "python": "Python 3.12",
    "html": "HTML Living Standard",
    "css": "CSS as supported by current evergreen browsers",
    "javascript": "ECMAScript 2023 (Node.js 20)",
    "react": "React 18",
    "database-sql": "standard SQL as supported by SQLite 3",
    "postgresql": "PostgreSQL 16",
    "database-design": "relational database design fundamentals",
}

# Languages whose code questions can really be executed (plan 6.4 #5). A
# course not listed here gets concept/scenario questions only.
COURSE_CODE_LANGUAGES = {
    "java-core": "java",
    "advanced-java": "java",
    "python": "python",
    "javascript": "javascript",
    "database-sql": "sql",
}

_LANGUAGE_RULES = {
    "python": "Python code is a complete script that prints its result with print(); standard library only (no files, network or imports beyond math, collections, itertools, functools, string, re).",
    "java": "Java code is one complete program: `public class Main` with a `main` method that prints with System.out.println.",
    "javascript": "JavaScript code is a complete Node.js script that prints with console.log; no imports.",
    "sql": "SQL code is a self-contained SQLite script: CREATE TABLE and INSERT statements followed by one final SELECT. expected_output is the final SELECT's rows, one row per line, columns joined by ' | ', NULL written as NULL.",
}

DRAFT_SYSTEM_PROMPT = """You write final-exam questions for an online programming course.
Every question is single-answer multiple choice with exactly 4 options and exactly one correct option.
Use only facts stated in the lesson material you are given, or standard, version-stable knowledge of the course baseline.
Prefer applied questions (predict the output of a short snippet, choose the right approach for a scenario, spot the bug) over definitions.
Distractors are plausible, of similar length to the correct option, and clearly wrong to an expert.
Never use "all of the above", "none of the above", "both A and B", trick wording, or negatives in capitals.
Write prompts and options in Markdown; put code in backticks or fenced blocks; never use raw HTML.
Respond with a single JSON object and nothing else."""

CHECK_SYSTEM_PROMPT = """You are an independent expert examiner. Answer each multiple-choice question on your own.
For each question report the index (0-3) of the single best option, whether exactly one option is correct,
whether the question depends on a language/library version in a way that makes the answer ambiguous,
and your confidence from 0 to 1. Respond with a single JSON object and nothing else."""

CHECK_SYSTEM_PROMPT_ALT = """Act as a strict reviewer verifying exam questions before publication.
Work through each question independently and pick the option that is correct. Flag a question if more than one
option could be defended, if none is correct, or if the answer changes between versions of the technology.
Give a calibrated confidence between 0 and 1. Reply with one JSON object only."""


def _lesson_block(lesson: dict) -> dict:
    return {
        "lesson_id": str(lesson.get("lesson_id") or ""),
        "title": str(lesson.get("title") or "")[:200],
        "summary": str(lesson.get("summary") or "")[:3000],
        "key_points": [str(p)[:500] for p in (lesson.get("key_points") or [])][:30],
    }


def build_draft_prompt(
    course_slug: str,
    course_title: str,
    module_title: str,
    lessons: list[dict],
    covered_concepts: list[str],
    avoid_prompts: list[str],
    count: int,
    difficulty_counts: dict[str, int],
    code_language: str | None,
) -> str:
    kinds = ["concept", "scenario"]
    if code_language:
        kinds += ["code_output", "spot_the_bug"]
    lines = [
        f"Course: {course_title} (baseline: {COURSE_BASELINES.get(course_slug, 'the current stable version')})",
        f"Module: {module_title}",
        f"Write exactly {count} questions on the lessons below.",
        "Difficulty mix: " + ", ".join(f"{n} {d}" for d, n in difficulty_counts.items() if n),
        "Allowed kinds: " + ", ".join(kinds) + ".",
    ]
    if code_language:
        lines.append(
            "For code_output questions the correct option must be exactly the program's output and expected_output must repeat it. "
            + _LANGUAGE_RULES[code_language]
        )
    else:
        lines.append("Do not write code_output or spot_the_bug questions for this course.")
    lines += [
        "",
        "Lessons (JSON):",
        json.dumps([_lesson_block(lesson) for lesson in lessons], ensure_ascii=False),
        "",
        "Concepts already covered - do not test these again: "
        + (json.dumps(covered_concepts[:80], ensure_ascii=False) if covered_concepts else "none"),
        "Public quiz questions on the site - do not reuse or paraphrase: "
        + (json.dumps([p[:300] for p in avoid_prompts[:40]], ensure_ascii=False) if avoid_prompts else "none"),
        "",
        "Return JSON of this shape:",
        '{"questions": [{"prompt": "...", "options": ["...", "...", "...", "..."], "correct_index": 0, '
        '"explanation": "...", "difficulty": "easy|medium|hard", "kind": "concept|scenario|code_output|spot_the_bug", '
        '"lesson_id": "<one of the lesson_id values above>", "concept": "short tag, 2-5 words", '
        '"source_fact": "the sentence or key point from the lesson material this tests, quoted", '
        '"why_others_wrong": ["...", "...", "..."], "code": "only for code kinds", "expected_output": "only for code_output"}]}',
        "Vary the position of the correct option.",
    ]
    return "\n".join(lines)


def build_check_prompt(items: list[tuple[str, DraftQuestion, list[str]]]) -> str:
    """items: (label, question, options in the order to show)."""
    questions = []
    for label, draft, options in items:
        entry = {"id": label, "question": draft.prompt, "options": options}
        if draft.code:
            entry["code"] = draft.code
        questions.append(entry)
    return (
        "Questions (JSON):\n"
        + json.dumps(questions, ensure_ascii=False)
        + '\n\nReturn {"answers": [{"id": "<id>", "answer_index": 0, "single_correct": true, '
        '"version_issue": false, "confidence": 0.9}]} with one entry per question.'
    )


def extract_json_object(text: str) -> object | None:
    """Parses a model reply that should be JSON, tolerating code fences and
    leading/trailing prose. None when nothing parses."""
    if not isinstance(text, str):
        return None
    candidate = text.strip()
    candidate = re.sub(r"^```[a-zA-Z]*\s*", "", candidate)
    candidate = re.sub(r"\s*```$", "", candidate)
    for attempt in (candidate,):
        try:
            return json.loads(attempt)
        except (ValueError, RecursionError):
            pass
    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = candidate.find(opener), candidate.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(candidate[start : end + 1])
            except (ValueError, RecursionError):
                continue
    return None


def _text(value, limit: int) -> str:
    return value.strip()[:limit] if isinstance(value, str) else ""


def _index(value) -> int:
    if isinstance(value, bool):
        return -1
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    if isinstance(value, str) and value.strip().upper() in ("A", "B", "C", "D"):
        return "ABCD".index(value.strip().upper())
    return -1


def parse_draft_item(item: object) -> DraftQuestion | None:
    """One drafted/imported question, or None if it isn't even an object.
    Field-level problems are left for check_shape to report."""
    if not isinstance(item, dict):
        return None
    options = item.get("options")
    options = [o if isinstance(o, str) else str(o) for o in options] if isinstance(options, list) else []
    reasons = item.get("why_others_wrong")
    reasons = [r for r in reasons if isinstance(r, str)] if isinstance(reasons, list) else []
    code = item.get("code")
    expected = item.get("expected_output")
    return DraftQuestion(
        prompt=_text(item.get("prompt"), 5000),
        options=options[:6],
        correct_index=_index(item.get("correct_index")),
        explanation=_text(item.get("explanation"), 5000),
        difficulty=_text(item.get("difficulty"), 20).lower(),
        kind=_text(item.get("kind"), 20).lower() or "concept",
        concept=_text(item.get("concept"), 300),
        lesson_ref=_text(item.get("lesson_id"), 200),
        source_fact=_text(item.get("source_fact"), 3000) or None,
        why_others_wrong=reasons[:5],
        code=code if isinstance(code, str) and code.strip() else None,
        expected_output=expected if isinstance(expected, str) and expected.strip() else None,
    )


def parse_draft_reply(text: str) -> list[object]:
    data = extract_json_object(text)
    if isinstance(data, dict):
        data = data.get("questions")
    return list(data) if isinstance(data, list) else []


def parse_check_reply(text: str) -> dict[str, dict]:
    data = extract_json_object(text)
    if isinstance(data, dict):
        data = data.get("answers")
    answers: dict[str, dict] = {}
    if not isinstance(data, list):
        return answers
    for entry in data:
        if not isinstance(entry, dict):
            continue
        label = str(entry.get("id", "")).strip()
        if not label:
            continue
        try:
            confidence = float(entry.get("confidence", 0))
        except (TypeError, ValueError):
            confidence = 0.0
        answers[label] = {
            "answer": _index(entry.get("answer_index")),
            "single_correct": entry.get("single_correct") is True,
            "version_issue": entry.get("version_issue") is True,
            "confidence": max(0.0, min(1.0, confidence)),
        }
    return answers


def shuffled_options(draft: DraftQuestion, rng: random.Random) -> tuple[list[str], list[int]]:
    """Options in a random order plus order[displayed] = original index."""
    order = list(range(len(draft.options)))
    rng.shuffle(order)
    return [draft.options[i] for i in order], order
