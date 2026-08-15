"""Text and DOM helpers every adapter reuses rather than re-inventing.

Pure functions over strings and ``bs4`` nodes. Nothing here fetches, and nothing
raises: a bad selector yields ``None``, so an adapter listing five fallbacks does
not need five try/excepts.
"""

from __future__ import annotations

import html as html_module
import re
from typing import Any, Iterable, List, Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

from .models import Review

_WS = re.compile(r"[^\S\n]+")
_BLANKS = re.compile(r"\n{3,}")
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_BLOCKS = ["p", "div", "li", "tr", "section", "article", "h1", "h2", "h3", "h4",
           "h5", "h6", "blockquote"]

def text(value: Any) -> str:
    """Unescape entities, drop control chars, collapse whitespace, strip.

    Newlines survive (runs of 3+ collapse to 2) so paragraph structure is kept;
    horizontal whitespace collapses to single spaces. Accepts a ``Tag`` or
    ``None`` as well as a string.
    """
    if value is None:
        return ""
    raw = value.get_text("\n") if isinstance(value, Tag) else str(value)
    raw = html_module.unescape(raw).replace(" ", " ").replace("​", "").replace("﻿", "")
    raw = _WS.sub(" ", _CTRL.sub("", raw).replace("\r\n", "\n").replace("\r", "\n"))
    return _BLANKS.sub("\n\n", "\n".join(ln.strip() for ln in raw.split("\n"))).strip()


def html_text(value: Any) -> str:
    """An HTML fragment as readable plain text: ``<br>`` and blocks become breaks."""
    if value is None:
        return ""
    if not isinstance(value, Tag) and "<" not in str(value):
        return text(value)
    try:
        working = BeautifulSoup(str(value), "html.parser")
    except Exception:  # noqa: BLE001 - bs4 can raise on pathological input
        return text(re.sub(r"<[^>]+>", " ", str(value)))
    for junk in working.find_all(["script", "style", "noscript", "template"]):
        junk.decompose()
    for br in working.find_all("br"):
        br.replace_with("\n")
    for block in working.find_all(_BLOCKS):
        block.insert_before("\n")
        block.insert_after("\n")
    return text(working.get_text(""))


def dedupe(items: Optional[Iterable[Any]]) -> List[Any]:
    """Order-preserving de-duplication; strings compare case-insensitively.

    ``"Fiction"`` and ``" fiction "`` collapse, and the first spelling wins.
    """
    out: List[Any] = []
    seen: set = set()
    for item in items or []:
        if isinstance(item, str):
            key: Any = item.strip().casefold()
            if not key:
                continue
        else:
            try:
                hash(item)
                key = item
            except TypeError:
                key = repr(item)
        if key not in seen:
            seen.add(key)
            out.append(item)
    return out


def sel(soup: Any, *selectors: str) -> Optional[str]:
    """The cleaned text of the first CSS selector that matches anything."""
    if soup is None:
        return None
    for selector in selectors:
        try:
            node = soup.select_one(selector) if selector else None
        except Exception:  # noqa: BLE001 - a bad selector is skipped, not raised
            continue
        if node is not None and (found := text(node)):
            return found
    return None


def sels(soup: Any, *selectors: str) -> List[str]:
    """Cleaned, de-duplicated text of every node matching any selector."""
    found: List[str] = []
    for selector in selectors:
        try:
            nodes = soup.select(selector) if soup is not None and selector else []
        except Exception:  # noqa: BLE001
            continue
        found.extend(t for node in nodes if (t := text(node)))
    return dedupe(found)


def attrs(soup: Any, selector: str, attribute: str) -> List[str]:
    """Every non-empty value of ``attribute`` on nodes matching ``selector``."""
    try:
        nodes = soup.select(selector) if soup is not None else []
    except Exception:  # noqa: BLE001
        return []
    return dedupe([v for node in nodes if (v := (node.get(attribute) or "").strip())])


def meta(soup: Any, *keys: str) -> Optional[str]:
    """The first matching ``<meta>`` content, by ``property``/``name``/``itemprop``."""
    if soup is None:
        return None
    for key in keys:
        for attr in ("property", "name", "itemprop"):
            try:
                node = soup.find("meta", attrs={attr: key})
            except Exception:  # noqa: BLE001
                continue
            if node is not None and (value := text(node.get("content"))):
                return value
    return None


def absolutise(base: str, url: Any) -> str:
    """Resolve a possibly-relative URL; ``//host/path`` is promoted to https."""
    raw = str(url or "").strip()
    if not raw or raw.startswith(("javascript:", "data:", "#", "mailto:")):
        return ""
    if raw.startswith("//"):
        return "https:" + raw
    try:
        return urljoin(base or "", raw)
    except ValueError:
        return raw


def review(body: Any, *, reviewer: Any = None, rating: Any = None, date: Any = None,
           url: Any = None, min_chars: int = 1) -> Optional[Review]:
    """Build a cleaned :class:`~bookscraper.models.Review`, or ``None`` if too thin."""
    prose = html_text(body)
    if len(prose) < max(1, min_chars):
        return None
    return Review(text=prose, reviewer=text(reviewer) or None, rating=text(rating) or None,
                  date=text(date) or None, url=text(url) or None)
