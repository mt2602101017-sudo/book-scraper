"""Amazon field extraction, out of ``amazon.py`` so each file stays small."""

from __future__ import annotations

import re
from typing import Any, List, Optional, Tuple

from ..covers import Covers
from ..extract import loads
from ..http import soup_of
from ..parse import absolutise, dedupe, meta, sel, sels, text
from ._amazon import image_island, physical_id, master, prose
from ._amazon_find import Page

MAX_EDITION_FETCHES = 2
#: Print formats worth spending a fetch on for a sibling cover, best first.
PRINT_FORMATS = {"HARDCOVER": 0, "PAPERBACK": 1, "MASS_PAPERBACK": 2,
                 "BOARD_BOOK": 5, "LIBRARY_BINDING": 5, "SPIRAL_BOUND": 5,
                 "LEATHER_BOUND": 5}
_DROP_CRUMBS = ("books", "kindle store", "audible books & originals")
_AUTHOR_HREF = re.compile(r"/e/B[A-Z0-9]{9}|field-author|/author/", re.IGNORECASE)


def collector(base: str) -> Covers:
    """Cover identity is Amazon's physical id, never the URL: the same art is
    published under several ids across renditions."""
    return Covers(base, key=physical_id, upgrade=master, limit=12)


def title(page: Page, tag: Optional[re.Match]) -> Optional[str]:
    return (sel(page.soup, "span#productTitle")
            or (text(tag.group("title")) if tag else None))


def authors(page: Page, tag: Optional[re.Match]) -> List[str]:
    found = sels(page.soup, "#bylineInfo span.author a.a-link-normal",
                 "#bylineInfo_feature_div span.author a")
    if not found and (byline := page.soup.select_one("#bylineInfo")) is not None:
        # Cut at "Format: " or the binding ("Paperback") is read as an author.
        raw = str(byline)
        cut = raw.find("Format: ")
        trimmed = soup_of(raw[:cut] if cut > 0 else raw)
        found = dedupe([text(a) for a in (trimmed.find_all("a") if trimmed else [])
                        if _AUTHOR_HREF.search(a.get("href") or "")])
    if not found and tag:
        parts = [p.strip() for p in tag.group("authors").split(",") if p.strip()]
        # "Ng, Celeste" -> "Celeste Ng"
        found = ([f"{parts[1]} {parts[0]}"]
                 if len(parts) == 2 and all(len(p.split()) <= 3 for p in parts)
                 else dedupe(parts))
    return [a for a in found if a]


def genres(page: Page) -> List[str]:
    """Breadcrumbs plus the Best Sellers Rank categories.

    The rank categories are why the detail map keeps container *nodes* and not just
    their text.
    """
    crumbs = [c for c in sels(
        page.soup, "#wayfinding-breadcrumbs_feature_div ul li span.a-list-item a")
        if c.casefold() not in _DROP_CRUMBS]
    ranked: List[str] = []
    for label, node in page.nodes.items():
        if "best sellers rank" not in label.casefold():
            continue
        for anchor in node.find_all("a"):
            found = text(anchor)
            if found and not re.match(r"^see top \d+", found, re.IGNORECASE):
                ranked.append(re.sub(r"\s*\((?:Books|Kindle Store)\)\s*$", "", found).strip())
    return [g for g in dedupe(crumbs + ranked) if g]


def blurb(page: Page) -> Optional[str]:
    for selector in ("#bookDescription_feature_div div.a-expander-content"
                     ".a-expander-partial-collapse-content",
                     "#bookDescription_feature_div",
                     "#editorialReviews_feature_div div.a-expander-content"):
        if len(found := prose(page.soup.select_one(selector))) >= 40:
            return found
    fallback = meta(page.soup, "og:description", "description") or ""
    # Without this guard Amazon's SEO boilerplate gets stored as the blurb.
    return fallback if fallback and not fallback.lower().startswith("amazon.com:") else None


def covers(page: Page) -> Covers:
    """This edition's images, from the inline island then the landing image."""
    found = collector(page.url)
    for entry in image_island(page.html):#JavaScript Image Island (Primary Target)
        # Exactly one URL per entry: the same art under several keys is one cover.
        raw = next((entry[k] for k in ("hiRes", "large", "thumb") if entry.get(k)), None)
        if raw is None and entry.get("physicalIdForMedia"):
            raw = f"https://m.media-amazon.com/images/I/{entry['physicalIdForMedia']}.jpg"
        found.add(raw)
    if not found and (node := page.soup.select_one("img#landingImage")) is not None:#Dynamic HTML Fallback
        found.add(node.get("data-old-hires"))
        # The value arrays are [height, width] on book pages: use the keys.
        found.extend(list(loads(node.get("data-a-dynamic-image") or "") or {}))
        found.add(node.get("src"))
    if not found:#Metadata Fallback: If all else fails, it extracts Open Graph/Twitter meta tags (og:image).
        found.add(meta(page.soup, "og:image", "twitter:image"))
    return found


def sibling_urls(page: Page) -> List[str]:
    """Other print editions' pages, best format first."""
    siblings: List[Tuple[int, str]] = []
    for node in page.soup.select("#tmmSwatches #tmmSwatchesList div[id^=tmm-grid-swatch-] a,"
                                " #formats a.a-button-text"):
        href = node.get("href") or ""
        if href.startswith("javascript:"):
            continue    # the currently-selected format
        parent = node.find_parent(id=re.compile(r"^tmm-grid-swatch-"))
        fmt = (parent.get("id").replace("tmm-grid-swatch-", "").upper()
               if parent is not None else "")
        if fmt in PRINT_FORMATS:
            siblings.append((PRINT_FORMATS[fmt], absolutise(page.url, href)))
    return [url for _, url in sorted(siblings)[:MAX_EDITION_FETCHES]]


def sibling_cover(page: Page) -> Any:
    """The one cover worth taking from a sibling edition's page."""
    entries = image_island(page.html)
    raw = next((e[k] for e in entries[:1] for k in ("hiRes", "large") if e.get(k)), None)
    if raw is None and (node := page.soup.select_one("img#landingImage")) is not None:
        raw = node.get("data-old-hires") or node.get("src")
    return raw
