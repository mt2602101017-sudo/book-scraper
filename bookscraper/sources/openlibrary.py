"""Open Library (``openlibrary.org``) adapter.

The simplest adapter in the project, and deliberately so: Open Library publishes a
free, documented, ISBN-indexed JSON API, so there is no HTML to parse, no browser,
no anti-bot wall and no title-matching guesswork. One request answers "what book is
this ISBN", which is why the other adapters already use it to seed their searches.

Two endpoints, both plain ``requests``:

* ``/api/books?bibkeys=ISBN:<isbn>&jscmd=data`` -- title, authors, publishers,
  publish date, subjects and cover URLs.
* ``/isbn/<isbn>.json`` -- the raw edition record, used only for ``languages``,
  which ``jscmd=data`` omits.

What it cannot give: **reviews**. Open Library is a catalogue, not a storefront, so
``reviews`` is always empty and the run says so once rather than pretending.
``origin`` is likewise absent -- Open Library has publisher *places* on some
records, but only via a further work lookup, so the shared probe is run over what
we did fetch and the field stays ``null`` when nothing turns up.

Because it is ISBN-native it needs no ``hint``: it resolves from the ISBN alone,
like Goodreads and Amazon, and so can run entirely on its own.
"""

from __future__ import annotations

import sys
from typing import Any, Dict, List, Optional, Tuple

from ..models import BookHint, BookMetadata, ScrapeResult
from ..base import BaseSource

BASE_URL = "https://openlibrary.org"
#: ``jscmd=data`` is the friendlier shape: names are resolved, not ``/authors/OL..``.
BOOKS_API = f"{BASE_URL}/api/books"
#: Cover sizes Open Library serves. ``L`` first: we want the biggest available.
COVER_SIZES: Tuple[str, ...] = ("large", "medium", "small")


class OpenLibrarySource(BaseSource):
    """Adapter for Open Library's public book API."""

    name = "openlibrary"
    display_name = "Open Library"
    prefers_browser = False

    # -- discovery -----------------------------------------------------------

    def find_book_url(self, hint: BookHint) -> Optional[str]:
        """The human-facing page for this ISBN. Always derivable, never fetched."""
        return f"{BASE_URL}/isbn/{hint.isbn13}"

    def _books_api(self, isbn: str) -> Dict[str, Any]:
        """The ``jscmd=data`` record for ``isbn``, or ``{}``."""
        payload = self.client.get_json(
            BOOKS_API,
            params={"bibkeys": f"ISBN:{isbn}", "format": "json", "jscmd": "data"},
        )
        if not isinstance(payload, dict):
            return {}
        record = payload.get(f"ISBN:{isbn}")
        return record if isinstance(record, dict) else {}

    def _edition(self, isbn: str) -> Dict[str, Any]:
        """The raw edition record, for the fields ``jscmd=data`` leaves out."""
        payload = self.client.get_json(f"{BASE_URL}/isbn/{isbn}.json")
        return payload if isinstance(payload, dict) else {}

    # -- field extraction ----------------------------------------------------

    @staticmethod
    def _names(entries: Any) -> List[str]:
        """``[{"name": "X"}, ...]`` -> ``["X", ...]``. Open Library's usual shape."""
        out: List[str] = []
        for entry in entries or []:
            if isinstance(entry, dict) and entry.get("name"):
                out.append(str(entry["name"]).strip())
            elif isinstance(entry, str) and entry.strip():
                out.append(entry.strip())
        return out

    def _language(self, edition: Dict[str, Any], result: ScrapeResult) -> Optional[str]:
        """English language name from ``languages: [{"key": "/languages/eng"}]``."""
        codes = [
            str(entry.get("key", "")).rsplit("/", 1)[-1]
            for entry in edition.get("languages") or []
            if isinstance(entry, dict)
        ]
        if not codes:
            result.warn(
                "openlibrary: this edition record carries no 'languages' field, so "
                "language is null rather than assumed from the title's script"
            )
            return None
        # ISO 639-2/B three-letter codes, mapped to the surface form the other
        # adapters emit so the five files agree with each other.
        names = {"eng": "English", "spa": "Spanish", "fre": "French",
                 "fra": "French", "ger": "German", "deu": "German",
                 "ita": "Italian", "por": "Portuguese", "rus": "Russian",
                 "jpn": "Japanese", "chi": "Chinese", "zho": "Chinese",
                 "ara": "Arabic", "hin": "Hindi", "dut": "Dutch", "nld": "Dutch"}
        return names.get(codes[0].lower(), codes[0])

    def _covers(self, record: Dict[str, Any], result: ScrapeResult) -> List[str]:
        """The single cover Open Library holds, at the largest size offered."""
        cover = record.get("cover")
        if not isinstance(cover, dict):
            result.warn("openlibrary: this record has no cover image")
            return []
        for size in COVER_SIZES:
            url = cover.get(size)
            if url:
                if size != "large":
                    result.warn(
                        f"openlibrary: only the {size!r} cover was available, not 'large'"
                    )
                return [str(url)]
        return []

    def _blurb(self, edition: Dict[str, Any], result: ScrapeResult) -> Optional[str]:
        """Description, which only some editions carry."""
        raw = edition.get("description")
        # Open Library stores this either as a plain string or as
        # ``{"type": "/type/text", "value": "..."}``.
        if isinstance(raw, dict):
            raw = raw.get("value")
        text = self.clean_text(raw)
        if not text:
            result.warn(
                "openlibrary: this edition record has no description; Open Library "
                "leaves it empty on most older editions"
            )
            return None
        return text

    def _metadata(
        self,
        hint: BookHint,
        record: Dict[str, Any],
        edition: Dict[str, Any],
        genres: List[str],
        result: ScrapeResult,
    ) -> BookMetadata:
        """Assemble the record, warning for anything Open Library does not publish."""
        metadata = self.new_metadata(hint)
        metadata.title = self.clean_text(record.get("title")) or None
        metadata.authors = self._names(record.get("authors"))
        publishers = self._names(record.get("publishers"))
        metadata.publisher = publishers[0] if publishers else None
        metadata.date_of_publication = self.iso_date(record.get("publish_date"))
        metadata.language = self._language(edition, result)
        metadata.genres = list(genres)

        # origin: searched for, never inferred -- same contract as every adapter.
        origin, searched = self.probe_origin([
            ("the /api/books record", record),
            ("the /isbn/ edition record", edition),
        ])
        metadata.origin = origin
        if not origin:
            self.origin_unavailable(result, self.origin_layers_clause(searched))
        if not metadata.title:
            result.warn("openlibrary: no title in the API record for this ISBN")
        return metadata

    def _genres(self, record: Dict[str, Any], result: ScrapeResult) -> List[str]:
        """Open Library ``subjects``, which are subject headings rather than genres.

        They mix true genres ("Fiction") with topical headings ("Grief", "Drowning")
        and Library-of-Congress strings ("FICTION / Literary"). All are reported as
        found -- trimming them to a curated list would be inventing a taxonomy Open
        Library does not publish -- but the warning says what they are.
        """
        subjects = self.dedupe(self._names(record.get("subjects")))
        if not subjects:
            result.warn("openlibrary: this record carries no subjects")
        else:
            result.warn(
                f"openlibrary: the {len(subjects)} genre(s) below are Open Library "
                "*subject headings*, which mix genres with topical and "
                "Library-of-Congress subjects"
            )
        return subjects

    # -- the hook ------------------------------------------------------------

    def _scrape_into(self, hint: BookHint, result: ScrapeResult) -> None:
        """Two JSON requests, then fill the result. No HTML, no browser."""
        record = self._books_api(hint.isbn13)
        if not record:
            result.warn(
                f"openlibrary: no record for ISBN {hint.isbn13}. Open Library is "
                "crowd-sourced, so an edition simply may not be catalogued"
            )
            return

        result.book_url = self.find_book_url(hint)
        edition = self._edition(hint.isbn13)
        genres = self._genres(record, result)

        result.genres = genres
        result.blurb = self._blurb(edition, result)
        result.cover_urls = self._covers(record, result)
        result.metadata = self._metadata(hint, record, edition, genres, result)

        # A catalogue, not a storefront: there is nothing to page through.
        result.reviews = []
        result.warn(
            "openlibrary: Open Library is a bibliographic catalogue and publishes no "
            "reader reviews, so the review count is 0 by nature, not by failure"
        )

        # ISBN-native, so its title/author is trustworthy enough to seed the
        # ISBN-hostile sources -- the API answered for this exact ISBN.
        if result.metadata and result.metadata.title:
            result.hint_updates = BookHint(
                isbn13=hint.isbn13,
                isbn10=self.clean_text(record.get("isbn_10", [None])[0])
                if isinstance(record.get("isbn_10"), list) and record.get("isbn_10")
                else hint.isbn10,
                title=result.metadata.title,
                authors=list(result.metadata.authors),
            )
        print("openlibrary: %s -> %d genre(s), %d cover(s), blurb %d chars"
              % (hint.isbn13, len(genres), len(result.cover_urls),
                 len(result.blurb or "")), file=sys.stderr)
