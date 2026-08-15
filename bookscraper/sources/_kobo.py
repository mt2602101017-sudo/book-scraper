"""Kobo helpers: the gizmo config, search cards, the ratings API, cover rebuilding."""

from __future__ import annotations

import html as html_module
import re
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit, urlunsplit

from ..covers import Covers
from ..extract import iso_date, loads
from ..http import HttpClient, soup_of
from ..models import Review
from ..parse import dedupe, html_text, review, text

#: ``/us/en`` is robots.txt-Allowed *and* does not geo-301; a bare ``/ebook/...``
#: redirects to the caller's storefront, changing catalogue, currency and reviews.
STORE_ROOT = "https://www.kobo.com/us/en"
#: Not behind Cloudflare, needs no headers or cookies, and returns an HTML
#: fragment holding every review for the book.
REVIEWS_API = "https://ratingsapi.kobo.com/V1/Ui/GetMoreReviews"
COVER_CDN = "https://cdn.kobo.com/book-images/{image_id}/1200/1200/100/False/cover.jpg"
#: A served Cloudflare challenge parses cleanly as "a page", so without these every
#: field silently comes back empty. No CAPTCHA is ever attempted.
CHALLENGE_MARKERS = ("challenged | kobo.com", "enable javascript and cookies to continue")
#: 2 = newest first. The default 0 shuffles between pages and yields duplicates.
REVIEW_SORT_NEWEST = 2
REVIEW_LIMIT_CEILING = 1000
REVIEW_PAGE_CEILING = 6

#: Measured at 1200: ~1199x1808. The CDN upscales past source, so a bigger box is
#: interpolated bytes rather than detail.
_IMAGE_ID = re.compile(r"book-images/([0-9a-fA-F-]{36})/")
_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                   r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
#: Kobo leaks markdown bold markers into the synopsis HTML.
_MD_BOLD = re.compile(r"\*{2,}")
#: Kobo emits ``2014-10-07T00:00:00Z``, whose ``T`` defeats iso_date's \b anchor.
_STAMP = re.compile(r"(\d{4})-(\d{2})-(\d{2})(?![\d-])")


def strip_tracking(url: str) -> str:
    """Drop ``?sId=``/``&ssId=``/``&cPos=`` so card and canonical URLs compare."""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def slug_base(url: str) -> str:
    """Kobo gives every edition of one work the same slug plus a number.

    Sibling-cover identity has to use this, not the title: search cards omit
    subtitles, so a title test demonstrably pulls in the wrong artwork.
    """
    slug = urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1].lower()
    return re.sub(r"-\d+$", "", slug)


def challenged(soup: Any) -> bool:
    head = str(soup)[:8192].lower()
    return any(marker in head for marker in CHALLENGE_MARKERS)


def gizmo(soup: Any, name: str) -> Dict[str, Any]:
    """Decode ``data-kobo-gizmo-config`` -- Kobo's metadata is not in ld+json.

    The attribute holds HTML-entity-escaped, **doubly** JSON-encoded text, so the
    unescape retry is mandatory: bs4 usually unescapes it, but not always.
    """
    node = soup.select_one(f'div[data-kobo-gizmo="{name}"][data-kobo-gizmo-config]')
    if node is None:
        return {}
    raw = node.get("data-kobo-gizmo-config") or ""
    for candidate in (raw, html_module.unescape(raw)):
        found = loads(candidate)
        if isinstance(found, dict):
            return found
    return {}


def nested(config: Dict[str, Any], key: str) -> Dict[str, Any]:
    """``googleBook``/``googleProduct`` are JSON *strings* inside the config."""
    value = config.get(key)
    if isinstance(value, dict):
        return value
    found = loads(str(value or ""))
    return found if isinstance(found, dict) else {}


def publication_date(value: Any) -> Optional[str]:
    """Peel the day off Kobo's timestamp before the shared date parser sees it."""
    found = text(value)
    match = _STAMP.match(found)
    return match.group(0) if match else iso_date(found)


def clean_blurb(raw: Any) -> str:
    body = html_text(raw)
    return text(_MD_BOLD.sub("", body) if "**" in body else body)


def collector(limit: int = 6) -> Covers:
    """A cover collector that rebuilds every URL at full size off its image id."""
    def rebuild(url: str) -> str:
        match = _IMAGE_ID.search(url)
        return COVER_CDN.format(image_id=match.group(1).lower()) if match else url

    def key(url: str) -> str:
        match = _IMAGE_ID.search(url)
        return match.group(1).lower() if match else url.lower()

    return Covers(STORE_ROOT, key=key, upgrade=rebuild, limit=limit)


def search_cards(soup: Any) -> List[Dict[str, Any]]:
    """Parse the React search results page into comparable cards."""
    from ..parse import absolutise

    cards: List[Dict[str, Any]] = []
    nodes = (soup.select("div[id^='list-item-']")
             or soup.select("div[data-testid='book-card-search-result-items']"))
    for node in nodes:
        link = (node.select_one("a[data-testid='title']")
                or node.select_one("a[href*='/ebook/'], a[href*='/audiobook/']"))
        if link is None:
            continue
        href = absolutise(STORE_ROOT, link.get("href"))
        image = node.select_one("img[data-testid='cover']") or node.select_one("img")
        cards.append({
            "url": href,
            "title": text(link.get("aria-label") or link.get_text(" ")),
            "authors": [text(a) for a in node.select("[data-testid='authors'] a")],
            "image": (image.get("src") if image is not None else "") or "",
            "crid": node.get("data-cross-revision-id") or "",
            "ebook": "/ebook/" in href,
        })
    return cards


def valid_crid(value: Any) -> str:
    """The review id, which must be a UUID -- no id, no reviews."""
    found = text(value)
    return found if _UUID.match(found) else ""


def api_reviews(client: HttpClient, crid: str, target: int, limit: int,
                seen: set) -> List[Review]:
    """Page ``ratingsapi.kobo.com``. ``offset`` is a 0-based **page** index."""
    out: List[Review] = []
    for page in range(REVIEW_PAGE_CEILING):
        response = client.get(REVIEWS_API, params={
            "id": crid, "offset": page, "limit": limit,
            "sortBy": REVIEW_SORT_NEWEST, "starRating": 0, "userLocale": "en-US"})
        if response is None:
            break
        fragment = soup_of(response.text, str(response.url))
        if fragment is None:
            break
        items = parse_reviews(fragment, seen)
        out += items
        node = fragment.select_one("input#TotalReviewCount")
        raw_total = (node.get("value") or "").strip() if node is not None else ""
        total = int(raw_total) if raw_total.isdigit() else None
        if (not items or len(out) >= target or len(items) < limit
                or (total is not None and len(out) >= total)):
            break
    return out


def parse_reviews(soup: Any, seen: set) -> List[Review]:
    """Parse review items out of an API fragment or the rendered widget."""
    out: List[Review] = []
    for node in soup.select("div.review-item"):
        body = text(node.select_one(".review-text"))
        heading = text(node.select_one("h2.review-title"))
        if heading and not body:
            continue  # a title with no body is not a review
        if heading and not body.lower().startswith(heading.lower()):
            body = f"{heading}\n\n{body}"
        # min_chars=2 is deliberate: Kobo has genuine one-word reviews.
        item = review(body, reviewer=text(node.select_one("span.review-author")),
                      rating=text(node.select_one(".rating-average")),
                      date=iso_date(text(node.select_one("span.review-date"))),
                      min_chars=2)
        if item is None:
            continue
        # The title is deliberately not part of the key: it can be empty.
        key = ((item.reviewer or "").strip().casefold(), (item.date or "").strip(),
               " ".join(item.text.split()).casefold()[:180])
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return dedupe(out)
