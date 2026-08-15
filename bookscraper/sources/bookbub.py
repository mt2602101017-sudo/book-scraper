"""BookBub adapter (``--sources bookbub``).

BookBub is an ebook *deals* site, not a bibliographic database, which shapes
everything here:

* **No ISBN anywhere on a book page**, so books can only be matched by
  title+author. Acceptance is always fuzzy and always warned about.
* **No publisher, publication date, place of publication or per-book language.**
  Those are genuinely absent from the site. The adapter still looks for each one
  (so it starts working if BookBub adds them) and warns when they come back
  empty, distinguishing "the panel was read and has no such row" from "the panel
  was not found" -- selector decay must never be reported as a fact about the
  site.
* **Aggregate ratings only.** Review *text* is behind sign-in, so the honest
  review ceiling is 0. Nothing is padded.

Two blockers, both verified live: Cloudflare challenges every path (including
``/robots.txt``) on a TLS fingerprint, so header tuning cannot help; and the book
body is client-side rendered. Hence ``prefers_browser = True``. Headless Chrome
passes the challenge on its own -- nothing here solves, forges or bypasses a
CAPTCHA -- and without a driver the adapter degrades to a warned empty result.

Discovery, since there is no anonymous search route (``/search`` is HTTP 404):

1. **Slug construction** -- ``/books/<title-slug>-by-<author-slug>``, trying a few
   title variants because BookBub keeps one record per edition.
2. **Author-page harvest** -- ``/authors/<author-slug>`` lists real ``/books/``
   slugs, including dated variants like ``...-2026-04-09`` that guessing cannot
   invent. Candidates are scored offline; only the best are fetched.

Field extraction is layered, and every fallback that fires appends a warning:
the ``data-book-json`` panel attribute, then JSON-LD ``Book``, then scoped CSS
selectors, then ``og:``/``twitter:`` meta tags.
"""

from __future__ import annotations

import sys
from ..verbosity import verbose
import difflib
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from bs4 import BeautifulSoup, Tag

from ..base import ORIGIN_KEY_SPELLINGS, BaseSource
from ..http_client import HttpClient
from ..models import BookHint, BookMetadata, ReviewItem, ScrapeResult

__all__ = ["BookBubSource"]

#: BookBub 301-redirects the bare apex to ``www``, so normalise up front.
BASE_URL = "https://www.bookbub.com"
BOOK_URL = BASE_URL + "/books/{slug}"
AUTHOR_URL = BASE_URL + "/authors/{slug}"

#: The attribute carrying the book panel's embedded JSON -- our primary source.
BOOK_JSON_ATTR = "data-book-json"
#: Selector we wait for in the browser; its presence means the book rendered.
BOOK_JSON_CSS = f"[{BOOK_JSON_ATTR}]"
#: Book links on an author listing. The listing carries no ``data-book-json``,
#: and its grid is lazily rendered, so we wait for a *cover image* inside a book
#: link (nav/footer links have no image) and scroll to trigger the rest.
AUTHOR_LINK_CSS = "a[href*='/books/']"
AUTHOR_WAIT_CSS = "a[href*='/books/'] img"
AUTHOR_SCROLL_PASSES = 3

#: How long to let Cloudflare's auto-solve plus the client-side render finish.
RENDER_WAIT_SECONDS = 20

#: Fetch budget, so a bad hint cannot turn into a dozen browser page loads.
MAX_SLUG_CANDIDATES = 3
MAX_AUTHOR_PAGE_CANDIDATES = 2
MAX_BOOK_FETCHES = 5

#: Accept without looking further only on an exact normalised title match.
STRONG_TITLE_RATIO = 0.95
#: Below this combined score we refuse the page rather than risk the wrong book.
MIN_ACCEPT_SCORE = 0.60
#: Above this we consider the match solid; below it the warning says "fuzzy".
CONFIDENT_SCORE = 0.90
#: Ceiling for the "candidate title *adds* words to the requested title"
#: containment case. Sitting below :data:`CONFIDENT_SCORE` guarantees the
#: "acceptance was FUZZY" warning fires, so a sequel can never be presented as a
#: confident match.
_ADDITIVE_CONTAINMENT_CEILING = 0.80

#: BCP-47 primary subtag -> language name, so this adapter emits ``"English"``
#: like the other four rather than ``"en"``. Kept in step with kobo's
#: ``_LANGUAGE_NAMES``; the field is only ever populated from a live page value.
LANGUAGE_NAMES: Dict[str, str] = {
    "en": "English", "fr": "French", "de": "German", "es": "Spanish",
    "it": "Italian", "nl": "Dutch", "pt": "Portuguese", "ja": "Japanese",
    "zh": "Chinese", "ko": "Korean", "ru": "Russian", "sv": "Swedish",
    "no": "Norwegian", "nb": "Norwegian", "da": "Danish", "fi": "Finnish",
    "pl": "Polish", "cs": "Czech", "tr": "Turkish", "ar": "Arabic",
    "he": "Hebrew", "hi": "Hindi", "el": "Greek", "hu": "Hungarian",
    "ro": "Romanian", "uk": "Ukrainian", "ca": "Catalan", "eu": "Basque",
    "gl": "Galician", "id": "Indonesian", "th": "Thai", "vi": "Vietnamese",
}

#: A second record is treated as another edition of the same book above this.
SIBLING_SCORE = 0.75
#: ...and only when the author line really matches too.
SIBLING_AUTHOR_RATIO = 0.85

#: ``"<Full Title> by <Author> - BookBub"`` -- the one field BookBub renders
#: server-side, used as the last-resort title/author source.
PAGE_TITLE_RE = re.compile(r"^(?P<title>.+?)\s+by\s+(?P<author>.+?)\s+-\s+BookBub\s*$")

#: BookBub's 404 page, which returns 200 to a browser.
NOT_FOUND_MARKERS = ("page not found", "page doesn't exist", "page does not exist")

#: Body markers from the Cloudflare managed-challenge interstitial. The status
#: code / ``cf-mitigated`` header half of the signature is handled by
#: :meth:`~bookscraper.http_client.HttpClient.block_reason`, because the shared
#: client refuses to hand a 403 body back to adapters at all.
CF_CHALLENGE_MARKERS = (
    "just a moment...",
    "window._cf_chl_opt",
    "__cf_chl_tk",
    "challenges.cloudflare.com",
    "/cdn-cgi/challenge-platform/",
    "enable javascript and cookies to continue",
)

#: Trailing ``-2026-04-09`` deal-date suffix BookBub appends to some slugs.
SLUG_DATE_SUFFIX_RE = re.compile(r"-\d{4}-\d{2}-\d{2}$")
#: ``/books/<slug>`` inside an href.
BOOK_HREF_RE = re.compile(r"/books/([a-z0-9][a-z0-9\-]*)", re.IGNORECASE)
#: Slugs that are routes, not books.
NON_BOOK_SLUGS = frozenset({"index", "search", "new-releases", "deals"})

#: ``alt="Book cover for <Title> by <Authors>"`` on every cover ``<img>``.
COVER_ALT_RE = re.compile(r"^\s*Book cover for\s+(?P<title>.+?)\s+by\s+(?P<author>.+?)\s*$",
                          re.IGNORECASE)

#: Splits a title away from its subtitle.
SUBTITLE_SPLIT_RE = re.compile(r"\s*(?::|\s[-–—]\s|\(|\[|;)\s*")

#: Cloudinary version segment (``v1732580509``) and a filename-with-extension.
CLOUDINARY_VERSION_RE = re.compile(r"^v\d+$")
CLOUDINARY_FILE_RE = re.compile(r"\.[A-Za-z0-9]{2,5}$")

#: Detail labels we hunt for even though no run so far has found BookBub
#: publishing them. :meth:`BookBubSource._detail_pairs` reads whatever
#: label/value rows the panel does render, so any of these starts working the day
#: BookBub adds it -- and the per-run warnings state only what that run saw.
PUBLISHER_LABELS = ("publisher", "published by", "imprint")
DATE_LABELS = (
    "publication date", "published", "publish date", "release date",
    "first published", "pub date", "on sale date",
)
ORIGIN_LABELS = ("country", "country of origin", "place of publication",
                 "published in", "origin")
#: ``Publisher Description`` is a section heading, never a publisher name.
LABEL_VALUE_DENYLIST = ("description", "descriptions")

#: BookBub's ``tags`` list mixes real subject tags ("Literary Fiction", "Ohio")
#: with pure marketing labels ("New York Times Bestselling Author",
#: "Noteworthy"). The latter are not genres, so they are filtered out -- and the
#: adapter warns with the count so nothing is silently dropped. BookBub's
#: ``dealsCategories`` are its own curated categories and are never filtered.
MARKETING_TAG_RE = re.compile(
    r"bestselling author"
    r"|authors and books"
    r"|^noteworthy$"
    r"|\b(?:new york times|usa today|wall street journal|sunday times"
    r"|publishers weekly|amazon charts)\b",
    re.IGNORECASE,
)

#: Where a language could plausibly be declared, most explicit first.
LANGUAGE_META_KEYS = ("books:language", "book:language", "inLanguage",
                      "og:book:language", "language")


@dataclass
class _Record:
    """One fetched BookBub book page, parsed and scored."""

    url: str
    slug: str
    soup: BeautifulSoup
    data: Dict[str, Any] = field(default_factory=dict)
    title: Optional[str] = None
    authors: List[str] = field(default_factory=list)
    score: float = 0.0
    title_ratio: float = 0.0
    author_ratio: float = 0.0
    source_layer: str = "book-json"


class BookBubSource(BaseSource):
    """Scrape one book from BookBub. Needs a real browser; degrades cleanly."""

    name = "bookbub"
    display_name = "BookBub"
    prefers_browser = True

    #: On by default, like every other adapter.
    #:
    #: It was briefly off, on the grounds that :data:`BOOK_JSON_CSS` had gone from
    #: live pages so every render burned the full ``RENDER_WAIT_SECONDS``. That
    #: diagnosis was wrong: the check had been made against a **404 page** (a slug
    #: guess that missed), where of course no book payload exists. On a page that
    #: resolves, ``[data-book-json]`` is present and parses -- verified directly
    #: against ``/books/happy-endings-are-all-alike-by-sandra-scoppettone``.
    #:
    #: What is true is that BookBub is the most expensive source per book, because
    #: it indexes no ISBN and has no anonymous search route, so discovery probes
    #: candidate slugs (see :meth:`_resolve`). It is also the thinnest: a *deals*
    #: site listing a fraction of published books, so a miss is usually the
    #: catalogue rather than the scraper, and ~3/7 fields is its honest ceiling
    #: even on a hit. Both are reported per run in ``metrics/`` rather than hidden
    #: behind a disabled flag.
    enabled_by_default = True

    def __init__(self, client: HttpClient) -> None:
        super().__init__(client)
        #: slug -> parsed record, so a candidate is never fetched twice.
        self._records: Dict[str, _Record] = {}
        self._fetches = 0
        self._resolved: Optional[_Record] = None
        self._siblings: List[_Record] = []
        #: warnings raised during resolution, folded into the ScrapeResult later.
        self._pending_warnings: List[str] = []
        #: Whether the detail panel node was located at all on the last parse.
        #: ``False`` means selector decay, which must be reported as such rather
        #: than as "BookBub publishes no such field".
        self._detail_panel_found: bool = True
        self._seed_title: Optional[str] = None
        self._seed_authors: List[str] = []

    # ------------------------------------------------------------------ public

    def find_book_url(self, hint: BookHint) -> Optional[str]:
        """Resolve ``hint`` to a BookBub ``/books/<slug>`` URL, or ``None``.

        BookBub carries no ISBN, so resolution is title+author only: Path A
        constructs canonical slugs, Path B harvests the author's listing page.
        The winning page is cached, so :meth:`scrape` does not re-fetch it.
        """
        record = self._resolve(hint)
        return record.url if record is not None else None

    def _scrape_into(self, hint: BookHint, result: ScrapeResult) -> None:
        """Do the actual work; :meth:`scrape` owns the safety net."""
        if not self.client.browser_available:
            result.warn(
                "BookBub needs a real browser (Cloudflare managed challenge + a "
                "client-side rendered book body); browser mode is disabled or no "
                "driver is available, so nothing could be scraped"
            )
            print('warning: No browser available; BookBub cannot be scraped', file=sys.stderr)
            return

        record = self._resolve(hint)
        for message in self._pending_warnings:
            result.warn(message)

        if record is None:
            print('warning: Could not resolve %s to a BookBub book page' % (hint.isbn13,), file=sys.stderr)
            return

        result.book_url = record.url
        print('Using BookBub record %s (score %.2f)' % (record.url, record.score), file=sys.stderr)

        genres = self._extract_genres(record, result)
        blurb = self._extract_blurb(record, result)
        covers = self._extract_covers(record, result)
        reviews = self._extract_reviews(record, result)

        result.genres = genres
        result.blurb = blurb or None
        result.cover_urls = covers
        result.reviews = reviews

        metadata = self._build_metadata(hint, record, genres, result)
        result.metadata = metadata

        if metadata is not None and metadata.title:
            result.hint_updates = BookHint(
                isbn13=hint.isbn13,
                isbn10=hint.isbn10,
                title=metadata.title,
                authors=list(metadata.authors),
            )
        return

    # -------------------------------------------------------------- resolution

    def _resolve(self, hint: BookHint) -> Optional[_Record]:
        """Run Path A then Path B; cache and return the best-matching record."""
        if self._resolved is not None:
            return self._resolved

        title, authors = self._seed(hint)
        if not title:
            self._warn_later(
                "cannot search BookBub: it indexes no ISBN and offers no anonymous "
                "search route, and neither the shared hint nor Open Library could "
                "resolve this ISBN to a title/author"
            )
            return None

        best: Optional[_Record] = None

        # -- Path A: construct the canonical slug ----------------------------
        for slug in self._slug_candidates(title, authors)[:MAX_SLUG_CANDIDATES]:
            record = self._fetch_record(slug)
            if record is None:
                continue
            self._score_record(record, title, authors)
            best = self._better(best, record)
            if self._is_strong(record):
                break

        # -- Path B: harvest the author's listing page -----------------------
        if best is None or not self._is_strong(best):
            for slug in self._author_page_candidates(title, authors):
                record = self._fetch_record(slug)
                if record is None:
                    continue
                self._score_record(record, title, authors)
                best = self._better(best, record)
                if self._is_strong(record):
                    break

        if best is None:
            self._warn_later(
                f"no BookBub book page could be located for {title!r} "
                f"by {', '.join(authors) or 'unknown author'}"
            )
            return None

        if best.score < MIN_ACCEPT_SCORE:
            self._warn_later(
                f"rejected best BookBub candidate {best.url} "
                f"(title {best.title!r} by {', '.join(best.authors) or '?'}) because it "
                f"matched the requested book too weakly (score {best.score:.2f} < "
                f"{MIN_ACCEPT_SCORE:.2f}); refusing to scrape a different book"
            )
            return None

        self._warn_later(
            "BookBub publishes no ISBN, so this page was accepted on a title+author "
            f"match only (title similarity {best.title_ratio:.2f}, author similarity "
            f"{best.author_ratio:.2f} against {title!r})"
        )
        if best.score < CONFIDENT_SCORE:
            self._warn_later(
                f"acceptance was FUZZY: BookBub returned {best.title!r} by "
                f"{', '.join(best.authors) or '?'} for requested {title!r} by "
                f"{', '.join(authors) or '?'} (score {best.score:.2f}); the edition may "
                "differ from the one the ISBN identifies"
            )

        self._siblings = [
            record for record in self._records.values()
            if record is not best
            and record.score >= SIBLING_SCORE
            and record.author_ratio >= SIBLING_AUTHOR_RATIO
        ]
        if self._siblings:
            self._warn_later(
                "BookBub keeps one record per edition; also matched "
                + ", ".join(f"{r.title!r} ({r.slug})" for r in self._siblings)
                + " and included their cover images as additional editions"
            )

        self._resolved = best
        return best

    def _seed(self, hint: BookHint) -> Tuple[Optional[str], List[str]]:
        """The ``(title, authors)`` the slugs are built from. Memoised.

        BookBub resolves inside :meth:`find_book_url`, which has no
        :class:`ScrapeResult` to warn into yet, so this cannot use the shared
        :meth:`~bookscraper.base.BaseSource.search_terms` -- it queues the note via
        ``_warn_later`` instead. The ISBN lookup itself is the shared one.

        The subtitle is kept, unlike Kobo and Audible: BookBub's own slugs include
        it (``/books/the-nightingale-a-novel-by-kristin-hannah``), so dropping it
        would build a URL that 404s.
        """
        if self._seed_title is not None or self._seed_authors:
            return self._seed_title, self._seed_authors

        title = self.clean_text(hint.title) or None
        authors = [a for a in (self.clean_text(a) for a in hint.authors or []) if a]
        if title and authors:
            self._seed_title, self._seed_authors = title, authors
            return title, authors

        seeded, subtitle, seeded_authors = self.seed_from_openlibrary(hint)
        if seeded and subtitle:
            seeded = f"{seeded}: {subtitle}"
        if seeded or seeded_authors:
            self._warn_later(
                "BookBub cannot be searched by ISBN and no title/author hint was "
                f"seeded, so the slug seed ({seeded!r} by "
                f"{', '.join(seeded_authors) or '?'}) was resolved from Open Library; "
                "only the URL was derived from it, never any metadata value below"
            )
        self._seed_title = title or seeded
        self._seed_authors = authors or seeded_authors
        return self._seed_title, self._seed_authors

    # ------------------------------------------------------------ slug helpers

    @staticmethod
    def _slugify(text: Any) -> str:
        """Turn ``"Reese's Book Club"`` into ``"reese-s-book-club"``.

        Matches BookBub's own convention, verified against live slugs: accents
        folded, ``&`` spelled out, every other non-alphanumeric run (apostrophes
        included -- ``isn't`` becomes ``isn-t``) collapsed to a single hyphen.
        """
        raw = "" if text is None else str(text)
        folded = unicodedata.normalize("NFKD", raw)
        folded = folded.encode("ascii", "ignore").decode("ascii")
        folded = folded.replace("&", " and ")
        slug = re.sub(r"[^A-Za-z0-9]+", "-", folded).strip("-").lower()
        return re.sub(r"-{2,}", "-", slug)

    def _title_variants(self, title: str) -> List[str]:
        """Title spellings worth trying, most specific first."""
        variants: List[str] = []

        def add(value: Optional[str]) -> None:
            text = self.clean_text(value)
            if text and text not in variants:
                variants.append(text)

        add(title)
        head = SUBTITLE_SPLIT_RE.split(title, maxsplit=1)[0]
        add(head)
        words = head.split()
        if len(words) > 3:
            add(" ".join(words[:3]))
        if len(words) > 1 and len(words[0]) >= 4:
            add(words[0])
        return variants

    def _author_variants(self, authors: Sequence[str]) -> List[str]:
        """Author spellings worth trying: first author, then BookBub's ``-and-`` join."""
        cleaned = [self.clean_text(a) for a in authors or []]
        cleaned = [a for a in cleaned if a]
        variants: List[str] = []
        if cleaned:
            variants.append(cleaned[0])
        if len(cleaned) > 1:
            joined = " and ".join(cleaned[:2])
            if joined not in variants:
                variants.append(joined)
        return variants

    def _slug_candidates(self, title: str, authors: Sequence[str]) -> List[str]:
        """Path A: ``<title-slug>-by-<author-slug>`` for each title/author pairing."""
        author_variants = self._author_variants(authors)
        if not author_variants:
            self._warn_later(
                "no author is known, so BookBub's '<title>-by-<author>' slug cannot be "
                "constructed; falling back to title-only candidates, which rarely exist"
            )
        candidates: List[str] = []
        for title_variant in self._title_variants(title):
            title_slug = self._slugify(title_variant)
            if not title_slug:
                continue
            if not author_variants:
                if title_slug not in candidates:
                    candidates.append(title_slug)
                continue
            for author_variant in author_variants:
                author_slug = self._slugify(author_variant)
                slug = f"{title_slug}-by-{author_slug}" if author_slug else title_slug
                if slug not in candidates:
                    candidates.append(slug)
        return candidates

    def _author_page_candidates(self, title: str, authors: Sequence[str]) -> List[str]:
        """Path B: harvest ``/authors/<slug>`` and score its book links offline.

        BookBub's author listing exposes the slug vocabulary that guessing cannot
        reach (dated deal slugs, "Tenth Anniversary Edition", ...). Titles come
        from each cover's ``alt`` text, so scoring costs no extra requests.
        """
        author_variants = self._author_variants(authors)
        if not author_variants:
            return []

        author_slug = self._slugify(author_variants[0])
        if not author_slug:
            return []

        url = AUTHOR_URL.format(slug=author_slug)
        soup = self._render(url, AUTHOR_WAIT_CSS, scroll_passes=AUTHOR_SCROLL_PASSES)
        if soup is None:
            self._warn_later(
                f"could not read BookBub's author listing {url}; slug construction was "
                "the only discovery path available"
            )
            return []
        if self._is_not_found(soup):
            self._warn_later(
                f"BookBub has no author page at {url}, so the author-listing "
                "discovery path could not be used"
            )
            return []

        harvested: List[Tuple[float, str, str]] = []
        seen: set = set()
        for anchor in soup.select(AUTHOR_LINK_CSS):
            href = anchor.get("href") or ""
            match = BOOK_HREF_RE.search(str(href))
            if match is None:
                continue
            slug = match.group(1).lower()
            if slug in seen or slug in NON_BOOK_SLUGS or slug in self._records:
                continue
            seen.add(slug)
            link_title, _link_author = self._title_from_anchor(anchor, slug)
            ratio = self._ratio(link_title, title)
            harvested.append((ratio, slug, link_title))

        if not harvested:
            self._warn_later(
                f"BookBub's author page {url} rendered no /books/ links, so it could "
                "not help resolve the book"
            )
            return []

        harvested.sort(key=lambda item: item[0], reverse=True)
        chosen = harvested[:MAX_AUTHOR_PAGE_CANDIDATES]
        self._warn_later(
            "slug construction did not find a confident match, so candidates were "
            "taken from BookBub's author listing: "
            + ", ".join(f"{slug} ({name!r}, {ratio:.2f})" for ratio, slug, name in chosen)
        )
        return [slug for _ratio, slug, _name in chosen]

    def _title_from_anchor(self, anchor: Tag, slug: str) -> Tuple[str, str]:
        """Best-effort ``(title, author)`` for one author-page book link."""
        image = anchor.find("img")
        if isinstance(image, Tag):
            match = COVER_ALT_RE.match(self.clean_text(image.get("alt") or ""))
            if match is not None:
                return match.group("title"), match.group("author")
        text = self.clean_text(anchor.get_text(" "))
        if text:
            return text, ""
        # Fall back to the slug itself: "sapiens-by-x-2026-04-09" -> "sapiens".
        bare = SLUG_DATE_SUFFIX_RE.sub("", slug)
        head = re.split(r"-by-", bare, maxsplit=1)[0]
        return head.replace("-", " "), ""

    # ------------------------------------------------------------------ fetching

    def _fetch_record(self, slug: str) -> Optional[_Record]:
        """Fetch and parse ``/books/<slug>``, honouring the fetch budget."""
        if not slug:
            return None
        if slug in self._records:
            return self._records[slug]
        if self._fetches >= MAX_BOOK_FETCHES:
            if verbose():
                print('  Fetch budget of %d book pages exhausted; skipping %s' % (MAX_BOOK_FETCHES, slug), file=sys.stderr)
            return None

        url = BOOK_URL.format(slug=slug)
        self._fetches += 1
        soup = self._fetch_soup(url)
        if soup is None:
            return None
        if self._is_not_found(soup):
            if verbose():
                print('  BookBub has no book at %s (404 page rendered)' % (url,), file=sys.stderr)
            return None

        record = self._parse_record(url, slug, soup)
        if record is None:
            self._warn_later(
                f"{url} rendered but carried neither a {BOOK_JSON_ATTR} payload, a "
                "JSON-LD Book block, a book heading nor a usable <title>, so it could "
                "not be identified"
            )
            return None
        self._records[slug] = record
        return record

    def _fetch_soup(self, url: str) -> Optional[BeautifulSoup]:
        """Fetch a BookBub page. Browser only -- plain HTTP cannot reach this site.

        There used to be a plain-``requests`` probe here first, on the reasoning that
        Cloudflare's aggressiveness is IP- and geo-sensitive so it was worth checking
        rather than assuming. Two things made that a bad deal:

        * it never worked. Measured over book pages and an author listing page:
          **0 of 3** got through, every one answered HTTP 403 with a
          ``Just a moment...`` interstitial of ~6 kB. The challenge keys off the TLS
          fingerprint, so no header tuning changes the outcome.
        * the pipeline builds a **new adapter per book**, so the "probe once" flag
          reset every time -- one guaranteed-403 request plus its courtesy delay for
          each of the ~10 000 books, and nothing to show for any of them.

        Even where plain HTTP does get through, the page body is client-side
        rendered (:data:`BOOK_JSON_CSS` is injected by JavaScript), so it would have
        needed the browser regardless. That is the whole justification for Selenium
        here, and it is why one path is enough. A block met by the browser is still
        recorded honestly by :meth:`_render`, and no CAPTCHA is ever attempted.
        """
        return self._render(url, BOOK_JSON_CSS)



    def _render(self, url: str, wait_css: str,
                scroll_passes: int = 0) -> Optional[BeautifulSoup]:
        """Browser fetch with block detection on the rendered DOM."""
        soup = self.client.get_rendered_soup(
            url, wait_css=wait_css, wait_seconds=RENDER_WAIT_SECONDS,
            scroll_passes=scroll_passes,
        )
        if soup is None:
            reason = self.client.block_reason(url)
            if reason:
                self._warn_later(
                    f"browser fetch of {url} was blocked by bot protection ({reason}); "
                    "no attempt was made to defeat it"
                )
            else:
                self._warn_later(f"browser could not render {url}")
            return None
        if self._is_cf_challenge(soup):
            self._warn_later(
                f"the browser was served Cloudflare's managed challenge for {url} "
                "instead of the page; not parsing it and not attempting to solve it"
            )
            return None
        return soup

    @staticmethod
    def _document_text(soup: BeautifulSoup, limit: int = 20000) -> str:
        """Lower-cased head of the raw markup, for marker sniffing."""
        try:
            return str(soup)[:limit].lower()
        except (ValueError, RuntimeError):
            return ""

    def _is_cf_challenge(self, soup: BeautifulSoup) -> bool:
        """True when this DOM is Cloudflare's interstitial rather than a page."""
        head = self._document_text(soup)
        return any(marker in head for marker in CF_CHALLENGE_MARKERS)

    def _is_not_found(self, soup: BeautifulSoup) -> bool:
        """True for BookBub's soft 404 (it answers a browser with HTTP 200)."""
        title = self.clean_text(soup.title.get_text() if soup.title else "").lower()
        if any(marker in title for marker in NOT_FOUND_MARKERS):
            return True
        heading = (self.select_text(soup, "h1") or "").lower()
        return any(marker in heading for marker in NOT_FOUND_MARKERS)

    # ------------------------------------------------------------------ parsing

    def _parse_record(self, url: str, slug: str,
                      soup: BeautifulSoup) -> Optional[_Record]:
        """Identify the book on a rendered page, layering four fallbacks."""
        data = self._book_json(soup)
        layer = "book-json"

        title = self.clean_text(data.get("title")) or None
        authors = self.split_list(data.get("authors"))

        if not title:
            for blob in self.jsonld(soup, want_type="Book"):
                candidate = self.clean_text(blob.get("name")) or None
                if candidate:
                    title = candidate
                    authors = authors or self.split_list(blob.get("author"))
                    layer = "json-ld"
                    break

        if not title:
            title = self.select_text(soup, ".book-info-header h1.book-info-title",
                                     "h1.book-info-title", ".book-info-title")
            if title:
                layer = "css"

        if not authors:
            authors = self.split_list(self.select_texts(
                soup,
                ".book-info-authors .person-name a",
                ".book-info-authors .person-name",
                ".book-info-authors",
            ))
            if authors and layer == "book-json":
                layer = "css"

        if not title or not authors:
            page_title = self.clean_text(soup.title.get_text() if soup.title else "")
            match = PAGE_TITLE_RE.match(page_title) or PAGE_TITLE_RE.match(
                self.meta(soup, "og:title", "twitter:title") or ""
            )
            if match is not None:
                title = title or self.clean_text(match.group("title"))
                authors = authors or self.split_list(match.group("author"))
                layer = "page-title"

        if not title:
            return None

        return _Record(url=url, slug=slug, soup=soup, data=data, title=title,
                       authors=authors, source_layer=layer)

    def _book_json(self, soup: BeautifulSoup) -> Dict[str, Any]:
        """Decode the ``data-book-json`` attribute; ``{}`` when absent or broken."""
        node = soup.select_one(BOOK_JSON_CSS)
        if node is None:
            return {}
        raw = node.get(BOOK_JSON_ATTR)
        if not raw:
            return {}
        payload = self._loads_lenient(str(raw))
        if isinstance(payload, dict):
            return payload
        if verbose():
            print('  %s was not a JSON object' % (BOOK_JSON_ATTR,), file=sys.stderr)
        return {}

    # ------------------------------------------------------------------ scoring

    @staticmethod
    def _normalise(text: Any) -> str:
        """Casefolded, punctuation-free form used for comparisons."""
        raw = "" if text is None else str(text)
        folded = unicodedata.normalize("NFKD", raw).encode("ascii", "ignore").decode()
        folded = re.sub(r"[^A-Za-z0-9]+", " ", folded).strip().lower()
        return re.sub(r"\s+", " ", folded)

    def _ratio(self, left: Any, right: Any) -> float:
        """Similarity in ``0.0..1.0``, with a containment bonus for subtitles.

        ``"Sapiens"`` versus ``"Sapiens: A Brief History of Humankind"`` are the
        same work under two BookBub edition records, so containment must not read
        as a mismatch -- but it must stay below an exact match so the closer
        record still wins.

        The bonus is **asymmetric**, mirroring Audible's ``_title_score``. Only
        ``left`` (the candidate) being the *shorter* string earns the full 0.85;
        the candidate being *longer* is also the shape of a sequel
        ("Dune" -> "Dune Messiah"), which used to score 0.85 here, combine with a
        perfect author match to 0.91, clear ``CONFIDENT_SCORE`` (0.90) and be
        accepted with **no fuzzy-match warning at all**. That direction is now
        capped below ``CONFIDENT_SCORE`` so such a record can still win if it is
        genuinely the only candidate, but never silently.
        """
        a, b = self._normalise(left), self._normalise(right)
        if not a or not b:
            return 0.0
        if a == b:
            return 1.0
        ratio = difflib.SequenceMatcher(None, a, b).ratio()
        if self.is_sequel_pair(a, b):
            # "ready player one" vs "ready player two": raw ratio is ~0.94, so an
            # explicit veto is the only thing that keeps a sequel out.
            return 0.0
        if a in b:
            # Candidate is the shorter form (BookBub dropped the subtitle).
            ratio = max(ratio, 0.85)
        elif b in a:
            ratio = min(max(ratio, 0.85), _ADDITIVE_CONTAINMENT_CEILING)
        return ratio

    def _score_record(self, record: _Record, title: str,
                      authors: Sequence[str]) -> None:
        """Attach title/author similarity and a weighted combined score."""
        record.title_ratio = self._ratio(record.title, title)
        wanted = " ".join(authors or [])
        got = " ".join(record.authors or [])
        record.author_ratio = self._ratio(got, wanted) if wanted else 0.0
        if not wanted:
            record.score = record.title_ratio
        else:
            record.score = 0.6 * record.title_ratio + 0.4 * record.author_ratio
        if verbose():
            print('  Candidate %s: title=%.2f author=%.2f score=%.2f' % (record.slug, record.title_ratio, record.author_ratio, record.score), file=sys.stderr)

    def _is_strong(self, record: _Record) -> bool:
        """Only an (almost) exact title plus a matching author stops the search."""
        return (
            record.title_ratio >= STRONG_TITLE_RATIO
            and (record.author_ratio >= SIBLING_AUTHOR_RATIO or not self._seed_authors)
        )

    @staticmethod
    def _better(current: Optional[_Record], candidate: _Record) -> _Record:
        if current is None or candidate.score > current.score:
            return candidate
        return current

    # ------------------------------------------------------------- field layers

    def _extract_genres(self, record: _Record, result: ScrapeResult) -> List[str]:
        """BookBub deal categories plus tags, in that order, deduped.

        Only the *book's own* record is used. The visible ``a.category-name``
        links belong to the "Deals in similar categories" carousel -- i.e. to
        other books -- so they are deliberately never read.
        """
        genres: List[str] = []
        for category in record.data.get("dealsCategories") or []:
            if isinstance(category, dict):
                name = self.clean_text(category.get("displayName")
                                       or category.get("partnerName"))
                if name:
                    genres.append(name)

        dropped: List[str] = []
        for tag in record.data.get("tags") or []:
            if isinstance(tag, dict):
                name = self.clean_text(tag.get("displayName"))
                if not name:
                    continue
                if MARKETING_TAG_RE.search(name):
                    dropped.append(name)
                    continue
                genres.append(name)
        if dropped:
            result.warn(
                f"dropped {len(dropped)} BookBub marketing tag(s) that are not genres: "
                + ", ".join(sorted(dropped))
            )

        if not genres:
            for blob in self.jsonld(record.soup, want_type="Book"):
                genres.extend(self.split_list(blob.get("genre")))
            if genres:
                result.warn("genres fell back to the JSON-LD 'genre' field")

        if not genres:
            result.warn(
                "no genres found: BookBub's category/tag list lives only in the "
                f"{BOOK_JSON_ATTR} payload, and the visible category links belong to "
                "the recommendation carousel (other books), so they were not used"
            )
        return self.dedupe(genres)

    def _extract_blurb(self, record: _Record, result: ScrapeResult) -> str:
        """Publisher description, then BookBub's editorial blurb, then meta tags."""
        raw = record.data.get("description")
        blurb = self.html_to_text(raw) if raw else ""

        if not blurb:
            editorial = record.data.get("blurb")
            blurb = self.html_to_text(editorial) if editorial else ""
            if blurb:
                result.warn(
                    "publisher description was empty; used BookBub's own editorial "
                    "blurb instead"
                )

        if not blurb:
            for blob in self.jsonld(record.soup, want_type="Book"):
                blurb = self.html_to_text(blob.get("description"))
                if blurb:
                    result.warn("blurb fell back to the JSON-LD 'description' field")
                    break

        if not blurb:
            blurb = self.html_to_text(self.select_text(
                record.soup,
                ".book-info-body .expandable-text-description",
                ".expandable-text-description",
                ".expandable-text-rendered",
            ))
            if blurb:
                result.warn("blurb fell back to the on-page description element")

        if not blurb:
            blurb = self.clean_text(self.meta(record.soup, "og:description",
                                              "twitter:description", "description"))
            if blurb:
                result.warn(
                    "blurb fell back to the og:description meta tag, which BookBub "
                    "truncates"
                )

        if not blurb:
            result.warn("no blurb/description could be extracted from the BookBub page")
        else:
            blurb = self._sanity_check_blurb(blurb, result)
        return blurb

    def _sanity_check_blurb(self, blurb: str, result: ScrapeResult) -> str:
        """Detect and trim an upstream-duplicated description.

        BookBub's own ``data-book-json.description`` is sometimes served with the
        text repeated two or three times, restarting **mid-word** ("...millions of
        readers." then "om playing the stock market...", i.e. "fr|om"). The
        corruption is upstream, but writing a 4,355-character blurb with broken
        sentences and *no warning* -- when every other source gives ~1,700 -- makes
        a downstream consumer think it is legitimate. So the repetition is
        detected, the blurb truncated at the first repeat, and the anomaly named.
        """
        paragraphs = [p.strip() for p in blurb.split("\n") if p.strip()]
        if len(paragraphs) < 2:
            return blurb

        # A repeated leading paragraph is the clearest signal, and the one the
        # observed corruption produces.
        first = paragraphs[0]
        if len(first) >= 40:
            repeat_at = blurb.find(first, len(first))
            if repeat_at != -1:
                trimmed = blurb[:repeat_at].strip()
                result.warn(
                    f"BookBub's description field was corrupt: its opening paragraph "
                    f"recurs at offset {repeat_at} of {len(blurb)} characters, i.e. the "
                    f"text is duplicated upstream. Truncated at the first repetition "
                    f"({len(trimmed)} chars kept of {len(blurb)}); the discarded tail "
                    f"may also restart mid-word"
                )
                print('warning: Truncated a duplicated BookBub description from %d to %d chars' % (len(blurb), len(trimmed)), file=sys.stderr)
                return trimmed

        # Weaker signal: any paragraph appearing more than once verbatim.
        seen: Dict[str, int] = {}
        for paragraph in paragraphs:
            if len(paragraph) >= 60:
                seen[paragraph] = seen.get(paragraph, 0) + 1
        repeated = [p for p, n in seen.items() if n > 1]
        if repeated:
            result.warn(
                f"BookBub's description repeats {len(repeated)} paragraph(s) verbatim, "
                "which suggests the field is duplicated upstream; the text was left "
                "intact but should not be trusted as a clean blurb"
            )
        return blurb

    def _extract_covers(self, record: _Record, result: ScrapeResult) -> List[str]:
        """Cover URLs at original Cloudinary resolution, one per distinct image.

        BookBub serves covers through Cloudinary with a transformation segment
        (``.../upload/t_ci_ar_6:9_padded,...,w_405/v1732580509/pro_pbid_189462.jpg``).
        Dropping that segment returns the untransformed original, which is the
        largest available rendition -- verified live at 36 KB versus 21 KB for the
        ``w_405`` variant. Records that matched the same book (BookBub keeps one
        record per edition) contribute additional numbered covers.
        """
        candidates: List[Tuple[str, str]] = []

        def add(url: Any, note: str) -> None:
            absolute = self.absolutise(record.url, url)
            if absolute:
                candidates.append((absolute, note))

        for source in [record] + list(self._siblings):
            add(source.data.get("coverUrl"), "book-json")
            if source is record:
                add(self.meta(source.soup, "og:image", "twitter:image"), "og:image")
                add(self._dom_cover(source), "dom")
            else:
                add(self._dom_cover(source), "dom")

        covers: List[str] = []
        keys: set = set()
        used_fallback = False
        for url, note in candidates:
            original, key = self._cloudinary_original(url)
            if not key or key in keys:
                continue
            keys.add(key)
            covers.append(original)
            if note != "book-json":
                used_fallback = True

        if used_fallback and covers:
            result.warn(
                f"at least one cover URL came from a fallback layer rather than the "
                f"{BOOK_JSON_ATTR} 'coverUrl' field"
            )
        if not covers:
            result.warn(
                "no cover image URL found: neither the "
                f"{BOOK_JSON_ATTR} 'coverUrl' field, the og:image meta tag nor "
                "img.book-cover-image were present"
            )
        return covers

    def _dom_cover(self, record: _Record) -> Optional[str]:
        """The book's *own* cover ``<img>``, never a recommendation-carousel one.

        Every cover image carries ``alt="Book cover for <Title> by <Authors>"``,
        so the book's own image can be picked by matching the alt text against
        the title we resolved. Only if that fails do we take the first
        ``img.book-cover-image`` in document order (which is the hero cover on
        every page observed).
        """
        images = record.soup.select("img.book-cover-image, .cover-image img")
        wanted = self._normalise(record.title)
        for image in images:
            match = COVER_ALT_RE.match(self.clean_text(image.get("alt") or ""))
            if match is None:
                continue
            if wanted and self._normalise(match.group("title")) == wanted:
                return image.get("src") or image.get("data-src")
        for image in images:
            src = image.get("src") or image.get("data-src")
            if src:
                return str(src)
        return None

    @staticmethod
    def _cloudinary_original(url: str) -> Tuple[str, str]:
        """Return ``(original_resolution_url, dedupe_key)`` for a Cloudinary URL.

        Non-Cloudinary URLs pass through unchanged with the whole URL as the key.
        """
        head, separator, tail = url.partition("/upload/")
        if not separator:
            return url, url
        segments = [segment for segment in tail.split("/") if segment]
        if not segments:
            return url, url
        start = 0
        for index, segment in enumerate(segments):
            if CLOUDINARY_VERSION_RE.match(segment) or CLOUDINARY_FILE_RE.search(segment):
                start = index
                break
        kept = segments[start:]
        return head + "/upload/" + "/".join(kept), kept[-1]

    def _extract_reviews(self, record: _Record, result: ScrapeResult) -> List[ReviewItem]:
        """Collect review text, then report the real ceiling -- never padded.

        BookBub exposes only *aggregate* ratings to anonymous clients: there is no
        ``/books/<slug>/reviews`` route (it 404s), no reviews XHR is issued during
        a full render, scrolling reveals nothing, and the string "review" appears
        on a book page only inside publisher prose. So the honest ceiling here is
        zero and this method says so with the real numbers.

        The two extraction layers below read *standards* (schema.org ``Review``
        and BookBub's own ``data-*-json`` attribute convention) rather than
        invented CSS selectors, so they will start working the day BookBub
        renders member reviews -- and the pagination loop then walks ``?page=N``
        until the target is met.
        """
        target = max(0, int(self.min_reviews or 0))
        cap = self.max_reviews if self.max_reviews and self.max_reviews > 0 else None

        reviews: List[ReviewItem] = []
        seen: set = set()

        def absorb(soup: BeautifulSoup) -> int:
            added = 0
            for item in self._reviews_from_jsonld(soup) + self._reviews_from_json_attrs(soup):
                key = self._normalise(item.text)[:400]
                if not key or key in seen:
                    continue
                seen.add(key)
                reviews.append(item)
                added += 1
                if cap is not None and len(reviews) >= cap:
                    break
            return added

        absorb(record.soup)

        # Pagination: only walked when page 1 actually yielded reviews, so a site
        # with no review section costs zero extra requests.
        page = 1
        while reviews and len(reviews) < target and page < 20:
            if cap is not None and len(reviews) >= cap:
                break
            page += 1
            soup = self._render(f"{record.url}?page={page}", BOOK_JSON_CSS)
            if soup is None:
                result.warn(f"review page {page} could not be loaded; stopping there")
                break
            if absorb(soup) == 0:
                if verbose():
                    print('  Review page %d added nothing new; stopping' % (page,), file=sys.stderr)
                break

        rating = self._rating_summary(record)
        if not reviews:
            # Only what this run actually observed. The previous wording asserted
            # that "/books/<slug>/reviews returns 404" and that "a full browser
            # render issues no reviews request" -- neither of which this code
            # probes: no /reviews URL is ever built and the browser's network log
            # is never inspected. Background knowledge belongs in the module
            # docstring/README, not in a per-run audit trail.
            result.warn(
                f"0 review texts recovered on this run: no review element was present "
                f"in the rendered book page, and BookBub exposed only its aggregate "
                f"rating ({rating}). Member review text is behind sign-in, which this "
                f"scraper does not attempt. Not padded with anything else (requested "
                f"minimum was {target})"
            )
        elif len(reviews) < target:
            result.warn(
                f"only {len(reviews)} review(s) available on BookBub, below the "
                f"requested minimum of {target}; aggregate rating: {rating}"
            )
        return reviews

    def _rating_summary(self, record: _Record) -> str:
        """Human-readable aggregate rating, from the book JSON then JSON-LD."""
        average = record.data.get("averageRating")
        count = record.data.get("ratingsCount")
        if average is None or count is None:
            for blob in self.jsonld(record.soup, want_type="Book"):
                aggregate = blob.get("aggregateRating")
                if isinstance(aggregate, dict):
                    average = average if average is not None else aggregate.get("ratingValue")
                    count = count if count is not None else aggregate.get("ratingCount")
                    break
        if average is None and count is None:
            return "no aggregate rating published either"
        try:
            average_text = f"{float(average):.2f}" if average is not None else "?"
        except (TypeError, ValueError):
            average_text = self.clean_text(average) or "?"
        return f"average {average_text} from {self.clean_text(count) or '?'} ratings"

    def _reviews_from_jsonld(self, soup: BeautifulSoup) -> List[ReviewItem]:
        """schema.org ``Review`` objects, standalone or nested under the Book."""
        items: List[ReviewItem] = []
        blobs = list(self.jsonld(soup, want_type=("Review", "UserReview")))
        for book in self.jsonld(soup, want_type="Book"):
            nested = book.get("review") or book.get("reviews")
            if isinstance(nested, dict):
                blobs.append(nested)
            elif isinstance(nested, list):
                blobs.extend(item for item in nested if isinstance(item, dict))
        for blob in blobs:
            item = self._review_from_mapping(blob)
            if item is not None:
                items.append(item)
        return items

    def _reviews_from_json_attrs(self, soup: BeautifulSoup) -> List[ReviewItem]:
        """Any ``data-*-json`` attribute carrying a list under a ``review`` key.

        BookBub's own convention is to ship page data in ``data-book-json``; this
        reads the same convention for reviews without guessing CSS class names.
        """
        items: List[ReviewItem] = []
        for node in soup.select("[data-book-json], [data-reviews-json], [data-review-json]"):
            for attribute, raw in (node.attrs or {}).items():
                if not attribute.startswith("data-") or not attribute.endswith("-json"):
                    continue
                if not isinstance(raw, str) or "review" not in raw.lower():
                    continue
                payload = self._loads_lenient(raw)
                for mapping in self._find_review_mappings(payload):
                    item = self._review_from_mapping(mapping)
                    if item is not None:
                        items.append(item)
        return items

    def _find_review_mappings(self, payload: Any, depth: int = 0) -> List[Dict[str, Any]]:
        """Walk decoded JSON for lists hanging off a ``review``-ish key."""
        found: List[Dict[str, Any]] = []
        if depth > 5:
            return found
        if isinstance(payload, dict):
            for key, value in payload.items():
                if "review" in str(key).lower():
                    if isinstance(value, dict):
                        found.append(value)
                    elif isinstance(value, list):
                        found.extend(item for item in value if isinstance(item, dict))
                elif isinstance(value, (dict, list)):
                    found.extend(self._find_review_mappings(value, depth + 1))
        elif isinstance(payload, list):
            for value in payload:
                found.extend(self._find_review_mappings(value, depth + 1))
        return found

    def _review_from_mapping(self, blob: Dict[str, Any]) -> Optional[ReviewItem]:
        """Turn one review-ish mapping into a :class:`ReviewItem`, or ``None``."""
        if not isinstance(blob, dict):
            return None
        body = None
        for key in ("reviewBody", "description", "text", "body", "comment", "content"):
            value = blob.get(key)
            if isinstance(value, str) and value.strip():
                body = value
                break
        if body is None:
            return None

        reviewer = blob.get("author") or blob.get("reviewer") or blob.get("userName")
        if isinstance(reviewer, dict):
            reviewer = reviewer.get("name")

        rating = blob.get("reviewRating") or blob.get("rating")
        if isinstance(rating, dict):
            rating = rating.get("ratingValue")

        return self.make_review(
            body,
            reviewer=reviewer,
            rating=rating,
            date=blob.get("datePublished") or blob.get("date") or blob.get("createdAt"),
            url=blob.get("url"),
            min_chars=2,
        )

    # ------------------------------------------------------------------ metadata

    def _build_metadata(self, hint: BookHint, record: _Record, genres: Sequence[str],
                        result: ScrapeResult) -> Optional[BookMetadata]:
        """Assemble the metadata record, warning honestly about absent fields."""
        metadata = self.new_metadata(hint)
        metadata.title = record.title
        metadata.authors = list(record.authors)
        metadata.genres = list(genres)

        if record.source_layer != "book-json":
            result.warn(
                f"title/authors came from the '{record.source_layer}' fallback layer "
                f"rather than the {BOOK_JSON_ATTR} payload"
            )
        if not metadata.authors:
            result.warn("no author could be parsed from the BookBub page")

        pairs = self._detail_pairs(record.soup, record.url)

        metadata.publisher = self._first_label(pairs, PUBLISHER_LABELS)
        if metadata.publisher:
            result.warn(
                f"publisher {metadata.publisher!r} was read from a BookBub detail row; "
                "BookBub did not previously expose this field, so treat it as new"
            )
        elif not self._detail_panel_found:
            result.warn(
                "publisher unavailable: the book detail panel (.book-panel / "
                ".book-info-body) could not be located on this page, so NO detail row "
                "could be read. This is a selector/layout change, not evidence that "
                "BookBub lacks the field"
            )
        else:
            result.warn(
                "publisher unavailable: BookBub publishes no publisher/imprint field "
                "on a book page (the detail panel was read successfully and carries no "
                "such row; there is no JSON-LD publisher and no meta tag either)"
            )

        metadata.date_of_publication = self._publication_date(record, pairs, result)
        metadata.origin = self._origin(record, pairs, result)
        metadata.language = self._language(record, result)

        if not metadata.genres:
            result.warn("metadata carries no genres")
        return metadata

    def _detail_pairs(self, soup: BeautifulSoup,
                      soup_url: str = "") -> Dict[str, str]:
        """Label -> value pairs from the book panel's detail rows.

        BookBub renders these as ``Length:`` on one line and ``304 Pages`` on the
        next (and occasionally inline as ``Label: value``). Both shapes are read.
        ``Publisher Description`` is a section heading and is filtered out, so it
        can never be mistaken for a publisher name.
        """
        panel = soup.select_one(".book-panel") or soup.select_one(".book-info-body")
        if panel is None:
            # Record that the *panel itself* was missing. Without this the
            # publisher/date/origin warnings below would go on asserting "BookBub
            # publishes no such field", which is a claim about the website -- so a
            # simple CSS rename upstream would be reported to the user as a
            # property of BookBub and a broken parser would look like a healthy run.
            self._detail_panel_found = False
            print('warning: Neither .book-panel nor .book-info-body matched on %s, so no detail rows could be read at all (selector decay, not an absent field)' % (soup_url or 'the book page',), file=sys.stderr)
            return {}
        self._detail_panel_found = True
        text = self.html_to_text(panel)
        lines = [line.strip() for line in text.split("\n") if line.strip()]
        pairs: Dict[str, str] = {}
        for index, line in enumerate(lines):
            if ":" in line:
                label, _, value = line.partition(":")
                label_key = label.strip().lower()
                value = value.strip()
                if not value and index + 1 < len(lines):
                    value = lines[index + 1].strip()
            else:
                continue
            if not label_key or len(label_key) > 40 or not value:
                continue
            if any(bad in label_key for bad in LABEL_VALUE_DENYLIST):
                continue
            if any(bad in value.lower() for bad in LABEL_VALUE_DENYLIST):
                continue
            pairs.setdefault(label_key, value)
        return pairs

    def _first_label(self, pairs: Dict[str, str],
                     labels: Iterable[str]) -> Optional[str]:
        """First non-empty value whose label matches any of ``labels``."""
        for label in labels:
            for key, value in pairs.items():
                if key == label or key.startswith(label):
                    cleaned = self.clean_text(value)
                    if cleaned:
                        return cleaned
        return None

    def _publication_date(self, record: _Record, pairs: Dict[str, str],
                          result: ScrapeResult) -> Optional[str]:
        """Publication date from a detail row, JSON-LD or a meta tag.

        ``blurbDate`` in the book JSON is deliberately **not** used: it is the
        date BookBub wrote its promotional blurb, not a publication date, and
        reporting it would be a fabrication.
        """
        raw = self._first_label(pairs, DATE_LABELS)
        origin_layer = "detail row"

        if not raw:
            for blob in self.jsonld(record.soup, want_type="Book"):
                candidate = blob.get("datePublished") or blob.get("copyrightYear")
                if candidate:
                    raw = str(candidate)
                    origin_layer = "JSON-LD datePublished"
                    break

        if not raw:
            raw = self.meta(record.soup, "book:release_date", "og:book:release_date",
                            "books:release_date", "datePublished")
            if raw:
                origin_layer = "meta tag"

        if not raw:
            result.warn(
                "date_of_publication unavailable: BookBub publishes no publication "
                "date. Its 'blurbDate' field was deliberately ignored because it is "
                "the date of BookBub's promotional blurb, not of publication"
            )
            return None

        value = self.iso_date(raw)
        result.warn(
            f"date_of_publication {value!r} came from the {origin_layer}; BookBub "
            "does not normally expose a publication date"
        )
        return value

    def _origin(self, record: _Record, pairs: Dict[str, str],
                result: ScrapeResult) -> Optional[str]:
        """Country / place of publication, from BookBub's own layers or not at all.

        Two real searches run, in this order: every :data:`ORIGIN_LABELS` row of
        the parsed detail panel, then the shared
        :meth:`~bookscraper.base.BaseSource.probe_origin` over the detail rows,
        the book JSON payload and the page DOM. Either one finding something
        returns a real scraped value, so the field self-heals; until then it is
        ``null``, and "the panel was read and has no such row" is warned
        differently from "the panel itself could not be found", so selector decay
        is never reported as a property of the site.
        """
        value = self._first_label(pairs, ORIGIN_LABELS)
        if value:
            result.warn(f"origin {value!r} was read from a BookBub detail row")
            return value

        layers: List[Tuple[str, Any]] = [
            ("the parsed detail rows", pairs),
            (f"the {BOOK_JSON_ATTR} book JSON payload", record.data),
            ("the page DOM and og:/book: meta tags", record.soup),
        ]
        probe = self.probe_origin_detail(layers)
        if probe.value:
            result.warn(
                f"origin {probe.value!r} was read from BookBub's page ({probe.where}); "
                "BookBub did not previously publish a place of publication, so treat "
                "this as new"
            )
            return probe.value

        searched = self.origin_layers_clause(probe.searched)
        spellings = len(ORIGIN_KEY_SPELLINGS)
        if not self._detail_panel_found:
            return self.origin_unavailable(
                result,
                "the book detail panel could not be located on this page, so none of "
                "the "
                + ", ".join(f"'{label}'" for label in ORIGIN_LABELS)
                + " detail rows could be read at all -- that is a selector/layout "
                "change on BookBub's side, not evidence that the field is absent. The "
                f"shared origin probe additionally searched {searched} for "
                f"{spellings} place-of-publication key/label spellings and found none",
            )
        return self.origin_unavailable(
            result,
            "BookBub's book detail panel was read successfully on this run and carries "
            "none of "
            + ", ".join(f"'{label}'" for label in ORIGIN_LABELS)
            + f"; the shared origin probe then searched {searched} for {spellings} "
            "place-of-publication key/label spellings and found none either. BookBub "
            "is an ebook deals site rather than a bibliographic database",
        )

    def _language(self, record: _Record, result: ScrapeResult) -> Optional[str]:
        """Language the book is written in, or the storefront locale as an inference."""
        explicit = self.meta(record.soup, *LANGUAGE_META_KEYS)
        if not explicit:
            for blob in self.jsonld(record.soup, want_type="Book"):
                candidate = blob.get("inLanguage")
                if isinstance(candidate, dict):
                    candidate = candidate.get("name") or candidate.get("alternateName")
                if candidate:
                    explicit = self.clean_text(candidate)
                    break
        if explicit:
            name = self._language_name(explicit)
            result.warn(
                f"language {name!r} came from an explicit page field ({explicit!r})"
            )
            return name

        # The storefront locale is deliberately NOT used. <html lang="en-US"> and
        # og:locale describe the *page*, not the book, so they answered "en" for
        # every book ever scraped -- a field that is always present, always
        # identical and therefore carries no information, while looking like data.
        # That is precisely the trap the assignment calls out ("language the book
        # is WRITTEN in"), so this returns null and says why.
        locale = None
        html_tag = record.soup.find("html")
        if isinstance(html_tag, Tag):
            locale = self.clean_text(html_tag.get("lang") or "")
        locale = locale or self.clean_text(self.meta(record.soup, "og:locale") or "")
        result.warn(
            "language is null: BookBub publishes no per-book language field. Its "
            + (f"storefront locale ({locale!r}) " if locale else "page locale ")
            + "describes the storefront, not the book, so it is deliberately NOT "
            "reported as the language the book was written in"
        )
        return None

    @staticmethod
    def _language_code(raw: str) -> str:
        """``'en-US'``/``'en_US'``/``'English'`` -> a short primary subtag."""
        text = str(raw).strip().replace("_", "-")
        primary = text.split("-")[0].strip().lower()
        return primary or text

    def _language_name(self, raw: str) -> str:
        """Normalise to the same surface form the other four adapters emit.

        Goodreads/Amazon/Kobo/Audible all write ``"English"``; emitting ``"en"``
        here made BookBub look like a different language to any consumer grouping
        on the field. A BCP-47 subtag is mapped through :data:`LANGUAGE_NAMES`;
        anything already spelled out is passed through unchanged.
        """
        text = str(raw or "").strip()
        if not text:
            return text
        code = self._language_code(text)
        if len(text) <= 5 or "-" in text or "_" in text:
            mapped = LANGUAGE_NAMES.get(code)
            if mapped:
                return mapped
        return text.title() if text.islower() else text

    # ------------------------------------------------------------------ warnings

    def _warn_later(self, message: str) -> None:
        """Queue a resolution-time warning for the eventual :class:`ScrapeResult`."""
        if message and message not in self._pending_warnings:
            self._pending_warnings.append(message)
            if verbose():
                print('  bookbub warning: %s' % (message,), file=sys.stderr)
