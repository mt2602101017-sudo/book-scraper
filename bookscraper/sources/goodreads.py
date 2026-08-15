"""Goodreads adapter.

Strategy, in the order the code tries it (every fallback that fires appends a
warning to the :class:`~bookscraper.models.ScrapeResult`):

1. **Resolve the book URL** with one request to ``/book/isbn/<isbn>``, which 301s
   to ``/book/show/<legacyId>-<slug>``. Works for ISBN-13 and ISBN-10, and unlike
   ``/search`` is not disallowed by ``robots.txt``. Fallbacks: the ISBN-10 form,
   then ``/search?q=<isbn>``, then a title search with author verification.
2. **Parse ``<script id="__NEXT_DATA__">``** and pull the Apollo cache from
   ``props.pageProps.apolloState``. The book node is resolved *deterministically*
   via ``ROOT_QUERY['getBookByLegacyId(...)'].__ref``, because most ``Book:``
   entries in the cache are stubs whose ``title`` is ``None``. Title, authors,
   publisher, date, language, genres, cover, blurb and the first 30 reviews all
   come from this one blob.
3. **DOM/CSS fallbacks** for the fields present in rendered markup, then
   ``og:``/JSON-LD meta tags. Publisher, date and language are absent from the book
   DOM entirely, so for those the fallback is the legacy
   ``/work/editions/<workId>`` page (``Allow``-listed in robots.txt).
4. **More than 30 reviews**: the anonymous AppSync GraphQL endpoint, whose URL and
   API key are re-derived at runtime from the page's own ``_app-*.js`` chunk --
   never hardcoded, because Goodreads rotates that key -- resuming from the
   ``nextPageToken`` the page already embedded. Only used when the embedded 30 fall
   short of ``--min-reviews``.

Deliberate non-goals, documented rather than faked:

* ``origin`` is searched on every run by the shared ``probe_origin`` across the
  Apollo cache, JSON-LD, DOM and editions listing; Goodreads publishes no such
  field. ``work.details.places`` is the story's *setting* (a novel set in Ohio
  lists "Ohio"), so it is reported separately and never mapped to ``origin``.
* ``language`` is this edition's text language. Goodreads has no "original
  language" field; when the original title is in a non-Latin script we say so in a
  warning rather than invent a value.
* ``prefers_browser`` is ``False`` -- one plain GET yields everything. A browser is
  attempted only if the page comes back without ``__NEXT_DATA__``, the documented
  soft-block signature.
"""

from __future__ import annotations

import sys
from ..verbosity import verbose
import json
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Any, Dict, Iterator, List, Optional, Tuple

from bs4 import BeautifulSoup

from ..base import ORIGIN_KEY_SPELLINGS, BaseSource
from ..http_client import HttpClient
from ..models import BookHint, BookMetadata, ReviewItem, ScrapeResult

__all__ = ["GoodreadsSource"]

#: Site root. Every path below is joined onto this.
BASE_URL = "https://www.goodreads.com"

#: Publication timestamps in Goodreads' data are midnight *Pacific*, not UTC,
#: so converting in UTC can land a day early around a DST boundary.
try:  # pragma: no cover - depends on the platform's tz database
    from zoneinfo import ZoneInfo

    _PUB_TZ: Any = ZoneInfo("America/Los_Angeles")
except (ImportError, KeyError, OSError, ValueError):  # no tzdata installed
    _PUB_TZ = timezone.utc

#: Goodreads sits behind AWS WAF. When it decides to challenge a client it
#: answers **HTTP 202** with a 2.4 KB JavaScript token page and the header
#: ``x-amzn-waf-action: challenge`` -- a 200-shaped response that contains no
#: book data at all. These are the exact markers to recognise it by. We never
#: try to defeat it: we say so, optionally re-request through a real browser
#: (which runs the site's own script), and otherwise degrade to no data.
_WAF_HEADER = "x-amzn-waf-action"
_WAF_BODY_MARKERS: Tuple[str, ...] = (
    "awswafcookiedomainlist",
    "awswafintegration",
    "token.awswaf.com",
    "challenge.js",
    "we need to verify that you're not a robot",
)

#: Image URLs that mean "this edition has no cover"; never save these.
_PLACEHOLDER_IMAGE_MARKERS: Tuple[str, ...] = (
    "no-cover",
    "nophoto",
    "no_cover",
    "blank.g",
)

#: ``._SY75_`` / ``._SX50_`` are downscale directives; strip them for full size.
_IMAGE_SIZE_SUFFIX_RE = re.compile(r"\._S[XY]\d+_(?=\.[A-Za-z0-9]{2,5}$)")
#: ``/books/<ts><sizeclass>/`` -- the ``i`` class is the original upload.
_IMAGE_SIZE_CLASS_RE = re.compile(r"(/books/\d+)[a-z](?=/)")
#: Stable identity of a Goodreads image across its two CDN hostnames.
_IMAGE_KEY_RE = re.compile(r"/books/(.+)$")

#: ``/book/show/<legacyId>-<slug>`` -> ``<legacyId>``.
_LEGACY_ID_RE = re.compile(r"/book/show/(\d+)")
#: Any link that leaks the *work* legacy id when the JSON blob is unavailable.
_WORK_ID_RE = re.compile(r"/work/(?:editions|quotes|shelves|)/?(\d+)")
#: "Showing 1-10 of 151" on the editions page.
_EDITION_TOTAL_RE = re.compile(r"Showing\s+[\d,]+\s*-\s*[\d,]+\s+of\s+([\d,]+)")
#: "Published May 12th 2015 by Penguin Books" (legacy editions page free text).
_PUBLISHED_BY_RE = re.compile(r"^published\s+(.*?)(?:\s+by\s+(.+))?$", re.IGNORECASE | re.DOTALL)

#: The Next.js chunk that carries the AppSync endpoint/key table.
_APP_CHUNK_RE = re.compile(r"/_next/static/chunks/pages/_app-[A-Za-z0-9_.-]+\.js")
#: Within that chunk: the Production GraphQL credentials (quoted or not).
_APPSYNC_KEY_RE = re.compile(r"""apiKey["']?\s*:\s*["'](da2-[a-z0-9]+)["']""")
_APPSYNC_ENDPOINT_RE = re.compile(
    r"""endpoint["']?\s*:\s*["'](https://[A-Za-z0-9.\-/]+appsync-api[A-Za-z0-9.\-/]*)["']"""
)

#: Recovered verbatim from Goodreads' own client bundle.
_REVIEWS_QUERY = """
query getReviews($filters: BookReviewsFilterInput!, $pagination: PaginationInput) {
  getReviews(filters: $filters, pagination: $pagination) {
    totalCount
    pageInfo { prevPageToken nextPageToken }
    edges { node { id rating createdAt text creator { name webUrl } } }
  }
}
""".strip()



@dataclass
class _BookPage:
    """One fetched ``/book/show/`` page plus whatever structure we found in it."""

    url: str
    soup: BeautifulSoup
    strategy: str = "dom"                        # 'json' once apolloState parses
    state: Dict[str, Any] = field(default_factory=dict)
    book: Dict[str, Any] = field(default_factory=dict)
    work: Dict[str, Any] = field(default_factory=dict)
    reviews_root: Dict[str, Any] = field(default_factory=dict)
    legacy_id: Optional[str] = None
    fuzzy: bool = False                          # accepted without an ISBN match
    how: str = "isbn redirect"                   # which resolver produced the URL
    #: The ISBN-13 the page itself advertised, when it advertised one at all.
    found_isbn13: Optional[str] = None
    #: True when the page's own ISBN matched the request, False when it did not,
    #: ``None`` when the page carried no ISBN to compare and we accepted it on
    #: trust. ``None``/``False`` must never seed the shared hint.
    isbn_confirmed: Optional[bool] = None

    @property
    def details(self) -> Dict[str, Any]:
        value = self.book.get("details")
        return value if isinstance(value, dict) else {}


@dataclass
class _Edition:
    """One row of the legacy ``/work/editions/`` listing."""

    cover_url: Optional[str] = None
    title: Optional[str] = None
    book_url: Optional[str] = None
    legacy_id: Optional[str] = None
    isbn13: Optional[str] = None
    isbn10: Optional[str] = None
    publisher: Optional[str] = None
    published: Optional[str] = None
    language: Optional[str] = None


class GoodreadsSource(BaseSource):
    """Scrape metadata, covers, blurb, reviews and genres from Goodreads."""

    name = "goodreads"
    display_name = "Goodreads"
    #: One plain GET yields 100% of the data; a browser is only a block fallback.
    prefers_browser = False

    #: Hard ceiling on cover images (1 edition-exact + extra editions).
    MAX_COVERS: int = 6
    #: Editions-listing pages to walk when hunting extra covers (10 rows each).
    MAX_EDITION_PAGES: int = 2
    #: Reviews per GraphQL call (verified: the API honours up to 100).
    GRAPHQL_PAGE_SIZE: int = 100
    #: Ceiling on GraphQL review pages, so a huge --min-reviews cannot run away.
    MAX_REVIEW_PAGES: int = 5
    #: Minimum characters for a review body to be worth keeping.
    MIN_REVIEW_CHARS: int = 20

    def __init__(self, client: HttpClient) -> None:
        super().__init__(client)
        self._page: Optional[_BookPage] = None
        self._editions_cache: Dict[str, Optional[BeautifulSoup]] = {}
        self._appsync: Optional[Tuple[str, str]] = None
        self._appsync_tried = False

    # ------------------------------------------------------------------ URL

    def find_book_url(self, hint: BookHint) -> Optional[str]:
        """Return the canonical ``/book/show/`` URL for ``hint``, or ``None``.

        The fetched page is cached on the instance, so a subsequent
        :meth:`scrape` costs no extra request.
        """
        probe = self.new_result(hint)
        page = self._load_page(hint, probe)
        for warning in probe.warnings:
            # Nobody owns this throwaway result, so surface its notes in the log.
            if verbose():
                print('  find_book_url: %s' % (warning,), file=sys.stderr)
        return page.url if page is not None else None

    def _candidates(self, hint: BookHint, result: ScrapeResult) -> Iterator[Tuple[str, str, bool]]:
        """Yield ``(url, how, fuzzy)`` resolution attempts, best first."""
        isbn13 = (hint.isbn13 or "").strip()
        if isbn13:
            yield f"{BASE_URL}/book/isbn/{isbn13}", "isbn13 redirect", False

        isbn10 = (hint.isbn10 or "").strip()
        if isbn10 and isbn10 != isbn13:
            yield f"{BASE_URL}/book/isbn/{isbn10}", "isbn10 redirect", False

        if isbn13:
            result.warn(
                "falling back to /search?q=<isbn>, which Goodreads' robots.txt "
                "disallows for generic crawlers (used only because /book/isbn/ failed)"
            )
            yield f"{BASE_URL}/search?q={isbn13}", "isbn search", False

        for url in self._search_by_title(hint, result):
            yield url, "title+author search", True

    def _search_by_title(self, hint: BookHint, result: ScrapeResult) -> Iterator[str]:
        """Title-only search, keeping rows whose author matches ``hint.authors``.

        Searching ``title + author`` in one ``q`` is a trap: Goodreads ranks
        study guides and "15-minute summary" cash-ins above the real book. A
        title-field search plus explicit author matching is what actually works,
        and a same-title-different-author row (a real hazard) is rejected.
        """
        title = self.clean_text(hint.title)
        if not title:
            return
        authors = [self.clean_text(a) for a in (hint.authors or []) if self.clean_text(a)]
        if not authors:
            result.warn(
                "no author hint available, so the title search cannot verify the "
                "candidate book; skipping it rather than risk the wrong edition"
            )
            return

        result.warn(
            "resolving via title+author search (robots-disallowed /search path) "
            "because no ISBN URL resolved; the match is fuzzy by construction"
        )
        soup = self.client.get_soup(
            f"{BASE_URL}/search",
            params={"q": self._search_title(title), "search[field]": "title"},
            referer=BASE_URL,
        )
        if soup is None:
            reason = self.client.block_reason(BASE_URL)
            result.warn(
                "title search page could not be fetched"
                + (f" ({reason})" if reason else "")
            )
            return

        rows = soup.select("tr[itemtype], table.tableList tr")
        if not rows:
            result.warn("title search returned no parseable result rows")
            return

        scored: List[Tuple[float, str, str, str]] = []
        for row in rows:
            link = row.select_one('a.bookTitle[itemprop="url"], a.bookTitle')
            if link is None:
                continue
            href = self.absolutise(BASE_URL, (link.get("href") or "").split("?")[0])
            if not href:
                continue
            row_title = self.clean_text(
                link.select_one('span[itemprop="name"]') or link
            )
            row_authors = [
                self.clean_text(node)
                for node in row.select('a.authorName > span[itemprop="name"], a.authorName')
            ]
            if not self._authors_match(authors, row_authors):
                continue
            similarity = self._title_similarity(title, row_title)
            if similarity < 0.80:
                continue
            scored.append((similarity, href, row_title, ", ".join(row_authors)))

        if not scored:
            result.warn(
                "no title-search row matched both the title and an expected author, "
                "so nothing was accepted (Goodreads ranks summaries and study "
                "guides above the real book)"
            )
            return

        scored.sort(key=lambda item: -item[0])
        for similarity, href, row_title, row_authors in scored[:3]:
            print('Title search candidate %s (%r by %s, similarity %.2f)' % (href, row_title, row_authors, similarity), file=sys.stderr)
            yield href

    @staticmethod
    def _search_title(title: str) -> str:
        """Trim marketing tails so the title is usable as a search term.

        Goodreads titles can carry a dash-separated marketing clause (``"Sapiens:
        A Brief History of Humankind - The #1 New York Times Bestseller ..."``);
        everything from the first en/em dash separator is dropped.
        """
        trimmed = re.split(r"\s+[–—]\s+", title)[0].strip()
        trimmed = re.sub(r"\s*\([^()]*#\s*\d+[^()]*\)\s*$", "", trimmed).strip()
        return (trimmed or title)[:180]

    @staticmethod
    def _normalise_person(value: str) -> str:
        folded = unicodedata.normalize("NFKD", value or "")
        folded = "".join(c for c in folded if not unicodedata.combining(c))
        return re.sub(r"[^a-z0-9 ]+", " ", folded.lower()).strip()

    def _authors_match(self, wanted: List[str], found: List[str]) -> bool:
        """True when any found author plausibly equals any wanted author."""
        targets = {self._normalise_person(a) for a in wanted if a}
        targets.discard("")
        for candidate in found:
            key = self._normalise_person(candidate)
            if not key:
                continue
            for target in targets:
                if key == target:
                    return True
                # Surname-level agreement, e.g. "Celeste Ng" vs "Ng, Celeste".
                if set(key.split()) & set(target.split()) and (
                    SequenceMatcher(None, key, target).ratio() >= 0.72
                ):
                    return True
        return False

    def _title_similarity(self, wanted: str, found: str) -> float:
        left = re.sub(r"[^a-z0-9 ]+", " ", (wanted or "").lower()).strip()
        right = re.sub(r"[^a-z0-9 ]+", " ", (found or "").lower()).strip()
        if not left or not right:
            return 0.0
        if left == right:
            return 1.0
        return SequenceMatcher(None, left, right).ratio()

    # ----------------------------------------------------------------- page

    def _load_page(self, hint: BookHint, result: ScrapeResult) -> Optional[_BookPage]:
        """Fetch (once) and structure the book page. Returns ``None`` if hopeless."""
        if self._page is not None:
            return self._page

        for url, how, fuzzy in self._candidates(hint, result):
            print('Resolving %s via %s: %s' % (hint.isbn13, how, url), file=sys.stderr)
            response = self.client.get(url, referer=BASE_URL)
            if response is None:
                reason = self.client.block_reason(url)
                result.warn(
                    f"{how} request failed"
                    + (f": {reason}" if reason else " (see log for the HTTP error)")
                )
                if not reason or not self.client.browser_available:
                    continue
                # The client swallowed the body as a bot-block. A real browser runs
                # the site's own challenge script, so it is worth one attempt.
                rendered = self.client.get_rendered_soup(url, wait_css="h1")
                if rendered is None:
                    continue
                page = self._structure(url, rendered, how, fuzzy, result, reason)
                if page is None or not self._verify(page, hint, result):
                    continue
                result.warn(
                    "the plain HTTP request was blocked; this record came from a "
                    "browser-rendered copy of the page"
                )
                self._page = page
                return page

            final_url = str(response.url or url)
            challenge = self._waf_reason(response)
            if challenge:
                result.warn(
                    f"goodreads.com served an anti-bot challenge instead of the book "
                    f"page ({challenge}); no attempt was made to defeat it"
                )
                print('warning: Anti-bot challenge on %s: %s' % (final_url, challenge), file=sys.stderr)

            soup = self.client.soup_from_response(response)
            if soup is None:
                result.warn(f"{how} returned a body that could not be parsed as HTML")
                continue

            if getattr(self.client, "browser", "auto") == "always":
                # The resolution GET has to be a plain request (we need the
                # redirect target), but honour --browser always for the parse.
                rendered = self.client.get_rendered_soup(final_url, wait_css="h1")
                if rendered is not None:
                    soup = rendered
                else:
                    result.warn(
                        "a rendered fetch was requested but the page could not be "
                        "rendered; parsed the plain HTTP body instead"
                    )

            page = self._structure(final_url, soup, how, fuzzy, result, challenge)
            if page is None:
                continue
            if not self._verify(page, hint, result):
                continue
            self._page = page
            return page

        result.warn("could not resolve this ISBN to a Goodreads book page")
        return None

    @staticmethod
    def _waf_reason(response: Any) -> Optional[str]:
        """Return a description if ``response`` is an AWS WAF challenge, else ``None``."""
        try:
            headers = response.headers or {}
            action = ""
            for key, value in headers.items():
                if str(key).lower() == _WAF_HEADER:
                    action = str(value)
                    break
            status = int(getattr(response, "status_code", 0) or 0)
            body = (response.text or "")[:4096].lower()
        except (AttributeError, TypeError, ValueError, UnicodeDecodeError):
            return None
        if action:
            return f"HTTP {status}, {_WAF_HEADER}: {action}"
        hits = [marker for marker in _WAF_BODY_MARKERS if marker in body]
        if hits:
            return f"HTTP {status}, AWS WAF challenge body (matched {hits[0]!r})"
        if status == 202 and "__next_data__" not in body and len(body) < 8000:
            return f"HTTP {status} with a tiny non-book body"
        return None

    def _structure(
        self,
        url: str,
        soup: BeautifulSoup,
        how: str,
        fuzzy: bool,
        result: ScrapeResult,
        challenge: Optional[str] = None,
    ) -> Optional[_BookPage]:
        """Wrap a fetched page, extracting ``__NEXT_DATA__`` when it is there."""
        page = _BookPage(url=url, soup=soup, how=how, fuzzy=fuzzy)
        page.legacy_id = self._legacy_id(url, soup)

        payload = self._next_data(soup)
        if payload is None:
            # Documented soft-block signature: a 200-shaped response with a
            # plausible body but no embedded Next.js payload. Try a browser if one
            # exists (it runs the site's own script), then carry on with the DOM.
            if challenge:
                result.warn(
                    "the challenge page carries no book data; a real browser is the "
                    "only remaining option for this page"
                )
            else:
                result.warn(
                    "page had no __NEXT_DATA__ payload (soft block, redesign, or a "
                    "non-book page); falling back to DOM/CSS extraction, which cannot "
                    "see publisher, edition date or language"
                )
            if self.client.browser_available:
                print('Retrying %s through a browser for __NEXT_DATA__' % (url,), file=sys.stderr)
                rendered = self.client.get_rendered_soup(url, wait_css="h1")
                if rendered is not None:
                    payload = self._next_data(rendered)
                    if payload is not None:
                        page.soup = rendered
                        result.warn("recovered __NEXT_DATA__ via the Selenium fallback")
                    else:
                        result.warn("browser-rendered page also lacked __NEXT_DATA__")
                else:
                    reason = self.client.block_reason(url)
                    result.warn(
                        "browser fallback unavailable or blocked"
                        + (f" ({reason})" if reason else "")
                    )
            else:
                result.warn("no browser available for the __NEXT_DATA__ fallback")

        if payload is None and (
            challenge or not self.select_text(page.soup, 'h1[data-testid="bookTitle"]')
        ):
            # Neither JSON nor a book title in the DOM. Reject the page instead of
            # scraping an anti-bot interstitial: its <noscript> block contains an
            # <h1> ("JavaScript is disabled") that would otherwise be mistaken for
            # a title, and an all-null record would look like a successful scrape.
            result.warn(
                "this page contained no book data at all ("
                + (challenge or "no h1[data-testid=bookTitle] in the DOM")
                + "), so nothing was extracted from it"
            )
            return None

        if payload is not None:
            state = self._apollo_state(payload)
            if not state:
                result.warn("__NEXT_DATA__ carried no props.pageProps.apolloState")
            else:
                page.state = state
                book, ref = self._resolve_book(state, page.legacy_id, result)
                if book:
                    page.book = book
                    page.strategy = "json"
                    if not page.legacy_id and book.get("legacyId") is not None:
                        page.legacy_id = str(book.get("legacyId"))
                    # Prefer the book's own canonical URL over whatever we asked
                    # for (never og:url -- that points at a different edition).
                    web_url = self.clean_text(book.get("webUrl"))
                    if "/book/show/" in web_url:
                        page.url = web_url
                        page.legacy_id = page.legacy_id or self._legacy_id(web_url, None)
                    page.work = self._deref(state, book.get("work"))
                    if verbose():
                        print('  Resolved book node %s' % (ref,), file=sys.stderr)
                else:
                    result.warn(
                        "could not resolve the Book node inside apolloState; using "
                        "DOM/CSS extraction instead"
                    )
                root = state.get("ROOT_QUERY")
                if isinstance(root, dict) and isinstance(root.get("getReviews"), dict):
                    page.reviews_root = root["getReviews"]
        return page

    def _next_data(self, soup: Optional[BeautifulSoup]) -> Optional[Dict[str, Any]]:
        """Decode ``<script id="__NEXT_DATA__">`` into a dict, or ``None``."""
        if soup is None:
            return None
        try:
            node = soup.find("script", id="__NEXT_DATA__")
        except (AttributeError, TypeError) as exc:
            if verbose():
                print('  __NEXT_DATA__ lookup failed: %s' % (exc,), file=sys.stderr)
            return None
        if node is None:
            return None
        raw = node.string if node.string is not None else node.get_text()
        if not raw or not raw.strip():
            if verbose():
                print('  __NEXT_DATA__ tag was empty', file=sys.stderr)
            return None
        try:
            payload = json.loads(raw)
        except ValueError as exc:
            print('warning: __NEXT_DATA__ was not valid JSON: %s' % (exc,), file=sys.stderr)
            return None
        return payload if isinstance(payload, dict) else None

    @staticmethod
    def _apollo_state(payload: Dict[str, Any]) -> Dict[str, Any]:
        props = payload.get("props")
        page_props = props.get("pageProps") if isinstance(props, dict) else None
        state = page_props.get("apolloState") if isinstance(page_props, dict) else None
        return state if isinstance(state, dict) else {}

    @staticmethod
    def _legacy_id(url: str, soup: Optional[BeautifulSoup]) -> Optional[str]:
        match = _LEGACY_ID_RE.search(url or "")
        if match:
            return match.group(1)
        if soup is not None:
            canonical = soup.find("link", attrs={"rel": "canonical"})
            if canonical is not None:
                match = _LEGACY_ID_RE.search(canonical.get("href") or "")
                if match:
                    return match.group(1)
        return None

    def _resolve_book(
        self, state: Dict[str, Any], legacy_id: Optional[str], result: ScrapeResult
    ) -> Tuple[Dict[str, Any], Optional[str]]:
        """Find the real Book node. Most ``Book:`` cache entries are stubs.

        Preferred route is the serialised ``ROOT_QUERY`` field key (note the
        ``legacyId`` argument is a JSON *string* there). Fallbacks: any
        ``getBookByLegacyId(...)`` key, then the only ``Book:`` entry that
        actually carries a title.
        """
        root = state.get("ROOT_QUERY")
        if isinstance(root, dict):
            keys: List[str] = []
            if legacy_id:
                keys.append('getBookByLegacyId({"legacyId":"%s"})' % legacy_id)
            keys.extend(
                k for k in root
                if isinstance(k, str) and k.startswith("getBookByLegacyId(") and k not in keys
            )
            for index, key in enumerate(keys):
                node = self._deref(state, root.get(key))
                if node:
                    if index > 0 and legacy_id:
                        result.warn(
                            f"apolloState had no getBookByLegacyId entry for legacy id "
                            f"{legacy_id}; used {key!r} instead"
                        )
                    return node, str(root.get(key, {}).get("__ref"))

        candidates = [
            (key, value)
            for key, value in state.items()
            if isinstance(key, str) and key.startswith("Book:")
            and isinstance(value, dict) and value.get("title")
        ]
        if len(candidates) == 1:
            result.warn(
                "resolved the Book node by scanning apolloState (ROOT_QUERY had no "
                "usable getBookByLegacyId entry)"
            )
            return candidates[0][1], candidates[0][0]
        if candidates:
            result.warn(
                f"apolloState held {len(candidates)} titled Book nodes and no usable "
                "ROOT_QUERY reference, so none was trusted"
            )
        return {}, None

    @staticmethod
    def _deref(state: Dict[str, Any], node: Any) -> Dict[str, Any]:
        """Follow an Apollo ``{'__ref': ...}`` pointer; ``{}`` when unresolvable."""
        if isinstance(node, dict):
            ref = node.get("__ref")
            if isinstance(ref, str):
                target = state.get(ref)
                return target if isinstance(target, dict) else {}
            return node
        return {}

    def _verify(self, page: _BookPage, hint: BookHint, result: ScrapeResult) -> bool:
        """Confirm the page really is ``hint``'s book; warn when it is fuzzy."""
        found13 = self.clean_text(page.details.get("isbn13")).replace("-", "")
        found10 = self.clean_text(page.details.get("isbn")).replace("-", "").upper()
        if not found13:
            for blob in self.jsonld(page.soup, "Book"):
                raw = self.clean_text(blob.get("isbn")).replace("-", "").upper()
                if len(raw) == 13:
                    found13 = raw
                elif len(raw) == 10:
                    found10 = found10 or raw
                if found13:
                    break

        wanted13 = (hint.isbn13 or "").strip()
        wanted10 = (hint.isbn10 or "").strip().upper()

        page.found_isbn13 = found13 or None

        if found13 and wanted13:
            if found13 == wanted13:
                page.isbn_confirmed = True
                return True
            page.isbn_confirmed = False
            if page.fuzzy:
                result.warn(
                    f"accepted {page.url} on a fuzzy title+author match: its ISBN-13 "
                    f"is {found13}, not the requested {wanted13}, so publisher, "
                    "edition date and cover belong to a different edition"
                )
                return True
            result.warn(
                f"{page.url} reports ISBN-13 {found13}, not the requested {wanted13}; "
                "rejecting it and trying the next resolution strategy"
            )
            return False

        if found10 and wanted10 and found10 == wanted10:
            page.isbn_confirmed = True
            return True

        # No ISBN on the page at all: accept it, but record that nothing was
        # confirmed. scrape() then refuses to seed the shared hint from this page,
        # so an unverified title cannot cascade into the other four adapters'
        # title+author discovery.
        page.isbn_confirmed = None
        if page.fuzzy:
            result.warn(
                f"accepted {page.url} on a fuzzy title+author match with no ISBN on "
                "the page to confirm it; treat the edition-specific fields with care. "
                "Because the identity is unconfirmed, this page's title/authors were "
                "NOT used to seed the other sources' searches"
            )
        else:
            result.warn(
                "could not confirm the resolved page's ISBN (no ISBN in the page "
                "data); accepting the redirect target on trust. Because the identity "
                "is unconfirmed, this page's title/authors were NOT used to seed the "
                "other sources' searches"
            )
        return True

    # ---------------------------------------------------------------- scrape

    def _scrape_into(self, hint: BookHint, result: ScrapeResult) -> None:
        """Scrape metadata, covers, blurb, reviews and genres. Never raises."""
        page = self._load_page(hint, result)
        if page is None:
            reason = self.client.block_reason(BASE_URL)
            if reason:
                result.warn(f"goodreads.com blocked automated access: {reason}")
            return

        result.book_url = page.url
        if page.strategy != "json":
            result.warn(
                "primary strategy (__NEXT_DATA__/apolloState) unavailable; every "
                "field below came from DOM, meta or editions-page fallbacks"
            )

        metadata = self._metadata(page, hint, result)
        result.metadata = metadata
        result.genres = list(metadata.genres)
        result.blurb = self._blurb(page, result)
        result.cover_urls = self._covers(page, hint, result)
        result.reviews = self._reviews(page, result)
        # Only a page whose own ISBN we confirmed may seed the shared hint.
        # Goodreads runs first, so a wrong title here would propagate into
        # Amazon's, Audible's, BookBub's and Kobo's title+author discovery and
        # produce four more files for the wrong book.
        if page.isbn_confirmed:
            result.hint_updates = BookHint(
                isbn13=hint.isbn13,
                isbn10=self._isbn10(page, hint),
                title=self._search_title(metadata.title) if metadata.title else None,
                authors=list(metadata.authors),
            )
        else:
            print('warning: Not seeding the shared title/author hint from %s: its ISBN could not be confirmed as %s' % (page.url, hint.isbn13), file=sys.stderr)
        print('goodreads: %s -> %d cover(s), %d review(s), %d genre(s), blurb %d chars' % (page.url, len(result.cover_urls), len(result.reviews), len(result.genres), len(result.blurb or '')), file=sys.stderr)

    # -------------------------------------------------------------- metadata

    def _metadata(self, page: _BookPage, hint: BookHint, result: ScrapeResult) -> BookMetadata:
        """Assemble the metadata record, warning for every fallback used."""
        metadata = self.new_metadata(hint)
        ld = (self.jsonld(page.soup, "Book") or [{}])[0]

        metadata.title = self._title(page, ld, result)
        metadata.authors = self._authors(page, ld, result)
        metadata.genres = self._genres(page, result)

        edition = self._matching_edition(page, hint, result)

        metadata.publisher = self._publisher(page, edition, result)
        metadata.date_of_publication = self._published(page, edition, result)
        metadata.language = self._language(page, ld, edition, result)
        metadata.origin = self._origin(page, result)
        return metadata

    def _title(self, page: _BookPage, ld: Dict[str, Any], result: ScrapeResult) -> Optional[str]:
        value = self.clean_text(page.book.get("title") or page.book.get("titleComplete"))
        if value:
            return value
        # Deliberately *not* a bare "h1" fallback: anti-bot interstitials carry an
        # <h1> of their own ("JavaScript is disabled") that must never be scraped.
        value = self.select_text(page.soup, 'h1[data-testid="bookTitle"]')
        if value:
            result.warn("title came from the h1[data-testid=bookTitle] fallback")
            return value
        value = self.clean_text(ld.get("name"))
        if value:
            result.warn("title came from the JSON-LD fallback")
            return value
        value = self.meta(page.soup, "og:title", "twitter:title")
        if value:
            result.warn("title came from the og:title meta fallback")
            return value
        result.warn("no title found by any strategy")
        return None

    def _authors(
        self, page: _BookPage, ld: Dict[str, Any], result: ScrapeResult
    ) -> List[str]:
        """Authors from the contributor edges; CSS route leaks reviewer names."""
        names: List[str] = []
        edges: List[Any] = []
        primary = page.book.get("primaryContributorEdge")
        if isinstance(primary, dict):
            edges.append(primary)
        secondary = page.book.get("secondaryContributorEdges")
        if isinstance(secondary, list):
            edges.extend(secondary)
        for edge in edges:
            if not isinstance(edge, dict):
                continue
            node = self._deref(page.state, edge.get("node"))
            name = self.clean_text(node.get("name"))
            if not name:
                continue
            role = self.clean_text(edge.get("role"))
            names.append(name if role.lower() in ("", "author") else f"{name} ({role})")
        names = self.dedupe(names)
        if names:
            return names

        ld_authors = self.split_list(ld.get("author"))
        if ld_authors:
            result.warn("authors came from the JSON-LD fallback, not the contributor edges")
            return ld_authors

        css = self.select_texts(page.soup, "span.ContributorLink__name")
        if css:
            result.warn(
                "authors came from the span.ContributorLink__name fallback, which also "
                "matches reviewer names on this page; only the first entry was kept"
            )
            return css[:1]
        result.warn("no authors found by any strategy")
        return []

    def _genres(self, page: _BookPage, result: ScrapeResult) -> List[str]:
        genres: List[str] = []
        raw = page.book.get("bookGenres")
        if isinstance(raw, list):
            for entry in raw:
                if not isinstance(entry, dict):
                    continue
                genre = entry.get("genre")
                name = self.clean_text(genre.get("name")) if isinstance(genre, dict) else ""
                if name:
                    genres.append(name)
        genres = self.dedupe(genres)
        if genres:
            return genres

        css = self.select_texts(
            page.soup,
            "span.BookPageMetadataSection__genreButton a.Button--tag span.Button__labelItem",
            "span.BookPageMetadataSection__genreButton a",
        )
        if css:
            result.warn(
                f"genres came from the CSS fallback, which only renders {len(css)} of "
                "the genres Goodreads holds (the rest sit behind a '...more' control)"
            )
            return css
        result.warn("no genres found by any strategy")
        return []

    def _publisher(
        self, page: _BookPage, edition: Optional[_Edition], result: ScrapeResult
    ) -> Optional[str]:
        value = self.clean_text(page.details.get("publisher"))
        if value:
            return value
        if edition is not None and edition.publisher:
            result.warn(
                "publisher came from the legacy /work/editions/ page; it is absent "
                "from the book page DOM entirely"
            )
            return edition.publisher
        result.warn(
            "no publisher found: Goodreads exposes it only inside __NEXT_DATA__ (or "
            "on the editions page), never in the book page's DOM or meta tags"
        )
        return None

    def _published(
        self, page: _BookPage, edition: Optional[_Edition], result: ScrapeResult
    ) -> Optional[str]:
        """Edition publication date, preferring the edition-exact epoch."""
        value = self._epoch_to_date(page.details.get("publicationTime"))
        if value:
            return value
        if edition is not None and edition.published:
            result.warn("edition publication date came from the /work/editions/ page")
            return edition.published

        work_details = page.work.get("details")
        if isinstance(work_details, dict):
            value = self._epoch_to_date(work_details.get("publicationTime"))
            if value:
                result.warn(
                    "no edition publication date available; using the work's "
                    f"first-publication date ({value}) instead"
                )
                return value

        dom = self.select_text(page.soup, 'p[data-testid="publicationInfo"]')
        if dom:
            value = self.iso_date(dom)
            if value:
                result.warn(
                    "publication date came from the DOM, which only shows the work's "
                    f"FIRST publication ({dom!r}), not this edition's"
                )
                return value
        result.warn("no publication date found by any strategy")
        return None

    def _language(
        self,
        page: _BookPage,
        ld: Dict[str, Any],
        edition: Optional[_Edition],
        result: ScrapeResult,
    ) -> Optional[str]:
        """Language of this edition's text, plus a translation caveat."""
        language = ""
        node = page.details.get("language")
        if isinstance(node, dict):
            language = self.clean_text(node.get("name"))
        elif node is not None:
            language = self.clean_text(node)

        if not language:
            language = self.clean_text(ld.get("inLanguage"))
            if language:
                result.warn("language came from the JSON-LD inLanguage fallback")
        if not language and edition is not None and edition.language:
            language = edition.language
            result.warn(
                "language came from the 'Edition language:' row of the "
                "/work/editions/ page"
            )
        if not language:
            result.warn(
                "no language found: it exists only in __NEXT_DATA__, JSON-LD or the "
                "editions page -- the book DOM does not carry it"
            )
            return None

        self._warn_if_translation(page, language, result)
        return language

    def _warn_if_translation(
        self, page: _BookPage, language: str, result: ScrapeResult
    ) -> None:
        """Flag the edition-language vs original-composition-language gap."""
        details = page.work.get("details")
        original = self.clean_text(details.get("originalTitle")) if isinstance(details, dict) else ""
        if not original or not self._is_non_latin(original):
            return
        result.warn(
            f"language {language!r} is the language of THIS EDITION's text; the work's "
            f"original title ({original!r}) is in a non-Latin script, so this edition "
            "is very likely a translation. Goodreads has no original-language field, "
            "so no value is invented for it"
        )

    @staticmethod
    def _is_non_latin(text: str) -> bool:
        letters = [c for c in text if c.isalpha()]
        if not letters:
            return False
        non_latin = sum(
            1 for c in letters if "LATIN" not in unicodedata.name(c, "")
        )
        return non_latin > len(letters) / 2

    def _origin(self, page: _BookPage, result: ScrapeResult) -> Optional[str]:
        """Place of publication, from Goodreads only -- searched for, not assumed.

        Every layer this run parsed is handed to the shared
        :meth:`~bookscraper.base.BaseSource.probe_origin`: the ``__NEXT_DATA__``
        Apollo cache's work and book records, the JSON-LD ``Book`` blocks, the
        book page DOM (``og:``/``meta`` included) and any legacy
        ``/work/editions/`` listing already fetched. A real hit is returned as a
        real value, so the field self-heals; otherwise the warning names the
        layers the probe reports having searched.

        The field that *looks* like an answer, ``work.details.places``, is the
        story's *setting*, so it is reported as a distinct note and never used.
        """
        layers: List[Tuple[str, Any]] = [
            ("the __NEXT_DATA__ Apollo cache's work record", page.work),
            ("its book record (book.details included)", page.book),
            ("the JSON-LD Book blocks", self.jsonld(page.soup, "Book")),
            ("the book page DOM and og:/meta tags", page.soup),
        ]
        editions = [s for s in self._editions_cache.values() if s is not None]
        if editions:
            layers.append(("the legacy /work/editions/ listing", editions[0]))

        probe = self.probe_origin_detail(layers)
        if probe.value:
            result.warn(
                f"origin {probe.value!r} was read from Goodreads' page "
                f"({probe.where}); Goodreads did not previously publish a place of "
                "publication, so treat this as new"
            )
            return probe.value

        self.origin_unavailable(
            result,
            "no place-of-publication key or label was found in the "
            f"{len(ORIGIN_KEY_SPELLINGS)} spellings the shared origin probe searches "
            "for, across every layer this run could parse ("
            f"{self.origin_layers_clause(probe.searched)}) -- all of which are read "
            "for the other fields",
        )
        details = page.work.get("details")
        places = details.get("places") if isinstance(details, dict) else None
        if isinstance(places, list) and places:
            named = [
                self.clean_text(p.get("name")) for p in places if isinstance(p, dict)
            ]
            named = [n for n in named if n]
            if named:
                result.warn(
                    "note: work.details.places (" + ", ".join(named[:4]) + ") is the "
                    "story's SETTING, not the place of publication, so it was "
                    "deliberately not used as origin"
                )
        return None

    def _isbn10(self, page: _BookPage, hint: BookHint) -> Optional[str]:
        value = self.clean_text(page.details.get("isbn")).replace("-", "").upper()
        if len(value) == 10:
            return value
        return hint.isbn10

    def _epoch_to_date(self, raw: Any) -> Optional[str]:
        """Epoch-milliseconds -> ``YYYY-MM-DD`` in Goodreads' own timezone."""
        if raw is None or isinstance(raw, bool):
            return None
        try:
            millis = int(raw)
        except (TypeError, ValueError):
            return None
        try:
            return datetime.fromtimestamp(millis / 1000.0, _PUB_TZ).date().isoformat()
        except (OverflowError, OSError, ValueError) as exc:
            if verbose():
                print('  Could not convert epoch %r: %s' % (raw, exc), file=sys.stderr)
            return None

    # ----------------------------------------------------------------- blurb

    def _blurb(self, page: _BookPage, result: ScrapeResult) -> Optional[str]:
        """Description as plain text, with the librarian's note stripped."""
        raw = page.book.get("description")
        source = "__NEXT_DATA__"
        text = self.html_to_text(raw) if raw else ""
        if not text:
            node = page.soup.select_one(
                'div[data-testid="description"] .Formatted'
            ) or page.soup.select_one('div[data-testid="description"]')
            if node is not None:
                text = self.html_to_text(node.decode_contents())
                if text:
                    source = "CSS"
                    # Only claim the fallback fired once we know it produced text:
                    # the wrapper div exists on review-less stub pages too.
                    result.warn(
                        "blurb came from the div[data-testid=description] CSS fallback"
                    )
        if not text:
            # og:description is truncated to ~54 chars, so it is a last resort only.
            meta_text = self.meta(page.soup, "og:description", "description")
            if meta_text:
                result.warn(
                    "blurb came from og:description, which Goodreads truncates -- it "
                    "is an excerpt, not the full description"
                )
                return meta_text
            result.warn("no blurb/description found by any strategy")
            return None

        cleaned, stripped = self._strip_librarian_note(text)
        if stripped:
            result.warn(
                "stripped a \"Librarian's note\" block from the blurb (editorial "
                "metadata about alternate cover editions, not description); such a "
                "note can precede or follow the real description and both are removed"
            )
        if verbose():
            print('  Blurb via %s: %d chars' % (source, len(cleaned)), file=sys.stderr)
        return cleaned or None

    @staticmethod
    def _strip_librarian_note(text: str) -> Tuple[str, bool]:
        """Remove a Goodreads librarian's note wherever it sits in the blurb.

        Librarians add "Librarian's note: An alternate cover edition can be found
        here" either **after** the description or, just as often, **before** it.
        The trailing case was handled; the leading case used to bail out
        (``match.start() == 0`` -> return unchanged), so the blurb's first
        sentence was editorial metadata about a *different* cover edition -- and,
        because the anchor text is kept while the ``<a href>`` is dropped, it
        ended on a dangling "here". Both positions are now handled in one pass.
        """
        pattern = re.compile(r"Librarian'?s note\s*:", re.IGNORECASE)
        match = pattern.search(text)
        if match is None:
            return text.strip(), False

        if match.start() > 0:
            # A note that follows the description: keep everything before it.
            return text[: match.start()].strip(), True

        # A note that *opens* the description: drop forward to the end of its
        # paragraph (blank line), or to the end of the line if it is unparagraphed.
        rest = text[match.end():]
        break_match = re.search(r"\n\s*\n", rest)
        if break_match is not None:
            remainder = rest[break_match.end():]
        else:
            newline = rest.find("\n")
            remainder = rest[newline + 1:] if newline != -1 else ""
        remainder = remainder.strip()
        if not remainder:
            # The note was the entire description -- keep the original rather
            # than emit an empty blurb, and report it as not stripped so the
            # caller does not claim a clean-up it did not achieve.
            return text.strip(), False
        # A note can legitimately appear at both ends.
        return pattern.split(remainder, maxsplit=1)[0].strip(), True

    # ---------------------------------------------------------------- covers

    def _covers(self, page: _BookPage, hint: BookHint, result: ScrapeResult) -> List[str]:
        """Edition-exact cover first, then other editions' covers, numbered."""
        urls: List[str] = []
        seen: set = set()

        def add(candidate: Any) -> bool:
            cleaned = self._clean_image(page.url, candidate)
            if not cleaned:
                return False
            key = self._image_key(cleaned)
            if key in seen:
                return False
            seen.add(key)
            urls.append(cleaned)
            return True

        if not add(page.book.get("imageUrl")):
            node = page.soup.select_one("div.BookCover__image img.ResponsiveImage")
            if node is not None and add(node.get("src")):
                result.warn("cover URL came from the div.BookCover__image CSS fallback")
            elif add(self.meta(page.soup, "og:image", "twitter:image")):
                result.warn("cover URL came from the og:image meta fallback")
            else:
                ld = (self.jsonld(page.soup, "Book") or [{}])[0]
                if add(ld.get("image")):
                    result.warn("cover URL came from the JSON-LD image fallback")
                else:
                    result.warn("no cover image URL found by any strategy")

        if not self.want_covers:
            print("cover downloads are off: not enumerating other editions' covers", file=sys.stderr)
            return urls

        # The listing renders 10 editions per page, so ask for only as many pages
        # as the remaining cover budget actually needs.
        shortfall = max(0, self.MAX_COVERS - len(urls))
        pages = min(self.MAX_EDITION_PAGES, max(1, -(-shortfall // 10)))
        for edition in self._editions(page, result, pages):
            if len(urls) >= self.MAX_COVERS:
                break
            add(edition.cover_url)

        if len(urls) > 1:
            print('Collected %d cover images (1 for this edition, %d from other editions)' % (len(urls), len(urls) - 1), file=sys.stderr)
        return urls

    def _clean_image(self, base: str, candidate: Any) -> str:
        """Absolutise, reject placeholders, and upgrade to the original upload."""
        url = self.absolutise(base, candidate)
        if not url:
            return ""
        lowered = url.lower()
        if any(marker in lowered for marker in _PLACEHOLDER_IMAGE_MARKERS):
            if verbose():
                print('  Discarding placeholder cover %s' % (url,), file=sys.stderr)
            return ""
        url = _IMAGE_SIZE_SUFFIX_RE.sub("", url)
        url = _IMAGE_SIZE_CLASS_RE.sub(r"\1i", url)
        return url

    @staticmethod
    def _image_key(url: str) -> str:
        """Identity of an image irrespective of which CDN host served it."""
        match = _IMAGE_KEY_RE.search(url)
        return (match.group(1) if match else url).lower()

    # -------------------------------------------------------------- editions

    def _work_id(self, page: _BookPage) -> Optional[str]:
        """Legacy work id, needed for ``/work/editions/<id>``."""
        legacy = page.work.get("legacyId")
        if legacy:
            return str(legacy)
        editions = page.work.get("editions")
        if isinstance(editions, dict):
            match = _WORK_ID_RE.search(str(editions.get("webUrl") or ""))
            if match:
                return match.group(1)
        details = page.work.get("details")
        if isinstance(details, dict):
            match = _WORK_ID_RE.search(str(details.get("webUrl") or ""))
            if match:
                return match.group(1)
        try:
            links = page.soup.select('a[href*="/work/"]')
        except Exception as exc:  # bs4 selector engine hiccup
            if verbose():
                print('  work-link scan failed: %s' % (exc,), file=sys.stderr)
            return None
        for link in links:
            match = _WORK_ID_RE.search(link.get("href") or "")
            if match:
                return match.group(1)
        return None

    def _editions(
        self, page: _BookPage, result: ScrapeResult, max_pages: int
    ) -> List[_Edition]:
        """Parse up to ``max_pages`` of the legacy work-editions listing."""
        work_id = self._work_id(page)
        if not work_id:
            result.warn(
                "could not determine the Goodreads work id, so other editions "
                "(extra covers, publisher fallback) could not be enumerated"
            )
            return []

        editions: List[_Edition] = []
        for number in range(1, max(1, int(max_pages)) + 1):
            url = f"{BASE_URL}/work/editions/{work_id}"
            if number > 1:
                url += f"?page={number}"
            soup = self._editions_soup(url, page.url, result)
            if soup is None:
                break
            rows = soup.select("div.elementList.clearFix")
            if not rows:
                result.warn(
                    f"editions page {number} had no parseable edition rows "
                    "(div.elementList.clearFix)"
                )
                break
            for row in rows:
                parsed = self._parse_edition(row)
                if parsed is not None:
                    editions.append(parsed)
            total = _EDITION_TOTAL_RE.search(self.clean_text(soup.get_text(" ")))
            if total:
                try:
                    if len(editions) >= int(total.group(1).replace(",", "")):
                        break
                except ValueError:
                    pass
            if len(rows) < 10:
                break
        if verbose():
            print('  Parsed %d edition row(s) for work %s' % (len(editions), work_id), file=sys.stderr)
        return editions

    def _editions_soup(
        self, url: str, referer: str, result: ScrapeResult
    ) -> Optional[BeautifulSoup]:
        if url in self._editions_cache:
            return self._editions_cache[url]
        soup = self.client.get_soup(url, referer=referer)
        if soup is None:
            reason = self.client.block_reason(url)
            result.warn(
                "could not fetch the /work/editions/ page"
                + (f" ({reason})" if reason else "")
            )
        elif not soup.select("div.elementList") and self.client.browser_available:
            # Most likely the same AWS WAF challenge as on the book page. Retry
            # through the browser, which runs the site's own challenge script.
            rendered = self.client.get_rendered_soup(url, wait_css="div.elementList")
            if rendered is not None and rendered.select("div.elementList"):
                result.warn(
                    "the /work/editions/ page came back without edition rows "
                    "(anti-bot challenge); recovered it through the browser"
                )
                soup = rendered
        self._editions_cache[url] = soup
        return soup

    def _parse_edition(self, row: Any) -> Optional[_Edition]:
        """Turn one ``div.elementList`` into an :class:`_Edition`."""
        try:
            image = row.select_one("div.leftAlignedImage img")
            link = row.select_one("a.bookTitle")
            data_rows = row.select("div.dataRow")
        except Exception as exc:  # selector engine hiccup on odd markup
            if verbose():
                print('  Edition row parse failed: %s' % (exc,), file=sys.stderr)
            return None

        edition = _Edition()
        if image is not None:
            edition.cover_url = image.get("src") or None
        if link is not None:
            edition.title = self.clean_text(link) or None
            href = (link.get("href") or "").split("?")[0]
            edition.book_url = self.absolutise(BASE_URL, href) or None
            match = _LEGACY_ID_RE.search(href)
            if match:
                edition.legacy_id = match.group(1)

        for data_row in data_rows:
            label_node = data_row.select_one("div.dataTitle")
            text = self.clean_text(data_row).replace("\n", " ")
            if label_node is None:
                if text.lower().startswith("published"):
                    match = _PUBLISHED_BY_RE.match(text)
                    if match:
                        edition.published = self.iso_date(match.group(1))
                        edition.publisher = self.clean_text(match.group(2)) or None
                continue
            label = self.clean_text(label_node).rstrip(":").lower()
            value = self.clean_text(text[len(self.clean_text(label_node)):])
            if label == "isbn":
                found13 = re.search(r"\b(97[89][0-9]{10})\b", value.replace("-", ""))
                found10 = re.search(r"ISBN10:\s*([0-9]{9}[0-9Xx])", value)
                edition.isbn13 = found13.group(1) if found13 else None
                edition.isbn10 = found10.group(1).upper() if found10 else None
            elif label == "edition language":
                edition.language = value or None
        return edition

    def _matching_edition(
        self, page: _BookPage, hint: BookHint, result: ScrapeResult
    ) -> Optional[_Edition]:
        """Find our own edition on the editions page (a publisher/date fallback).

        Only fetched when ``__NEXT_DATA__`` did not supply publisher, edition
        date and language -- otherwise it would be a pointless extra request.
        """
        details = page.details
        node = details.get("language")
        have_language = bool(
            self.clean_text(node.get("name")) if isinstance(node, dict) else node
        )
        if details.get("publisher") and details.get("publicationTime") and have_language:
            return None

        for edition in self._editions(page, result, 1):
            if (
                (edition.isbn13 and edition.isbn13 == (hint.isbn13 or "").strip())
                or (edition.isbn10 and hint.isbn10 and edition.isbn10 == hint.isbn10.upper())
                or (edition.legacy_id and page.legacy_id and edition.legacy_id == page.legacy_id)
            ):
                print('Matched our edition on the editions page: %s' % (edition.title,), file=sys.stderr)
                return edition
        result.warn(
            "our edition was not on page 1 of the /work/editions/ listing, so the "
            "publisher/date/language fallback could not be used"
        )
        return None

    # ---------------------------------------------------------------- reviews

    def _reviews(self, page: _BookPage, result: ScrapeResult) -> List[ReviewItem]:
        """Embedded reviews first, then GraphQL pages, then a DOM fallback."""
        wanted = max(0, int(self.min_reviews or 0))
        if self.max_reviews is not None:
            wanted = min(wanted, max(0, int(self.max_reviews)))

        seen: set = set()
        reviews: List[ReviewItem] = []
        embedded = self._reviews_from_state(page, seen)
        reviews.extend(embedded)
        if embedded:
            print('Recovered %d review(s) from the embedded page data' % (len(embedded),), file=sys.stderr)
        else:
            result.warn("no reviews found in the embedded page data")

        if not reviews:
            # Fallback, not a supplement: the DOM cards are the same reviews the
            # embedded data holds, so this only runs when that route came up dry.
            dom = self._reviews_from_dom(page, seen)
            if dom:
                result.warn(
                    f"{len(dom)} review(s) came from the article.ReviewCard DOM "
                    "fallback because the embedded review data was unusable"
                )
                reviews.extend(dom)

        total = self._review_total(page)
        if len(reviews) < wanted and total != 0:
            extra = self._reviews_from_graphql(page, wanted - len(reviews), seen, result)
            if extra:
                result.warn(
                    f"fetched {len(extra)} additional review(s) through Goodreads' "
                    "anonymous GraphQL API to reach the requested minimum"
                )
                reviews.extend(extra)

        if len(reviews) < wanted:
            result.warn(
                f"only {len(reviews)} review(s) could be recovered, short of the "
                f"requested {wanted}"
                + (f" (Goodreads reports {total} in total for this work)" if total else "")
            )
        if self.max_reviews is not None and len(reviews) > self.max_reviews:
            reviews = reviews[: self.max_reviews]
        return reviews

    def _review_total(self, page: _BookPage) -> Optional[int]:
        raw = page.reviews_root.get("totalCount")
        try:
            return int(raw) if raw is not None else None
        except (TypeError, ValueError):
            return None

    def _reviews_from_state(self, page: _BookPage, seen: set) -> List[ReviewItem]:
        """Resolve ``ROOT_QUERY.getReviews.edges`` in Goodreads' own ranking order."""
        out: List[ReviewItem] = []
        edges = page.reviews_root.get("edges")
        nodes: List[Dict[str, Any]] = []
        if isinstance(edges, list):
            for edge in edges:
                if isinstance(edge, dict):
                    node = self._deref(page.state, edge.get("node"))
                    if node:
                        nodes.append(node)
        if not nodes:
            # Unordered fallback: every Review: entry in the cache.
            nodes = [
                value for key, value in page.state.items()
                if isinstance(key, str) and key.startswith("Review:")
                and isinstance(value, dict)
            ]
        for node in nodes:
            item = self._review_from_node(page, node, seen)
            if item is not None:
                out.append(item)
        return out

    def _review_from_node(
        self, page: _BookPage, node: Dict[str, Any], seen: set
    ) -> Optional[ReviewItem]:
        identity = self.clean_text(node.get("id"))
        creator = self._deref(page.state, node.get("creator"))
        shelving = self._deref(page.state, node.get("shelving"))
        return self._build_review(
            text=node.get("text"),
            reviewer=creator.get("name"),
            rating=node.get("rating"),
            created=node.get("createdAt"),
            url=shelving.get("webUrl"),
            identity=identity,
            seen=seen,
        )

    def _reviews_from_dom(self, page: _BookPage, seen: set) -> List[ReviewItem]:
        """Server-rendered review cards -- works with no JavaScript at all."""
        out: List[ReviewItem] = []
        try:
            cards = page.soup.select("article.ReviewCard")
        except Exception as exc:
            if verbose():
                print('  ReviewCard selector failed: %s' % (exc,), file=sys.stderr)
            return out
        for card in cards:
            bodies = card.select("section.ReviewCard__content span.Formatted") or card.select(
                "span.Formatted, div.ReviewText"
            )
            if not bodies:
                continue
            # Both a truncated and a full copy can be emitted; keep the longest.
            body = max(bodies, key=lambda node: len(node.get_text()))
            rating = None
            stars = card.select_one("span.RatingStars, div.ShelfStatus span.RatingStars")
            if stars is not None:
                match = re.search(r"(\d+(?:\.\d+)?)\s*out of\s*(\d+)",
                                  stars.get("aria-label") or "")
                if match:
                    rating = f"{match.group(1)}/{match.group(2)}"
            url = None
            date = None
            for anchor in card.select("a"):
                href = anchor.get("href") or ""
                if "/review/show" in href:
                    url = self.absolutise(page.url, href)
                    date = self.iso_date(anchor.get_text())
                    break
            reviewer = self.select_text(card, ".ReviewerProfile__name", ".ReviewerProfile__name a")
            item = self._build_review(
                text=body.decode_contents(),
                reviewer=reviewer,
                rating=rating,
                created=None,
                url=url,
                identity=url or "",
                seen=seen,
                date=date,
            )
            if item is not None:
                out.append(item)
        return out

    def _reviews_from_graphql(
        self, page: _BookPage, needed: int, seen: set, result: ScrapeResult
    ) -> List[ReviewItem]:
        """Page past the embedded 30 via Goodreads' anonymous AppSync API.

        Resumes from the ``nextPageToken`` the page already embedded, so the
        first extra page does not repeat what we already have. Reviews obtained
        this way carry no permalink: the query document is used exactly as
        recovered from Goodreads' own bundle (schema introspection is blocked,
        so adding fields cannot be validated first).
        """
        if needed <= 0:
            return []
        credentials = self._appsync_credentials(page, result)
        if credentials is None:
            return []
        endpoint, api_key = credentials

        resource_type, resource_id = self._review_resource(page)
        if not resource_id:
            result.warn(
                "no work/book id was available, so the GraphQL review query could "
                "not be built"
            )
            return []

        page_info = page.reviews_root.get("pageInfo")
        token = None
        if isinstance(page_info, dict):
            token = self.clean_text(page_info.get("nextPageToken")) or None

        out: List[ReviewItem] = []
        for attempt in range(1, self.MAX_REVIEW_PAGES + 1):
            if len(out) >= needed:
                break
            pagination: Dict[str, Any] = {
                "limit": max(1, min(self.GRAPHQL_PAGE_SIZE, needed - len(out) + 5)),
            }
            if token:
                pagination["after"] = token
            payload = {
                "query": _REVIEWS_QUERY,
                "variables": {
                    "filters": {"resourceType": resource_type, "resourceId": resource_id},
                    "pagination": pagination,
                },
            }
            data = self.client.post_json(
                endpoint,
                payload,
                headers={"X-Api-Key": api_key, "Origin": BASE_URL, "Referer": page.url},
            )
            if not isinstance(data, dict):
                result.warn(
                    "GraphQL review request failed (the client-embedded API key is "
                    "rotated by Goodreads); keeping the reviews already recovered"
                )
                break
            errors = data.get("errors")
            if errors:
                messages = "; ".join(
                    str(e.get("message")) for e in errors if isinstance(e, dict)
                )[:200]
                result.warn(f"GraphQL review query returned errors: {messages}")
                break
            block = data.get("data") or {}
            block = block.get("getReviews") if isinstance(block, dict) else None
            if not isinstance(block, dict):
                result.warn("GraphQL review response had no getReviews payload")
                break
            edges = block.get("edges")
            if not isinstance(edges, list) or not edges:
                if verbose():
                    print('  GraphQL review page %d was empty; stopping' % (attempt,), file=sys.stderr)
                break
            for edge in edges:
                node = edge.get("node") if isinstance(edge, dict) else None
                if not isinstance(node, dict):
                    continue
                creator = node.get("creator")
                item = self._build_review(
                    text=node.get("text"),
                    reviewer=creator.get("name") if isinstance(creator, dict) else None,
                    rating=node.get("rating"),
                    created=node.get("createdAt"),
                    url=None,
                    identity=self.clean_text(node.get("id")),
                    seen=seen,
                )
                if item is not None:
                    out.append(item)
            info = block.get("pageInfo")
            token = (
                self.clean_text(info.get("nextPageToken")) or None
                if isinstance(info, dict) else None
            )
            print('GraphQL review page %d: %d edge(s), %d kept so far' % (attempt, len(edges), len(out)), file=sys.stderr)
            if not token:
                break
        return out

    def _review_resource(self, page: _BookPage) -> Tuple[str, str]:
        """``('WORK', id)`` where possible -- it exposes all editions' reviews."""
        work_id = self.clean_text(page.work.get("id"))
        if work_id:
            return "WORK", work_id
        return "BOOK", self.clean_text(page.book.get("id"))

    def _appsync_credentials(
        self, page: _BookPage, result: ScrapeResult
    ) -> Optional[Tuple[str, str]]:
        """Re-derive the AppSync endpoint + API key from the page's own JS chunk.

        The key is a client-embedded secret that Goodreads rotates, so it is
        never hardcoded here: we read the ``_app-*.js`` URL out of the page we
        already have, fetch it, and lift the ``Production`` entry of its
        environment table.
        """
        if self._appsync_tried:
            return self._appsync
        self._appsync_tried = True

        chunk: Optional[str] = None
        try:
            scripts = page.soup.find_all("script", src=True)
        except (AttributeError, TypeError) as exc:
            if verbose():
                print('  script scan failed: %s' % (exc,), file=sys.stderr)
            scripts = []
        for script in scripts:
            match = _APP_CHUNK_RE.search(script.get("src") or "")
            if match:
                chunk = self.absolutise(page.url, match.group(0))
                break
        if not chunk:
            result.warn(
                "could not locate the Next.js _app chunk, so the GraphQL endpoint "
                "and API key could not be derived at runtime"
            )
            return None

        response = self.client.get(chunk, referer=page.url)
        if response is None:
            result.warn("could not fetch the Next.js _app chunk for GraphQL credentials")
            return None
        try:
            body = response.text or ""
        except (UnicodeDecodeError, ValueError) as exc:
            result.warn(f"the _app chunk could not be decoded ({exc})")
            return None

        start = 0
        for _ in range(20):
            index = body.find("Production", start)
            if index < 0:
                break
            start = index + 1
            window = body[index: index + 2000]
            key_match = _APPSYNC_KEY_RE.search(window)
            endpoint_match = _APPSYNC_ENDPOINT_RE.search(window)
            if key_match and endpoint_match:
                self._appsync = (endpoint_match.group(1), key_match.group(1))
                print('Derived the GraphQL endpoint %s from the live page bundle' % (endpoint_match.group(1),), file=sys.stderr)
                return self._appsync

        result.warn(
            "the _app chunk no longer contains a recognisable Production GraphQL "
            "entry, so deeper review pagination is unavailable"
        )
        return None

    def _build_review(
        self,
        *,
        text: Any,
        reviewer: Any,
        rating: Any,
        created: Any,
        url: Any,
        identity: str,
        seen: set,
        date: Optional[str] = None,
    ) -> Optional[ReviewItem]:
        """Clean, de-duplicate and wrap one review. ``None`` when unusable.

        Two identity keys are registered per review -- its own id/permalink and a
        normalised prefix of its text -- so the same review reached through two
        different strategies (embedded JSON, DOM card, GraphQL) is only kept once
        even though those routes expose different identifiers.
        """
        body = self.html_to_text(text)
        if not body.strip():
            return None
        keys = {re.sub(r"\W+", " ", body[:200]).strip().lower()}
        if identity:
            keys.add(identity)
        if keys & seen:
            return None
        rating_text = None
        if isinstance(rating, (int, float)) and not isinstance(rating, bool) and rating:
            rating_text = f"{int(rating)}/5"
        elif rating:
            rating_text = self.clean_text(rating) or None
        item = self.make_review(
            body,
            reviewer=self.clean_text(reviewer).strip() or None,
            rating=rating_text,
            date=date or self._epoch_to_utc_date(created),
            url=self.clean_text(url) or None,
            min_chars=self.MIN_REVIEW_CHARS,
        )
        if item is None:
            return None
        seen.update(keys)
        return item

    def _epoch_to_utc_date(self, raw: Any) -> Optional[str]:
        if raw is None or isinstance(raw, bool):
            return None
        try:
            millis = int(raw)
        except (TypeError, ValueError):
            return None
        try:
            return datetime.fromtimestamp(millis / 1000.0, timezone.utc).date().isoformat()
        except (OverflowError, OSError, ValueError):
            return None
