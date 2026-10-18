"""Certificate IDs: WNS-{EXAM_CODE}-{YYYY}-{8 Crockford base32 chars}."""

import re
from collections.abc import Callable

ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
RANDOM_LENGTH = 8
EXAM_CODE_PATTERN = re.compile(r"^[A-Z][A-Z0-9]{1,11}$")
EXAM_CODE_RULE = "code must be 2-12 upper-case letters/digits starting with a letter"
CERTIFICATE_ID_PATTERN = re.compile(r"^WNS-[A-Z][A-Z0-9]{1,11}-\d{4}-[0-9A-HJKMNP-TV-Z]{8}$")
MAX_ID_LENGTH = 40


def build_certificate_id(exam_code: str, year: int, choice: Callable[[str], str]) -> str:
    """`choice` picks one character from a string (secrets.choice in production)."""
    suffix = "".join(choice(ALPHABET) for _ in range(RANDOM_LENGTH))
    return f"WNS-{exam_code}-{year:04d}-{suffix}"


def normalize_certificate_id(raw: str) -> str | None:
    """Upper-cased, trimmed ID if it has the right shape, else None (so an
    obviously wrong ID never reaches the database)."""
    if not isinstance(raw, str):
        return None
    candidate = raw.strip().upper()
    if not candidate or len(candidate) > MAX_ID_LENGTH:
        return None
    return candidate if CERTIFICATE_ID_PATTERN.fullmatch(candidate) else None
