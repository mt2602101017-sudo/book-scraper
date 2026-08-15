"""Audible field extraction, out of ``audible.py`` so each file stays small.

Every selector here is ``slot``-qualified on purpose: a product page carries ~37
other cover images from "Listeners also enjoyed" carousels, so an unqualified
``h1``, ``img``, ``adbl-text-block`` or ``adbl-star-rating`` scrapes the carousel.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from ..extract import jsonld, split_list
from ..parse import dedupe, html_text, meta, sel, text
from ._audible import genre_chips

#: ``sel`` is re-exported so audible.py reads one field module, not two.
__all__ = ["DETAILS_JSON", "PEOPLE_JSON", "named", "label_authors", "title",
           "authors", "language", "genres", "blurb", "sel"]

DETAILS_JSON = ('adbl-product-details adbl-product-metadata > '
                'script[type="application/json"]')
PEOPLE_JSON = ('adbl-product-metadata[combine-authors-narrators] > '
               'script[type="application/json"]')
_BY_PREFIX = re.compile(r"^by[:\s]+", re.IGNORECASE)


def named(raw: Any) -> Optional[str]:
    """A JSON-LD value that may be a bare string or ``{"name": ...}``."""
    return text(raw.get("name") if isinstance(raw, dict) else raw) or None


def label_authors(node: Any) -> List[str]:
    """The ``.authorLabel`` text, minus its "By:" prefix, split on commas."""
    cleaned = _BY_PREFIX.sub("", sel(node, ".authorLabel") or "")
    return [p for p in (part.strip() for part in cleaned.split(",")) if p]


def title(soup: Any, audiobook: Dict[str, Any]) -> Optional[str]:
    found = text(audiobook.get("name")) or sel(soup, 'h1[slot="title"]')
    if found:
        return found
    raw = meta(soup, "og:title", "twitter:title", "title") or ""
    # Strips the " Audiobook | Audible.com" suffix.
    return re.sub(r"\s+Audiobook\b.*$", "", raw).strip() or raw or None


def authors(soup: Any, audiobook: Dict[str, Any], people: Dict[str, Any]) -> List[str]:
    """Writers only. ``readBy`` / ``narrators`` are voice talent, never authors."""
    found = split_list(audiobook.get("author"))
    if not found:
        found = [n for a in people.get("authors") or []
                 if isinstance(a, dict) and (n := text(a.get("name")))]
    return dedupe(found or label_authors(soup))


def language(audiobook: Dict[str, Any], details: Dict[str, Any]) -> Optional[str]:
    """Audible writes a bare lower-case English word, not a BCP-47 code."""
    found = text(audiobook.get("inLanguage")) or text(details.get("language"))
    return (found.title() if found.islower() else found) or None


def genres(soup: Any, audiobook: Dict[str, Any], details: Dict[str, Any]) -> List[str]:
    crumbs: List[str] = []
    for block in jsonld(soup, "BreadcrumbList"):
        for element in block.get("itemListElement") or []:
            if not isinstance(element, dict):
                continue
            item = element.get("item")
            name = text(item.get("name") if isinstance(item, dict)
                        else element.get("name"))
            if name and name.lower() != "home":
                crumbs.append(name)
    categories = [n for c in details.get("categories") or []
                  if isinstance(c, dict) and (n := text(c.get("name")))]
    found = crumbs + genre_chips(soup) + categories + split_list(audiobook.get("genre"))
    return [g for g in dedupe(found) if g]


def blurb(soup: Any, audiobook: Dict[str, Any]) -> Optional[str]:
    """Server-rendered in full: there is no "read more" to expand."""
    if found := html_text(audiobook.get("description")):
        return found
    node = soup.select_one('adbl-text-block[slot="summary"]')
    if node is not None and (found := html_text(node.decode_contents())):
        return found
    return meta(soup, "og:description", "description", "twitter:description")
