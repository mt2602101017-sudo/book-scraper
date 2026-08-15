"""Kobo field extraction, out of ``kobo.py`` so each file stays small."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from ..extract import jsonld, split_list
from ..languages import name_of
from ..match import fold
from ..parse import dedupe, meta, sel, sels, text
from ._kobo import clean_blurb

_NON_WORD = re.compile(r"[^a-z0-9]+")
_BREADCRUMB_CHROME = {"kobo books", "home", "ebooks", "audiobooks", "books"}


def rows(soup: Any) -> Dict[str, str]:
    """The ``bookitem-secondary-metadata`` rows, keyed by their own label.

    The one row with no colon is the publisher, so it gets that synthetic key.
    """
    found: Dict[str, str] = {}
    for item in soup.select("div.bookitem-secondary-metadata li"):
        raw = text(item)
        label, sep, value = raw.partition(":")
        if not sep:
            found.setdefault("publisher", raw.strip())
            continue
        key = _NON_WORD.sub("_", label.strip().lower()).strip("_")
        if key and value.strip():
            found.setdefault(key, value.strip())
    return found


def titles(soup: Any, google_book: Dict[str, Any],
           work: Dict[str, Any]) -> Tuple[str, Optional[str]]:
    """``(main, full)`` -- the bare title, and the one carrying any subtitle."""
    main = text(google_book.get("name")) or sel(soup, "h1.title.product-field") or ""
    if not main and (raw := meta(soup, "og:title", "twitter:title")):
        # og:title is "Title eBook by Author", so this split is load-bearing.
        main = re.split(r"\s+eBook by\s+", raw)[0].strip()
    subtitle = (text(work.get("alternativeHeadline"))
                or sel(soup, "span.subtitle.product-field"))
    return main, (f"{main}: {subtitle}" if main and subtitle else main or None)


def authors(soup: Any, google_book: Dict[str, Any], work: Dict[str, Any]) -> List[str]:
    """JSON authors topped up from the DOM: the JSON silently drops co-authors."""
    found = split_list(google_book.get("author") or work.get("author"))
    seen = {fold(a) for a in found}
    for anchor in soup.select(
            "span.authors.product-field.contributor-list span.visible-contributors "
            "a.contributor-name, span.authors.product-field a.contributor-name"):
        holder = anchor.find_parent("li") or anchor.parent
        tag = holder.select_one("span.mobile-library-tag") if holder else None
        role = text(tag).lower()
        if role and "author" not in role:
            continue  # a narrator, illustrator or translator
        name = text(anchor)
        if name and fold(name) not in seen:
            seen.add(fold(name))
            found.append(name)
    return dedupe(found)


def publisher(google_book: Dict[str, Any], google_product: Dict[str, Any],
              detail_rows: Dict[str, str]) -> Optional[str]:
    for node in (google_book.get("publisher"), google_product.get("brand")):
        found = text(node.get("name") if isinstance(node, dict) else node)
        if found:
            return found
    return detail_rows.get("publisher") or None


def language(google_book: Dict[str, Any], detail_rows: Dict[str, str]) -> Optional[str]:
    """The DOM's own label wins over the JSON language code."""
    return detail_rows.get("language") or name_of(google_book.get("inLanguage"))


def genres(soup: Any, google_book: Dict[str, Any]) -> List[str]:
    found = split_list(google_book.get("genre"))
    if not found:
        # The breadcrumb trail is the only fallback with a real taxonomy.
        for index, block in enumerate(jsonld(soup, "BreadcrumbList")):
            names = []
            for element in block.get("itemListElement") or []:
                if not isinstance(element, dict):
                    continue
                item = element.get("item")
                name = text(item.get("name") if isinstance(item, dict)
                            else element.get("name"))
                if name and fold(name) not in _BREADCRUMB_CHROME:
                    names.append(name)
            # Block 0 is page navigation, whose leaf is the book's own title.
            found += names[:-1] if index == 0 else names
    if not found:
        found = sels(soup, "ul.category-rankings li a")
    return dedupe(found)


def blurb(soup: Any, work: Dict[str, Any]) -> Optional[str]:
    # decode_contents(), not get_text(): the HTML is what builds the paragraphs.
    for selector in ("div[data-full-synopsis]", "div.synopsis-description",
                     "div#synopsis-desc"):
        node = soup.select_one(selector)
        if node is not None and (found := clean_blurb(node.decode_contents())):
            return found
    # Kobo truncates these to ~200 characters, so they are a last resort.
    return text(work.get("description")) or meta(soup, "og:description") or None
