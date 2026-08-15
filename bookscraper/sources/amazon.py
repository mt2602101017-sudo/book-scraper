"""Amazon -- ISBN-10 *is* the ASIN, so the detail page is usually one hop away.

``/dp/<isbn10>`` is fully server-rendered: every field, and the ~13 reviews the page
embeds, are in the static HTML. The quirks that earn their keep live next door:
:mod:`_amazon` (the bidi-mark label cleaning, the image island, review parsing),
:mod:`_amazon_find` (the discovery ladder, the interstitial and sign-in detection)
and :mod:`_amazon_fields`.

Review pagination is deliberately absent. Both listing routes 302 to ``/ax/claim``
for anonymous clients, so the honest ceiling is what the detail page embeds; the old
sign-in bypass also breached Amazon's Conditions of Use.
"""

from __future__ import annotations

from typing import Any, Dict, List

from .. import isbn as isbn_utils
from ..base import Source
from ..extract import iso_date
from ..models import Hint, Result
from ._amazon import bullet, parse_reviews
from ._amazon_find import ASIN_IN_URL, TITLE_TAG, Page, fetch, resolve
from . import _amazon_fields as fields


class Amazon(Source):
    name = "amazon"
    display_name = "Amazon"

    def __init__(self, client: Any) -> None:
        super().__init__(client)
        self._pages: Dict[str, Page] = {}

    def _scrape(self, hint: Hint, result: Result) -> None:
        page = resolve(self.client, self._pages, hint, result)
        if page is None:
            result.warn("amazon: no product page could be matched for this ISBN")
            return
        result.book_url = page.url
        match = ASIN_IN_URL.search(page.url)
        asin = match.group(1).upper() if match else ""

        tag = TITLE_TAG.search(page.html[:4096])
        record = self.new_book(hint)
        record.title = fields.title(page, tag)
        record.authors = fields.authors(page, tag)
        record.publisher = bullet(page.values, "Publisher")
        record.date_of_publication = iso_date(
            bullet(page.values, "Publication date", "Release date"))
        record.language = bullet(page.values, "Language")
        genres = fields.genres(page)
        record.genres = genres
        # Physical-goods listings really do print this, so the bullet wins; the
        # probe then sweeps the merged map and the DOM. Never #glow-ingress-line2,
        # which is the delivery locale, and never the publisher's imprint.
        record.origin = (bullet(page.values, "Country of Origin",
                                "Country/Region of Origin")
                         or self.origin(result, [
                             ("the merged product-detail map", page.values),
                             ("the page DOM and og:/meta tags", page.soup)]))

        result.book = record
        result.genres = genres
        result.blurb = fields.blurb(page)
        result.cover_urls = self._covers(page)
        result.reviews = parse_reviews(page.soup, page.url, set())
        if len(result.reviews) < (self.min_reviews or 0):
            result.warn("amazon: only the reviews embedded in the detail page are "
                        "available -- both listing routes redirect anonymous clients "
                        "to a sign-in wall, which is not circumvented")
        if record.title:
            result.hint = Hint(
                isbn13=hint.isbn13, title=record.title, authors=list(record.authors),
                # A B0... Kindle or audio ASIN is not an ISBN, and propagating one
                # as if it were would poison every later source.
                isbn10=asin if isbn_utils.is_valid(asin) else hint.isbn10)

    def _covers(self, page: Page) -> List[str]:
        """This edition's covers, then one per sibling print edition."""
        found = fields.covers(page)
        for url in fields.sibling_urls(page):
            if found.full:
                break
            sibling = fetch(self.client, self._pages, url, referer=page.url)
            if sibling.ok:
                found.add(fields.sibling_cover(sibling))
        return found.urls
