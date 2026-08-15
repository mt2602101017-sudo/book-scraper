"""ISBN-10/13 normalisation, checksums and conversion. Pure stdlib, no I/O."""

from __future__ import annotations

import re
from typing import Optional

#: Hyphens, dashes, spaces and underscores are all discarded.
_STRIP = re.compile(r"[\s \-‐‑‒–—―−_]+")
#: A copy-pasted "ISBN-13:" style label, which scraped pages carry.
_LABEL = re.compile(r"^\s*e?isbn(?:[\s\-_]*1[03])?\s*[:=]?\s*", re.IGNORECASE)
#: Bookland EAN prefixes: a 13-digit code outside these is not an ISBN.
_PREFIXES = ("978", "979")


def normalize(raw: str) -> str:
    """Strip label, hyphens and spaces; upper-case a trailing ``x``."""
    if not isinstance(raw, str):
        raise ValueError(f"ISBN must be a string, got {type(raw).__name__}")
    return _STRIP.sub("", _LABEL.sub("", raw.strip())).upper()


def check10(body9: str) -> str:
    """ISBN-10 check character for 9 digits; ``'X'`` stands for 10."""
    check = (11 - sum(int(c) * (10 - i) for i, c in enumerate(body9)) % 11) % 11
    return "X" if check == 10 else str(check)


def check13(body12: str) -> str:
    """ISBN-13 check digit for 12 digits."""
    return str((10 - sum(int(c) * (1, 3)[i % 2] for i, c in enumerate(body12)) % 10) % 10)


def to_isbn13(raw: str) -> str:
    """Coerce an ISBN-10 or ISBN-13 in any punctuation style to a bare ISBN-13.

    Raises :class:`ValueError` naming the normalised input and what was wrong
    with it, because the message is shown against a CSV row number.
    """
    v = normalize(raw or "")
    if not v:
        raise ValueError(f"no ISBN found in {raw!r}")
    if len(v) == 13:
        if not v.isdigit() or not v.startswith(_PREFIXES):
            raise ValueError(f"{raw!r} -> {v!r}: 13 digits must be numeric and start 978/979")
        if check13(v[:12]) != v[12]:
            raise ValueError(f"{raw!r} -> {v!r}: check digit should be {check13(v[:12])!r}")
        return v
    if len(v) == 10:
        if not v[:9].isdigit() or not (v[9].isdigit() or v[9] == "X"):
            raise ValueError(f"{raw!r} -> {v!r}: an ISBN-10 is 9 digits then a digit or 'X'")
        if check10(v[:9]) != v[9]:
            raise ValueError(f"{raw!r} -> {v!r}: check digit should be {check10(v[:9])!r}")
        body = "978" + v[:9]
        return body + check13(body)
    raise ValueError(f"{raw!r} -> {v!r} is {len(v)} characters; an ISBN is 10 or 13")


def to_isbn10(isbn: str) -> Optional[str]:
    """The ISBN-10 form, or ``None`` -- only 978-prefixed ISBN-13s have one."""
    try:
        v = to_isbn13(isbn)
    except ValueError:
        return None
    return v[3:12] + check10(v[3:12]) if v.startswith("978") else None


def is_valid(raw: str) -> bool:
    """True when ``raw`` is a checksum-valid ISBN-10 or ISBN-13. Never raises."""
    try:
        to_isbn13(raw)
    except ValueError:
        return False
    return True
