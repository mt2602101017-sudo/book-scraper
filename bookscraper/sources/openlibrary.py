"""Open Library -- the ISBN-native seed for every ISBN-hostile storefront.

Two JSON calls, no HTML, no anti-bot wall. It runs first so its title and authors
can seed the shared hint: BookBub, Kobo and Audible have no ISBN lookup at all and
can only be searched by title+author, so without this each of them would have to
make the same request itself.

The catalogue publishes subject *headings*, not curated genres, so ``genre`` here
mixes true genres ("Fiction") with topical headings ("Grief") and Library of
Congress strings ("FICTION / Literary"). They are reported verbatim: filtering
them would be an editorial guess, not a scrape.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..base import Source
from ..extract import iso_date
from ..models import Hint, Result
from ..parse import dedupe, text

BASE = "https://openlibrary.org"

#: Open Library emits MARC / ISO 639-2**B** codes (``fre``, ``ger``, ``chi``,
#: ``dut``), not the 639-2T forms, so both spellings of the dual-code languages
#: must be here or French, German, Chinese and Dutch all break.
LANGUAGES = {"eng": "English", "spa": "Spanish", "fre": "French", "fra": "French",
             "ger": "German", "deu": "German", "ita": "Italian", "por": "Portuguese",
             "rus": "Russian", "jpn": "Japanese", "chi": "Chinese", "zho": "Chinese",
             "ara": "Arabic", "hin": "Hindi", "dut": "Dutch", "nld": "Dutch"}


class OpenLibrary(Source):
    name = "openlibrary"
    display_name = "Open Library"

    def _scrape(self, hint: Hint, result: Result) -> None:
        result.book_url = f"{BASE}/isbn/{hint.isbn13}"

        # format=json is mandatory: without it the endpoint returns a JSONP body
        # ("var _OLBookInfo = {...};") that no JSON decoder will touch. The reply
        # is keyed by the exact bibkey sent, so a missing key means "not catalogued"
        # -- which is the common, honest failure here.
        keys = [k for k in (hint.isbn13, hint.isbn10) if k]
        payload = self.client.json(f"{BASE}/api/books", params={
            "bibkeys": ",".join(f"ISBN:{k}" for k in keys),
            # jscmd=data resolves author/publisher/subject *names* rather than
            # /authors/OL...A reference keys -- but it omits languages and
            # description entirely, which is why the second request exists.
            "format": "json", "jscmd": "data"})
        record: Dict[str, Any] = next(
            (r for k in keys if isinstance(r := (payload or {}).get(f"ISBN:{k}"), dict)), {})
        if not record:
            result.warn(f"openlibrary: ISBN {hint.isbn13} is not in the catalogue")
            return

        # /isbn/<isbn>.json 302-redirects to the edition record. Only language and
        # the description live here; disabling redirects would null both.
        edition = self.client.json(f"{BASE}/isbn/{hint.isbn13}.json") or {}

        genres = dedupe(self._names(record.get("subjects")))
        book = self.new_book(hint)
        book.title = text(record.get("title")) or None
        book.authors = self._names(record.get("authors"))
        publishers = self._names(record.get("publishers"))
        book.publisher = publishers[0] if publishers else None
        book.date_of_publication = iso_date(record.get("publish_date"))
        book.language = self._language(edition)
        book.genres = genres
        # The one adapter where the probe has a real chance: edition records carry
        # ``publish_places``, which is a genuine place of publication.
        book.origin = self.origin(result, [("the /api/books record", record),
                                           ("the /isbn/ edition record", edition)])

        result.book = book
        result.genres = genres
        result.blurb = self._blurb(edition)
        result.cover_urls = self._cover(record)
        # Reviews: Open Library is a bibliographic catalogue and publishes none.

        result.hint = Hint(isbn13=hint.isbn13, title=book.title, authors=book.authors,
                           isbn10=self._isbn10(record) or hint.isbn10)

    @staticmethod
    def _names(entries: Any) -> List[str]:
        """``[{"name": "X", "url": ...}]`` -> ``["X"]``, the jscmd=data shape."""
        return [t for e in entries or []
                if isinstance(e, dict) and (t := text(e.get("name")))]

    @staticmethod
    def _language(edition: Dict[str, Any]) -> Optional[str]:
        """``[{"key": "/languages/eng"}]`` -> ``"English"``, else the bare code."""
        codes = [str(e.get("key", "")).rsplit("/", 1)[-1]
                 for e in edition.get("languages") or [] if isinstance(e, dict)]
        if not codes:
            return None
        return LANGUAGES.get(codes[0].lower(), codes[0])

    @staticmethod
    def _blurb(edition: Dict[str, Any]) -> Optional[str]:
        """``description`` is either a string or ``{"type": ..., "value": ...}``."""
        raw = edition.get("description")
        if isinstance(raw, dict):
            raw = raw.get("value")
        return text(raw) or None

    @staticmethod
    def _cover(record: Dict[str, Any]) -> List[str]:
        """The largest cover the record offers, read off it -- never constructed."""
        covers = record.get("cover") or {}
        return next(([str(url)] for size in ("large", "medium", "small")
                     if (url := covers.get(size))), [])

    @staticmethod
    def _isbn10(record: Dict[str, Any]) -> Optional[str]:
        found = record.get("isbn_10")
        return str(found[0]) if isinstance(found, list) and found else None
