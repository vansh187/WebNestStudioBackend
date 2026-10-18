"""Question shape and grounding checks (plan section 6.4 #1-#2), prompt
normalization and hashing (6.5), and the certificate recipient-name rule (4.6)."""

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field

DIFFICULTIES = ("easy", "medium", "hard")
KINDS = ("concept", "scenario", "code_output", "spot_the_bug")
CODE_KINDS = ("code_output", "spot_the_bug")

MAX_PROMPT_CHARS = 1500
MAX_OPTION_CHARS = 500
MAX_EXPLANATION_CHARS = 2000
MAX_CONCEPT_CHARS = 120
MAX_SOURCE_FACT_CHARS = 1000
MAX_CODE_CHARS = 4000
MAX_EXPECTED_OUTPUT_CHARS = 2000
OPTION_LENGTH_RATIO = 3
# Short answers ("4", "None", "O(n)") would otherwise fail the 3x ratio.
OPTION_LENGTH_FLOOR = 4
GROUNDING_OVERLAP = 0.6
MIN_FACT_TOKENS = 3

_BANNED_OPTION_PATTERNS = (
    re.compile(r"\b(all|none)\s+of\s+(the\s+)?(above|these|them)\b", re.IGNORECASE),
    re.compile(r"\bboth\s+\(?[a-d]\)?\s+and\s+\(?[a-d]\)?\b", re.IGNORECASE),
    re.compile(r"\bboth\s+(of\s+)?(the\s+)?above\b", re.IGNORECASE),
    re.compile(r"\bneither\s+\(?[a-d]\)?\s+nor\s+\(?[a-d]\)?\b", re.IGNORECASE),
)
_CODE_FENCE = re.compile(r"```.*?(```|$)", re.DOTALL)
_INLINE_CODE = re.compile(r"`[^`\n]*`")
_RAW_HTML = re.compile(
    r"<\s*/?\s*(script|iframe|style|img|a|div|span|p|br|hr|html|head|body|object|embed|svg|math|form|input|"
    r"button|textarea|select|link|meta|base|table|tr|td|th|video|audio|source|frame|frameset|details|summary)\b[^>]*>",
    re.IGNORECASE,
)
# Any tag carrying an event handler or a javascript: URL, and Markdown links
# to javascript: - plain prose such as "if one = 2" is not matched.
_HTML_EVENT_OR_URL = re.compile(
    r"(<[a-z][^>]*\bon[a-z]+\s*=|<[a-z][^>]*javascript\s*:|\]\(\s*<?\s*javascript\s*:)", re.IGNORECASE
)
_TOKEN = re.compile(r"[a-z0-9_]+")
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")
_NAME_ALLOWED_PUNCTUATION = set(" .'-’")


@dataclass
class DraftQuestion:
    """A question as drafted, imported or written by an admin, before checks."""

    prompt: str
    options: list[str]
    correct_index: int
    explanation: str
    difficulty: str
    kind: str
    concept: str
    lesson_ref: str  # lesson UUID or key as supplied
    source_fact: str | None = None
    why_others_wrong: list[str] = field(default_factory=list)
    code: str | None = None
    expected_output: str | None = None


def normalize_text(text: str) -> str:
    """Lower-case word tokens joined by single spaces (markdown and punctuation dropped)."""
    text = unicodedata.normalize("NFKC", text or "").lower()
    return " ".join(_TOKEN.findall(text))


def prompt_hash(prompt_norm: str, correct_option: str) -> str:
    material = f"{prompt_norm}\n{normalize_text(correct_option)}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def tokens(text: str) -> set[str]:
    return set(normalize_text(text).split())


def jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    union = a | b
    return len(a & b) / len(union) if union else 0.0


def strip_code(text: str) -> str:
    return _INLINE_CODE.sub(" ", _CODE_FENCE.sub(" ", text or ""))


def has_raw_html(text: str) -> bool:
    """Raw HTML outside code spans/fences (Markdown only; `List<String>` in
    backticks is fine)."""
    outside_code = strip_code(text)
    return bool(_RAW_HTML.search(outside_code) or _HTML_EVENT_OR_URL.search(outside_code))


def _option_plain(option: str) -> str:
    """Option text as compared with program output: code markers removed."""
    text = (option or "").strip()
    fenced = re.fullmatch(r"```[a-zA-Z0-9]*\s*\n?(.*?)\n?```", text, re.DOTALL)
    if fenced:
        return fenced.group(1)
    if len(text) >= 2 and text.startswith("`") and text.endswith("`"):
        return text.strip("`")
    return text


def option_as_output(option: str) -> str:
    return _option_plain(option)


def check_shape(draft: DraftQuestion, require_rationale: bool) -> list[str]:
    """Plan 6.4 #1. Returns failure reasons; empty means it passed."""
    problems: list[str] = []
    prompt = (draft.prompt or "").strip()
    if not prompt:
        problems.append("prompt is empty")
    elif len(prompt) > MAX_PROMPT_CHARS:
        problems.append(f"prompt is longer than {MAX_PROMPT_CHARS} characters")
    if has_raw_html(prompt):
        problems.append("prompt contains raw HTML")

    options = draft.options if isinstance(draft.options, list) else []
    if len(options) != 4 or not all(isinstance(o, str) for o in options):
        problems.append("there must be exactly 4 text options")
        return problems
    stripped = [o.strip() for o in options]
    if any(not o for o in stripped):
        problems.append("an option is empty")
    if any(len(o) > MAX_OPTION_CHARS for o in stripped):
        problems.append(f"an option is longer than {MAX_OPTION_CHARS} characters")
    if len({normalize_text(o) or o.lower() for o in stripped}) != 4:
        problems.append("options are not distinct")
    if any(pattern.search(o) for o in stripped for pattern in _BANNED_OPTION_PATTERNS):
        problems.append("uses all/none of the above or a combined option")
    if any(has_raw_html(o) for o in stripped):
        problems.append("an option contains raw HTML")
    lengths = [len(o) for o in stripped if o]
    if lengths and max(lengths) > OPTION_LENGTH_RATIO * max(min(lengths), OPTION_LENGTH_FLOOR):
        problems.append("option lengths are too uneven")

    if not isinstance(draft.correct_index, int) or isinstance(draft.correct_index, bool) or not 0 <= draft.correct_index <= 3:
        problems.append("correct_index must be 0-3")
    if not (draft.explanation or "").strip():
        problems.append("explanation is missing")
    elif len(draft.explanation) > MAX_EXPLANATION_CHARS:
        problems.append("explanation is too long")
    if draft.difficulty not in DIFFICULTIES:
        problems.append("difficulty must be easy, medium or hard")
    if draft.kind not in KINDS:
        problems.append("kind is not supported")
    concept = (draft.concept or "").strip()
    if not concept:
        problems.append("concept is missing")
    elif len(concept) > MAX_CONCEPT_CHARS:
        problems.append("concept is too long")
    if require_rationale:
        reasons = draft.why_others_wrong if isinstance(draft.why_others_wrong, list) else []
        if len(reasons) != 3 or not all(isinstance(r, str) and r.strip() for r in reasons):
            problems.append("why_others_wrong must give 3 reasons")
    if draft.source_fact is not None and len(draft.source_fact) > MAX_SOURCE_FACT_CHARS:
        problems.append("source_fact is too long")
    if draft.kind in CODE_KINDS:
        if not (draft.code or "").strip():
            problems.append("code questions need code")
        elif len(draft.code) > MAX_CODE_CHARS:
            problems.append("code is too long")
        if draft.kind == "code_output" and not (draft.expected_output or "").strip():
            problems.append("code_output questions need expected_output")
    if draft.expected_output is not None and len(draft.expected_output) > MAX_EXPECTED_OUTPUT_CHARS:
        problems.append("expected_output is too long")
    return problems


def context_passages(context: dict) -> list[str]:
    """Summary sentences, key points and titles of a module context."""
    passages: list[str] = []
    for lesson in context.get("lessons") or []:
        if not isinstance(lesson, dict):
            continue
        for value in (lesson.get("title"), lesson.get("summary")):
            if isinstance(value, str) and value.strip():
                passages.extend(p for p in _SENTENCE_SPLIT.split(value) if p.strip())
        for point in lesson.get("key_points") or []:
            if isinstance(point, str) and point.strip():
                passages.append(point)
    return passages


def is_grounded(source_fact: str, passages: list[str]) -> bool:
    """Plan 6.4 #2: the fact appears in the material (normalized substring) or
    shares >= 60% of its tokens with one summary sentence or key point."""
    fact_norm = normalize_text(source_fact)
    fact_tokens = set(fact_norm.split())
    if len(fact_tokens) < MIN_FACT_TOKENS:
        return False
    for passage in passages:
        passage_norm = normalize_text(passage)
        if fact_norm and fact_norm in passage_norm:
            return True
        passage_tokens = set(passage_norm.split())
        if passage_tokens and len(fact_tokens & passage_tokens) / len(fact_tokens) >= GROUNDING_OVERLAP:
            return True
    return False


def clean_recipient_name(raw: str) -> str:
    """Trimmed, inner spaces collapsed, 2-80 characters; letters, combining
    marks, spaces and . ' - only. Raises ValueError with a learner-facing message."""
    if not isinstance(raw, str):
        raise ValueError("Name must be text")
    name = " ".join(unicodedata.normalize("NFC", raw).split())
    if not 2 <= len(name) <= 80:
        raise ValueError("Name must be 2-80 characters")
    for char in name:
        if char in _NAME_ALLOWED_PUNCTUATION:
            continue
        if unicodedata.category(char)[0] not in ("L", "M"):
            raise ValueError("Name may only contain letters, spaces and . ' -")
    if not any(unicodedata.category(char)[0] == "L" for char in name):
        raise ValueError("Name must contain letters")
    return name
