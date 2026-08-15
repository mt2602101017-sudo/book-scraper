"""ISBN normalisation, checksum validation and 10 <-> 13 conversion.

Pure stdlib, no I/O. Every public function is total: it either returns a
correct value or raises :class:`ValueError` with a message that names the
offending input and says what was wrong with it.

Reference checksums (used in ``tests/test_isbn.py``):
    ``0143127551`` <-> ``9780143127550``
    ``9780062316097`` <-> ``0062316095``
"""

from __future__ import annotations

import re
from typing import List, Optional, Tuple

__all__ = [
    "normalize",
    "is_valid_isbn10",
    "is_valid_isbn13",
    "isbn10_check_digit",
    "isbn13_check_digit",
    "isbn10_to_isbn13",
    "isbn13_to_isbn10",
    "to_isbn13",
    "hyphenate",
    "variants",
]

#: Characters we silently discard: ASCII hyphen, all Unicode dashes we are
#: likely to meet in copy-pasted text, every kind of space, and underscores.
_STRIP_RE = re.compile(r"[\s \-‐‑‒–—―−_]+")

#: Book EAN prefixes. A 13-digit ISBN must start with one of these.
_VALID_PREFIXES = ("978", "979")


def normalize(raw: str) -> str:
    """Strip hyphens/spaces/underscores and upper-case a trailing ``x``.

    Also tolerates the common ``ISBN:`` / ``ISBN-13:`` label prefix that shows
    up when scraping product pages, and does *not* validate anything -- use
    :func:`is_valid_isbn10` / :func:`is_valid_isbn13` for that.

    >>> normalize(" isbn-10: 0-306-40615-x ")
    '030640615X'
    """
    if not isinstance(raw, str):
        raise ValueError(f"ISBN must be a string, got {type(raw).__name__}")
    s = raw.strip()
    # Drop a leading "ISBN", "ISBN10", "ISBN-13", "eISBN" style label.
    s = re.sub(r"^\s*e?isbn(?:[\s\-_]*1[03])?\s*[:=]?\s*", "", s, flags=re.IGNORECASE)
    s = _STRIP_RE.sub("", s)
    return s.upper()


def isbn10_check_digit(first9: str) -> str:
    """Return the ISBN-10 check character for 9 leading digits (may be ``'X'``).

    ISBN-10 is valid when ``sum(d_i * (10 - i)) % 11 == 0`` for ``i`` in 0..9,
    with ``X`` standing for the value 10.
    """
    if len(first9) != 9 or not first9.isdigit():
        raise ValueError(f"ISBN-10 body must be exactly 9 digits, got {first9!r}")
    total = sum(int(ch) * (10 - i) for i, ch in enumerate(first9))
    check = (11 - (total % 11)) % 11
    return "X" if check == 10 else str(check)


def isbn13_check_digit(first12: str) -> str:
    """Return the ISBN-13 check digit for 12 leading digits.

    ISBN-13 is valid when ``sum(d_i * (1 if i even else 3)) % 10 == 0``.
    """
    if len(first12) != 12 or not first12.isdigit():
        raise ValueError(f"ISBN-13 body must be exactly 12 digits, got {first12!r}")
    total = sum(int(ch) * (1 if i % 2 == 0 else 3) for i, ch in enumerate(first12))
    return str((10 - (total % 10)) % 10)


def is_valid_isbn10(s: str) -> bool:
    """True if ``s`` normalises to a checksum-valid ISBN-10. Never raises."""
    try:
        v = normalize(s)
    except ValueError:
        return False
    if len(v) != 10:
        return False
    if not v[:9].isdigit():
        return False
    if not (v[9].isdigit() or v[9] == "X"):
        return False
    return isbn10_check_digit(v[:9]) == v[9]


def is_valid_isbn13(s: str) -> bool:
    """True if ``s`` normalises to a checksum-valid ISBN-13. Never raises.

    Also requires the ``978``/``979`` Bookland prefix: a 13-digit EAN outside
    those prefixes is not an ISBN even when its checksum happens to work out.
    """
    try:
        v = normalize(s)
    except ValueError:
        return False
    if len(v) != 13 or not v.isdigit():
        return False
    if not v.startswith(_VALID_PREFIXES):
        return False
    return isbn13_check_digit(v[:12]) == v[12]


def isbn10_to_isbn13(s: str) -> str:
    """Convert a valid ISBN-10 to its ISBN-13 form (always ``978``-prefixed).

    :raises ValueError: if ``s`` is not a checksum-valid ISBN-10.
    """
    v = normalize(s)
    if not is_valid_isbn10(v):
        raise ValueError(
            f"{s!r} is not a valid ISBN-10 (normalised to {v!r}); "
            "expected 9 digits plus a check digit or 'X' with a valid mod-11 checksum"
        )
    body = "978" + v[:9]
    return body + isbn13_check_digit(body)


def isbn13_to_isbn10(s: str) -> Optional[str]:
    """Convert an ISBN-13 back to ISBN-10, or return ``None`` when impossible.

    Only ``978``-prefixed ISBN-13s have an ISBN-10 equivalent; ``979`` ones
    (and anything invalid) yield ``None`` rather than raising, because callers
    treat the ISBN-10 as an optional nicety.
    """
    try:
        v = normalize(s)
    except ValueError:
        return None
    if not is_valid_isbn13(v):
        return None
    if not v.startswith("978"):
        return None
    body = v[3:12]
    return body + isbn10_check_digit(body)


def to_isbn13(raw: str) -> str:
    """Coerce an ISBN-10 or ISBN-13 in any punctuation style to a bare ISBN-13.

    This is the single entry point the CLI and pipeline use.

    :raises ValueError: with a message that states the normalised input, its
        length, and which checksum failed.
    """
    if raw is None:
        raise ValueError("No ISBN supplied: expected a 10- or 13-digit ISBN")
    v = normalize(raw)
    if not v:
        raise ValueError(f"No ISBN found in input {raw!r}")

    if len(v) == 13:
        if not v.startswith(_VALID_PREFIXES):
            raise ValueError(
                f"{raw!r} normalised to {v!r}, which is 13 digits but does not start "
                "with the Bookland prefix 978 or 979, so it is not an ISBN-13"
            )
        if not v.isdigit():
            raise ValueError(
                f"{raw!r} normalised to {v!r}: a 13-digit ISBN must be all digits "
                "('X' is only legal as an ISBN-10 check digit)"
            )
        expected = isbn13_check_digit(v[:12])
        if expected != v[12]:
            raise ValueError(
                f"{raw!r} normalised to {v!r} has a bad ISBN-13 checksum: "
                f"check digit is {v[12]!r} but should be {expected!r}"
            )
        return v

    if len(v) == 10:
        if not v[:9].isdigit() or not (v[9].isdigit() or v[9] == "X"):
            raise ValueError(
                f"{raw!r} normalised to {v!r}: an ISBN-10 must be 9 digits followed "
                "by a digit or 'X'"
            )
        expected10 = isbn10_check_digit(v[:9])
        if expected10 != v[9]:
            raise ValueError(
                f"{raw!r} normalised to {v!r} has a bad ISBN-10 checksum: "
                f"check digit is {v[9]!r} but should be {expected10!r}"
            )
        return isbn10_to_isbn13(v)

    raise ValueError(
        f"{raw!r} normalised to {v!r}, which is {len(v)} characters long; "
        "an ISBN must be 10 or 13 characters after removing hyphens and spaces"
    )


# ---------------------------------------------------------------------------
# Hyphenation (best-effort, cosmetic only)
# ---------------------------------------------------------------------------
# Splitting an ISBN correctly needs the ISBN International RangeMessage. We
# ship only the parts we can vouch for:
#   * registration-group ranges for the 978 prefix (the classic, stable table)
#   * registrant lengths for group 978-0 (English, US/UK) -- by far the most
#     common case in this project's test data
# For any other group we emit prefix-group-body-check, which is still readable
# and never *wrong*, just less finely divided. hyphenate() never raises.

#: (inclusive_low, inclusive_high, group_digit_count) over the digits after "978".
_GROUP_RANGES_978: Tuple[Tuple[int, int, int], ...] = (
    (0, 5, 1),        # 0-5
    (600, 649, 3),    # 600-649
    (65, 65, 2),      # 65
    (7, 7, 1),        # 7
    (80, 94, 2),      # 80-94
    (950, 989, 3),    # 950-989
    (9900, 9989, 4),  # 9900-9989
    (99900, 99999, 5),
)

#: For group 978-0: (inclusive_low, inclusive_high, registrant_digit_count)
#: compared against the first 7 digits after the group, read as an integer
#: (so "0623160" -> 623160).
_REGISTRANT_RANGES_978_0: Tuple[Tuple[int, int, int], ...] = (
    (0, 1999999, 2),
    (2000000, 2279999, 3),
    (2280000, 2289999, 4),
    (2290000, 3689999, 3),
    (3690000, 3699999, 4),
    (3700000, 6389999, 3),
    (6390000, 6397999, 4),
    (6398000, 6399999, 5),
    (6400000, 6449999, 3),
    (6450000, 6459999, 7),
    (6460000, 6479999, 3),
    (6480000, 6489999, 7),
    (6490000, 6549999, 3),
    (6550000, 6559999, 4),
    (6560000, 6999999, 3),
    (7000000, 8499999, 2),
    (8500000, 8999999, 3),
    (9000000, 9499999, 4),
    (9500000, 9999999, 5),
)


def _group_length(after_prefix: str) -> Optional[int]:
    """Return how many digits of ``after_prefix`` form the registration group."""
    for low, high, length in _GROUP_RANGES_978:
        candidate = after_prefix[:length]
        if len(candidate) < length or not candidate.isdigit():
            continue
        if low <= int(candidate) <= high:
            return length
    return None


def _registrant_length(group: str, after_group: str) -> Optional[int]:
    """Return the registrant length for ``group``'s body, or ``None`` if unknown.

    Only registration group ``978-0`` is tabulated. Splitting the registrant from
    the publication element for the other groups needs the full ISBN
    International *RangeMessage* (a few hundred ranges, revised continuously);
    shipping a half-remembered copy of it would produce confidently wrong output,
    so unknown groups deliberately return ``None`` and :func:`hyphenate` falls
    back to the coarser ``prefix-group-body-check`` form. That form has one hyphen
    fewer than the canonical rendering but never misplaces one.
    """
    if group != "0":
        return None
    key = after_group[:7].ljust(7, "0")
    if not key.isdigit():
        return None
    value = int(key)
    for low, high, length in _REGISTRANT_RANGES_978_0:
        if low <= value <= high:
            # Nothing left for the publication element -> refuse to split.
            return length if length < len(after_group) else None
    return None


def hyphenate(isbn13: str) -> str:
    """Return a hyphenated, human-friendly ISBN-13. Best-effort and cosmetic.

    Falls back to progressively coarser splits (and finally the bare digits)
    rather than raising, so it is always safe to call in a log line.

    >>> hyphenate("9780143127550")
    '978-0-14-312755-0'
    """
    try:
        v = normalize(isbn13)
    except ValueError:
        return str(isbn13)
    if len(v) != 13 or not v.isdigit():
        return v

    prefix, rest, check = v[:3], v[3:12], v[12]
    if prefix != "978":
        # 979-10/11/12 are 2-digit groups; 979-8 is a 1-digit group.
        glen = 1 if rest.startswith("8") else 2
        if glen < len(rest):
            return f"{prefix}-{rest[:glen]}-{rest[glen:]}-{check}"
        return f"{prefix}-{rest}-{check}"

    glen = _group_length(rest)
    if glen is None or glen >= len(rest):
        return f"{prefix}-{rest}-{check}"
    group, after_group = rest[:glen], rest[glen:]

    rlen = _registrant_length(group, after_group)
    if rlen is not None:
        registrant, publication = after_group[:rlen], after_group[rlen:]
        return f"{prefix}-{group}-{registrant}-{publication}-{check}"

    return f"{prefix}-{group}-{after_group}-{check}"


def variants(raw: str) -> List[str]:
    """Return the distinct ISBN spellings a search box might accept.

    Order: ISBN-13, ISBN-10 (when it exists), hyphenated ISBN-13. Useful for
    adapters that need to retry a site search with a different form.
    Returns ``[]`` instead of raising when ``raw`` is not a valid ISBN.
    """
    try:
        thirteen = to_isbn13(raw)
    except ValueError:
        return []
    out = [thirteen]
    ten = isbn13_to_isbn10(thirteen)
    if ten:
        out.append(ten)
    pretty = hyphenate(thirteen)
    if pretty != thirteen:
        out.append(pretty)
    return out
