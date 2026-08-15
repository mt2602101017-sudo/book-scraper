"""Structured-data helpers: JSON-LD, embedded JSON blobs, dates and lists.

Every site here publishes most of what we want as machine-readable data rather
than as prose -- Goodreads in a ``__NEXT_DATA__`` Apollo cache, Amazon and Audible
in ``schema.org`` JSON-LD, Kobo in both. Parsing that is far more durable than
chasing CSS classes, so these helpers exist to make the JSON route the easy one.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any, Dict, Iterable, Iterator, List, Optional, Union

from .parse import dedupe, text

_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
#: Splitters for "Fiction, Fantasy; Young Adult / Romance" style lists.
_LIST_SPLIT = re.compile(r"\s*(?:[,;/|]|\band\b|&)\s*", re.IGNORECASE)
#: Date layouts tried in order, most specific first.
_FORMATS = ("%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y", "%m/%d/%Y", "%B %d, %Y", "%b %d, %Y",
            "%B %d %Y", "%b %d %Y", "%d %B %Y", "%d %b %Y", "%B %Y", "%b %Y", "%Y-%m")


def loads(raw: str) -> Optional[Any]:
    """``json.loads`` with two cheap repairs for real-world sloppiness.

    ``RecursionError`` is caught alongside ``ValueError``: ``json.loads`` raises
    it (not a ``ValueError``) on a pathologically nested array, and a page
    carrying such a blob must degrade to "unparseable" like any other rather than
    take the adapter down.
    """
    for candidate in (raw.strip(), re.sub(r",\s*([}\]])", r"\1", _CTRL.sub(" ", raw))):
        try:
            return json.loads(candidate)
        except (ValueError, RecursionError):
            continue
    return None


def script_json(soup: Any, element_id: str) -> Optional[Any]:
    """Decode ``<script id="...">`` -- how Goodreads ships ``__NEXT_DATA__``."""
    if soup is None:
        return None
    node = soup.find("script", attrs={"id": element_id})
    if node is None:
        return None
    return loads(node.string or node.get_text() or "")


def jsonld(soup: Any, want: Optional[Union[str, Iterable[str]]] = None
           ) -> List[Dict[str, Any]]:
    """Every JSON-LD object in ``soup``, flattened and optionally filtered.

    Tolerates the three things sites actually do -- a bare object, a top-level
    array, an ``@graph`` wrapper -- plus nested combinations. A block that fails
    to parse is skipped, never raised.

    :param want: keep only objects whose ``@type`` matches, case-insensitively
        (``@type`` may itself be a list). ``None`` keeps all.
    """
    if soup is None:
        return []
    wanted = ({want.lower()} if isinstance(want, str)
              else {str(w).lower() for w in want} if want is not None else None)
    collected: List[Dict[str, Any]] = []

    def absorb(node: Any, depth: int = 0) -> None:
        if depth > 6:
            return
        if isinstance(node, list):
            for item in node:
                absorb(item, depth + 1)
        elif isinstance(node, dict):
            collected.append(node)
            for key in ("@graph", "mainEntity", "mainEntityOfPage", "itemListElement"):
                if key in node:
                    absorb(node[key], depth + 1)

    try:
        blocks = soup.find_all("script", attrs={
            "type": re.compile(r"application/ld\+json", re.IGNORECASE)})
    except Exception:  # noqa: BLE001
        return []
    for block in blocks:
        if (raw := block.string or block.get_text()) and raw.strip():
            absorb(loads(raw))

    if wanted is None:
        return collected

    def matches(obj: Dict[str, Any]) -> bool:
        declared = obj.get("@type")
        if isinstance(declared, (list, tuple, set)):
            return any(str(d).lower() in wanted for d in declared)
        return str(declared).lower() in wanted if declared else False

    return [obj for obj in collected if matches(obj)]


def nodes_of_type(state: Dict[str, Any], typename: str) -> Iterator[Dict[str, Any]]:
    """Every node in a normalised Apollo/Redux cache with ``__typename``.

    Goodreads' ``__NEXT_DATA__`` holds a flat ``apolloState`` map whose keys are
    opaque ids, so nodes are found by type rather than by path.
    """
    for value in (state or {}).values():
        if isinstance(value, dict) and value.get("__typename") == typename:
            yield value


def deref(state: Dict[str, Any], ref: Any) -> Dict[str, Any]:
    """Follow an Apollo ``{"__ref": "Book:kca://..."}`` pointer to its node."""
    if isinstance(ref, dict):
        key = ref.get("__ref")
        if key is None:
            return ref
        found = (state or {}).get(key)
        return found if isinstance(found, dict) else {}
    return {}


def split_list(raw: Any) -> List[str]:
    """Split ``"Fiction, Fantasy & Young Adult"`` into a clean, deduped list.

    Accepts a string, or an iterable of strings / ``{"name": ...}`` dicts, which
    is the shape JSON-LD ``genre`` and ``author`` fields take.
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        candidates: List[str] = _LIST_SPLIT.split(raw)
    elif isinstance(raw, dict):
        candidates = [str(raw.get("name") or raw.get("@id") or "")]
    elif isinstance(raw, Iterable):
        candidates = [str(i.get("name") or "") if isinstance(i, dict) else str(i)
                      for i in raw]
    else:
        candidates = [str(raw)]
    return dedupe([t for c in candidates
                   if (t := text(c).strip(" .,-|/")) and len(t) <= 120])


def iso_date(raw: Any) -> Optional[str]:
    """Normalise a human date to ISO-8601 where derivable, else return the text.

    ``"January 5, 2016"`` -> ``"2016-01-05"``; ``"June 2016"`` -> ``"2016-06"``;
    ``"2016"`` -> ``"2016"``. Unrecognisable but non-empty input comes back
    cleaned, so nothing a site published is silently lost.
    """
    found = text(raw)
    if not found:
        return None
    found = re.sub(r"^(?:first\s+)?published[:\s]*", "", found, flags=re.IGNORECASE)
    candidate = re.sub(r"\b(\d{1,2})(?:st|nd|rd|th)\b", r"\1", found,
                       flags=re.IGNORECASE).strip(" ,.;")
    if match := re.search(r"\b\d{4}-\d{2}-\d{2}\b", candidate):
        return match.group(0)
    for fmt in _FORMATS:
        try:
            parsed = datetime.strptime(candidate, fmt)
        except ValueError:
            continue
        month_only = fmt in ("%B %Y", "%b %Y", "%Y-%m")
        return parsed.strftime("%Y-%m" if month_only else "%Y-%m-%d")
    # Pull "Month DD, YYYY" or "DD Month YYYY" out of a longer phrase.
    phrase = re.search(
        r"\b(?:(\d{1,2})\s+)?([A-Za-z]{3,9})\.?\s+(?:(\d{1,2}),?\s+)?(\d{4})\b", candidate)
    if phrase:
        day = phrase.group(1) or phrase.group(3)
        month_name, year = phrase.group(2), phrase.group(4)
        for fmt in ("%B", "%b"):
            try:
                month = datetime.strptime(month_name.title(), fmt).month
            except ValueError:
                continue
            if day and 1 <= int(day) <= 31:
                try:
                    return datetime(int(year), month, int(day)).strftime("%Y-%m-%d")
                except ValueError:
                    pass
            return f"{year}-{month:02d}"
    if year := re.search(r"\b(1[0-9]{3}|20[0-9]{2})\b", candidate):
        return year.group(1)
    return candidate


def epoch_date(value: Any) -> Optional[str]:
    """An epoch-**milliseconds** timestamp as ``YYYY-MM-DD`` (Goodreads' format)."""
    try:
        seconds = int(value) / 1000
    except (TypeError, ValueError):
        return None
    try:
        return datetime.utcfromtimestamp(seconds).strftime("%Y-%m-%d")
    except (OverflowError, OSError, ValueError):
        return None
