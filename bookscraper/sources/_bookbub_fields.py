"""BookBub field extraction, out of ``bookbub.py`` so each file stays small."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..covers import Covers
from ..extract import split_list
from ..languages import name_of
from ..parse import dedupe, html_text, meta, sel, sels, text
from ._bookbub import (LANGUAGE_META, MARKETING_TAG, PAGE_TITLE, collector,
                       cover_names_book, sanity_check_blurb)

#: Re-exported so bookbub.py reads one field module rather than two.
__all__ = ["title", "authors", "language", "genres", "blurb", "covers", "meta"]


def title(soup: Any, data: Dict[str, Any], blob: Dict[str, Any]) -> Optional[str]:
    found = (text(data.get("title")) or text(blob.get("name"))
             or sel(soup, ".book-info-title"))
    if found:
        return found
    # The one thing BookBub renders server-side, so this survives a total
    # client-side render failure.
    for raw in (text(soup.title) if soup.title else "",
                meta(soup, "og:title", "twitter:title") or ""):
        if match := PAGE_TITLE.match(raw):
            return match.group("title").strip()
    return None


def authors(soup: Any, data: Dict[str, Any], blob: Dict[str, Any]) -> List[str]:
    found = (split_list(data.get("authors")) or split_list(blob.get("author"))
             or split_list(sels(soup, ".book-info-authors .person-name",
                                ".book-info-authors")))
    if not found and soup.title and (match := PAGE_TITLE.match(text(soup.title))):
        found = split_list(match.group("author"))
    return dedupe(found)


def language(soup: Any, blob: Dict[str, Any]) -> Optional[str]:
    """Null by design when no per-book field exists.

    ``<html lang>`` and ``og:locale`` describe the storefront, not the book, and
    answered "en" for every book ever scraped, so they are never used.
    """
    raw = meta(soup, *LANGUAGE_META)
    if not raw and (found := blob.get("inLanguage")) is not None:
        raw = (found.get("name") or found.get("alternateName")
               if isinstance(found, dict) else found)
    return name_of(raw)


def genres(data: Dict[str, Any], blob: Dict[str, Any]) -> List[str]:
    """BookBub's own curated categories and tags.

    Never ``a.category-name``: that is the "Deals in similar categories" carousel,
    i.e. *other* books' genres.
    """
    found: List[str] = []
    for category in data.get("dealsCategories") or []:
        if isinstance(category, dict):
            found.append(text(category.get("displayName")
                              or category.get("partnerName")))
    for tag in data.get("tags") or []:
        name = text(tag.get("displayName")) if isinstance(tag, dict) else ""
        if name and not MARKETING_TAG.search(name):
            found.append(name)
    return [g for g in dedupe(found or split_list(blob.get("genre"))) if g]


def blurb(soup: Any, data: Dict[str, Any], blob: Dict[str, Any]) -> Optional[str]:
    for raw in (data.get("description"), data.get("blurb"), blob.get("description"),
                sel(soup, ".expandable-text-description", ".expandable-text-rendered")):
        if found := html_text(raw):
            return sanity_check_blurb(found)
    return meta(soup, "og:description", "twitter:description", "description")


def covers(soup: Any, data: Dict[str, Any], url: str, resolved: str) -> Covers:
    found = collector(url)
    found.add(data.get("coverUrl"))
    found.add(meta(soup, "og:image", "twitter:image"))
    images = soup.select("img.book-cover-image, .cover-image img")
    preferred = next((i for i in images if cover_names_book(i, resolved)), None)
    for image in ([preferred] if preferred is not None else images[:1]):
        found.add(image.get("src") or image.get("data-src"))
    return found
