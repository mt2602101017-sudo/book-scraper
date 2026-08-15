"""Rakuten Kobo (``www.kobo.com``) site adapter.

Kobo is the awkward one. Three facts drive every decision here:

1. **``www.kobo.com`` is behind a Cloudflare TLS-fingerprint (JA3/JA4) gate.**
   Plain ``requests`` gets ``HTTP 403`` + ``cf-mitigated: challenge`` +
   ``<title>Challenged | Kobo.com</title>`` whatever headers we send, so header
   tuning cannot help. Measured 0 of 3 pages reachable, so product and search pages
   go straight to Selenium rather than spending a guaranteed 403 first -- a real
   browser clears the managed challenge on its own. No CAPTCHA is touched, nothing
   authenticates, no unblocking proxy is used. Without a browser the adapter reports
   the book as unreachable, never as absent from the catalogue.

2. **The sibling hosts are not protected.** ``ratingsapi.kobo.com`` (all reviews in
   one call) and ``cdn.kobo.com`` (covers) answer plain ``requests``, so the
   expensive browser is only ever used for product and search pages.

3. **The metadata is not in a ``ld+json`` tag.** It lives in an
   HTML-entity-escaped, *doubly* JSON-encoded string in the
   ``data-kobo-gizmo-config`` attribute of the rating widget. That is the primary
   path; the legacy CSS DOM, the JS-injected ``ld+json`` blocks and the ``og:``
   tags are the fallbacks, in that order, each warning when it fires.

Locale is pinned to ``/us/en/``: the bare path 301s to the caller's geo storefront
(changing catalogue, currency and review locale), while the explicit prefix is
stable and is the one ``robots.txt`` allows.

``origin`` comes back ``null``: every parsed layer goes through the shared
``probe_origin`` and Kobo publishes nothing of the sort. The country data it does
carry is sales-territory licensing (``eligibleRegion`` / ``ineligibleRegion``,
located and counted per run), which hangs off a priced ``Offer`` inside the
checkout ``ReadAction`` -- where the ebook may be *sold*, not where it was
published. It is named and rejected rather than fabricated into ``origin``.
"""

from __future__ import annotations

import sys
from ..verbosity import verbose
import html as html_module
import re
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import quote_plus, urlsplit, urlunsplit

from bs4 import BeautifulSoup, Tag

from ..base import ORIGIN_KEY_SPELLINGS, BaseSource
from ..http_client import HttpClient
from ..models import BookHint, BookMetadata, ReviewItem, ScrapeResult

__all__ = ["KoboSource"]

#: Sales-territory keys :meth:`KoboSource._region_licensing` hunts for so the
#: origin warning can name and reject them with a *counted* rather than a
#: remembered list. Folded to lower case at compare time.
_REGION_KEYS: Tuple[str, ...] = ("eligibleregion", "ineligibleregion")

#: Pinned storefront. ``/us/en`` is explicitly ``Allow``-ed by robots.txt and
#: does not geo-redirect; the bare ``/ebook/...`` path does.
STORE_ROOT = "https://www.kobo.com/us/en"

#: Reviews service. A separate origin that is *not* behind Cloudflare.
REVIEWS_API = "https://ratingsapi.kobo.com/V1/Ui/GetMoreReviews"

#: Cover CDN. ``{imageId}/{w}/{h}/{quality}/{grayscale}/{slug}.jpg``; the slug is
#: ignored by the CDN and ``w``/``h`` are a bounding box with aspect preserved.
COVER_CDN = "https://cdn.kobo.com/book-images/{image_id}/{w}/{h}/{q}/False/cover.jpg"

#: Cover box we ask for. Measured: 1200 -> 1199x1808 (~1.4 MB). The CDN happily
#: upscales past the source resolution, so anything much larger is interpolated
#: bytes rather than detail.
COVER_BOX: Tuple[int, int, int] = (1200, 1200, 100)

#: Exact Cloudflare challenge signature from live recon (lower-cased).
CHALLENGE_MARKERS: Tuple[str, ...] = (
    "challenged | kobo.com",
    "enable javascript and cookies to continue",
)

#: ``sortBy=2`` is "newest first" -- the only orderings that page deterministically
#: are 2 (newest) and 3 (oldest); the default 0 shuffles between pages.
REVIEW_SORT_NEWEST = 2

#: Hard ceilings so a pathological page can never make us hammer the API.
REVIEW_LIMIT_CEILING = 1000
REVIEW_PAGE_CEILING = 6

#: Titles that are *about* a book rather than the book. Kobo's search is full of
#: them and they fuzzy-match the real title almost perfectly.
COMPANION_MARKERS: Tuple[str, ...] = (
    "trivia-on-books", "trivia on books", "conversation starters",
    "digest & review", "digest and review", "summary of", "summary and analysis",
    "a summary", "study guide", "studyguide", "sparknotes", "joosr",
    "workbook", "quicklet", "book club", "a guide to", "key takeaways",
    "reading guide", "instaread", "shortcut", "review and analysis",
)

#: ISO 639-1 -> English name, so ``inLanguage: 'en'`` becomes "English" when the
#: human-readable DOM label is not available. A static code table, not scraped data.
_LANGUAGE_NAMES: Dict[str, str] = {
    "en": "English", "fr": "French", "de": "German", "es": "Spanish",
    "it": "Italian", "nl": "Dutch", "pt": "Portuguese", "ja": "Japanese",
    "zh": "Chinese", "ko": "Korean", "ru": "Russian", "sv": "Swedish",
    "no": "Norwegian", "nb": "Norwegian", "da": "Danish", "fi": "Finnish",
    "pl": "Polish", "cs": "Czech", "tr": "Turkish", "ar": "Arabic",
    "he": "Hebrew", "hi": "Hindi", "el": "Greek", "hu": "Hungarian",
    "ro": "Romanian", "uk": "Ukrainian", "ca": "Catalan", "eu": "Basque",
    "gl": "Galician", "id": "Indonesian", "th": "Thai", "vi": "Vietnamese",
}

#: Breadcrumb sentinels that are navigation chrome, not genres.
_BREADCRUMB_CHROME = {"kobo books", "home", "ebooks", "audiobooks", "books"}

_IMAGE_ID_RE = re.compile(r"book-images/([0-9a-fA-F-]{36})/")
_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                      r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_ISBN_RE = re.compile(r"\b(97[89][0-9]{10})\b")
_NON_WORD_RE = re.compile(r"[^a-z0-9]+")
_MD_BOLD_RE = re.compile(r"\*{2,}")


class KoboSource(BaseSource):
    """Adapter for Rakuten Kobo's ebook store."""

    name = "kobo"
    display_name = "Kobo"
    #: ``www.kobo.com`` needs a real browser to get past Cloudflare.
    prefers_browser = True

    def __init__(self, client: HttpClient) -> None:
        super().__init__(client)
        #: search page kept from discovery so sibling-edition covers cost no
        #: extra request
        self._search_soup: Optional[BeautifulSoup] = None
        self._match_mode: str = ""
        #: the title/authors we actually searched with (hint, or seeded)
        self._wanted_title: str = ""
        self._wanted_authors: List[str] = []

    # ------------------------------------------------------------------ fetch

    def _looks_challenged(self, soup: Optional[BeautifulSoup]) -> bool:
        """True when ``soup`` is Kobo's Cloudflare interstitial, not content."""
        if soup is None:
            return False
        try:
            head = str(soup)[:8192].lower()
        except Exception as exc:  # pragma: no cover - defensive
            if verbose():
                print('  Could not stringify soup for block detection: %s' % (exc,), file=sys.stderr)
            return False
        return any(marker in head for marker in CHALLENGE_MARKERS)


    def _fetch_page(self, url: str, result: ScrapeResult, *,
                    wait_css: Optional[str] = None,
                    wait_seconds: int = 15) -> Optional[BeautifulSoup]:
        """Fetch a ``www.kobo.com`` page through the browser. Requests cannot reach it.

        Kobo is behind Cloudflare, which answers plain ``requests`` with an HTTP 403
        wall on every path -- measured 0 of 3 across a product page, a search page
        and a second product page. The challenge keys off the TLS fingerprint
        (Python's OpenSSL vs a real Chrome's BoringSSL), so no amount of header
        tuning changes it, and a plain attempt is a guaranteed 403 plus a wasted
        courtesy delay. So there is one path, and it is the one that works.

        Kobo's product pages never fire their ``load`` event either (third-party
        trackers hold the connection open), which used to make every rendered fetch
        look like a hard failure. :meth:`HttpClient.get_rendered_soup` drives Chrome
        with ``page_load_strategy='eager'`` and treats a page-load timeout as
        non-fatal, so the DOM comes back through the normal path.

        Headless Chrome passes the challenge by being a real browser. Nothing here
        solves, forges or bypasses a CAPTCHA; if one is served, the fetch fails and
        says so. Returns ``None`` (with warnings recorded) when it cannot be read.
        """
        if not self.client.browser_available:
            result.warn(
                "no browser is available (selenium or its driver is missing), so "
                f"the Cloudflare-gated page could not be fetched: {url}. Kobo has no "
                "plain-HTTP route, so this book is reported as unreachable, never as "
                "absent from Kobo's catalogue"
            )
            return None

        soup = self.client.get_rendered_soup(url, wait_css=wait_css,
                                             wait_seconds=wait_seconds)
        if soup is not None and not self._looks_challenged(soup):
            return soup
        if soup is not None:
            result.warn("the browser was served the Cloudflare challenge page too; "
                        "no CAPTCHA was attempted")
            return None

        result.warn(f"the browser returned nothing for {url}")
        return None

    # -------------------------------------------------------------- discovery

    @staticmethod
    def _strip_tracking(url: str) -> str:
        """Drop Kobo's ``?sId=/&ssId=/&cPos=`` tracking query string."""
        if not url:
            return ""
        try:
            parts = urlsplit(url)
        except ValueError:
            # Unbalanced bracket in the netloc ("Invalid IPv6 URL"). Nothing to
            # strip that we can reason about, so hand the URL back untouched.
            return url
        return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))

    @staticmethod
    def _norm(text: Any) -> str:
        """Lower-case, punctuation-free, single-spaced form used for matching."""
        return _NON_WORD_RE.sub(" ", str(text or "").lower()).strip()

    @classmethod
    def _main_title(cls, text: Any) -> str:
        """Normalised title with any ``: subtitle`` / ``(series)`` tail removed."""
        raw = str(text or "")
        raw = re.split(r"[:(\[]", raw, maxsplit=1)[0]
        return cls._norm(raw)

    @staticmethod
    def _ratio(a: str, b: str) -> float:
        if not a or not b:
            return 0.0
        return SequenceMatcher(None, a, b).ratio()

    def _is_companion(self, card_title: str, wanted: str) -> bool:
        """True when the candidate is a study guide/summary *about* the book."""
        low = (card_title or "").lower()
        want_low = (wanted or "").lower()
        return any(m in low and m not in want_low for m in COMPANION_MARKERS)

    def _title_matches(self, candidate: str, wanted: str) -> Tuple[bool, float]:
        """Fuzzy title comparison. Returns ``(accepted, score)``."""
        cand_full, want_full = self._norm(candidate), self._norm(wanted)
        if not cand_full or not want_full:
            return False, 0.0
        cand_main, want_main = self._main_title(candidate), self._main_title(wanted)
        score = max(self._ratio(cand_full, want_full), self._ratio(cand_main, want_main))
        if cand_full == want_full or cand_main == want_main:
            return True, max(score, 1.0)
        for short, long in ((cand_main, want_full), (want_main, cand_full),
                            (cand_full, want_full), (want_full, cand_full)):
            if short and long and len(short) >= 6 and long.startswith(short):
                return True, max(score, 0.92)
        return score >= 0.86, score

    @staticmethod
    def _slug_base(url: str) -> str:
        """``/us/en/ebook/being-mortal-5`` -> ``being-mortal``.

        Kobo gives each format/edition of one work the same slug plus a numeric
        disambiguator, while *different* works (adaptations, box sets, graphic
        novels) get genuinely different slugs. Comparing the numeric-suffix-free
        slug is therefore a much stricter "same work?" test than comparing
        titles, whose subtitles Kobo drops from search cards.
        """
        try:
            path = urlsplit(url or "").path.rstrip("/")
        except ValueError:
            path = str(url or "").split("?", 1)[0].rstrip("/")
        slug = path.rsplit("/", 1)[-1].lower()
        return re.sub(r"-\d+$", "", slug)

    def _authors_match(self, candidate_authors: Sequence[str],
                       wanted_authors: Sequence[str]) -> bool:
        """True when any wanted author (or their surname) shows up in the candidate."""
        if not wanted_authors:
            return True
        haystack = self._norm(" ".join(candidate_authors))
        if not haystack:
            return False
        for author in wanted_authors:
            needle = self._norm(author)
            if not needle:
                continue
            if needle in haystack:
                return True
            surname = needle.split()[-1] if needle.split() else ""
            if len(surname) >= 4 and surname in haystack:
                return True
        return False


    def _is_book_page(self, soup: Optional[BeautifulSoup]) -> bool:
        """True when ``soup`` is a product page rather than a search page."""
        if soup is None:
            return False
        return bool(
            soup.select_one('div[data-kobo-gizmo="RatingAndReviewWidget"]')
            or soup.select_one("h1.title.product-field")
        )

    def _canonical_of(self, soup: Optional[BeautifulSoup], fallback: str = "") -> str:
        """``link[rel=canonical]`` (tracking stripped), else ``fallback``."""
        if soup is not None:
            node = soup.select_one("link[rel=canonical]")
            href = (node.get("href") if isinstance(node, Tag) else "") or ""
            href = self._strip_tracking(self.absolutise(STORE_ROOT, href))
            if href and "/search" not in href:
                return href
        return self._strip_tracking(fallback)

    def _find_by_isbn(self, isbn: str,
                      result: ScrapeResult) -> Tuple[Optional[str], bool]:
        """Kobo 301-redirects ``search?query=<its own EAN>`` to the product page.

        Works only for the ISBN of the exact edition Kobo sells, so it misses for
        most print ISBNs -- hence the title+author fallback.

        Returns ``(url, reached)``; ``reached`` is False when Kobo could not be
        contacted at all, so the caller can stop trying further ISBN spellings.
        """
        if not isbn:
            return None, True
        url = f"{STORE_ROOT}/search?query={isbn}"

        # There was a "cheap path" here that asked for the search URL with
        # allow_redirects=False, hoping a bare 301 would name the product page
        # without any parsing. It cannot work: www.kobo.com answers plain requests
        # with the Cloudflare 403 wall, so the client hands back None and the branch
        # never fired -- measured 0 of 3. All it bought was one extra blocked request
        # and its courtesy delay per book, plus a "blocked host" entry in the report
        # for a wall we deliberately route around. The rendered fetch below detects
        # the same redirect from the landed page's canonical URL.
        soup = self._fetch_page(url, result, wait_css=None, wait_seconds=15)
        if soup is None:
            return None, False
        if self._is_book_page(soup):
            canonical = self._canonical_of(soup, url)
            print('Kobo resolved ISBN %s straight to the product page %s' % (isbn, canonical), file=sys.stderr)
            return canonical, True
        self._search_soup = soup
        print('Kobo has no product indexed under ISBN %s' % (isbn,), file=sys.stderr)
        return None, True

    def _parse_search_cards(self, soup: Optional[BeautifulSoup]) -> List[Dict[str, Any]]:
        """Parse the (React) search results page into candidate dicts."""
        cards: List[Dict[str, Any]] = []
        if soup is None:
            return cards
        nodes = soup.select("div[id^='list-item-']") or soup.select(
            "div[data-testid='book-card-search-result-items']"
        )
        for position, node in enumerate(nodes, start=1):
            link = node.select_one("a[data-testid='title']") or node.select_one(
                "a[href*='/ebook/'], a[href*='/audiobook/']"
            )
            if link is None:
                continue
            href = self._strip_tracking(self.absolutise(STORE_ROOT, link.get("href")))
            if not href:
                continue
            title = self.clean_text(link.get("aria-label") or link.get_text(" "))
            authors = [
                self.clean_text(a) for a in node.select("[data-testid='authors'] a")
            ]
            image = node.select_one("img[data-testid='cover']") or node.select_one("img")
            cards.append({
                "position": position,
                "url": href,
                "title": title,
                "authors": self.dedupe([a for a in authors if a]),
                "image": self.absolutise(STORE_ROOT, image.get("src") if image else ""),
                "cross_revision_id": node.get("data-cross-revision-id") or "",
                "is_ebook": "/ebook/" in href,
            })
        return cards

    def _find_by_title_author(self, title: str, authors: Sequence[str],
                              result: ScrapeResult) -> Tuple[Optional[str], float]:
        """Title+author search fallback. Returns ``(url, match_score)``."""
        query = " ".join(part for part in [title, " ".join(authors[:1])] if part).strip()
        if not query:
            return None, 0.0
        url = f"{STORE_ROOT}/search?query={quote_plus(query)}"
        # Never reuse the ISBN search page here -- it answered a different query.
        self._search_soup = None
        soup = self._fetch_page(url, result,
                                wait_css="div[id^='list-item-']", wait_seconds=15)
        if soup is None:
            return None, 0.0
        self._search_soup = soup

        cards = self._parse_search_cards(soup)
        if not cards:
            result.warn(f"Kobo's search returned no usable result cards for {query!r}")
            return None, 0.0

        scored: List[Tuple[float, Dict[str, Any]]] = []
        for card in cards:
            if self._is_companion(card["title"], title):
                if verbose():
                    print('  Rejecting companion title %r' % (card['title'],), file=sys.stderr)
                continue
            ok, score = self._title_matches(card["title"], title)
            if not ok:
                continue
            if not self._authors_match(card["authors"], authors):
                if verbose():
                    print('  Rejecting %r: authors %s do not match %s' % (card['title'], card['authors'], list(authors)), file=sys.stderr)
                continue
            scored.append((score + (0.05 if card["is_ebook"] else 0.0), card))

        if not scored:
            result.warn(
                f"none of Kobo's {len(cards)} search result(s) for {query!r} "
                "matched the wanted title and author, so no product page was accepted"
            )
            return None, 0.0

        # Ties are common because search cards omit subtitles ("Sapiens" the book
        # and "Sapiens: A Graphic History" both arrive as "Sapiens"). Break them
        # on the shortest title -- adaptations, box sets and companions carry the
        # longer ones -- then on Kobo's own relevance order.
        scored.sort(key=lambda pair: (-pair[0], len(self._norm(pair[1]["title"])),
                                      pair[1]["position"]))
        best_score, best = scored[0]
        print('Kobo search matched %r by %s -> %s (score %.2f)' % (best['title'], ', '.join(best['authors']) or '?', best['url'], best_score), file=sys.stderr)
        return best["url"], min(best_score, 1.0)

    def _find_by_slug(self, title: str, result: ScrapeResult) -> Optional[str]:
        """Last resort: guess the product slug from the title."""
        slug = re.sub(r"-{2,}", "-", _NON_WORD_RE.sub("-", (title or "").lower())).strip("-")
        if not slug:
            return None
        url = f"{STORE_ROOT}/ebook/{slug}"
        print('Trying a slug guess for Kobo: %s' % (url,), file=sys.stderr)
        soup = self._fetch_page(url, result,
                                wait_css="div[data-kobo-gizmo-config]", wait_seconds=15)
        if soup is None:
            result.warn(f"slug guess {url} could not be fetched")
            return None
        if not self._is_book_page(soup):
            result.warn(f"slug guess {url} is not a Kobo product page")
            return None
        result.warn(f"accepted {url} from a slug guess; Kobo slugs carry "
                    "disambiguating suffixes, so this match is weak")
        return self._canonical_of(soup, url)

    def find_book_url(self, hint: BookHint) -> Optional[str]:
        """Resolve ``hint`` to a Kobo product URL, or ``None``.

        Order: ISBN-13 redirect, ISBN-10 redirect, title+author search, slug
        guess. Never raises.
        """
        scratch = self.new_result(hint)
        url = self._discover(hint, scratch)
        for warning in scratch.warnings:
            print('find_book_url: %s' % (warning,), file=sys.stderr)
        return url

    def _discover(self, hint: BookHint, result: ScrapeResult) -> Optional[str]:
        """Shared discovery body; records warnings on ``result``."""
        self._search_soup = None
        self._match_mode = ""
        self._wanted_title = (hint.title or "").strip()
        self._wanted_authors = [a for a in (hint.authors or []) if a and a.strip()]
        reached = True
        for isbn in self.dedupe([hint.isbn13, hint.isbn10]):
            url, reached = self._find_by_isbn(str(isbn), result)
            if url:
                self._match_mode = "isbn-redirect"
                return url
            if not reached:
                break  # Kobo is unreachable; other ISBN spellings will not help
        if not reached and self.client.block_reason(STORE_ROOT) \
                and not self.client.browser_available:
            # Gated host and no way to render it: every remaining path is on the
            # same host, so stop rather than repeating the same failure.
            result.warn(
                f"www.kobo.com is gated and no browser is available, so ISBN "
                f"{hint.isbn13} could not be looked up and no data was recovered "
                "from this source"
            )
            return None
        result.warn(
            f"Kobo does not index ISBN {hint.isbn13}: it catalogues the EAN of "
            "its own EPUB edition, not arbitrary print ISBNs, so discovery fell "
            "back to a title+author search"
        )

        title, authors = self.search_terms(hint, result)
        self._wanted_title = title or ""
        self._wanted_authors = list(authors)
        if not title:
            result.warn("no title to search Kobo with, so the book could not be located")
            return None

        url, score = self._find_by_title_author(title, authors, result)
        if url:
            self._match_mode = "title-author-search"
            result.warn(
                f"book page accepted on a fuzzy title+author search match "
                f"(similarity {score:.2f}) because Kobo could not resolve the ISBN"
            )
            return url

        url = self._find_by_slug(title, result)
        if url:
            self._match_mode = "slug-guess"
            return url
        return None

    # ------------------------------------------------------- page extraction

    def _gizmo_config(self, soup: BeautifulSoup, gizmo: str) -> Optional[Dict[str, Any]]:
        """Decode one ``data-kobo-gizmo-config`` attribute into a dict.

        BeautifulSoup already unescapes the HTML entities in the attribute value,
        so a single :func:`json.loads` normally suffices; the escaped form is
        retried for safety.
        """
        node = soup.select_one(f'div[data-kobo-gizmo="{gizmo}"][data-kobo-gizmo-config]')
        if node is None:
            return None
        raw = node.get("data-kobo-gizmo-config") or ""
        payload = self._loads_lenient(raw)
        if not isinstance(payload, dict):
            payload = self._loads_lenient(html_module.unescape(raw))
        return payload if isinstance(payload, dict) else None

    def _nested_json(self, config: Dict[str, Any], key: str) -> Dict[str, Any]:
        """``googleBook``/``googleProduct`` are JSON strings inside the JSON config."""
        raw = config.get(key)
        if isinstance(raw, dict):
            return raw
        if not isinstance(raw, str) or not raw.strip():
            return {}
        payload = self._loads_lenient(raw)
        return payload if isinstance(payload, dict) else {}

    def _secondary_metadata(self, soup: BeautifulSoup) -> Dict[str, str]:
        """Parse ``div.bookitem-secondary-metadata`` into ``{label: value}``.

        The publisher row carries no label, so it is stored under ``publisher``.
        """
        fields: Dict[str, str] = {}
        for item in soup.select("div.bookitem-secondary-metadata li"):
            text = self.clean_text(item)
            if not text:
                continue
            if ":" in text:
                label, _, value = text.partition(":")
                label = self._norm(label).replace(" ", "_")
                value = self.clean_text(value)
                if label and value:
                    fields.setdefault(label, value)
            else:
                fields.setdefault("publisher", text)
        return fields

    def _dom_authors(self, soup: BeautifulSoup) -> List[str]:
        """Contributor names from the legacy DOM list (carries roles Kobo's JSON omits)."""
        names: List[str] = []
        for anchor in soup.select(
            "span.authors.product-field.contributor-list span.visible-contributors "
            "a.contributor-name, span.authors.product-field a.contributor-name"
        ):
            name = self.clean_text(anchor)
            if not name:
                continue
            role_node = anchor.find_parent("li") or anchor.parent
            role = ""
            if isinstance(role_node, Tag):
                tag = role_node.select_one("span.mobile-library-tag")
                role = self.clean_text(tag).lower() if tag is not None else ""
            if role and "author" not in role:
                if verbose():
                    print('  Skipping non-author contributor %r (%s)' % (name, role), file=sys.stderr)
                continue
            names.append(name)
        return self.dedupe(names)

    def _breadcrumb_genres(self, soup: BeautifulSoup) -> List[str]:
        """Genres from the ``BreadcrumbList`` ld+json trails (root -> leaf)."""
        genres: List[str] = []
        for index, block in enumerate(self.jsonld(soup, "BreadcrumbList")):
            elements = block.get("itemListElement")
            if not isinstance(elements, list):
                continue
            names: List[str] = []
            for element in elements:
                if not isinstance(element, dict):
                    continue
                item = element.get("item")
                name = item.get("name") if isinstance(item, dict) else element.get("name")
                name = self.clean_text(name)
                if name and self._norm(name) not in _BREADCRUMB_CHROME:
                    names.append(name)
            if index == 0 and names:
                # Block 0 is page navigation; its last hop is the book title.
                names = names[:-1]
            genres.extend(names)
        return self.dedupe(genres)

    def _cover_urls(self, soup: BeautifulSoup, google_book: Dict[str, Any],
                    google_product: Dict[str, Any], result: ScrapeResult,
                    book_url: str, wanted_title: str,
                    wanted_authors: Sequence[str]) -> List[str]:
        """Collect one high-resolution cover per distinct Kobo image id."""
        work = google_book.get("workExample")
        work = work if isinstance(work, dict) else {}
        candidates: List[str] = [
            str(work.get("image") or ""),
            str(google_product.get("image") or ""),
            str(work.get("thumbnailUrl") or ""),
        ]
        node = soup.select_one("div.main-product-image img.cover-image") or \
            soup.select_one("img.cover-image")
        if node is not None:
            # The class attribute contains a double space upstream, so select on
            # the class rather than on a literal class string.
            candidates.append(self.absolutise(STORE_ROOT, node.get("src")))
        else:
            result.warn("cover <img class='cover-image'> not found; used the JSON image URLs")
        candidates.append(self.meta(soup, "og:image", "twitter:image") or "")

        # Other formats of the *same* work (the audiobook, mainly) carry their own
        # artwork. The search page is already in hand, so this costs no extra
        # request. Identity is decided on the numeric-suffix-free slug, not on the
        # title: search cards omit subtitles, so "Sapiens" the book and "Sapiens:
        # A Graphic History" both present as "Sapiens" and a title test happily
        # pulls in the wrong artwork.
        siblings = 0
        product_slug = self._slug_base(book_url)
        for card in self._parse_search_cards(self._search_soup):
            if not card["image"] or not product_slug:
                continue
            if self._slug_base(card["url"]) != product_slug:
                continue
            if self._is_companion(card["title"], wanted_title):
                continue
            if not self._authors_match(card["authors"], wanted_authors):
                continue
            candidates.append(card["image"])
            siblings += 1

        urls: List[str] = []
        seen_ids: set = set()
        for candidate in candidates:
            absolute = self.absolutise(STORE_ROOT, candidate)
            if not absolute:
                continue
            match = _IMAGE_ID_RE.search(absolute)
            if match is None:
                if absolute not in urls:
                    urls.append(absolute)
                    result.warn(f"could not parse a Kobo image id out of {absolute}; "
                                "using the URL as published (lower resolution)")
                continue
            image_id = match.group(1).lower()
            if image_id in seen_ids:
                continue
            seen_ids.add(image_id)
            width, height, quality = COVER_BOX
            urls.append(COVER_CDN.format(image_id=image_id, w=width, h=height, q=quality))
        if siblings:
            print('Added %d sibling-edition cover candidate(s) from the search page' % (siblings,), file=sys.stderr)
        return urls

    def _blurb(self, soup: BeautifulSoup, google_book: Dict[str, Any],
               result: ScrapeResult) -> Optional[str]:
        """The full synopsis, with three documented fallbacks."""
        node = soup.select_one("div[data-full-synopsis]")
        if node is not None:
            text = self._clean_blurb(node.decode_contents())
            if text:
                return text
            result.warn("div[data-full-synopsis] was present but empty")

        node = soup.select_one("div.synopsis-description, div#synopsis-desc")
        if node is not None:
            text = self._clean_blurb(node.decode_contents())
            if text:
                result.warn(
                    "full synopsis (div[data-full-synopsis]) was missing; fell back "
                    "to the JavaScript-filled .synopsis-description block, which is "
                    "the collapsed preview and may be shorter than the real blurb"
                )
                return text

        work = google_book.get("workExample")
        work = work if isinstance(work, dict) else {}
        teaser = self.clean_text(work.get("description")) or \
            self.meta(soup, "og:description", "description")
        if teaser:
            result.warn(
                "no synopsis element found; fell back to the og:description/JSON "
                "teaser, which Kobo truncates to ~200 characters"
            )
            return self._clean_blurb(teaser)

        result.warn("no blurb/synopsis could be extracted from the Kobo page")
        return None

    def _publication_date(self, raw: Any) -> Optional[str]:
        """Normalise a Kobo date to ISO-8601.

        Kobo publishes ``'2014-10-07T00:00:00Z'`` / ``'2014-10-07T00:00:00'``.
        The shared :meth:`iso_date` helper anchors its ISO pattern on a word
        boundary, which the ``T`` swallows, so the day part is peeled off here
        first; anything else (``'October 7, 2014'``) goes through the helper.
        """
        text = self.clean_text(raw)
        if not text:
            return None
        stamp = re.match(r"(\d{4})-(\d{2})-(\d{2})(?![\d-])", text)
        if stamp is not None:
            return stamp.group(0)
        return self.iso_date(text)

    def _clean_blurb(self, raw: Any) -> str:
        """HTML fragment -> prose, minus Kobo's leaked markdown bold markers."""
        text = self.html_to_text(raw)
        if "**" in text:
            text = _MD_BOLD_RE.sub("", text)
            if verbose():
                print('  Stripped leaked markdown bold markers from the blurb', file=sys.stderr)
        return self.clean_text(text)

    # ---------------------------------------------------------------- reviews

    def _review_count_hint(self, google_book: Dict[str, Any]) -> Optional[int]:
        """``aggregateRating.reviewCount`` -- written reviews, not star-only ratings."""
        rating = google_book.get("aggregateRating")
        if not isinstance(rating, dict):
            return None
        try:
            return max(0, int(str(rating.get("reviewCount") or "").strip() or 0))
        except (TypeError, ValueError):
            return None

    def _parse_review_nodes(self, soup: Optional[BeautifulSoup]) -> List[ReviewItem]:
        """Parse Kobo's review-listing HTML fragment (or an in-page widget)."""
        if soup is None:
            return []
        nodes = soup.select("div.review-listing li.review-item-wrapper div.review-item") \
            or soup.select("li.review-item-wrapper div.review-item") \
            or soup.select("div.review-item")
        reviews: List[ReviewItem] = []
        for node in nodes:
            body = node.select_one("span.review-text") or node.select_one(".review-text")
            reviewer = node.select_one(
                "span.review-author[data-automation-test-hook='author']"
            ) or node.select_one("span.review-author")
            date = node.select_one(
                "span.review-date[data-automation-test-hook='datePublished']"
            ) or node.select_one("span.review-date")
            rating = node.select_one("div.rating-average") or node.select_one(".rating-average")
            title = node.select_one("h2.review-title")
            text = self.html_to_text(body) if body is not None else ""
            heading = self.clean_text(title) if title is not None else ""
            if heading and text and not text.lower().startswith(heading.lower()):
                text = f"{heading}\n\n{text}"
            elif heading and not text:
                continue  # a title with no body is not a review
            review = self.make_review(
                text,
                reviewer=self.clean_text(reviewer) if reviewer is not None else None,
                rating=self.clean_text(rating) if rating is not None else None,
                date=self.iso_date(self.clean_text(date)) if date is not None else None,
                min_chars=2,
            )
            if review is not None:
                reviews.append(review)
        return reviews

    def _reviews_from_jsonld(self, soup: BeautifulSoup) -> List[ReviewItem]:
        """Reviews from the ld+json ``Book``/``Product`` blocks JavaScript injects."""
        reviews: List[ReviewItem] = []
        for block in self.jsonld(soup, ("Book", "Product")):
            entries = block.get("review")
            if isinstance(entries, dict):
                entries = [entries]
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                author = entry.get("author")
                rating = entry.get("reviewRating")
                heading = self.clean_text(entry.get("name"))
                body = self.clean_text(entry.get("reviewBody"))
                if heading and body and not body.lower().startswith(heading.lower()):
                    body = f"{heading}\n\n{body}"
                review = self.make_review(
                    body or heading,
                    reviewer=(author.get("name") if isinstance(author, dict) else author),
                    rating=(rating.get("ratingValue") if isinstance(rating, dict) else rating),
                    date=self.iso_date(entry.get("datePublished")),
                    min_chars=2,
                )
                if review is not None:
                    reviews.append(review)
        return reviews

    @staticmethod
    def _review_key(review: ReviewItem) -> Tuple[str, str, str]:
        """Dedup key. The review *title* can be empty, so it is never the key."""
        return (
            (review.reviewer or "").strip().casefold(),
            (review.date or "").strip(),
            " ".join((review.text or "").split()).casefold()[:180],
        )

    def _fetch_reviews(self, cross_revision_id: str, google_book: Dict[str, Any],
                       book_soup: BeautifulSoup, result: ScrapeResult) -> List[ReviewItem]:
        """Collect reviews, newest first, deduplicated.

        Primary path is ``ratingsapi.kobo.com`` (no Cloudflare, no headers, no
        cookies) which returns every review in a single call. Fallbacks are the
        JavaScript-filled widget in the rendered book page and the injected
        ``ld+json`` review array.
        """
        target = max(int(self.min_reviews or 0), 25)
        if self.max_reviews is not None:
            target = max(1, min(target, int(self.max_reviews)))
        total_hint = self._review_count_hint(google_book)

        collected: List[ReviewItem] = []
        seen: set = set()

        def absorb(items: Sequence[ReviewItem]) -> int:
            added = 0
            for item in items:
                key = self._review_key(item)
                if key in seen:
                    continue
                seen.add(key)
                collected.append(item)
                added += 1
            return added

        server_total: Optional[int] = None
        if cross_revision_id:
            # The request size must respect the effective ceiling, otherwise a
            # popular book downloads (and writes) every review it has: with
            # --min-reviews 25 and a reviewCount hint of ~296 this asked for 301
            # and wrote 301 files, 10x every other source -- and with
            # --max-reviews 5 it still fetched all 301 and discarded 296.
            wanted = max(target, 50)
            if self.max_reviews is not None:
                wanted = min(wanted, max(1, int(self.max_reviews)))
            else:
                # No explicit cap: allow a generous over-fetch so de-duplication
                # and the min-review target both have room, but stay bounded.
                wanted = max(wanted, min((total_hint or 0) + 5, 4 * max(target, 25)))
            limit = min(REVIEW_LIMIT_CEILING, wanted)
            if verbose():
                print('  Kobo review request limit=%d (target=%d, max_reviews=%s, hint=%s)' % (limit, target, self.max_reviews, total_hint), file=sys.stderr)
            for page in range(REVIEW_PAGE_CEILING):
                response = self.client.get(REVIEWS_API, params={
                    "id": cross_revision_id,
                    "offset": page,          # 0-based PAGE index, not a row offset
                    "limit": limit,
                    "sortBy": REVIEW_SORT_NEWEST,
                    "starRating": 0,
                    "userLocale": "en-US",
                })
                if response is None:
                    result.warn(
                        "the Kobo reviews service (ratingsapi.kobo.com/V1/Ui/"
                        f"GetMoreReviews) did not answer for page {page}"
                    )
                    break
                fragment = self.client.soup_from_response(response)
                if fragment is None:
                    result.warn("the Kobo reviews service returned an unparseable body")
                    break
                if server_total is None:
                    node = fragment.select_one("input#TotalReviewCount")
                    raw_total = (node.get("value") if isinstance(node, Tag) else "") or ""
                    if raw_total.strip().isdigit():
                        server_total = int(raw_total.strip())
                items = self._parse_review_nodes(fragment)
                if not items:
                    if page == 0:
                        result.warn("the Kobo reviews service returned no review items")
                    break
                absorb(items)
                if len(collected) >= target:
                    break
                if server_total is not None and len(collected) >= server_total:
                    break
                if len(items) < limit:
                    break  # server had nothing more to give
                if verbose():
                    print('  Paginating Kobo reviews: page %d -> %d collected' % (page, len(collected)), file=sys.stderr)
        else:
            result.warn("no crossRevisionId found, so the reviews service could "
                        "not be queried")

        if len(collected) < target:
            widget = self._parse_review_nodes(book_soup)
            if widget and absorb(widget):
                result.warn(
                    "topped up the review list from the review widget rendered in "
                    "the product page because the reviews service returned fewer "
                    "than requested"
                )
        if len(collected) < target:
            structured = self._reviews_from_jsonld(book_soup)
            if structured and absorb(structured):
                result.warn(
                    "topped up the review list from the page's ld+json review "
                    "array (a JavaScript-injected first page of reviews)"
                )

        available = server_total if server_total is not None else total_hint
        if len(collected) < target:
            if available is not None and available <= len(collected):
                ratings = self._rating_count(google_book)
                result.warn(
                    ("Kobo has no written reviews for this edition"
                     if available == 0 else
                     f"Kobo has only {available} written review(s) for this edition")
                    + (f" (its other {ratings} ratings are star-only, with no text)"
                       if ratings else "")
                    + f", so the {target}-review target is not reachable here; "
                      "no padding was added"
                )
            else:
                result.warn(
                    f"recovered {len(collected)} review(s), fewer than the "
                    f"{target} requested"
                    + (f" (Kobo reports {available} exist)" if available is not None else "")
                )
        print('Kobo reviews: %d collected (server total %s, aggregate hint %s)' % (len(collected), server_total, total_hint), file=sys.stderr)
        return collected

    @staticmethod
    def _rating_count(google_book: Dict[str, Any]) -> Optional[str]:
        rating = google_book.get("aggregateRating")
        if isinstance(rating, dict):
            value = str(rating.get("ratingCount") or "").strip()
            return value or None
        return None

    # ------------------------------------------------------- field extractors
    #
    # Every one of these takes the already-fetched page plus the decoded JSON and
    # returns a value, appending a warning to ``result`` whenever it had to fall
    # back from the embedded JSON to the CSS DOM or to a meta tag.

    def _edition_isbn(self, work: Dict[str, Any], google_product: Dict[str, Any],
                      dom: Dict[str, str]) -> str:
        """The ISBN-13 of the edition Kobo actually sells (``''`` if unpublished)."""
        raw = self.clean_text(work.get("isbn")) or \
            self.clean_text(google_product.get("gtin13")) or \
            self.clean_text(dom.get("book_id"))
        found = _ISBN_RE.search(raw or "")
        return found.group(1) if found else ""

    def _extract_title(self, soup: BeautifulSoup, google_book: Dict[str, Any],
                       work: Dict[str, Any],
                       result: ScrapeResult) -> Tuple[Optional[str], Optional[str]]:
        """Return ``(main_title, "Main: Subtitle")``.

        The bare main title is what gets handed to other sources as a search hint;
        the combined form is the richer bibliographic record we report.
        """
        main = self.clean_text(google_book.get("name"))
        if not main:
            main = self.select_text(soup, "h1.title.product-field", "h1.title", "h1") or ""
            if main:
                result.warn("title came from the DOM (h1.title.product-field), "
                            "not the embedded JSON")
        if not main:
            meta_title = self.meta(soup, "og:title", "twitter:title") or ""
            main = re.split(r"\s+eBook by\s+", meta_title)[0].strip()
            if main:
                result.warn("title came from the og:title meta tag")
        if not main:
            result.warn("title not found anywhere on the Kobo page")
            return None, None
        subtitle = self.clean_text(work.get("alternativeHeadline")) or \
            self.select_text(soup, "span.subtitle.product-field")
        return main, (f"{main}: {subtitle}" if subtitle else main)

    def _extract_authors(self, soup: BeautifulSoup, google_book: Dict[str, Any],
                         work: Dict[str, Any], result: ScrapeResult) -> List[str]:
        """Authors from the JSON, topped up with any the DOM lists as well.

        ``googleBook.author`` is a dict for a single author and a list for
        several, and it silently drops co-authors the contributor list shows.
        """
        authors = self.split_list(google_book.get("author") or work.get("author"))
        dom_authors = self._dom_authors(soup)
        if not authors and dom_authors:
            result.warn("authors came from the DOM contributor list, not the "
                        "embedded JSON")
            return dom_authors
        if dom_authors:
            known = {self._norm(a) for a in authors}
            extra = [a for a in dom_authors if self._norm(a) not in known]
            if extra:
                print('DOM contributed extra author(s): %s' % (', '.join(extra),), file=sys.stderr)
                authors = self.dedupe(list(authors) + extra)
        if not authors:
            result.warn("no authors found on the Kobo page")
        return list(authors)

    def _extract_publisher(self, soup: BeautifulSoup, google_book: Dict[str, Any],
                           google_product: Dict[str, Any], dom: Dict[str, str],
                           result: ScrapeResult
                           ) -> Tuple[Optional[str], Optional[str]]:
        """Return ``(publisher, imprint)``. The imprint is a DOM-only field."""
        node = google_book.get("publisher")
        publisher = self.clean_text(node.get("name") if isinstance(node, dict) else node)
        if not publisher:
            brand = google_product.get("brand")
            publisher = self.clean_text(
                brand.get("name") if isinstance(brand, dict) else brand)
            if publisher:
                result.warn("publisher came from googleProduct.brand, not googleBook")
        if not publisher:
            publisher = dom.get("publisher") or ""
            if publisher:
                result.warn("publisher came from the DOM secondary-metadata list")
        if not publisher:
            result.warn("publisher not found on the Kobo page")

        imprint = dom.get("imprint") or self.select_text(
            soup, "div.bookitem-secondary-metadata li a[href*='fcsearchfield=Imprint'] span"
        )
        if imprint:
            print('Kobo imprint for this edition: %s' % (imprint,), file=sys.stderr)
        return publisher or None, imprint or None

    def _extract_origin(self, soup: BeautifulSoup, work: Dict[str, Any],
                        google_book: Dict[str, Any], google_product: Dict[str, Any],
                        dom: Dict[str, str], imprint: Optional[str],
                        result: ScrapeResult) -> Optional[str]:
        """Place of publication, searched for across every layer this run parsed.

        The search is :meth:`~bookscraper.base.BaseSource.probe_origin` over the
        blobs and DOM this run actually got, so if Kobo ever starts publishing a
        place of publication the field fills itself in -- and the warning names
        the layers the probe reports having searched, not a list written by hand.

        The near-miss is ``eligibleRegion`` / ``ineligibleRegion``, which
        :meth:`_region_licensing` locates, counts and describes from the blob
        itself on every run. Where the runs so far have found them -- hanging off
        a priced ``Offer`` inside the page's checkout ``potentialAction`` -- makes
        them sales-territory geo-licensing (where the ebook may be *sold*) rather
        than a place of publication, so they are named and rejected instead of
        reported as origin.
        """
        layers: List[Tuple[str, Any]] = [
            ("the data-kobo-gizmo-config googleBook blob", google_book),
            ("its workExample record", work),
            ("the googleProduct blob", google_product),
            ("the injected ld+json blocks", self.jsonld(soup)),
            ("the bookitem-secondary-metadata DOM rows", dom),
            ("the page DOM and og:/meta tags", soup),
        ]
        probe = self.probe_origin_detail(layers)
        if probe.value:
            result.warn(
                f"origin {probe.value!r} was read from Kobo's page ({probe.where}); "
                "Kobo did not previously publish a place of publication, so treat "
                "this as new"
            )
            return probe.value

        regions = self._region_licensing([
            ("googleBook", google_book), ("googleProduct", google_product),
        ])
        return self.origin_unavailable(
            result,
            "no place-of-publication key or label was found in the "
            f"{len(ORIGIN_KEY_SPELLINGS)} spellings the shared origin probe searches "
            f"for, across every layer this run could parse ("
            f"{self.origin_layers_clause(probe.searched)}). "
            + self._region_licensing_clause(regions)
            + (f"; the imprint on this page is {imprint!r}, a company rather than a place"
               if imprint else ""),
        )

    def _region_licensing(
        self, layers: Sequence[Tuple[str, Any]]
    ) -> Dict[str, Dict[str, Any]]:
        """Locate Kobo's sales-territory region lists in this run's blobs.

        Returns ``{'eligibleregion': {...}, 'ineligibleregion': {...}}`` with the
        dotted path each list was found at, how many entries it has and what kind
        of object carries it -- all read off the page, so nothing about the shape
        of Kobo's geo-licensing data is asserted from memory.
        """
        found: Dict[str, Dict[str, Any]] = {}
        for label, blob in layers:
            for pair in self.iter_json_pairs(blob, path=label):
                key = pair.key.casefold()
                if key not in _REGION_KEYS or key in found:
                    continue
                names = self._region_names(pair.value)
                parent = pair.parent if isinstance(pair.parent, dict) else {}
                found[key] = {
                    "path": pair.path,
                    "count": len(names) if names else (1 if pair.value else 0),
                    "names": names[:3],
                    "type": self.clean_text(parent.get("@type")),
                    "priced": any(k in parent for k in ("price", "priceCurrency")),
                }
        return found

    @staticmethod
    def _region_names(raw: Any) -> List[str]:
        """Country names/codes in an ``(in)eligibleRegion`` value, in page order."""
        items = raw if isinstance(raw, (list, tuple)) else [raw]
        names: List[str] = []
        for item in items:
            if isinstance(item, dict):
                text = str(item.get("name") or item.get("@id") or "").strip()
            elif isinstance(item, str):
                text = item.strip()
            else:
                text = ""
            if text:
                names.append(text)
        return names

    def _region_licensing_clause(self, regions: Dict[str, Dict[str, Any]]) -> str:
        """Describe the geo-licensing lists this run found, or say it found none."""
        if not regions:
            return (
                "This run found no eligibleRegion/ineligibleRegion pair in those "
                "layers either, so there was no country-shaped value to reject"
            )
        bits: List[str] = []
        for key in ("eligibleregion", "ineligibleregion"):
            info = regions.get(key)
            if not info:
                continue
            count = info["count"]
            shown = ", ".join(info["names"]) if info["names"] else ""
            bits.append(
                f"{info['path']} ({count} " + ("entry" if count == 1 else "entries")
                + (f": {shown}" if count and count <= 3 and shown else "") + ")"
            )
        carrier = next(
            (r for r in regions.values() if r.get("type") or r.get("priced")), None
        )
        where = ""
        if carrier:
            where = (
                " -- they hang off a "
                + ("priced " if carrier["priced"] else "")
                + (carrier["type"] or "object")
                + (" inside the page's checkout action"
                   if "potentialaction" in str(carrier["path"]).casefold() else "")
            )
        return (
            "The only country data this run found is " + " and ".join(bits) + where
            + ", i.e. sales-territory geo-licensing (which storefronts may sell this "
            "ebook), not where the book was published"
        )

    def _extract_date(self, work: Dict[str, Any], google_product: Dict[str, Any],
                      dom: Dict[str, str], result: ScrapeResult,
                      edition_differs: bool) -> Optional[str]:
        """ISO-8601 release date of the edition Kobo sells."""
        published = self._publication_date(work.get("datePublished"))
        if not published:
            published = self._publication_date(google_product.get("releasedate"))
            if published:
                result.warn("publication date came from googleProduct.releasedate")
        if not published:
            published = self._publication_date(dom.get("release_date"))
            if published:
                result.warn("publication date came from the DOM 'Release Date:' row")
        if not published:
            result.warn("publication date not found on the Kobo page")
        elif edition_differs:
            result.warn(
                "date_of_publication is the release date of the ebook edition Kobo "
                "sells, not of the print edition behind the queried ISBN"
            )
        return published

    def _extract_language(self, google_book: Dict[str, Any], dom: Dict[str, str],
                          result: ScrapeResult) -> Optional[str]:
        """The language the book is written in, as an English name where possible."""
        code = self.clean_text(google_book.get("inLanguage")).lower()
        dom_language = dom.get("language") or ""
        language = dom_language or _LANGUAGE_NAMES.get(code.split("-")[0], "") or code
        if not language:
            result.warn("language not found on the Kobo page")
        elif not code:
            result.warn("language came from the DOM 'Language:' row, not "
                        "googleBook.inLanguage")
        return language or None

    def _extract_genres(self, soup: BeautifulSoup, google_book: Dict[str, Any],
                        result: ScrapeResult) -> List[str]:
        """Flat genre list: ``googleBook.genre``, then three DOM fallbacks."""
        genres = self.split_list(google_book.get("genre"))
        if not genres:
            genres = self._breadcrumb_genres(soup)
            if genres:
                result.warn("genres came from the BreadcrumbList ld+json trails, "
                            "not googleBook.genre")
        if not genres:
            genres = self.select_texts(
                soup, "ul.category-rankings li a.rankingAnchor",
                "ul.category-rankings li a")
            if genres:
                result.warn("genres came from the sidebar category rankings")
        if not genres:
            leaf = self.meta(soup, "genre")
            if leaf:
                genres = self.split_list(leaf)
                result.warn("only the single leaf genre from meta[property=genre] "
                            "was available")
        genres = self.dedupe(genres)
        if not genres:
            result.warn("no genres found on the Kobo page")
        return list(genres)

    def _extract_crid(self, soup: BeautifulSoup, config: Dict[str, Any],
                      book_url: str, result: ScrapeResult) -> str:
        """The ``crossRevisionId`` the reviews service is keyed on."""
        crid = self.clean_text(config.get("crossRevisionId"))
        if _UUID_RE.match(crid or ""):
            return crid
        actions = self._gizmo_config(soup, "ItemDetailActions") or {}
        crid = self.clean_text(actions.get("crossRevisionId"))
        if _UUID_RE.match(crid or ""):
            result.warn("crossRevisionId came from the ItemDetailActions gizmo, "
                        "not the rating widget")
            return crid
        crid = self._crid_from_search(book_url)
        if crid:
            result.warn("crossRevisionId came from the search result card")
        return crid

    # ------------------------------------------------------------------ scrape

    def _scrape_into(self, hint: BookHint, result: ScrapeResult) -> None:
        """Scrape body. Records warnings instead of raising."""
        book_url = self._discover(hint, result)
        if not book_url:
            print('warning: Could not locate %s on Kobo' % (hint.describe(),), file=sys.stderr)
            return
        result.book_url = book_url

        soup = self._fetch_page(book_url, result,
                                wait_css="div[data-kobo-gizmo-config]", wait_seconds=20)
        if soup is None:
            print('warning: Kobo product page %s could not be fetched' % (book_url,), file=sys.stderr)
            return
        result.book_url = self._canonical_of(soup, book_url)

        config = self._gizmo_config(soup, "RatingAndReviewWidget") or {}
        if not config:
            result.warn(
                "the RatingAndReviewWidget gizmo config (Kobo's primary metadata "
                "carrier) was not found; falling back to the ld+json blocks and "
                "the legacy CSS DOM. Kobo may have migrated this page to its new "
                "React frontend."
            )
        google_book = self._nested_json(config, "googleBook")
        google_product = self._nested_json(config, "googleProduct")

        if not google_book:
            for block in self.jsonld(soup, "Book"):
                google_book = block
                result.warn("googleBook JSON was unavailable; used the page's "
                            "ld+json Book block instead")
                break
        if not google_product:
            for block in self.jsonld(soup, "Product"):
                google_product = block
                result.warn("googleProduct JSON was unavailable; used the page's "
                            "ld+json Product block instead")
                break

        work = google_book.get("workExample")
        work = work if isinstance(work, dict) else {}
        dom = self._secondary_metadata(soup)
        if not dom:
            result.warn("div.bookitem-secondary-metadata was not found, so the DOM "
                        "cross-check for publisher/date/language is unavailable")

        metadata = self.new_metadata(hint)

        kobo_isbn = self._edition_isbn(work, google_product, dom)
        edition_differs = bool(kobo_isbn) and kobo_isbn != hint.isbn13

        main_title, metadata.title = self._extract_title(soup, google_book, work, result)
        metadata.authors = self._extract_authors(soup, google_book, work, result)
        metadata.publisher, imprint = self._extract_publisher(
            soup, google_book, google_product, dom, result)
        metadata.origin = self._extract_origin(
            soup, work, google_book, google_product, dom, imprint, result)
        metadata.date_of_publication = self._extract_date(
            work, google_product, dom, result, edition_differs)
        metadata.language = self._extract_language(google_book, dom, result)
        metadata.genres = self._extract_genres(soup, google_book, result)
        result.genres = list(metadata.genres)

        self._verify_identity(hint, result, metadata, kobo_isbn)

        # -- covers ------------------------------------------------------------
        result.cover_urls = self._cover_urls(
            soup, google_book, google_product, result, result.book_url or "",
            self._wanted_title or metadata.title or "",
            self._wanted_authors or metadata.authors,
        )

        # -- blurb -------------------------------------------------------------
        result.blurb = self._blurb(soup, google_book, result)

        # -- reviews -----------------------------------------------------------
        crid = self._extract_crid(soup, config, result.book_url or "", result)
        result.reviews = self._fetch_reviews(crid, google_book, soup, result)

        # -- hand the title/authors on to later sources ------------------------
        if main_title or metadata.authors:
            result.hint_updates = BookHint(
                isbn13=hint.isbn13,
                isbn10=hint.isbn10,
                title=main_title or None,
                authors=list(metadata.authors),
            )

        result.metadata = metadata
        print('Kobo: %r | %s | %s | %d genre(s) | %d cover(s) | %d review(s) | blurb %d chars' % (metadata.title, ', '.join(metadata.authors) or 'no authors', metadata.publisher or 'no publisher', len(result.genres), len(result.cover_urls), len(result.reviews), len(result.blurb or '')), file=sys.stderr)

    def _crid_from_search(self, book_url: str) -> str:
        """Recover a ``crossRevisionId`` from the search cards, if we have them."""
        target = self._strip_tracking(book_url or "")
        for card in self._parse_search_cards(self._search_soup):
            if card["url"] == target and _UUID_RE.match(card["cross_revision_id"] or ""):
                return str(card["cross_revision_id"])
        return ""

    def _verify_identity(self, hint: BookHint, result: ScrapeResult,
                         metadata: BookMetadata, kobo_isbn: str) -> None:
        """Guard against having landed on the wrong book, and say how sure we are."""
        if kobo_isbn and kobo_isbn == hint.isbn13:
            print("Kobo's edition ISBN matches the queried ISBN exactly", file=sys.stderr)
            return
        if kobo_isbn:
            result.warn(
                f"Kobo sells a different edition: its ISBN is {kobo_isbn} but "
                f"{hint.isbn13} was requested. Publisher, imprint, release date and "
                "language below describe Kobo's ebook edition, not the queried "
                "print edition"
            )
        else:
            result.warn("Kobo did not publish an ISBN for this edition, so the "
                        "queried ISBN could not be confirmed on the page")

        if self._match_mode == "isbn-redirect":
            return

        wanted_title = self._wanted_title or hint.title or ""
        wanted_authors = self._wanted_authors or list(hint.authors or [])
        if not wanted_title:
            result.warn(
                "identity could only be confirmed loosely: there was no title hint "
                "to compare Kobo's product page against"
            )
            return
        ok, score = self._title_matches(metadata.title or "", wanted_title)
        if ok and self._authors_match(metadata.authors, wanted_authors):
            result.warn(
                f"identity confirmed by fuzzy title+author match only "
                f"(similarity {score:.2f}); no shared ISBN was available"
            )
        else:
            result.warn(
                f"WEAK MATCH: Kobo's page reports {metadata.title!r} by "
                f"{', '.join(metadata.authors) or 'unknown'}, which only scores "
                f"{score:.2f} against the requested {wanted_title!r} by "
                f"{', '.join(wanted_authors) or 'unknown'}. Treat this record with "
                "suspicion"
            )
