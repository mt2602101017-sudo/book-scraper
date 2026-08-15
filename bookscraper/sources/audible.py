"""Audible (www.audible.com) adapter.

Audible is *ISBN-hostile*: it indexes Amazon ASINs with no ISBN lookup at all, so
discovery goes through a title+author search. Everything else is easy -- despite
looking like a client-rendered Web Components app, the product page is fully
server-rendered and carries a complete ``schema.org/Audiobook`` JSON-LD record, so
no browser is needed.

Extraction order (each fallback that fires appends a warning):

1. **JSON-LD** -- the ``Audiobook`` object, plus the sibling ``BreadcrumbList``
   for the category hierarchy.
2. **Embedded component JSON** -- the blobs hydrating ``adbl-product-metadata``.
3. **CSS on the server-rendered custom elements** -- ``h1[slot="title"]``,
   ``adbl-text-block[slot="summary"]``, ``adbl-review-tile`` and friends.
4. **``og:``/``twitter:`` meta tags** for title and cover.

Three site-specific hazards handled explicitly:

* **Silent geo-redirect.** From a non-US IP every request is 302'd to
  ``www.audible.in`` with the path discarded into an ``ipRedirectOriginalURL``
  parameter, so a naive scraper gets HTTP 200 and the *wrong site's* homepage.
  ``ipRedirectOverride`` and ``overrideBaseCountry`` are appended to every URL and
  the final URL is checked before anything is parsed.
* **Transient HTTP 503 throttle** (the "Whoops..." page) -- a leaky-bucket rate
  limit, not a bot block. Critical fetches retry patiently; review pagination just
  stops early and reports the real count.
* **Recommendation carousels.** A page carries ~37 other product images belonging
  to "Listeners also enjoyed", so every selector is qualified by its ``slot``.

Semantic caveat, warned on every run: Audible describes an *audiobook edition*,
not the print book behind the ISBN, so ``publisher`` is the audio imprint,
``date_of_publication`` the audio release date and ``language`` the narration
language. ``origin`` is searched by the shared ``probe_origin`` over all three
layers; Audible publishes none, so it stays ``null``. The country-shaped values it
does carry (``regionsAllowed`` -- a licensing allowlist -- and the storefront
currency) are located per run and named as *rejected*, never repurposed.
"""

from __future__ import annotations

import sys
from ..verbosity import verbose
import json
import re
import time
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urlencode, urlsplit

from bs4 import BeautifulSoup, Tag

from .. import isbn as isbn_utils
from ..base import ORIGIN_KEY_SPELLINGS, BaseSource
from ..http_client import HttpClient
from ..models import BookHint, BookMetadata, ReviewItem, ScrapeResult

__all__ = ["AudibleSource"]

#: Country-*shaped* JSON keys :meth:`AudibleSource._rejected_country_fields`
#: looks for so the origin warning can name what this run actually found (and
#: why each is refused) instead of reciting a remembered list. Compared
#: lower-cased.
_COUNTRY_SHAPED_KEYS: Dict[str, str] = {
    "regionsallowed": "a distribution licensing allowlist of ISO country codes, "
                      "i.e. where the audiobook may be sold",
    "pricecurrency": "the currency of the storefront this run forced with "
                     "overrideBaseCountry",
}

_HOST = "www.audible.com"
_BASE = f"https://{_HOST}"

#: Appended to *every* audible.com URL. Both are required as a pair to defeat
#: the IP-based geo redirect; cookies are neither necessary nor sufficient.
_GEO_PARAMS: Dict[str, str] = {
    "ipRedirectOverride": "true",
    "overrideBaseCountry": "true",
}

#: Marker in the body of Audible's transient 503 throttle page.
_THROTTLE_MARKER = "crackedegg.jpg"

#: Waits (seconds) before re-trying a *critical* fetch that came back empty for
#: a non-block reason. Audible's throttle penalty is measured in minutes, so the
#: client's own sub-10s backoff is not enough on its own.
_PATIENT_WAITS: Tuple[float, ...] = (30.0, 75.0)

#: Server-side hard cap on the review XHR page size (asking for 50 returns 5).
_REVIEW_PAGE_SIZE = 5

#: Never request more review pages than this, whatever --min-reviews says.
_MAX_REVIEW_PAGES = 12

#: Amazon dynamic-resize token. ``_SL500_`` is already the native maximum for
#: Audible cover masters, so this only normalises search-result thumbnails up.
_SL_TOKEN_RE = re.compile(r"\._SL\d+_")

#: Social-share composites with Audible branding burned into the pixels.
_COVER_JUNK = ("_CLa", "PJAdblSocialShare", "AudibleLogo")

_IMAGE_ID_RE = re.compile(r"/images/[A-Z]/([A-Za-z0-9%+-]+)\.")
_ASIN_FROM_PD_RE = re.compile(r"/pd/(?:[^/]+/)?([A-Z0-9]{10})(?:[/?#]|$)", re.IGNORECASE)
_TAG_KIND_RE = re.compile(r"^/tag/([^/]+)/")
_NEXT_PAGE_ID_RE = re.compile(r"^nextReviewsPageNumber", re.IGNORECASE)
_PUNCT_RE = re.compile(r"[^0-9a-z]+")

#: Title similarity at or above which we stop calling the match "fuzzy".
_EXACT_TITLE_SCORE = 0.995

#: Signals that a differently-titled search hit is another *edition* of the same
#: work (a translation, a re-issue, a regional printing) rather than a sequel.
_EDITION_MARKER_RE = re.compile(
    r"[\(\[]|\b(?:edition|unabridged|abridged|version|translat\w*|reissue|"
    r"anniversary|deluxe)\b",
    re.IGNORECASE,
)

#: Chip kinds we treat as genres. ``mood`` ("Witty", "Inspiring") and
#: ``audible_editors`` ("Audible Essentials") are editorial, not genre, so they
#: are excluded and merely logged.
#:
#: ``"external"`` is deliberately **absent**: it was never a real Audible tag
#: kind, only the default this code used when ``_TAG_KIND_RE`` failed to match,
#: which made every absolute-href marketing "topic" link a genre. ``"goodreads"``
#: *is* included -- ``/tag/goodreads/Games-Audiobooks/...`` chips are genuine
#: subject tags that used to be discarded into the editorial-mood bucket.
_GENRE_CHIP_KINDS = frozenset({"genre", "theme", "category", "goodreads"})

#: Free, ISBN-native metadata provider used *only* to turn an ISBN into a
#: title+author search query when the pipeline has not seeded one (i.e. when
#: Audible is run without Goodreads). No field is ever copied from here into the
#: Audible metadata record.


def _normalise_title(raw: Any) -> str:
    """Lower-case, strip punctuation and collapse whitespace for comparison."""
    return _PUNCT_RE.sub(" ", str(raw or "").lower()).strip()


def _main_title(normalised: str) -> str:
    """Drop a trailing subtitle so "sapiens a brief history" ~ "sapiens"."""
    for separator in (" a brief ", " the story of "):
        if separator in normalised:
            return normalised.split(separator, 1)[0].strip()
    return normalised


def _is_prefix_of(short: str, long_: str) -> bool:
    return bool(short) and (long_ == short or long_.startswith(short + " "))


def _title_score(want: str, got: str) -> float:
    """Similarity in ``0.0..1.0`` that tolerates a missing/extra subtitle.

    The two prefix directions are deliberately scored differently:

    * the *candidate* being a prefix of what we want scores 1.0 -- that is just
      Audible dropping the subtitle ("Sapiens" for "Sapiens: A Brief History of
      Humankind"), which is what its search page actually does;
    * what we want being a prefix of the *candidate* only scores 0.90, because
      that is also the shape of a sequel ("Dune" -> "Dune Messiah"). It stays
      acceptable, but it is reported as a fuzzy match rather than an exact one.
    """
    left, right = _normalise_title(want), _normalise_title(got)
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    left_main, right_main = _main_title(left), _main_title(right)
    scores = [
        SequenceMatcher(None, left, right).ratio(),
        SequenceMatcher(None, left_main, right_main).ratio(),
    ]
    if _is_prefix_of(right_main, left) or _is_prefix_of(right, left):
        scores.append(1.0)
    if _is_prefix_of(left_main, right) or _is_prefix_of(left, right):
        scores.append(0.90)
    return max(scores)


def _surnames(authors: Iterable[str]) -> List[str]:
    """Comparable surname tokens for a list of author names."""
    out: List[str] = []
    for author in authors or []:
        tokens = [t for t in _normalise_title(author).split() if len(t) > 2]
        if tokens:
            out.append(tokens[-1])
    return out


@dataclass
class _Candidate:
    """One ``li.productListItem`` from an Audible search results page."""

    asin: str
    url: str
    title: str
    authors: List[str] = field(default_factory=list)
    cover_url: Optional[str] = None
    language: Optional[str] = None
    release_date: Optional[str] = None
    title_score: float = 0.0
    author_match: bool = False
    isbn_match: bool = False
    position: int = 0

    @property
    def exact(self) -> bool:
        """True when acceptance needed no fuzzy allowance."""
        return self.isbn_match or (
            self.title_score >= _EXACT_TITLE_SCORE and self.author_match
        )


class AudibleSource(BaseSource):
    """Scrape metadata, covers, blurb, reviews and genres from Audible."""

    name = "audible"
    display_name = "Audible"
    #: The PDP only *looks* client-rendered; requests+bs4 is sufficient.
    prefers_browser = False

    #: Minimum title similarity to accept a search hit at all (author must agree).
    FUZZY_FLOOR = 0.85
    #: At/above this we call the title match exact rather than fuzzy.
    FUZZY_CEILING = _EXACT_TITLE_SCORE
    #: Upper bound on how many edition covers we ask the pipeline to download.
    MAX_COVERS = 6

    def __init__(self, client: HttpClient) -> None:
        super().__init__(client)
        self._resolved: Optional[_Candidate] = None
        self._siblings: List[_Candidate] = []

    # -- URL plumbing --------------------------------------------------------

    def _url(self, path: str, params: Optional[Dict[str, Any]] = None) -> str:
        """Build an absolute audible.com URL with the geo-override pair appended."""
        query: Dict[str, Any] = dict(params or {})
        query.update(_GEO_PARAMS)
        if not path.startswith("/"):
            path = "/" + path
        return f"{_BASE}{path}?{urlencode(query)}"

    @staticmethod
    def _strip_query(url: str) -> str:
        return (url or "").split("?", 1)[0].split("#", 1)[0]

    def _redirect_problem(self, final_url: str) -> Optional[str]:
        """Describe the geo redirect if ``final_url`` left ``www.audible.com``."""
        if not final_url:
            return None
        if "ipRedirectFrom=" in final_url:
            return f"geo-redirected away from {_HOST} (landed on {final_url[:120]})"
        host = HttpClient.host_of(final_url)
        if host and host != _HOST:
            return f"redirected off {_HOST} to {host}"
        return None

    # -- fetching ------------------------------------------------------------

    def _fetch_soup(
        self,
        url: str,
        result: ScrapeResult,
        what: str,
        *,
        patient: int = 0,
        allow_browser: bool = False,
    ) -> Optional[BeautifulSoup]:
        """Fetch ``url`` and parse it, or warn and return ``None``.

        Handles all three Audible failure modes: the geo redirect (checked on the
        *final* response URL), the transient 503 throttle (retried patiently up
        to ``patient`` times) and a genuine bot block (never retried, optionally
        re-tried once through Selenium when ``allow_browser``).
        """
        waits = _PATIENT_WAITS[: max(0, int(patient))]
        for attempt in range(len(waits) + 1):
            response = self.client.get(url, referer=_BASE + "/")
            if response is not None:
                problem = self._redirect_problem(str(response.url or ""))
                if problem is not None:
                    result.warn(
                        f"{what}: {problem}; the ipRedirectOverride/overrideBaseCountry "
                        "pair no longer defeats Audible's IP geo-redirect"
                    )
                    print('warning: %s: %s' % (what, problem), file=sys.stderr)
                    return None
                soup = self.client.soup_from_response(response)
                if soup is not None:
                    body_head = (response.text or "")[:4096]
                    if _THROTTLE_MARKER in body_head:
                        print('warning: %s: Audible served its transient throttle page' % (what,), file=sys.stderr)
                    else:
                        return soup
                else:
                    result.warn(f"{what}: response body could not be parsed as HTML")
                    return None

            blocked = self.client.block_reason(url)
            if blocked:
                result.warn(f"{what}: blocked by bot protection ({blocked})")
                print('warning: %s: blocked by bot protection (%s)' % (what, blocked), file=sys.stderr)
                if allow_browser:
                    return self._fetch_rendered(url, result, what)
                return None

            if attempt < len(waits):
                wait = waits[attempt]
                result.warn(
                    f"{what}: empty response (Audible's transient 503 throttle is the "
                    f"usual cause); waiting {wait:.0f}s and retrying"
                )
                print('warning: %s: no usable response; waiting %.0fs before retry %d/%d' % (what, wait, attempt + 1, len(waits)), file=sys.stderr)
                time.sleep(wait)
                continue

            result.warn(
                f"{what}: could not be fetched (Audible returned no usable response; "
                "its 503 rate limit has a multi-minute penalty)"
            )
            print('warning: %s: giving up on %s' % (what, url), file=sys.stderr)
            if allow_browser:
                return self._fetch_rendered(url, result, what)
            return None
        return None

    def _fetch_rendered(
        self, url: str, result: ScrapeResult, what: str
    ) -> Optional[BeautifulSoup]:
        """Last-ditch Selenium attempt. Absence of a browser is not an error."""
        if not self.client.browser_available:
            result.warn(
                f"{what}: no browser available for a rendered retry "
                "(Selenium is an optional dependency); degrading"
            )
            return None
        print('%s: retrying %s through a real browser' % (what, url), file=sys.stderr)
        soup = self.client.get_rendered_soup(url, wait_css='h1[slot="title"]', wait_seconds=12)
        if soup is None:
            result.warn(f"{what}: rendered (browser) retry also failed")
        else:
            result.warn(f"{what}: recovered via the Selenium fallback path")
        return soup

    # -- discovery -----------------------------------------------------------

    def find_book_url(self, hint: BookHint) -> Optional[str]:
        """Resolve ``hint`` to an Audible product page URL, or ``None``."""
        result = self.new_result(hint)
        candidate = self._resolve(hint, result)
        for warning in result.warnings:
            if verbose():
                print('  find_book_url: %s' % (warning,), file=sys.stderr)
        return candidate.url if candidate else None

    def _resolve(self, hint: BookHint, result: ScrapeResult) -> Optional[_Candidate]:
        """Find the best-matching Audible edition for ``hint``.

        Audible has no ISBN index at all (verified: every ISBN-10/13 form routes
        to ``/no-search-results``), so this is a title+author search guarded by a
        similarity check so we never accept a companion "Summary & Analysis of
        ..." title or a same-title book by a different author.
        """
        title, authors = self.search_terms(hint, result)
        if not title:
            result.warn(
                "cannot search Audible: no title is known for this ISBN. Audible "
                "indexes ASINs, not ISBNs, so a title+author pair is required, and "
                "neither the shared hint nor Open Library could supply one"
            )
            return None

        query = " ".join([title] + list(authors[:2])).strip()
        search_url = self._url("/search", {"keywords": query})
        print('Searching Audible for %r' % (query,), file=sys.stderr)
        soup = self._fetch_soup(search_url, result, "search page", patient=1)
        if soup is None:
            return None

        candidates = self._parse_search_results(soup, search_url)
        if not candidates:
            result.warn(
                f"Audible search for {query!r} returned no product list items "
                "(zero results, or the search markup changed)"
            )
            return None
        if verbose():
            print('  Audible search returned %d candidate item(s)' % (len(candidates),), file=sys.stderr)

        scored = self._score_candidates(candidates, hint, title, authors)
        accepted = [c for c in scored if self._acceptable(c)]
        if not accepted:
            best = scored[0] if scored else None
            result.warn(
                "no Audible search result matched the target book"
                + (
                    f" (closest: {best.title!r} by {', '.join(best.authors) or 'unknown'}, "
                    f"title similarity {best.title_score:.2f})"
                    if best
                    else ""
                )
            )
            return None

        chosen = accepted[0]
        self._siblings = accepted
        if chosen.isbn_match:
            print('Accepted ASIN %s: it is an ISBN-10 that converts to %s' % (chosen.asin, hint.isbn13), file=sys.stderr)
        elif not chosen.exact:
            result.warn(
                f"accepted Audible result {chosen.title!r} by "
                f"{', '.join(chosen.authors) or 'unknown author'} (ASIN {chosen.asin}) "
                f"on a FUZZY match: title similarity {chosen.title_score:.2f} against "
                f"{title!r}, author match={chosen.author_match}"
            )
        if len(accepted) > 1:
            print('Audible lists %d edition(s) of this work: %s' % (len(accepted), ', '.join((c.asin for c in accepted))), file=sys.stderr)
        return chosen



    def _parse_search_results(
        self, soup: BeautifulSoup, base_url: str
    ) -> List[_Candidate]:
        """Parse ``li.productListItem`` rows into candidates.

        The class attribute is literally ``class="bc-list-item\\tproductListItem"``
        (with a TAB), so this must be a real CSS class selector.
        """
        candidates: List[_Candidate] = []
        try:
            items = soup.select("li.productListItem")
        except Exception as exc:  # pragma: no cover - bad selector is impossible here
            if verbose():
                print('  Search result selector failed: %s' % (exc,), file=sys.stderr)
            return []

        for position, item in enumerate(items, start=1):
            link = item.select_one('a.bc-link[href^="/pd/"]') or item.select_one(
                'a[href*="/pd/"]'
            )
            href = self._strip_query(str(link.get("href") or "")) if link else ""
            asin = self._asin_of(item, href)
            if not asin:
                continue
            title = self.clean_text(item.get("aria-label")) or self.select_text(
                item, "h3.bc-heading a", "h3 a", "h3"
            ) or ""
            candidates.append(
                _Candidate(
                    asin=asin,
                    url=self.absolutise(base_url, href) if href else self._pd_url(asin),
                    title=title,
                    authors=self._label_values(item, ".authorLabel", "By:"),
                    cover_url=self._candidate_cover(item, base_url),
                    language=(self._label_values(item, ".languageLabel", "Language:") or [None])[0],
                    release_date=(
                        self._label_values(item, ".releaseDateLabel", "Release date:")
                        or [None]
                    )[0],
                    position=position,
                )
            )
        return candidates

    def _asin_of(self, item: Tag, href: str) -> str:
        """ASIN from the item id, the impression div, or the /pd/ href."""
        raw_id = str(item.get("id") or "")
        if raw_id.startswith("product-list-item-"):
            asin = raw_id[len("product-list-item-"):].strip()
            if asin:
                return asin
        impression = item.select_one("div.adbl-asin-impression[data-asin]")
        if impression:
            asin = self.clean_text(impression.get("data-asin"))
            if asin:
                return asin
        match = _ASIN_FROM_PD_RE.search(href or "")
        return match.group(1) if match else ""

    def _label_values(self, item: Tag, selector: str, prefix: str) -> List[str]:
        """Text of a search-result label, minus its ``By:``/``Language:`` prefix."""
        node = item.select_one(selector)
        if node is None:
            return []
        text = self.clean_text(node)
        if text.lower().startswith(prefix.lower()):
            text = text[len(prefix):]
        text = text.replace("\n", " ")
        parts = [self.clean_text(p) for p in text.split(",")]
        return [p for p in parts if p]

    def _candidate_cover(self, item: Tag, base_url: str) -> Optional[str]:
        """Cover art URL for one search result, normalised to the native size."""
        node = item.select_one('img[alt$=" cover art"][src]') or item.select_one("img[src]")
        if node is None:
            return None
        return self._clean_cover_url(self.absolutise(base_url, node.get("src")))

    def _pd_url(self, asin: str) -> str:
        """Slug-less product URL; Audible 302s it to the canonical slugged form."""
        return self._url(f"/pd/{asin}")

    def _score_candidates(
        self,
        candidates: Sequence[_Candidate],
        hint: BookHint,
        title: str,
        authors: Sequence[str],
    ) -> List[_Candidate]:
        """Annotate candidates with title/author/ISBN match strength."""
        wanted_surnames = _surnames(authors)
        for candidate in candidates:
            candidate.title_score = _title_score(title, candidate.title)
            candidate_blob = _normalise_title(" ".join(candidate.authors))
            if not wanted_surnames:
                candidate.author_match = False
            else:
                candidate.author_match = any(
                    surname in candidate_blob for surname in wanted_surnames
                )
            candidate.isbn_match = self._asin_is_our_isbn(candidate.asin, hint)
        return sorted(
            candidates,
            key=lambda c: (
                0 if c.isbn_match else 1,
                -round(c.title_score, 3),
                0 if c.author_match else 1,
                c.position,
            ),
        )

    @staticmethod
    def _asin_is_our_isbn(asin: str, hint: BookHint) -> bool:
        """Some Audible ASINs *are* ISBN-10s -- a free definitive match check."""
        if not asin or len(asin) != 10:
            return False
        try:
            return isbn_utils.to_isbn13(asin) == hint.isbn13
        except ValueError:
            return False

    def _acceptable(self, candidate: _Candidate) -> bool:
        """Accept only a confident title match, and require the author to agree.

        A near-perfect title with no author agreement is exactly how the wrong
        book sneaks in (Audible carries an *Everything I Never Told You* by Ajay
        K Pandey alongside Celeste Ng's), so author agreement is mandatory
        whenever we know the author at all.
        """
        if candidate.isbn_match:
            return True
        return candidate.title_score >= self.FUZZY_FLOOR and candidate.author_match

    def _is_sibling_edition(self, candidate: _Candidate, chosen: _Candidate) -> bool:
        """True if ``candidate`` is another edition of ``chosen``'s work.

        Used only to gather extra cover art, so it is stricter than
        :meth:`_acceptable`: either the title matches exactly, or the difference
        is explicitly marked as an edition/translation. That keeps a sequel
        ("Dune Messiah" for "Dune") out of the cover list.
        """
        if candidate.asin == chosen.asin or not candidate.cover_url:
            return False
        if candidate.isbn_match or candidate.title_score >= self.FUZZY_CEILING:
            return True
        return bool(_EDITION_MARKER_RE.search(candidate.title or ""))

    # -- scrape --------------------------------------------------------------

    def _scrape_into(self, hint: BookHint, result: ScrapeResult) -> None:
        candidate = self._resolve(hint, result)
        if candidate is None:
            return
        self._resolved = candidate
        result.book_url = candidate.url

        soup = self._fetch_soup(
            candidate.url, result, "product page", patient=2, allow_browser=True
        )
        if soup is None:
            # Discovery still worked, so surface the URL and the search-page cover.
            if candidate.cover_url:
                result.cover_urls = self.dedupe([candidate.cover_url])
                result.warn(
                    "cover URL came from the search results page because the product "
                    "page could not be fetched"
                )
            return

        canonical = self._canonical_url(soup, candidate.url)
        if canonical:
            result.book_url = canonical

        audiobook = self._audiobook_ldjson(soup, result)
        details = self._component_json(
            soup, 'adbl-product-details adbl-product-metadata > script[type="application/json"]'
        )
        people = self._component_json(
            soup,
            'adbl-product-metadata[combine-authors-narrators] > script[type="application/json"]',
        )

        metadata = self._build_metadata(
            hint, result, soup, audiobook, details, people, canonical
        )
        genres = self._extract_genres(soup, result, audiobook, details)
        metadata.genres = list(genres)
        result.genres = list(genres)
        result.metadata = metadata

        result.blurb = self._extract_blurb(soup, result, audiobook)
        result.cover_urls = self._extract_covers(soup, result, audiobook, candidate)
        result.reviews = self._extract_reviews(soup, result, candidate)

        self._note_narrators(result, audiobook, people)
        if metadata.title or metadata.authors:
            result.hint_updates = BookHint(
                isbn13=hint.isbn13,
                isbn10=hint.isbn10,
                title=metadata.title,
                authors=list(metadata.authors),
            )

    # -- page-level parsing helpers -----------------------------------------

    def _canonical_url(self, soup: BeautifulSoup, fallback: str) -> Optional[str]:
        node = soup.select_one('link[rel="canonical"][href]')
        href = self.clean_text(node.get("href")) if node else ""
        if not href:
            href = self.meta(soup, "og:url") or ""
        if href:
            return self.absolutise(fallback, href)
        return self._strip_query(fallback) or None

    def _audiobook_ldjson(
        self, soup: BeautifulSoup, result: ScrapeResult
    ) -> Dict[str, Any]:
        """The ``@type=Audiobook`` JSON-LD object -- the primary data source."""
        blobs = self.jsonld(soup, ("Audiobook", "Book", "AudiobookFormat"))
        for blob in blobs:
            if blob.get("name") or blob.get("description"):
                return blob
        result.warn(
            "primary strategy failed: no schema.org Audiobook JSON-LD block on the "
            "product page; falling back to the embedded component JSON and CSS selectors"
        )
        return {}

    def _component_json(self, soup: BeautifulSoup, selector: str) -> Dict[str, Any]:
        """Decode one of the inline ``application/json`` component blobs."""
        try:
            node = soup.select_one(selector)
        except Exception as exc:  # pragma: no cover
            if verbose():
                print('  Selector %r failed: %s' % (selector, exc), file=sys.stderr)
            return {}
        if node is None:
            return {}
        raw = node.string or node.get_text()
        if not raw or not raw.strip():
            return {}
        try:
            payload = json.loads(raw)
        except ValueError as exc:
            if verbose():
                print('  Component JSON at %r is not valid JSON: %s' % (selector, exc), file=sys.stderr)
            return {}
        return payload if isinstance(payload, dict) else {}

    # -- metadata ------------------------------------------------------------

    def _build_metadata(
        self,
        hint: BookHint,
        result: ScrapeResult,
        soup: BeautifulSoup,
        audiobook: Dict[str, Any],
        details: Dict[str, Any],
        people: Dict[str, Any],
        canonical: Optional[str],
    ) -> BookMetadata:
        """Assemble the metadata record, warning on every fallback that fires."""
        metadata = self.new_metadata(hint)

        metadata.title = self._pick_title(soup, result, audiobook)
        metadata.authors = self._pick_authors(soup, result, audiobook, people)
        metadata.publisher = self._pick_publisher(result, audiobook, details)
        metadata.date_of_publication = self._pick_date(result, audiobook, details)
        metadata.language = self._pick_language(result, audiobook, details)
        metadata.origin = self._pick_origin(result, soup, audiobook, details)
        return metadata

    def _pick_title(
        self, soup: BeautifulSoup, result: ScrapeResult, audiobook: Dict[str, Any]
    ) -> Optional[str]:
        title = self.clean_text(audiobook.get("name"))
        if title:
            return title
        # Fallback 1: the server-rendered web component heading. Must be
        # slot-qualified or a carousel heading wins.
        title = self.select_text(soup, 'h1[slot="title"]', "h1.bc-heading")
        if title:
            result.warn("title came from h1[slot=\"title\"] because JSON-LD had no name")
            return title
        # Fallback 2: og:/twitter: meta.
        title = self.meta(soup, "og:title", "twitter:title", "title")
        if title:
            result.warn("title came from og:/twitter: meta tags (DOM heading missing)")
            return re.sub(r"\s+Audiobook\b.*$", "", title).strip() or title
        result.warn("title not found by any strategy")
        return None

    def _pick_authors(
        self,
        soup: BeautifulSoup,
        result: ScrapeResult,
        audiobook: Dict[str, Any],
        people: Dict[str, Any],
    ) -> List[str]:
        authors = [
            self.clean_text(a.get("name") if isinstance(a, dict) else a)
            for a in self._as_list(audiobook.get("author"))
        ]
        authors = self.dedupe([a for a in authors if a])
        if authors:
            return authors
        embedded = self.dedupe(
            [
                self.clean_text(a.get("name"))
                for a in people.get("authors") or []
                if isinstance(a, dict) and a.get("name")
            ]
        )
        if embedded:
            result.warn("authors came from the embedded component JSON (JSON-LD had none)")
            return embedded
        label = self.select_text(soup, "span.authorLabel", ".authorLabel")
        if label:
            cleaned = re.sub(r"^by[:\s]+", "", label, flags=re.IGNORECASE)
            parsed = self.dedupe([p for p in (self.clean_text(x) for x in cleaned.split(",")) if p])
            if parsed:
                result.warn("authors came from the .authorLabel DOM text (no JSON available)")
                return parsed
        result.warn("authors not found by any strategy")
        return []

    def _pick_publisher(
        self, result: ScrapeResult, audiobook: Dict[str, Any], details: Dict[str, Any]
    ) -> Optional[str]:
        raw = audiobook.get("publisher")
        publisher = self.clean_text(raw.get("name") if isinstance(raw, dict) else raw)
        if not publisher:
            nested = details.get("publisher")
            publisher = self.clean_text(
                nested.get("name") if isinstance(nested, dict) else nested
            )
            if publisher:
                result.warn(
                    "publisher came from the embedded component JSON (JSON-LD had none)"
                )
        if not publisher:
            result.warn("publisher not found (Audible exposes it only in JSON, never in the DOM)")
            return None
        result.warn(
            f"publisher {publisher!r} is the AUDIOBOOK imprint for this ASIN, not the "
            "print publisher behind the ISBN -- do not let it overwrite an "
            "ISBN-authoritative value"
        )
        return publisher

    def _pick_date(
        self, result: ScrapeResult, audiobook: Dict[str, Any], details: Dict[str, Any]
    ) -> Optional[str]:
        raw = self.clean_text(audiobook.get("datePublished"))
        date = self.iso_date(raw) if raw else None
        if not date:
            legacy = self.clean_text(details.get("releaseDate"))
            date = self._iso_from_us_short(legacy)
            if date:
                result.warn(
                    "date_of_publication came from the embedded component JSON's "
                    f"MM-DD-YY releaseDate ({legacy!r}); JSON-LD datePublished was absent"
                )
        if not date:
            result.warn("date_of_publication not found")
            return None
        result.warn(
            "date_of_publication is the AUDIOBOOK release date for this ASIN, not the "
            "print publication date of the ISBN"
        )
        return date

    def _iso_from_us_short(self, raw: str) -> Optional[str]:
        """Convert Audible's ``MM-DD-YY`` release date to ISO-8601."""
        match = re.fullmatch(r"(\d{2})-(\d{2})-(\d{2})", (raw or "").strip())
        if not match:
            return self.iso_date(raw) if raw else None
        month, day, year = match.groups()
        century = "20" if int(year) < 70 else "19"
        return f"{century}{year}-{month}-{day}"

    def _pick_language(
        self, result: ScrapeResult, audiobook: Dict[str, Any], details: Dict[str, Any]
    ) -> Optional[str]:
        raw = self.clean_text(audiobook.get("inLanguage"))
        source = "JSON-LD inLanguage"
        if not raw:
            raw = self.clean_text(details.get("language"))
            source = "the embedded component JSON"
            if raw:
                result.warn("language came from the embedded component JSON (JSON-LD had none)")
        if not raw:
            result.warn("language not found")
            return None
        # Audible writes a bare lower-case English word ("english"), not a
        # BCP-47 code, so normalise the casing ourselves.
        language = raw.strip()
        language = language.title() if language.islower() else language
        if verbose():
            print('  Language %r normalised to %r from %s' % (raw, language, source), file=sys.stderr)
        result.warn(
            f"language {language!r} is the NARRATION language of this audiobook edition, "
            "which is not necessarily the language the book was written in"
        )
        return language

    def _pick_origin(
        self,
        result: ScrapeResult,
        soup: BeautifulSoup,
        audiobook: Dict[str, Any],
        details: Dict[str, Any],
    ) -> Optional[str]:
        """Place of publication, searched for across every layer this run parsed.

        The search is the shared
        :meth:`~bookscraper.base.BaseSource.probe_origin` over the JSON-LD, the
        embedded component JSON and the page DOM, so the warning's "layers
        searched" clause is the probe's own report and the field self-heals if
        Audible ever starts publishing a place of publication.

        The country-shaped values Audible *does* carry are located per run by
        :meth:`_rejected_country_fields` and named as rejected, never repurposed:
        ``regionsAllowed`` is a distribution licensing allowlist of ISO codes,
        and ``priceCurrency`` / ``#reviewsCountry`` describe the storefront this
        adapter forced with ``overrideBaseCountry``.
        """
        layers: List[Tuple[str, Any]] = [
            ("the Audiobook/Product JSON-LD", audiobook),
            ("the embedded component JSON", details),
            ("the page DOM (adbl-metadata slots and og:/meta tags)", soup),
        ]
        probe = self.probe_origin_detail(layers)
        if probe.value:
            result.warn(
                f"origin {probe.value!r} was read from Audible's page ({probe.where}); "
                "Audible did not previously publish a place of publication, so treat "
                "this as new"
            )
            return probe.value

        return self.origin_unavailable(
            result,
            "no place-of-publication key or label was found in the "
            f"{len(ORIGIN_KEY_SPELLINGS)} spellings the shared origin probe searches "
            "for, across every layer this run could parse ("
            f"{self.origin_layers_clause(probe.searched)}). "
            + self._rejected_country_fields(layers, soup),
        )

    def _rejected_country_fields(
        self, layers: Sequence[Tuple[str, Any]], soup: BeautifulSoup
    ) -> str:
        """Name the country-shaped fields *this run* found, and why each is refused.

        Everything here is located on the page as the sentence is built, so the
        warning can never go on describing a field Audible has stopped serving
        (or miss one it has added).
        """
        rejected: List[str] = []
        for key, why in _COUNTRY_SHAPED_KEYS.items():
            for label, blob in layers:
                if isinstance(blob, Tag) or blob is None:
                    continue
                hit = next(
                    (p for p in self.iter_json_pairs(blob)
                     if p.key.casefold() == key),
                    None,
                )
                if hit is None:
                    continue
                size = len(hit.value) if isinstance(hit.value, (list, tuple)) else 0
                rejected.append(
                    f"{hit.path}"
                    + (f" ({size} entries)" if size else "")
                    + f" is {why}"
                )
                break
        try:
            node = soup.select_one("#reviewsCountry, input[name='reviewsCountry']")
        except Exception as exc:  # pragma: no cover - defensive
            if verbose():
                print('  #reviewsCountry lookup failed: %s' % (exc,), file=sys.stderr)
            node = None
        if node is not None:
            rejected.append(
                "the #reviewsCountry form field names the storefront this run forced "
                "with overrideBaseCountry, not a place of publication"
            )
        if not rejected:
            return (
                "This run found no country-shaped value on the page at all, so there "
                "was nothing to reject"
            )
        return (
            "The country-shaped values this run did find are rejected deliberately: "
            + "; ".join(rejected)
        )

    # -- genres --------------------------------------------------------------

    def _extract_genres(
        self,
        soup: BeautifulSoup,
        result: ScrapeResult,
        audiobook: Dict[str, Any],
        details: Dict[str, Any],
    ) -> List[str]:
        """Union the formal breadcrumb taxonomy with the tag chips."""
        genres: List[str] = []
        moods: List[str] = []

        # Primary: the rich tag chips, split by their /tag/<kind>/ segment.
        try:
            chips = soup.select('adbl-chip-group[slot="chips"] adbl-chip')
        except Exception as exc:  # pragma: no cover
            if verbose():
                print('  Chip selector failed: %s' % (exc,), file=sys.stderr)
            chips = []
        unclassified: List[str] = []
        for chip in chips:
            label = self.clean_text(chip)
            if not label:
                continue
            href = str(chip.get("href") or "")
            # Match against the href's *path*, so an absolute
            # "https://www.audible.com/tag/genre/..." classifies the same as the
            # relative "/tag/genre/...". Matching the raw attribute meant every
            # absolute chip fell through to kind="external" -- which was in the
            # accepted set, so marketing "topic" ad links ("Body Language" on
            # Ready Player One, "Design Theory" on Thinking, Fast and Slow) were
            # written out as genres.
            try:
                path = urlsplit(href).path or href
            except ValueError:
                path = href
            match = _TAG_KIND_RE.match(path)
            if match is None:
                unclassified.append(label)
                continue
            kind = match.group(1).lower()
            if kind in _GENRE_CHIP_KINDS:
                genres.append(label)
            else:
                moods.append(label)
        if unclassified:
            # Never silently promoted to genres any more, and never silently
            # dropped either: the user is told exactly what was discarded.
            result.warn(
                "ignored %d chip(s) whose /tag/<kind>/ classification could not be "
                "determined from their href (most are marketing 'topic' links, not "
                "genres): %s"
                % (len(unclassified), ", ".join(self.dedupe(unclassified)))
            )
        if not chips:
            result.warn(
                'genre chips (adbl-chip-group[slot="chips"]) not found; falling back to '
                "the JSON-LD breadcrumb taxonomy only"
            )

        # Secondary: the BreadcrumbList hierarchy, minus the leading "Home".
        crumbs: List[str] = []
        for blob in self.jsonld(soup, "BreadcrumbList"):
            for element in blob.get("itemListElement") or []:
                if not isinstance(element, dict):
                    continue
                item = element.get("item")
                label = self.clean_text(
                    item.get("name") if isinstance(item, dict) else element.get("name")
                )
                if label and label.lower() not in ("home", ""):
                    crumbs.append(label)
        if crumbs:
            genres = crumbs + genres
        else:
            result.warn("JSON-LD BreadcrumbList category hierarchy not found")

        # Tertiary: the top-level category from the component JSON.
        for category in details.get("categories") or []:
            if isinstance(category, dict):
                label = self.clean_text(category.get("name"))
                if label:
                    genres.append(label)

        # Quaternary: JSON-LD genre, if Audible ever adds one.
        genres.extend(self.split_list(audiobook.get("genre")))

        final = self.dedupe([g for g in genres if g])
        if moods:
            print('Excluded %d editorial mood/editors tag(s) from genres: %s' % (len(moods), ', '.join(self.dedupe(moods))), file=sys.stderr)
        if not final:
            result.warn("no genres/categories found by any strategy")
        return final

    # -- blurb ---------------------------------------------------------------

    def _extract_blurb(
        self, soup: BeautifulSoup, result: ScrapeResult, audiobook: Dict[str, Any]
    ) -> Optional[str]:
        """Publisher summary. Server-rendered in full -- no "read more" to expand."""
        raw = audiobook.get("description")
        blurb = self.html_to_text(raw) if raw else ""
        if blurb:
            return blurb
        node = soup.select_one('adbl-product-details adbl-text-block[slot="summary"]')
        if node is None:
            node = soup.select_one('adbl-text-block[slot="summary"]')
        if node is not None:
            try:
                inner = node.decode_contents()
            except Exception as exc:  # pragma: no cover - bs4 serialisation
                if verbose():
                    print('  decode_contents on the summary block failed: %s' % (exc,), file=sys.stderr)
                inner = str(node)
            blurb = self.html_to_text(inner)
            if blurb:
                result.warn(
                    'blurb came from adbl-text-block[slot="summary"] because JSON-LD '
                    "carried no description"
                )
                return blurb
        description = self.meta(soup, "og:description", "description", "twitter:description")
        if description:
            result.warn("blurb came from the og:description meta tag (a truncated form)")
            return description
        result.warn("no blurb/publisher summary found by any strategy")
        return None

    # -- covers --------------------------------------------------------------

    def _clean_cover_url(self, url: str) -> str:
        """Normalise an Amazon image URL to the native 500px master, or drop it."""
        if not url:
            return ""
        if any(junk in url for junk in _COVER_JUNK):
            return ""
        return _SL_TOKEN_RE.sub("._SL500_", url)

    @staticmethod
    def _image_id(url: str) -> str:
        """The Amazon image id, so two size variants dedupe to one cover."""
        match = _IMAGE_ID_RE.search(url or "")
        return match.group(1).lower() if match else (url or "").lower()

    def _extract_covers(
        self,
        soup: BeautifulSoup,
        result: ScrapeResult,
        audiobook: Dict[str, Any],
        candidate: _Candidate,
    ) -> List[str]:
        """This edition's cover first, then one cover per sibling edition."""
        found: List[str] = []

        primary = self._clean_cover_url(self.clean_text(audiobook.get("image")))
        if not primary:
            node = soup.select_one('adbl-product-image[slot="image"] img[src]')
            if node is not None:
                primary = self._clean_cover_url(
                    self.absolutise(result.book_url or _BASE, node.get("src"))
                )
                if primary:
                    result.warn(
                        'cover came from adbl-product-image[slot="image"] because '
                        "JSON-LD had no image"
                    )
        if not primary:
            og_image = self.meta(soup, "og:image", "twitter:image")
            primary = self._clean_cover_url(og_image or "")
            if primary:
                result.warn("cover came from the og:image meta tag")
            elif og_image:
                result.warn(
                    "the only cover on the page is an og:image social-share composite "
                    "with Audible branding burned in; skipped it"
                )
        if primary:
            found.append(primary)
        else:
            result.warn("no cover image URL found for this edition")

        # Audible product pages have no "other editions"/"other formats" section
        # at all, so sibling editions (and their distinct cover art) can only
        # come from the search results page we already fetched -- no extra
        # request is made for them.
        seen = {self._image_id(u) for u in found}
        extra = 0
        skipped = 0
        for sibling in self._siblings:
            if not self._is_sibling_edition(sibling, candidate):
                continue
            url = self._clean_cover_url(sibling.cover_url or "")
            if not url or self._image_id(url) in seen:
                continue
            if len(found) >= self.MAX_COVERS:
                skipped += 1
                continue
            seen.add(self._image_id(url))
            found.append(url)
            extra += 1
        if extra:
            print('Collected %d additional cover(s) from other Audible edition(s)' % (extra,), file=sys.stderr)
        if skipped:
            print('Audible lists more editions; stopped at MAX_COVERS=%d (skipped %d)' % (self.MAX_COVERS, skipped), file=sys.stderr)
        return self.dedupe(found)

    # -- reviews -------------------------------------------------------------

    def _extract_reviews(
        self, soup: BeautifulSoup, result: ScrapeResult, candidate: _Candidate
    ) -> List[ReviewItem]:
        """Five tiles from the product page, then the ``/pd/reviews`` XHR pages.

        ``pageSize`` is capped at 5 server-side (asking for 50 returns 5), so
        reaching 25+ reviews costs one request per five reviews. Pagination stops
        early -- with an honest warning -- if Audible starts throttling.
        """
        target = max(0, int(self.min_reviews or 0))
        if self.max_reviews is not None:
            target = min(target, int(self.max_reviews)) if target else int(self.max_reviews)
        target = max(target, _REVIEW_PAGE_SIZE)

        reviews: List[ReviewItem] = []
        seen: set = set()
        skipped_empty = 0
        duplicates = 0

        def absorb(container: Optional[BeautifulSoup]) -> int:
            nonlocal skipped_empty, duplicates
            added = 0
            for tile in self._review_tiles(container):
                item = self._review_from_tile(tile)
                if item is None:
                    skipped_empty += 1
                    continue
                key = _normalise_title(item.text)[:400]
                if key in seen:
                    duplicates += 1
                    continue
                seen.add(key)
                reviews.append(item)
                added += 1
            return added

        absorb(soup)
        if not reviews:
            result.warn(
                "no adbl-review-tile elements on the product page; Audible's review "
                "markup may have changed"
            )

        asin = self._hidden_value(soup, "reviewsAsinUS") or candidate.asin
        country = self._hidden_value(soup, "reviewsCountry") or "US"
        next_page = self._next_page_number(soup) or 1

        pages_needed = -(-max(0, target - len(reviews)) // _REVIEW_PAGE_SIZE)
        budget = min(pages_needed, _MAX_REVIEW_PAGES)
        for _ in range(budget):
            if len(reviews) >= target:
                break
            page_url = self._url(
                "/pd/reviews",
                {
                    "country": country,
                    "asin": asin,
                    "sort": "MostRelevant",
                    "filter": "allStars",
                    "page": next_page,
                    "pageSize": _REVIEW_PAGE_SIZE,
                    "showPaging": "true",
                },
            )
            fragment = self._fetch_soup(page_url, result, f"review page {next_page}")
            if fragment is None:
                result.warn(
                    f"review pagination stopped at page {next_page} after "
                    f"{len(reviews)} review(s); Audible stopped responding "
                    "(its 503 rate limit trips on review pagination)"
                )
                break
            added = absorb(fragment)
            if added == 0:
                print('Review page %d added no new reviews; stopping pagination' % (next_page,), file=sys.stderr)
                break
            next_page = self._next_page_number(fragment) or (next_page + 1)

        if self.max_reviews is not None and len(reviews) > int(self.max_reviews):
            reviews = reviews[: int(self.max_reviews)]
        if skipped_empty:
            print('Skipped %d review tile(s) with an empty body' % (skipped_empty,), file=sys.stderr)
        if duplicates:
            print('Dropped %d duplicate review(s) during pagination' % (duplicates,), file=sys.stderr)
        print('Collected %d unique review(s) from Audible' % (len(reviews),), file=sys.stderr)
        return reviews

    def _review_tiles(self, container: Optional[BeautifulSoup]) -> List[Tag]:
        if container is None:
            return []
        for selector in ("adbl-review-tile", "div#customer-reviews adbl-review-tile"):
            try:
                tiles = container.select(selector)
            except Exception as exc:  # pragma: no cover
                if verbose():
                    print('  Review selector %r failed: %s' % (selector, exc), file=sys.stderr)
                continue
            if tiles:
                return tiles
        return []

    def _review_from_tile(self, tile: Tag) -> Optional[ReviewItem]:
        """Build a :class:`ReviewItem` from one ``adbl-review-tile``.

        No ``url`` is set: Audible review tiles carry no permalink at all.
        """
        body_node = tile.select_one('adbl-text-block[slot="review-summary"]')
        if body_node is None:
            body_node = tile.select_one("adbl-text-block")
        if body_node is None:
            return None
        try:
            body_html = body_node.decode_contents()
        except Exception as exc:  # pragma: no cover
            if verbose():
                print('  decode_contents on a review body failed: %s' % (exc,), file=sys.stderr)
            body_html = str(body_node)

        headline = self.select_text(tile, 'h3[slot="review-title"]', "h3")
        text = self.html_to_text(body_html)
        if headline and text and not text.startswith(headline):
            text = f"{headline}\n\n{text}"
        elif headline and not text:
            text = headline

        stars = tile.select_one('adbl-star-rating[slot="stars"]') or tile.select_one(
            "adbl-star-rating"
        )
        overall = self.clean_text(stars.get("value")) if stars else ""
        parts = [f"{overall}/5 overall"] if overall else []
        for attr, label in (("story-rating", "story"), ("performance-rating", "performance")):
            value = self.clean_text(tile.get(attr))
            if value:
                parts.append(f"{label} {value}/5")
        rating = ", ".join(parts) or None

        return self.make_review(
            text,
            reviewer=tile.get("reviewer"),
            rating=rating,
            date=tile.get("review-date"),
            # Deliberately None. An <adbl-review-tile> carries no permalink, and
            # stamping the product-page URL here put one identical "URL:" header on
            # all 25 reviews -- where Goodreads and Amazon each hold a genuine
            # per-review permalink. A consumer joining on URL would have collapsed
            # Audible's reviews to one, or mistaken the product page for a review.
            url=None,
            min_chars=2,
        )

    def _hidden_value(self, soup: BeautifulSoup, element_id: str) -> Optional[str]:
        node = soup.find("input", id=element_id)
        if node is None:
            return None
        return self.clean_text(node.get("value")) or None

    def _next_page_number(self, soup: Optional[BeautifulSoup]) -> Optional[int]:
        """Read Audible's own ``nextReviewsPageNumber<COUNTRY>`` pointer."""
        if soup is None:
            return None
        node = soup.find("input", id=_NEXT_PAGE_ID_RE)
        if node is None:
            return None
        raw = self.clean_text(node.get("value"))
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return None
        return value if value > 0 else None

    # -- misc ----------------------------------------------------------------

    def _note_narrators(
        self, result: ScrapeResult, audiobook: Dict[str, Any], people: Dict[str, Any]
    ) -> None:
        """Log narrators, and never merge them into ``authors``.

        Audible's ``readBy`` is the voice talent, not the writer. It is recorded
        as a warning so the provenance is visible in the metadata JSON without
        polluting the author list.
        """
        narrators = [
            self.clean_text(n.get("name") if isinstance(n, dict) else n)
            for n in self._as_list(audiobook.get("readBy"))
        ]
        if not any(narrators):
            narrators = [
                self.clean_text(n.get("name"))
                for n in people.get("narrators") or []
                if isinstance(n, dict)
            ]
        narrators = self.dedupe([n for n in narrators if n])
        if narrators:
            result.warn(
                "narrator(s) " + ", ".join(narrators) + " were deliberately NOT merged "
                "into authors (Audible lists voice talent, not writers)"
            )

    @staticmethod
    def _as_list(value: Any) -> List[Any]:
        if value is None:
            return []
        if isinstance(value, (list, tuple)):
            return list(value)
        return [value]
