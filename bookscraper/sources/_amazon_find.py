"""Amazon page fetching and the three-rung discovery ladder.

The rungs exist because ISBN-10 *is* the ASIN, but only sometimes: a 979- ISBN has
no ISBN-10 at all, and a book may only be listed under a Kindle ASIN.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .. import isbn as isbn_utils
from ..http import soup_of
from ..match import authors_agree, is_derivative, is_sequel, title_score
from ..models import Hint, Result
from ..parse import absolutise, dedupe, text
from ._amazon import BASE, bullet, details

MAX_CANDIDATES = 3
FUZZY_TITLE = 0.72

#: The ``<title>`` tag carries title, ISBN-13 and authors -- a real fallback when
#: the detail bullets are missing, and a second source for the ISBN.
TITLE_TAG = re.compile(r"Amazon\.com:\s*(?P<title>.+?):\s*(?P<isbn13>97[89]\d{10}):"
                       r"\s*(?P<authors>.+?):\s*Books", re.IGNORECASE | re.DOTALL)
_NOT_FOUND = re.compile(r"<title[^>]*>\s*Page Not Found", re.IGNORECASE)
#: The sign-in wall's ``<title>`` sits ~69 KB in, behind a huge inline metrics
#: script, and its input is ``id="ap_email_login"`` -- so a head-only sniff or an
#: exact-id check silently misses it.
_SIGNIN = re.compile(r'<title[^>]*>\s*Amazon Sign-?In|id="ap_email|id="ap_login_form',
                     re.IGNORECASE)
#: An Akamai interstitial arrives as HTTP 200 with a short body. A real detail page
#: is ~2.4 MB, so the length gate is safe. The proof-of-work is never solved.
_INTERSTITIAL = ("bm-verify", "/_sec/verify?provider=interstitial",
                 "triggerinterstitialchallenge", "m.media-amazon.com/images/s/sash/")
_INTERSTITIAL_MAX = 5000
_BACKOFF = (5.0, 15.0)
ASIN_IN_URL = re.compile(r"/(?:dp|gp/product)/([A-Z0-9]{10})", re.IGNORECASE)


@dataclass
class Page:
    """One fetched Amazon page, and what is wrong with it if anything."""

    url: str
    html: str = ""
    soup: Any = None
    #: interstitial | signin | notfound | missing-anchor | unparseable | unreachable
    block: Optional[str] = None
    values: Dict[str, str] = field(default_factory=dict)
    nodes: Dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.soup is not None and self.block is None


def fetch(client: Any, cache: Dict[str, Page], url: str, *, referer: str = BASE) -> Page:
    """Fetch with interstitial retries and the ``#productTitle`` anchor check.

    The cache holds failures too: without it the ladder re-fetches the same page
    for discovery, verification, covers and reviews, re-eating the backoff each time.
    """
    if url in cache:
        return cache[url]
    page = Page(url=url)
    for attempt in range(len(_BACKOFF) + 1):
        response = client.get(url, referer=referer)
        if response is None:
            page.block = "unreachable"
            break
        try:
            page.html = response.text or ""
        except (UnicodeDecodeError, ValueError):
            page.block = "unparseable"   # Amazon occasionally mislabels charset
            break
        page.url = str(response.url or url)
        if _NOT_FOUND.search(page.html[:8192]):
            page.block = "notfound"      # deliberately never retried in a browser
            break
        if "/ax/claim" in page.url.lower() or _SIGNIN.search(page.html):
            page.block = "signin"
            break
        if len(page.html) < _INTERSTITIAL_MAX and any(
                m in page.html[:8192].lower() for m in _INTERSTITIAL):
            page.block = "interstitial"
            if attempt < len(_BACKOFF):
                time.sleep(_BACKOFF[attempt])
                continue
            break
        page.soup = soup_of(page.html, page.url)
        page.block = None if page.soup is not None else "unparseable"
        break

    if page.block == "interstitial" and client.browser.available:
        page.soup = client.rendered(url, wait_css="#productTitle", wait_seconds=10)
        if page.soup is not None:
            page.html, page.block = str(page.soup), None
    if page.soup is not None and "/dp/" in url and not page.soup.select_one("#productTitle"):
        # A soft-blocked 200 is not a product page; without this it parses into an
        # all-null record.
        page.block = "missing-anchor"
    if page.soup is not None:
        page.values, page.nodes = details(page.soup)
    cache[url] = page
    return page


def verify(page: Page, hint: Hint) -> str:
    """``isbn`` | ``fuzzy`` | ``mismatch`` -- does this page own our ISBN?"""
    found13 = isbn_utils.normalize(bullet(page.values, "ISBN-13", "ISBN13") or "")
    found10 = isbn_utils.normalize(bullet(page.values, "ISBN-10", "ISBN10") or "")
    if not found13 and (match := TITLE_TAG.search(page.html[:4096])):
        found13 = match.group("isbn13")
    wanted10 = hint.isbn10 or isbn_utils.to_isbn10(hint.isbn13) or ""
    if found13 == hint.isbn13 or (found10 and found10 == wanted10):
        return "isbn"
    # A page advertising a *different* ISBN is a different book, full stop. Only a
    # page with no ISBN at all (a Kindle or audio ASIN) may be matched fuzzily.
    return "mismatch" if (found13 or found10) else "fuzzy"


def plausible(hint: Hint, candidate: str) -> bool:
    """Accept a no-ISBN page only on a strong title *and* author agreement."""
    if not hint.title or is_sequel(hint.title, candidate) \
            or is_derivative(candidate, hint.title):
        return False
    if title_score(hint.title, candidate) < FUZZY_TITLE:
        return False
    return authors_agree(hint.authors, [candidate])


def candidates(listing: Page) -> List[Tuple[str, str]]:
    """``(asin, title)`` per search result, in Amazon's own ranking order."""
    out: List[Tuple[str, str]] = []
    for node in listing.soup.select(
            'div[data-component-type="s-search-result"][data-asin], '
            "div[data-asin][data-index]"):
        asin = (node.get("data-asin") or "").strip().upper()
        if re.fullmatch(r"[A-Z0-9]{10}", asin):
            out.append((asin, text(node.select_one("h2 span") or node.select_one("h2"))))
    return dedupe(out)


def canonical(page: Page) -> Page:
    node = page.soup.select_one('link[rel="canonical"]')
    if node is not None:
        page.url = absolutise(page.url, node.get("href")) or page.url
    return page


def resolve(client: Any, cache: Dict[str, Page], hint: Hint, result: Result
            ) -> Optional[Page]:
    """ISBN-10 as ASIN, then an ISBN-13 search, then a title+author search.

    The ISBN-10 must be **recomputed** with a real mod-11 check digit: truncating
    the ISBN-13 404s, and ``/dp/<isbn13>`` is never valid because an ISBN-13 is not
    an ASIN.
    """
    asin = hint.isbn10 or isbn_utils.to_isbn10(hint.isbn13)
    if asin:
        page = fetch(client, cache, f"{BASE}/dp/{asin}")
        if page.ok and verify(page, hint) != "mismatch":
            return canonical(page)

    queries = [str(hint.isbn13)]      # required for 979-, which has no ISBN-10
    if hint.title:
        queries.append(" ".join([hint.title] + list(hint.authors or [])[:2]))
    for query in queries:
        listing = fetch(client, cache, f"{BASE}/s?k={query}&i=stripbooks")
        if not listing.ok:
            continue
        opened = 0
        for candidate_asin, candidate_title in candidates(listing):
            # A study guide is not the book; do not spend a fetch on it.
            if is_derivative(candidate_title, hint.title or ""):
                continue
            if opened >= MAX_CANDIDATES:
                break
            opened += 1
            page = fetch(client, cache, f"{BASE}/dp/{candidate_asin}", referer=listing.url)
            if not page.ok:
                continue
            verdict = verify(page, hint)
            if verdict == "isbn":
                return canonical(page)
            if verdict == "fuzzy" and plausible(hint, candidate_title):
                result.warn("amazon: the ISBN did not resolve to a product page, so "
                            f"{candidate_title!r} was matched by title and author -- "
                            "a weaker identification than an ISBN")
                return canonical(page)
    return None
