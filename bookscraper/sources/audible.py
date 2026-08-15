"""Audible -- no ISBN index at all, so every book is found by title and author.

Every ISBN-10/13 form routes to ``/no-search-results``, so a title is mandatory.
Three things earn their keep, all in :mod:`_audible`: the geo-override parameter
pair on every URL (without it a non-US request lands on audible.in with HTTP 200),
the patient retries through a multi-minute throttle, and the slot-qualified
selectors that keep the recommendation carousels out.

Author agreement is **mandatory** unless the ASIN happens to be our ISBN-10, because
Audible carries an *Everything I Never Told You* by Ajay K Pandey alongside Celeste
Ng's and a near-perfect title alone is how the wrong one gets in.

What Audible describes is an **audiobook edition**: ``publisher`` is the audio
imprint, ``date_of_publication`` the audio release date, ``language`` the narration
language. Narrators are never merged into authors.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .. import isbn as isbn_utils
from ..base import Source
from ..extract import iso_date, jsonld
from ..match import authors_agree, title_score
from ..models import Hint, Result
from ..parse import meta, text
from . import _audible_fields as fields
from ._audible import (BASE, EDITION_CEILING, EDITION_MARKER, FUZZY_FLOOR, asin_of,
                       collector, component_json, fetch, paged_reviews, review_tiles,
                       strip_query, url_for, us_short_date)


@dataclass
class Candidate:
    """One search row, scored against what we asked for."""

    asin: str
    url: str
    title: str
    authors: List[str] = field(default_factory=list)
    cover: Any = None
    position: int = 0
    score: float = 0.0
    isbn_match: bool = False
    author_match: bool = False

    @property
    def acceptable(self) -> bool:
        return self.isbn_match or (self.score >= FUZZY_FLOOR and self.author_match)

    @property
    def sibling(self) -> bool:
        """Another edition of the same book, worth taking a cover from."""
        return (self.isbn_match or self.score >= EDITION_CEILING
                or bool(EDITION_MARKER.search(self.title)))


class Audible(Source):
    name = "audible"
    display_name = "Audible"

    def __init__(self, client: Any) -> None:
        super().__init__(client)
        self._rows: List[Candidate] = []

    def _scrape(self, hint: Hint, result: Result) -> None:
        title, authors = self.terms(hint, result)
        if not title:
            result.warn("audible: no title is known, and Audible has no ISBN lookup "
                        "at all, so it cannot be searched")
            return

        chosen = self._resolve(hint, title, authors)
        if chosen is None:
            result.warn(f"audible: no product matched {title!r} closely enough, with "
                        "the author agreeing, to be filed under this ISBN")
            return
        if not chosen.isbn_match:
            result.warn(f"audible: matched {chosen.title!r} by title and author "
                        f"(similarity {chosen.score:.2f}); Audible indexes no ISBN, so "
                        "this is weaker than an ISBN lookup")

        soup = fetch(self.client, chosen.url, patient=2, allow_browser=True)
        if soup is None:
            result.warn("audible: the product page could not be fetched, so only the "
                        "search row's cover is reported")
            found = collector(BASE)
            found.add(chosen.cover)
            result.cover_urls = found.urls
            return

        node = soup.select_one('link[rel="canonical"][href]')
        result.book_url = strip_query((node.get("href") if node is not None else None)
                                     or meta(soup, "og:url") or chosen.url)

        blobs = jsonld(soup, ("Audiobook", "Product", "Book"))
        audiobook: Dict[str, Any] = next(
            (b for b in blobs if b.get("name") or b.get("description")), {})
        details = component_json(soup, fields.DETAILS_JSON)
        people = component_json(soup, fields.PEOPLE_JSON)

        record = self.new_book(hint)
        record.title = fields.title(soup, audiobook)
        record.authors = fields.authors(soup, audiobook, people)
        record.publisher = (fields.named(audiobook.get("publisher"))
                            or fields.named(details.get("publisher")))
        record.date_of_publication = (iso_date(audiobook.get("datePublished"))
                                      or us_short_date(details.get("releaseDate")))
        record.language = fields.language(audiobook, details)
        genres = fields.genres(soup, audiobook, details)
        record.genres = genres
        # Never repurpose the country-shaped fields Audible does carry: regionsAllowed
        # is a licensing allowlist, and priceCurrency and #reviewsCountry are the
        # storefront this run forced with overrideBaseCountry.
        record.origin = self.origin(result, [
            ("the Audiobook/Product JSON-LD", audiobook),
            ("the embedded component JSON", details),
            ("the page DOM (adbl-metadata slots and og:/meta tags)", soup)])

        result.book = record
        result.genres = genres
        result.blurb = fields.blurb(soup, audiobook)
        result.cover_urls = self._covers(soup, audiobook, result.book_url, chosen)
        result.reviews = self._reviews(soup, chosen.asin)
        result.warn("audible: this is the AUDIOBOOK edition -- publisher is the audio "
                    "imprint, the date is the audio release and the language is the "
                    "narration language, none of which need match the print ISBN")

    def _resolve(self, hint: Hint, title: str,
                 authors: List[str]) -> Optional[Candidate]:
        """Search once, score every row, take the best acceptable one."""
        if self._rows:
            return next((c for c in self._rows if c.acceptable), None)
        query = " ".join([title] + list(authors[:2])).strip()
        soup = fetch(self.client, url_for("/search", {"keywords": query}), patient=1)
        if soup is None:
            return None

        rows: List[Candidate] = []
        # A real CSS class selector: the attribute is
        # class="bc-list-item\tproductListItem" with a literal tab, so substring
        # matching on the raw string is unreliable.
        for position, item in enumerate(soup.select("li.productListItem")):
            link = (item.select_one('a.bc-link[href^="/pd/"]')
                    or item.select_one('a[href*="/pd/"]'))
            href = strip_query(link.get("href") or "") if link is not None else ""
            asin = asin_of(item, href)
            if not asin and not href:
                continue
            # The label is on the row itself, not on the link.
            row_title = (text(item.get("aria-label"))
                         or fields.sel(item, "h3.bc-heading a", "h3 a", "h3") or "")
            row_authors = fields.label_authors(item)
            cover = (item.select_one('img[alt$=" cover art"][src]')
                     or item.select_one("img[src]"))
            candidate = Candidate(
                asin=asin, position=position, title=row_title, authors=row_authors,
                url=(f"{BASE}{href}" if href.startswith("/")
                     else href or url_for(f"/pd/{asin}")),
                cover=cover.get("src") if cover is not None else None)
            candidate.score = title_score(title, row_title, prefix_is_exact=True)
            candidate.author_match = authors_agree(authors, row_authors)
            # Some Audible ASINs literally are ISBN-10s: a free definitive match
            # that outranks every fuzzy score.
            candidate.isbn_match = (len(asin) == 10 and isbn_utils.is_valid(asin)
                                    and isbn_utils.to_isbn13(asin) == hint.isbn13)
            rows.append(candidate)

        rows.sort(key=lambda c: (0 if c.isbn_match else 1, -round(c.score, 3),
                                 0 if c.author_match else 1, c.position))
        self._rows = rows
        return next((c for c in rows if c.acceptable), None)

    def _covers(self, soup: Any, audiobook: Dict[str, Any], url: str,
                chosen: Candidate) -> List[str]:
        """This edition's cover, then sibling editions' from the search rows.

        Audible product pages have no "other editions" section, so the extra covers
        cost no extra request -- they come from the search page already fetched.
        """
        found = collector(url)
        found.add(audiobook.get("image"))
        node = soup.select_one('adbl-product-image[slot="image"] img[src]')
        found.add(node.get("src") if node is not None else None)
        found.add(meta(soup, "og:image", "twitter:image"))
        found.add(chosen.cover)
        for row in self._rows:
            if row.asin != chosen.asin and row.sibling:
                found.add(row.cover)
        return found.urls

    def _reviews(self, soup: Any, asin: str) -> List[Any]:
        """The five tiles the page renders, then the XHR fragment for the rest."""
        seen: set = set()
        found = review_tiles(soup, seen)
        target = max(0, int(self.min_reviews or 0))
        if self.max_reviews is not None:
            target = min(target, int(self.max_reviews)) if target else int(self.max_reviews)
        target = max(target, 5)
        if len(found) < target:
            found += paged_reviews(self.client, soup, asin, target, len(found), seen)
        return found[:self.max_reviews] if self.max_reviews is not None else found
