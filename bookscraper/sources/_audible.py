"""Audible helpers: geo-pinned URLs, patient fetching, review tiles, cover hygiene."""

from __future__ import annotations

import re
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode, urlsplit

from ..covers import Covers
from ..extract import iso_date, loads
from ..http import HttpClient, soup_of
from ..models import Review
from ..parse import dedupe, html_text, review, sel, text

HOST = "www.audible.com"
BASE = f"https://{HOST}"
#: **Required as a pair on every audible.com URL.** From a non-US IP the request is
#: 302'd to www.audible.in with the path discarded into an ``ipRedirectOriginalURL``
#: param, so a naive fetch gets HTTP 200 and the wrong site's homepage. Cookies are
#: neither necessary nor sufficient.
GEO = {"ipRedirectOverride": "true", "overrideBaseCountry": "true"}

#: Audible's throttle penalty is multi-minute, so the client's sub-10 s backoff is
#: not enough. Applied to the search page (one wait) and the product page (both).
PATIENT_WAITS = (30.0, 75.0)
#: A transient HTTP 503 "Whoops..." rate-limit page. It parses cleanly as a page, so
#: it must be detected by content or garbage gets scraped. Not a bot block.
THROTTLE_MARKER = "crackedegg.jpg"
#: Server-side hard cap: asking for 50 returns 5, so this sets the page maths.
REVIEW_PAGE_SIZE = 5
MAX_REVIEW_PAGES = 12
MAX_COVERS = 6
FUZZY_FLOOR = 0.85
#: Above this, two differently-named products are the same book in another edition.
EDITION_CEILING = 0.995

_ASIN_FROM_PD = re.compile(r"/pd/(?:[^/]+/)?([A-Z0-9]{10})(?:[/?#]|$)", re.IGNORECASE)
_TAG_KIND = re.compile(r"^/tag/([^/]+)/")
#: Chips under these kinds are real taxonomy; ``mood`` and ``audible_editors`` are
#: editorial. An unclassifiable chip is **dropped**, never defaulted -- defaulting is
#: how marketing ad links ("Body Language" on *Ready Player One*) became genres.
GENRE_CHIPS = frozenset({"genre", "theme", "category", "goodreads"})
#: Social-share composites with Audible branding burned into the pixels -- which is
#: most ``og:image`` values.
_COVER_JUNK = ("_CLa", "PJAdblSocialShare", "AudibleLogo")
#: ``_SL500_`` is already the native master size; this only upsizes thumbnails.
_SL_TOKEN = re.compile(r"\._SL\d+_")
_IMAGE_ID = re.compile(r"/images/[A-Z]/([A-Za-z0-9%+-]+)\.")
EDITION_MARKER = re.compile(
    r"[\(\[]|\b(?:edition|unabridged|abridged|version|translat\w*|reissue|"
    r"anniversary|deluxe)\b", re.IGNORECASE)
_NEXT_PAGE_ID = re.compile(r"^nextReviewsPageNumber", re.IGNORECASE)
_US_SHORT = re.compile(r"(\d{2})-(\d{2})-(\d{2})")


def url_for(path: str, params: Optional[Dict[str, Any]] = None) -> str:
    """Build an audible.com URL, always carrying the geo-override pair."""
    query = dict(params or {})
    query.update(GEO)
    return f"{BASE}{path if path.startswith('/') else '/' + path}?{urlencode(query)}"


def strip_query(url: str) -> str:
    return url.split("?")[0].split("#")[0]


def fetch(client: HttpClient, url: str, *, patient: int = 0,
          allow_browser: bool = False) -> Any:
    """Fetch and parse, retrying patiently through Audible's rate limit.

    The final URL is verified **before** anything is parsed: without that check the
    adapter happily parses audible.in.
    """
    from ..transport import warn

    for attempt in range(patient + 1):
        response = client.get(url, referer=BASE + "/")
        if response is not None:
            final = str(response.url)
            if "ipRedirectFrom=" in final or HttpClient.host_of(final) != HOST:
                warn(f"warning: audible: the request landed on "
                     f"{HttpClient.host_of(final)}, not {HOST}")
                return None
            body = response.text or ""
            if THROTTLE_MARKER not in body[:4096]:
                return soup_of(body, final)
        if attempt < patient:
            time.sleep(PATIENT_WAITS[min(attempt, len(PATIENT_WAITS) - 1)])
    if allow_browser and client.browser.available:
        return client.rendered(url, wait_css='h1[slot="title"]', wait_seconds=12)
    return None


def asin_of(item: Any, href: str) -> str:
    """The product ASIN, from the row id, the impression div, then the href."""
    raw_id = item.get("id") or ""
    if raw_id.startswith("product-list-item-"):
        return raw_id[len("product-list-item-"):]
    node = item.select_one("div.adbl-asin-impression[data-asin]")
    if node is not None and (found := (node.get("data-asin") or "").strip()):
        return found
    match = _ASIN_FROM_PD.search(href)
    return match.group(1) if match else ""


def collector(base: str) -> Covers:
    """Reject branded composites, upsize to the native master, key by image id."""
    def upgrade(url: str) -> str:
        return _SL_TOKEN.sub("._SL500_", url)

    def key(url: str) -> str:
        if any(junk in url for junk in _COVER_JUNK):
            return ""     # an empty key is refused by Covers.add
        match = _IMAGE_ID.search(url)
        return match.group(1).lower() if match else url.lower()

    return Covers(base or BASE, key=key, upgrade=upgrade, limit=MAX_COVERS)


def us_short_date(raw: Any) -> Optional[str]:
    """Component JSON writes ``MM-DD-YY``: not ISO, not US long form."""
    found = text(raw)
    match = _US_SHORT.fullmatch(found)
    if match is None:
        return iso_date(found)
    month, day, year = match.groups()
    return f"{'20' if int(year) < 70 else '19'}{year}-{month}-{day}"


def component_json(soup: Any, selector: str) -> Dict[str, Any]:
    """Decode one inline ``application/json`` blob hydrating a web component."""
    node = soup.select_one(selector)
    if node is None:
        return {}
    found = loads(node.string or node.get_text() or "")
    return found if isinstance(found, dict) else {}


def genre_chips(soup: Any) -> List[str]:
    """Genre chips, classified on the URL **path**.

    Matching the raw href let absolute ``https://www.audible.com/tag/genre/...``
    chips fall through to a default kind, which is how ad links became genres.
    """
    found: List[str] = []
    for chip in soup.select('adbl-chip-group[slot="chips"] adbl-chip'):
        href = chip.get("href") or ""
        match = _TAG_KIND.match(urlsplit(href).path or href)
        if match and match.group(1).lower() in GENRE_CHIPS and (name := text(chip)):
            found.append(name)
    return found


def review_tiles(soup: Any, seen: set) -> List[Review]:
    """Parse ``<adbl-review-tile>`` elements.

    Reviewer, date and the story/performance ratings are **attributes** on the
    custom element, not child nodes. A tile carries no permalink, so ``url`` stays
    ``None`` -- stamping the product URL put one identical header on all 25 reviews.
    """
    out: List[Review] = []
    for tile in soup.select("adbl-review-tile"):
        node = (tile.select_one('adbl-text-block[slot="review-summary"]')
                or tile.select_one("adbl-text-block"))
        body = html_text(node.decode_contents()) if node is not None else ""
        headline = sel(tile, 'h3[slot="review-title"]', "h3") or ""
        if headline and body and not body.startswith(headline):
            body = f"{headline}\n\n{body}"
        elif headline and not body:
            body = headline

        parts = []
        stars = tile.select_one('adbl-star-rating[slot="stars"]')
        if stars is not None and (overall := (stars.get("value") or "").strip()):
            parts.append(f"{overall}/5 overall")
        for attribute, label in (("story-rating", "story"),
                                 ("performance-rating", "performance")):
            if value := (tile.get(attribute) or "").strip():
                parts.append(f"{label} {value}/5")

        item = review(body, reviewer=tile.get("reviewer"), rating=", ".join(parts),
                      date=tile.get("review-date"), url=None, min_chars=2)
        if item is None:
            continue
        from ..match import fold
        key = fold(item.text)[:400]
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return dedupe(out)


def paged_reviews(client: HttpClient, soup: Any, asin: str, target: int,
                  have: int, seen: set) -> List[Review]:
    """Page the review XHR fragment, five reviews per request."""
    def value_of(element_id: Any, default: str) -> str:
        node = soup.find("input", id=element_id)
        return ((node.get("value") or "").strip() if node is not None else "") or default

    asin = value_of("reviewsAsinUS", asin)
    country = value_of("reviewsCountry", "US")
    try:
        page = int(value_of(_NEXT_PAGE_ID, "0"))
    except ValueError:
        page = 0
    page = page if page > 0 else 2

    out: List[Review] = []
    budget = min(-(-max(0, target - have) // REVIEW_PAGE_SIZE), MAX_REVIEW_PAGES)
    for _ in range(max(0, budget)):
        if have + len(out) >= target:
            break
        # patient=0: pagination stops early and honestly rather than sleeping.
        fragment = fetch(client, url_for("/pd/reviews", {
            "country": country, "asin": asin, "sort": "MostRelevant",
            "filter": "allStars", "page": page, "pageSize": REVIEW_PAGE_SIZE,
            "showPaging": "true"}))
        if fragment is None:
            break
        added = review_tiles(fragment, seen)
        if not added:
            break
        out += added
        page += 1
    return out
