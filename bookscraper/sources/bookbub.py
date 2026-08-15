"""BookBub -- no ISBN, no search route, so the URL slug has to be *constructed*.

Two independent blockers make this browser-only: Cloudflare challenges every path
including ``/robots.txt``, keyed on the TLS fingerprint (a plain-requests probe
measured 0 of 3, all HTTP 403 behind a ~6 kB "Just a moment..." page), and the book
body is client-side rendered anyway. Headless Chrome clears the challenge unaided;
no CAPTCHA is solved. A plain-requests probe must **not** be re-added: a fresh
adapter is built per book, so any "probe once" flag resets and every book pays a
guaranteed 403 plus its courtesy delay.

``/search`` is HTTP 404, so discovery constructs ``<title-slug>-by-<author-slug>``
and, failing that, harvests the author's listing -- the only way to reach slugs
guessing cannot invent (dated deal slugs, "Tenth Anniversary Edition"). Acceptance
is therefore always fuzzy, and the scoring is what stops a different book being
filed under the requested ISBN.

Reviews are an honest zero: BookBub exposes only aggregate ratings anonymously, and
member review text sits behind sign-in, which is not attempted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from ..base import Source
from ..extract import iso_date, jsonld, loads
from ..match import title_score
from ..models import Hint, Result
from ..parse import text
from . import _bookbub_fields as fields
from ._bookbub import (ADDITIVE_CEILING, AUTHOR_SCROLLS, AUTHOR_URL, AUTHOR_WAIT_CSS,
                       BOOK_HREF, BOOK_JSON_ATTR, BOOK_JSON_CSS, BOOK_URL, CONFIDENT,
                       COVER_ALT, DATE_LABELS, MAX_AUTHOR_CANDIDATES, MAX_BOOK_FETCHES,
                       MAX_SLUG_CANDIDATES, MIN_ACCEPT, NON_BOOK_SLUGS, ORIGIN_LABELS,
                       PUBLISHER_LABELS, RENDER_WAIT, SLUG_DATE_SUFFIX, STRONG_TITLE,
                       author_variants, challenged, detail_pairs, first_label,
                       not_found, slugify, title_variants)


@dataclass
class Record:
    """One rendered book page and the payload it hydrates itself from."""

    url: str
    soup: Any
    data: Dict[str, Any] = field(default_factory=dict)
    title: str = ""
    score: float = 0.0


class BookBub(Source):
    name = "bookbub"
    display_name = "BookBub"
    needs_browser = True

    def __init__(self, client: Any) -> None:
        super().__init__(client)
        self._fetches = 0
        self._cache: Dict[str, Optional[Record]] = {}

    def _scrape(self, hint: Hint, result: Result) -> None:
        if not self.client.browser.available:
            # Without this guard every render returns None and a missing driver is
            # misreported as "the book is not on BookBub".
            result.warn("bookbub: needs a browser (Cloudflare challenges every path "
                        "on the TLS fingerprint, and the page is client-rendered), so "
                        "this is 'unreachable', not 'absent from the catalogue'")
            return

        # BookBub's own slugs embed the subtitle, so keep it -- the opposite of what
        # Kobo and Audible want.
        title, authors = self.terms(hint, result, subtitle=True)
        if not title:
            result.warn("bookbub: no title is known, and BookBub indexes no ISBN and "
                        "has no anonymous search route, so it cannot be searched")
            return

        found = self._resolve(title, authors)
        if found is None:
            result.warn(f"bookbub: no page scored above {MIN_ACCEPT:.2f} for "
                        f"{title!r}, so nothing was filed under this ISBN")
            return
        if found.score < CONFIDENT:
            result.warn(f"bookbub: acceptance was FUZZY -- {found.title!r} scored "
                        f"{found.score:.2f} against {title!r}. BookBub carries no "
                        "ISBN, so the slug match is the only identification.")

        result.book_url = found.url
        pairs = detail_pairs(found.soup)
        books = jsonld(found.soup, "Book")
        blob: Dict[str, Any] = books[0] if books else {}

        resolved = fields.title(found.soup, found.data, blob)
        if not resolved:
            result.warn("bookbub: no title could be parsed, so no record was written")
            return
        book = self.new_book(hint)
        book.title = resolved
        book.authors = fields.authors(found.soup, found.data, blob)
        book.publisher = first_label(pairs, PUBLISHER_LABELS)
        book.date_of_publication = iso_date(
            first_label(pairs, DATE_LABELS)
            # Never blurbDate: that is when BookBub wrote its promo copy.
            or blob.get("datePublished") or blob.get("copyrightYear")
            or fields.meta(found.soup, "book:release_date", "og:book:release_date",
                           "datePublished"))
        book.language = fields.language(found.soup, blob)
        genres = fields.genres(found.data, blob)
        book.genres = genres
        book.origin = (first_label(pairs, ORIGIN_LABELS) or self.origin(result, [
            ("the parsed detail rows", pairs),
            (f"the {BOOK_JSON_ATTR} book JSON payload", found.data),
            ("the page DOM and og:/book: meta tags", found.soup)]))

        result.book = book
        result.genres = genres
        result.blurb = fields.blurb(found.soup, found.data, blob)
        result.cover_urls = fields.covers(found.soup, found.data, found.url, resolved).urls
        result.warn("bookbub: no reader reviews are available -- BookBub exposes only "
                    "aggregate ratings anonymously, and member review text is behind "
                    "sign-in, which is not attempted")

    # -- discovery -----------------------------------------------------------

    def _resolve(self, title: str, authors: List[str]) -> Optional[Record]:
        """Guess the slug, then fall back to harvesting the author's listing."""
        people = author_variants(authors)
        slugs = ([f"{slugify(t)}-by-{slugify(a)}" for t in title_variants(title)
                  for a in people] if people
                 else [slugify(t) for t in title_variants(title)])

        best: Optional[Record] = None
        for slug in list(dict.fromkeys(slugs))[:MAX_SLUG_CANDIDATES]:
            best = self._better(best, BOOK_URL.format(slug=slug), title, authors)
            if best is not None and best.score >= STRONG_TITLE:
                return best
        if people and (best is None or best.score < STRONG_TITLE):
            for url in self._from_author_page(people[0], title):
                best = self._better(best, url, title, authors)
                if best is not None and best.score >= STRONG_TITLE:
                    break
        return best if best is not None and best.score >= MIN_ACCEPT else None

    def _better(self, best: Optional[Record], url: str, title: str,
                authors: List[str]) -> Optional[Record]:
        found = self._record(url, title, authors)
        return found if found is not None and (best is None
                                               or found.score > best.score) else best

    def _from_author_page(self, author: str, title: str) -> List[str]:
        """Book URLs off the author's listing, best title match first.

        Candidate titles come from the cover image's ``alt``, so ranking them costs
        no requests at all.
        """
        soup = self.client.rendered(AUTHOR_URL.format(slug=slugify(author)),
                                    wait_css=AUTHOR_WAIT_CSS, wait_seconds=RENDER_WAIT,
                                    scrolls=AUTHOR_SCROLLS)
        if soup is None or challenged(soup):
            return []
        scored: List[Tuple[float, str]] = []
        seen: set = set()
        for anchor in soup.select("a[href*='/books/']"):
            match = BOOK_HREF.search(anchor.get("href") or "")
            if match is None or (slug := match.group(1).lower()) in seen \
                    or slug in NON_BOOK_SLUGS:
                continue
            seen.add(slug)
            image = anchor.select_one("img")
            alt = COVER_ALT.match((image.get("alt") or "") if image is not None else "")
            if alt is not None:
                candidate = alt.group("title")
            elif found := text(anchor.get_text(" ")):
                candidate = found
            else:
                candidate = SLUG_DATE_SUFFIX.sub("", slug).split("-by-", 1)[0].replace("-", " ")
            scored.append((-self._score_title(title, candidate),
                           BOOK_URL.format(slug=slug)))
        return [url for _, url in sorted(scored)[:MAX_AUTHOR_CANDIDATES]]

    def _record(self, url: str, title: str, authors: List[str]) -> Optional[Record]:
        """Render one book page and score it. Cached, and budgeted."""
        if url in self._cache:
            return self._cache[url]
        if self._fetches >= MAX_BOOK_FETCHES:
            return None
        self._fetches += 1
        soup = self.client.rendered(url, wait_css=BOOK_JSON_CSS, wait_seconds=RENDER_WAIT)
        record: Optional[Record] = None
        if soup is not None and not challenged(soup) and not not_found(soup):
            node = soup.select_one(BOOK_JSON_CSS)
            data = loads((node.get(BOOK_JSON_ATTR) or "") if node is not None else "")
            record = Record(url=url, soup=soup,
                            data=data if isinstance(data, dict) else {})
            record.title = fields.title(soup, record.data, {}) or ""
            record.score = self._score(title, authors, record)
        self._cache[url] = record
        return record

    @staticmethod
    def _score_title(wanted: str, candidate: str) -> float:
        return title_score(wanted, candidate, additive_ceiling=ADDITIVE_CEILING)

    def _score(self, title: str, authors: List[str], record: Record) -> float:
        """0.6 title + 0.4 author, or title alone when no author is known."""
        title_ratio = self._score_title(title, record.title)
        if not authors:
            return title_ratio
        found = fields.authors(record.soup, record.data, {})
        author_ratio = max((self._score_title(a, b) for a in authors for b in found),
                           default=0.0)
        return 0.6 * title_ratio + 0.4 * author_ratio
