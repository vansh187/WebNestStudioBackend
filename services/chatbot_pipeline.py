import json
import re

_FENCE_PATTERN = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE | re.MULTILINE)
_PRICING_KEYWORDS = re.compile(
    r"[$₹€£]|\b(price|pricing|cost|costs|budget|quote|quotation|per hour|/hr|payment|payments)\b", re.IGNORECASE
)

COLLECTED_FIELD_KEYS = ("project_type", "project_goal", "pages_features", "budget_range", "timeline_expectation")


class MalformedChatOutputError(Exception):
    """Raised when the LLM's JSON output for a chat turn or plan generation
    cannot be parsed into the expected shape."""


def _parse_json_object(raw: str) -> dict:
    text = (raw or "").strip()
    text = _FENCE_PATTERN.sub("", text).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise MalformedChatOutputError(f"invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise MalformedChatOutputError("JSON root was not an object")
    return data


def parse_chat_turn(raw: str) -> dict:
    """Validates the per-turn chat envelope: {"reply": str, "collected_fields":
    {...}, "ready_for_plan": bool}. Unknown/missing collected_fields keys are
    tolerated (coerced to null) - only "reply" being a non-empty string and
    "ready_for_plan" being a bool are hard requirements."""
    data = _parse_json_object(raw)

    reply = data.get("reply")
    if not isinstance(reply, str) or not reply.strip():
        raise MalformedChatOutputError("Missing or empty 'reply'")

    ready_for_plan = data.get("ready_for_plan")
    if not isinstance(ready_for_plan, bool):
        raise MalformedChatOutputError("'ready_for_plan' must be a boolean")

    raw_fields = data.get("collected_fields")
    if not isinstance(raw_fields, dict):
        raise MalformedChatOutputError("'collected_fields' must be an object")

    collected_fields: dict[str, object] = {}
    for key in COLLECTED_FIELD_KEYS:
        value = raw_fields.get(key)
        collected_fields[key] = _normalize_field_value(key, value)

    return {"reply": reply.strip(), "collected_fields": collected_fields, "ready_for_plan": ready_for_plan}


def _normalize_field_value(key: str, value: object) -> object:
    """These values get stored straight into Text DB columns and echoed back
    to the frontend - a model that returns a number/bool/nested object for a
    field instead of the requested string must not be allowed to reach the
    database layer (asyncpg rejects non-str binds to Text columns outright),
    so every shape other than the expected one is coerced or dropped here
    rather than trusted as-is."""
    if value is None:
        return None

    if key == "pages_features":
        if isinstance(value, list):
            items = [str(v).strip() for v in value if isinstance(v, (str, int, float)) and str(v).strip()]
            return items or None
        if isinstance(value, str):
            return [value] if value.strip() else None
        if isinstance(value, (int, float, bool)):
            return [str(value)]
        return None

    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    # bools, dicts, lists (for a scalar field) etc. have no sensible string
    # form worth keeping - drop rather than risk a garbled/misleading value.
    return None


def parse_plan_output(raw: str, max_weeks: int) -> dict:
    """Validates the plan-generation envelope: {"total_weeks": int, "weeks":
    [{"week_range", "title", "description"}, ...]}."""
    data = _parse_json_object(raw)

    total_weeks = data.get("total_weeks")
    if isinstance(total_weeks, bool):
        total_weeks = None
    elif isinstance(total_weeks, str) and total_weeks.strip().isdigit():
        total_weeks = int(total_weeks.strip())
    if not isinstance(total_weeks, int) or not (1 <= total_weeks <= max_weeks):
        raise MalformedChatOutputError(f"'total_weeks' must be an integer between 1 and {max_weeks}")

    weeks_raw = data.get("weeks")
    if not isinstance(weeks_raw, list) or not weeks_raw:
        raise MalformedChatOutputError("'weeks' must be a non-empty list")

    weeks: list[dict] = []
    for item in weeks_raw:
        if not isinstance(item, dict):
            raise MalformedChatOutputError("Each week entry must be an object")
        week_range = item.get("week_range")
        title = item.get("title")
        description = item.get("description")
        if not all(isinstance(v, str) and v.strip() for v in (week_range, title, description)):
            raise MalformedChatOutputError("Each week entry needs non-empty week_range/title/description strings")
        weeks.append({"week_range": week_range.strip(), "title": title.strip(), "description": description.strip()})

    return {"total_weeks": total_weeks, "weeks": weeks}


def scrub_pricing_language(weeks: list[dict]) -> tuple[list[dict], bool]:
    """Defense-in-depth beyond the prompt instruction: strips any sentence
    that mentions pricing/cost/currency from each week's title/description.
    Returns the (possibly modified) weeks plus whether anything was flagged,
    so the caller can log it for review rather than fail the whole request."""
    flagged = False
    cleaned_weeks = []
    for week in weeks:
        cleaned = dict(week)
        for field in ("title", "description"):
            text = cleaned[field]
            if _PRICING_KEYWORDS.search(text):
                flagged = True
                sentences = re.split(r"(?<=[.!?])\s+", text)
                kept = [s for s in sentences if not _PRICING_KEYWORDS.search(s)]
                cleaned[field] = " ".join(kept).strip() or text
        cleaned_weeks.append(cleaned)
    return cleaned_weeks, flagged


def contains_pricing_language(weeks: list[dict]) -> bool:
    return any(_PRICING_KEYWORDS.search(week["title"]) or _PRICING_KEYWORDS.search(week["description"]) for week in weeks)
