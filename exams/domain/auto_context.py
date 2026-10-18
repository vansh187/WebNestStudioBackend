"""Builds exam module context from the lesson content stored in the database,
so exams need no admin upload (EXAM_ENABLED=true). Same shape as the
contract's uploaded context: {module_title, lessons: [{lesson_id, title,
summary, key_points, avoid_prompts}]}. An uploaded context always wins."""

import re
from html import unescape
from html.parser import HTMLParser

from exams.records import LessonMaterial

MAX_SUMMARY_CHARS = 3000
# The whole lesson (up to this much) is kept for grounding, so a fact from
# late in a long lesson still passes; only summary and key points are sent
# to the model.
MAX_TEXT_CHARS = 20000
MAX_KEY_POINTS = 40
MAX_KEY_POINT_CHARS = 400
MIN_KEY_POINT_WORDS = 5
_SKIPPED_TAGS = {"script", "style", "noscript", "svg", "head"}
_BLOCK_TAGS = {"p", "div", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "pre", "blockquote", "section", "article", "td", "th"}
_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9`\"'(])")
_MARKDOWN_NOISE = re.compile(r"(```.*?```|`|\*\*|__|^#+\s*|^\s*[-*+]\s+|^\s*\d+\.\s+|!\[[^\]]*\]\([^)]*\)|\[([^\]]*)\]\([^)]*\))", re.MULTILINE | re.DOTALL)


class _TextExtractor(HTMLParser):
    """Visible text of an HTML fragment, plus the text of each list item and
    heading-led paragraph (good key-point candidates)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.items: list[str] = []
        self._skip = 0
        # One buffer per open <li>: a nested list keeps the outer item's text.
        self._open_items: list[list[str]] = []

    def handle_starttag(self, tag, attrs):
        if tag in _SKIPPED_TAGS:
            self._skip += 1
        elif tag == "li":
            self._open_items.append([])
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in _SKIPPED_TAGS and self._skip:
            self._skip -= 1
        elif tag == "li" and self._open_items:
            text = " ".join("".join(self._open_items.pop()).split())
            if text:
                self.items.append(text)
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def close(self):
        super().close()
        while self._open_items:  # unclosed <li> at the end of the fragment
            text = " ".join("".join(self._open_items.pop()).split())
            if text:
                self.items.append(text)

    def handle_data(self, data):
        if self._skip:
            return
        self.parts.append(data)
        if self._open_items:
            # Text inside a nested item belongs to the inner item and, as
            # context, to every enclosing one.
            for buffer in self._open_items:
                buffer.append(data)


def _html_text(body: str) -> tuple[str, list[str]]:
    parser = _TextExtractor()
    try:
        parser.feed(body)
        parser.close()
    except Exception:
        # Malformed markup: fall back to stripping tags.
        return re.sub(r"<[^>]+>", " ", unescape(body)), []
    return "".join(parser.parts), parser.items


def _markdown_text(body: str) -> tuple[str, list[str]]:
    items = [m.group(1).strip() for m in re.finditer(r"^\s*(?:[-*+]|\d+\.)\s+(.+)$", body, re.MULTILINE)]
    text = _MARKDOWN_NOISE.sub(lambda m: m.group(2) or " ", body)
    return text, [_MARKDOWN_NOISE.sub(lambda m: m.group(2) or " ", item) for item in items]


def lesson_text(content: dict | None) -> tuple[str, list[str]]:
    """(plain text, list items) of a lesson's content blob {format, body}."""
    if not isinstance(content, dict):
        return "", []
    body = content.get("body")
    if not isinstance(body, str) or not body.strip():
        return "", []
    fmt = str(content.get("format") or "html").lower()
    text, items = _markdown_text(body) if fmt in ("md", "markdown") else _html_text(body)
    lines = [" ".join(line.split()) for line in text.splitlines()]
    return "\n".join(line for line in lines if line), [" ".join(i.split()) for i in items if i.strip()]


def _sentences(text: str) -> list[str]:
    return [s.strip() for line in text.splitlines() for s in _SENTENCE.split(line) if s.strip()]


def _key_points(text: str, items: list[str]) -> list[str]:
    """List items first, then sentences; when there are more than fit, an
    even spread across the whole lesson (not just its beginning)."""
    candidates: list[str] = []
    seen: set[str] = set()
    for candidate in items + _sentences(text):
        if len(candidate.split()) < MIN_KEY_POINT_WORDS:
            continue
        candidate = candidate[:MAX_KEY_POINT_CHARS]
        folded = candidate.casefold()
        if folded not in seen:
            seen.add(folded)
            candidates.append(candidate)
    if len(candidates) <= MAX_KEY_POINTS:
        return candidates
    step = len(candidates) / MAX_KEY_POINTS
    return [candidates[int(i * step)] for i in range(MAX_KEY_POINTS)]


def _summary(text: str) -> str:
    """At most MAX_SUMMARY_CHARS, ending on a whole sentence."""
    if len(text) <= MAX_SUMMARY_CHARS:
        return text
    out = ""
    for sentence in _sentences(text):
        if len(out) + len(sentence) + 1 > MAX_SUMMARY_CHARS:
            break
        out = f"{out} {sentence}".strip()
    return out or text[:MAX_SUMMARY_CHARS].rsplit(" ", 1)[0]


def build_module_context(course_slug: str, course_title: str, module_key: str, module_title: str, lessons: list[LessonMaterial]) -> dict | None:
    """The context for one module, or None when its lessons carry no usable
    text (then nothing can be grounded and no questions are generated)."""
    built = []
    for lesson in lessons:
        text, items = lesson_text(lesson.content)
        points = _key_points(text, items)
        if not points:
            continue
        built.append(
            {
                "lesson_id": lesson.key or str(lesson.lesson_id),
                "title": lesson.title,
                "summary": _summary(text),
                "text": text[:MAX_TEXT_CHARS],
                "key_points": points,
                "avoid_prompts": [p[:500] for p in lesson.quiz_prompts if isinstance(p, str)][:50],
            }
        )
    if not built:
        return None
    return {
        "course_slug": course_slug,
        "course_title": course_title,
        "module_id": module_key,
        "module_title": module_title,
        "lessons": built,
        "source": "auto",
    }
