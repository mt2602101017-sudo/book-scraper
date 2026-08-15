"""Goodreads field extraction, out of ``goodreads.py`` so each file stays small."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, List, Optional

from ..covers import Covers
from ..extract import deref
from ..parse import dedupe, html_text, meta, sel, sels, text
from ._goodreads import clean_image, image_key
from ._goodreads_find import Page

MAX_COVERS = 6


def collector(base: str) -> Covers:
    """Cover identity is the ``/books/...`` path, which is stable across the two
    Goodreads CDN hostnames, and every URL is upgraded to the original upload."""
    return Covers(base, key=image_key,
                  upgrade=lambda url: clean_image(base, url) or url, limit=MAX_COVERS)


def title(page: Page) -> Optional[str]:
    return (text(page.book.get("title")) or text(page.book.get("titleComplete"))
            or sel(page.soup, 'h1[data-testid="bookTitle"]'))


def authors(page: Page) -> List[str]:
    """Contributor edges, with a non-author role kept in parentheses."""
    edges = [page.book.get("primaryContributorEdge")]
    edges += list(page.book.get("secondaryContributorEdges") or [])
    names = []
    for edge in edges:
        if not isinstance(edge, dict):
            continue
        name = text(deref(page.state, edge.get("node")).get("name"))
        role = text(edge.get("role"))
        if name:
            names.append(name if role.lower() in ("", "author") else f"{name} ({role})")
    # The CSS fallback is truncated to one on purpose: that class also matches
    # reviewer names further down the page.
    return dedupe(names) or sels(page.soup, "span.ContributorLink__name")[:1]


def published(millis: Any, *, pacific: bool = True) -> Optional[str]:
    """An epoch-**milliseconds** timestamp as ``YYYY-MM-DD``.

    ``publicationTime`` is midnight *Pacific*, and converting it in UTC lands a day
    early around daylight saving. Review ``createdAt`` is genuinely UTC, hence the
    flag rather than one shared assumption.
    """
    zone: Any = timezone.utc
    if pacific:
        try:
            from zoneinfo import ZoneInfo
            zone = ZoneInfo("America/Los_Angeles")
        except Exception:  # noqa: BLE001 - no tz database available
            pass
    try:
        return datetime.fromtimestamp(int(millis) / 1000, zone).date().isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def language(page: Page) -> Optional[str]:
    node = page.details.get("language")
    return (text(node.get("name")) if isinstance(node, dict) else text(node)) or None


def genres(page: Page) -> List[str]:
    return dedupe([n for entry in page.book.get("bookGenres") or []
                   if (n := text(((entry or {}).get("genre") or {}).get("name")))])


def blurb(page: Page) -> Optional[str]:
    from ._goodreads import strip_librarian_note

    body = html_text(page.book.get("description"))
    if not body:
        # The .Formatted child is the real one; the bare wrapper exists on stubs too.
        node = (page.soup.select_one('div[data-testid="description"] .Formatted')
                or page.soup.select_one('div[data-testid="description"]'))
        body = html_text(node.decode_contents()) if node is not None else ""
    body = body or meta(page.soup, "og:description", "description") or ""
    return strip_librarian_note(body) or None


def covers(page: Page) -> Covers:
    """This edition's own cover, preferred over any other edition's."""
    found = collector(page.url)
    found.add(page.book.get("imageUrl"))
    if not found:
        node = page.soup.select_one("div.BookCover__image img.ResponsiveImage")
        found.add(node.get("src") if node is not None else None)
    if not found:
        found.add(meta(page.soup, "og:image", "twitter:image"))
    return found
