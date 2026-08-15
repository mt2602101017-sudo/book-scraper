"""BookBub helpers: slug construction, detail rows, blurb repair, cover upscaling."""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Dict, List, Optional, Tuple

from ..covers import Covers, filename_key
from ..match import SUBTITLE, fold
from ..parse import dedupe, html_text, sel, text

BASE = "https://www.bookbub.com"   # the apex 301s to www, so normalise up front
BOOK_URL = BASE + "/books/{slug}"
AUTHOR_URL = BASE + "/authors/{slug}"

#: The payload the page hydrates itself from -- and the wait selector. 20 s covers
#: Cloudflare's auto-solve *plus* the client-side render.
BOOK_JSON_ATTR = "data-book-json"
BOOK_JSON_CSS = f"[{BOOK_JSON_ATTR}]"
RENDER_WAIT = 20
#: The author grid is lazy and carries no book JSON, so it needs its own waits: a
#: cover image inside a book link, since nav and footer links have no image.
AUTHOR_WAIT_CSS = "a[href*='/books/'] img"
AUTHOR_SCROLLS = 3

#: Each unit here is ~20 s of headless Chrome, so the budget is real.
MAX_SLUG_CANDIDATES = 3
MAX_AUTHOR_CANDIDATES = 2
MAX_BOOK_FETCHES = 5

STRONG_TITLE = 0.95   # stop looking
MIN_ACCEPT = 0.60     # below this, refuse the page outright
CONFIDENT = 0.90      # below this, say the acceptance was fuzzy
#: A candidate that *adds* words ("Dune" -> "Dune Messiah") is sequel-shaped: it may
#: still win as the only candidate, but it must never clear CONFIDENT silently.
ADDITIVE_CEILING = 0.80

#: The browser can still be served the challenge, and parsing it yields garbage.
CF_MARKERS = ("just a moment...", "window._cf_chl_opt", "__cf_chl_tk",
              "challenges.cloudflare.com", "/cdn-cgi/challenge-platform/",
              "enable javascript and cookies to continue")
#: BookBub answers a browser HTTP 200 for a missing slug.
NOT_FOUND_MARKERS = ("page not found", "page doesn't exist", "page does not exist")

PAGE_TITLE = re.compile(r"^(?P<title>.+?)\s+by\s+(?P<author>.+?)\s+-\s+BookBub\s*$")
COVER_ALT = re.compile(r"^\s*Book cover for\s+(?P<title>.+?)\s+by\s+(?P<author>.+?)\s*$",
                       re.IGNORECASE)
BOOK_HREF = re.compile(r"/books/([a-z0-9][a-z0-9\-]*)", re.IGNORECASE)
SLUG_DATE_SUFFIX = re.compile(r"-\d{4}-\d{2}-\d{2}$")
NON_BOOK_SLUGS = frozenset({"index", "search", "new-releases", "deals"})
#: Promotional tags that are not genres.
MARKETING_TAG = re.compile(
    r"bestselling author|authors and books|^noteworthy$"
    r"|\b(?:new york times|usa today|wall street journal|sunday times"
    r"|publishers weekly|amazon charts)\b", re.IGNORECASE)

PUBLISHER_LABELS = ("publisher", "published by", "imprint")
ORIGIN_LABELS = ("country", "country of origin", "place of publication",
                 "published in", "origin")
DATE_LABELS = ("publication date", "published", "publish date", "release date",
               "first published", "pub date", "on sale date")
#: Applied to label *and* value, so "Publisher Description" can never be read as a
#: publisher name.
LABEL_DENYLIST = ("description", "descriptions")
LANGUAGE_META = ("books:language", "book:language", "inLanguage",
                 "og:book:language", "language")

_CLOUDINARY_VERSION = re.compile(r"^v\d+$")
_CLOUDINARY_FILE = re.compile(r"\.[A-Za-z0-9]{2,5}$")


def slugify(value: Any) -> str:
    """BookBub's own slug convention, verified live.

    ``&`` becomes " and "; ``"Reese's Book Club"`` becomes ``reese-s-book-club``.
    """
    plain = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore")
    folded = plain.decode("ascii").replace("&", " and ")
    slug = re.sub(r"[^A-Za-z0-9]+", "-", folded).strip("-").lower()
    return re.sub(r"-{2,}", "-", slug)


def title_variants(title: str) -> List[str]:
    """Title spellings to try as slugs, most specific first."""
    found = [title]
    head = SUBTITLE.split(title, maxsplit=1)[0].strip()
    if head and head != title:
        found.append(head)
    words = title.split()
    if len(words) > 3:
        found.append(" ".join(words[:3]))
    if words and len(words[0]) >= 4:
        found.append(words[0])
    return dedupe(found)


def author_variants(authors: List[str]) -> List[str]:
    """BookBub joins co-authors with ``-and-`` in the slug."""
    cleaned = [a for a in (text(a) for a in authors) if a]
    if not cleaned:
        return []
    return dedupe([cleaned[0]] + ([" and ".join(cleaned[:2])] if len(cleaned) > 1 else []))


def challenged(soup: Any) -> bool:
    head = str(soup)[:20000].lower()
    return any(marker in head for marker in CF_MARKERS)


def not_found(soup: Any) -> bool:
    title = text(soup.title).lower() if soup.title else ""
    heading = (sel(soup, "h1") or "").lower()
    return any(m in title or m in heading for m in NOT_FOUND_MARKERS)


def detail_pairs(soup: Any) -> Dict[str, str]:
    """``Label:``/value rows out of the book panel, as plain text."""
    panel = soup.select_one(".book-panel") or soup.select_one(".book-info-body")
    if panel is None:
        return {}
    lines = [ln.strip() for ln in html_text(panel).split("\n") if ln.strip()]
    pairs: Dict[str, str] = {}
    for index, line in enumerate(lines):
        label, sep, inline = line.partition(":")
        if not sep or len(label) > 40:
            continue
        key = label.strip().lower()
        value = inline.strip() or (lines[index + 1] if index + 1 < len(lines) else "")
        if key and value and not any(
                bad in key or bad in value.lower() for bad in LABEL_DENYLIST):
            pairs.setdefault(key, value)
    return pairs


def first_label(pairs: Dict[str, str], labels: Tuple[str, ...]) -> Optional[str]:
    return next((pairs[label] for label in labels if pairs.get(label)), None)


def _upscale(url: str) -> str:
    """Strip a Cloudinary transformation segment. Verified 36 kB versus 21 kB."""
    if "/upload/" not in url:
        return url
    head, _, tail = url.partition("/upload/")
    segments = tail.split("/")
    start = next((i for i, s in enumerate(segments)
                  if _CLOUDINARY_VERSION.match(s) or _CLOUDINARY_FILE.search(s)), None)
    return head + "/upload/" + "/".join(segments[start:]) if start is not None else url


def collector(base: str) -> Covers:
    return Covers(base, key=filename_key, upgrade=_upscale, limit=6)


def sanity_check_blurb(blurb: str) -> str:
    """BookBub sometimes serves the description repeated, restarting mid-word.

    Observed: 4 355 characters where every other source gave ~1 700.
    """
    paragraphs = blurb.split("\n")
    first = paragraphs[0] if paragraphs else ""
    if len(first) >= 40 and (repeat := blurb.find(first, len(first))) != -1:
        return blurb[:repeat].strip()
    return blurb


def cover_names_book(image: Any, title: str) -> bool:
    """Does this image's ``alt`` name the book we resolved?

    That is what keeps a recommendation-carousel cover out of the results.
    """
    match = COVER_ALT.match(image.get("alt") or "")
    return match is not None and fold(match.group("title")) == fold(title)
