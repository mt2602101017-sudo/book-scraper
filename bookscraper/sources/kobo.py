"""Rakuten Kobo -- browser-only, and the metadata hides in an HTML attribute.

Cloudflare gates ``www.kobo.com`` on the TLS fingerprint (Python's OpenSSL versus
Chrome's BoringSSL), so plain ``requests`` gets a 403 on every path no matter what
headers it sends. Header tuning provably cannot help, so this adapter goes straight
to the browser rather than spending a guaranteed 403 and poisoning the run report
with a wall it deliberately routes around. Two sibling hosts are *not* behind
Cloudflare and answer plain requests: ``ratingsapi.kobo.com`` for reviews and
``cdn.kobo.com`` for covers.

The record itself is not in ld+json: it is entity-escaped, doubly JSON-encoded text
inside a ``data-kobo-gizmo-config`` attribute, and half of it lives one level down
under ``googleBook.workExample``. Kobo's ISBN search only matches the EAN of the
exact EPUB it sells, so title+author search is the normal path, not an edge case.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional
from urllib.parse import quote_plus

from ..base import Source
from ..extract import jsonld
from ..match import authors_agree, best
from ..models import Hint, Result
from ..parse import dedupe, meta
from . import _kobo_fields as fields
from ._kobo import (REVIEW_LIMIT_CEILING, STORE_ROOT, api_reviews, challenged,
                    collector, gizmo, nested, parse_reviews, publication_date,
                    search_cards, slug_base, strip_tracking, valid_crid)

_NON_WORD = re.compile(r"[^a-z0-9]+")


class Kobo(Source):
    name = "kobo"
    display_name = "Kobo"
    needs_browser = True

    def __init__(self, client: Any) -> None:
        super().__init__(client)
        #: The search page is retained from discovery: it powers sibling covers and
        #: the review-id fallback at zero extra requests. Cleared before a new
        #: query, because the ISBN page answered a different question.
        self._search: Any = None

    def _scrape(self, hint: Hint, result: Result) -> None:
        if not self.client.browser.available:
            result.warn("kobo: needs a browser (Cloudflare gates every path on the "
                        "TLS fingerprint), and none is available -- so this is "
                        "'unreachable', not 'absent from the catalogue'")
            return

        url = self._discover(hint, result)
        if not url:
            result.warn("kobo: no product page could be matched for this book")
            return
        soup = self._render(url, wait_css="div[data-kobo-gizmo-config]", wait_seconds=20)
        if soup is None:
            result.warn(f"kobo: {url} could not be rendered")
            return

        config = gizmo(soup, "RatingAndReviewWidget")
        google_book = nested(config, "googleBook")
        google_product = nested(config, "googleProduct")
        work = google_book.get("workExample")
        work = work if isinstance(work, dict) else {}
        rows = fields.rows(soup)
        result.book_url = self._canonical(soup, url)

        main, full = fields.titles(soup, google_book, work)
        record = self.new_book(hint)
        record.title = full
        record.authors = fields.authors(soup, google_book, work)
        record.publisher = fields.publisher(google_book, google_product, rows)
        record.date_of_publication = (publication_date(work.get("datePublished"))
                                      or publication_date(google_product.get("releasedate"))
                                      or publication_date(rows.get("release_date")))
        record.language = fields.language(google_book, rows)
        genres = fields.genres(soup, google_book)
        record.genres = genres
        record.origin = self.origin(result, [
            ("the data-kobo-gizmo-config googleBook blob", google_book),
            ("its workExample record", work),
            ("the googleProduct blob", google_product),
            ("the injected ld+json blocks", jsonld(soup)),
            ("the bookitem-secondary-metadata DOM rows", rows),
            ("the page DOM and og:/meta tags", soup)])

        result.book = record
        result.genres = genres
        result.blurb = fields.blurb(soup, work)
        result.cover_urls = self._covers(soup, work, google_product,
                                         result.book_url, main, record.authors)
        result.reviews = self._reviews(soup, config, google_book, result.book_url)
        # The BARE main title: later sources search with this, and handing them
        # "Main: Subtitle" degrades every one of their searches.
        if main:
            result.hint = Hint(isbn13=hint.isbn13, isbn10=hint.isbn10,
                               title=main, authors=list(record.authors))

    # -- fetching and discovery ---------------------------------------------

    def _render(self, url: str, *, wait_css: Optional[str], wait_seconds: int) -> Any:
        soup = self.client.rendered(url, wait_css=wait_css, wait_seconds=wait_seconds)
        return None if soup is None or challenged(soup) else soup

    def _discover(self, hint: Hint, result: Result) -> Optional[str]:
        """ISBN redirect, then title+author search, then a slug guess."""
        for isbn in dedupe([hint.isbn13, hint.isbn10]):
            if not isbn:
                continue
            url = f"{STORE_ROOT}/search?query={isbn}"
            soup = self._render(url, wait_css=None, wait_seconds=15)
            if soup is None:
                break  # the browser gave nothing; another spelling will not help
            if self._is_book_page(soup):
                return self._canonical(soup, url)
            self._search = soup

        title, authors = self.terms(hint, result)
        if title:
            query = " ".join(p for p in [title, " ".join(authors[:1])] if p).strip()
            self._search = None  # never reuse the ISBN page for a title query
            self._search = self._render(f"{STORE_ROOT}/search?query={quote_plus(query)}",
                                        wait_css="div[id^='list-item-']", wait_seconds=15)
            if self._search is not None:
                if found := self._best_card(self._search, title, authors):
                    return found
            slug = re.sub(r"-{2,}", "-", _NON_WORD.sub("-", title.lower())).strip("-")
            soup = self._render(f"{STORE_ROOT}/ebook/{slug}",
                                wait_css="div[data-kobo-gizmo-config]", wait_seconds=15)
            if soup is not None and self._is_book_page(soup):
                return self._canonical(soup, f"{STORE_ROOT}/ebook/{slug}")
        return None

    @staticmethod
    def _is_book_page(soup: Any) -> bool:
        """What distinguishes a redirected product page from a search page."""
        return bool(soup.select_one('div[data-kobo-gizmo="RatingAndReviewWidget"]')
                    or soup.select_one("h1.title.product-field"))

    @staticmethod
    def _best_card(soup: Any, title: str, authors: List[str]) -> Optional[str]:
        """The best search card for this book, or ``None``.

        An ebook edition is preferred, and the shared matcher breaks remaining ties
        towards the shorter title -- listings omit subtitles, so a graphic-novel
        adaptation arrives under the same name as the book.
        """
        cards = search_cards(soup)
        ranked = best(title, authors,
                      [(c["title"], c["authors"], c) for c in cards],
                      floor=0.86, prefix_is_exact=False)
        if not ranked:
            return None
        ebooks = [(score, card) for score, card in ranked if card["ebook"]]
        return (ebooks or ranked)[0][1]["url"]

    @staticmethod
    def _canonical(soup: Any, fallback: str) -> str:
        node = soup.select_one("link[rel=canonical]")
        href = strip_tracking(node.get("href") or "") if node is not None else ""
        # Without the veto a search page canonicalises to itself.
        return href if href and "/search" not in href else fallback

    # -- artefacts ----------------------------------------------------------

    def _covers(self, soup: Any, work: Dict[str, Any], google_product: Dict[str, Any],
                url: str, title: str, authors: List[str]) -> List[str]:
        candidates = [str(work.get("image") or ""),
                      str(google_product.get("image") or ""),
                      str(work.get("thumbnailUrl") or "")]
        # The upstream class attribute has a double space, so select by class.
        node = (soup.select_one("div.main-product-image img.cover-image")
                or soup.select_one("img.cover-image"))
        candidates.append((node.get("src") or "") if node is not None else "")
        candidates.append(meta(soup, "og:image", "twitter:image") or "")
        if self._search is not None:
            # Sibling editions, free -- the search page is already in hand.
            # Identity is the slug base, never the title: cards omit subtitles, so
            # a title test demonstrably pulls in the wrong artwork.
            for card in search_cards(self._search):
                if (card["image"] and slug_base(card["url"]) == slug_base(url)
                        and authors_agree(authors, card["authors"])):
                    candidates.append(card["image"])
        found = collector()
        found.extend(candidates)
        return found.urls

    def _reviews(self, soup: Any, config: Dict[str, Any],
                 google_book: Dict[str, Any], url: str) -> List[Any]:
        """Every review, from the ratings API, then the rendered widget."""
        crid = valid_crid(config.get("crossRevisionId")) or valid_crid(
            gizmo(soup, "ItemDetailActions").get("crossRevisionId"))
        if not crid and self._search is not None:
            crid = next((valid_crid(card["crid"]) for card in search_cards(self._search)
                         if strip_tracking(card["url"]) == strip_tracking(url)), "")

        target = max(int(self.min_reviews or 0), 25)
        if self.max_reviews is not None:
            target = max(1, min(target, int(self.max_reviews)))
        # Ask for what is wanted, not for everything: a naive large limit once
        # wrote 301 files for one book, ten times every other source.
        rating = google_book.get("aggregateRating") or {}
        hinted = rating.get("reviewCount") if isinstance(rating, dict) else None
        wanted = max(target, 50)
        if self.max_reviews is not None:
            wanted = min(wanted, max(1, int(self.max_reviews)))
        else:
            wanted = max(wanted, min(int(hinted or 0) + 5, 4 * max(target, 25)))

        seen: set = set()
        found = api_reviews(self.client, crid, target,
                            min(REVIEW_LIMIT_CEILING, wanted), seen) if crid else []
        if len(found) < target:
            found += parse_reviews(soup, seen)   # the JS-filled widget, already loaded
        return found[:self.max_reviews] if self.max_reviews is not None else found
