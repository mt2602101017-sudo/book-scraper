"""Goodreads -- the highest-yield source, and the whole page in one request.

``/book/isbn/<isbn>`` 301s to the book page, whose ``<script id="__NEXT_DATA__">``
carries a normalised Apollo cache holding title, authors, publisher, date, language,
genres, cover, blurb **and the first ~30 reviews**. That is why this adapter parses
JSON rather than chasing CSS classes.

Publisher is absent from the book page markup entirely, and the other editions'
covers live only on the legacy ``/work/editions/`` listing -- so one extra request
is spent there, but only when the book page has left a gap. See :mod:`_goodreads`
for that listing and the GraphQL review pager, and :mod:`_goodreads_find` for why
the ISBN route comes before search.
"""

from __future__ import annotations

import re
from typing import Any, List

from ..base import Source
from ..extract import jsonld
from ..models import Hint, Result
from ..parse import absolutise, html_text, review, text
from . import _goodreads_fields as fields
from ._goodreads import MIN_REVIEW_CHARS, editions, embedded_reviews, paged_reviews, rating_of
from ._goodreads_find import Page, load, query, work_id


class Goodreads(Source):
    name = "goodreads"
    display_name = "Goodreads"

    def _scrape(self, hint: Hint, result: Result) -> None:
        page = load(self.client, hint, result)
        if page is None:
            result.warn("goodreads: no book page could be resolved for this ISBN")
            return
        result.book_url = page.url

        record = self.new_book(hint)
        record.title = fields.title(page)
        record.authors = fields.authors(page)
        record.publisher = text(page.details.get("publisher")) or None
        record.date_of_publication = fields.published(page.details.get("publicationTime"))
        record.language = fields.language(page)
        genres = fields.genres(page)
        covers = fields.covers(page)

        edition_soups: List[Any] = []
        if not covers.full or not (record.publisher and record.date_of_publication
                                   and record.language):
            if found_id := work_id(page):
                extra, found, edition_soups = editions(
                    self.client, found_id, fields.MAX_COVERS - len(covers))
                covers.extend(extra)
                record.publisher = record.publisher or found.get("publisher")
                record.date_of_publication = record.date_of_publication or found.get("date")
                record.language = record.language or found.get("language")

        record.genres = genres
        # work["details"]["places"] is the story's *setting* and is never an origin.
        record.origin = self.origin(result, [
            ("the __NEXT_DATA__ Apollo cache's work record", page.work),
            ("its book record (details included)", page.book),
            ("the JSON-LD Book blocks", jsonld(page.soup, "Book")),
            ("the book page DOM and og:/meta tags", page.soup),
            *[("the legacy /work/editions/ listing", s) for s in edition_soups[:1]]])

        result.book = record
        result.genres = genres
        result.cover_urls = covers.urls
        result.blurb = fields.blurb(page)
        result.reviews = self._reviews(page)

        # Only seed the shared hint when the page's own ISBN matched ours. Goodreads
        # runs early, so a wrong title here propagates into Amazon, Audible, BookBub
        # and Kobo discovery and produces four more wrong-book files.
        if not page.fuzzy and record.title:
            result.hint = Hint(isbn13=hint.isbn13, isbn10=hint.isbn10,
                               title=query(record.title), authors=list(record.authors))

    def _reviews(self, page: Page) -> List[Any]:
        """The embedded ~30, then GraphQL pages until the minimum is met.

        Each review is registered under both its id and a normalised prefix of its
        body, because the two routes expose different identifiers for the same one.
        """
        seen: set = set()
        out: List[Any] = []
        for node in embedded_reviews(page.state):
            body = html_text(node.get("text"))
            keys = {str(node.get("id") or ""),
                    re.sub(r"\W+", " ", body[:200]).strip().lower()}
            if not body or keys & seen:
                continue
            from ..extract import deref
            shelving = deref(page.state, node.get("shelving"))
            item = review(body, reviewer=deref(page.state, node.get("creator")).get("name"),
                          rating=rating_of(node.get("rating")),
                          date=fields.published(node.get("createdAt"), pacific=False),
                          url=absolutise(page.url, shelving.get("webUrl")),
                          min_chars=MIN_REVIEW_CHARS)
            if item is not None:
                seen |= keys
                out.append(item)

        wanted = max(0, int(self.min_reviews or 0))
        if self.max_reviews is not None:
            wanted = min(wanted, max(0, int(self.max_reviews)))
        if len(out) < wanted:
            # WORK is preferred over BOOK: it exposes every edition's reviews.
            resource = (("WORK", text(page.work.get("id"))) if page.work.get("id")
                        else ("BOOK", text(page.book.get("id"))))
            token = (((page.state.get("ROOT_QUERY") or {}).get("getReviews") or {})
                     .get("pageInfo") or {}).get("nextPageToken")
            out += paged_reviews(self.client, page.soup, page.url, resource, token,
                                 wanted - len(out), seen)
        return out[:self.max_reviews] if self.max_reviews is not None else out
