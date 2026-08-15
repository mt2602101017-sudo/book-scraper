"""Source-agnostic data containers passed between adapters, the pipeline and storage.

Nothing in this module talks to the network or the filesystem; these are plain
dataclasses plus the one JSON projection (:meth:`BookMetadata.to_json_dict`)
that defines the on-disk metadata contract.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

__all__ = ["BookHint", "ReviewItem", "BookMetadata", "ScrapeResult"]


@dataclass
class BookHint:
    """What we know about the book *before* scraping a given source.

    ``isbn13`` is always present and always normalised. ``title`` / ``authors``
    start empty and get filled in by the first source that resolves them
    (Goodreads, in practice) so that ISBN-hostile sources such as Audible and
    BookBub can fall back to a title+author search.
    """

    isbn13: str
    isbn10: Optional[str] = None
    title: Optional[str] = None
    authors: List[str] = field(default_factory=list)

    def describe(self) -> str:
        """Return a short human-readable form for log lines."""
        bits = [self.isbn13]
        if self.title:
            bits.append(repr(self.title))
        if self.authors:
            bits.append("by " + ", ".join(self.authors))
        return " ".join(bits)


@dataclass
class ReviewItem:
    """One customer/user review. Only ``text`` is guaranteed non-empty."""

    text: str
    reviewer: Optional[str] = None
    rating: Optional[str] = None
    date: Optional[str] = None
    url: Optional[str] = None

    def to_text_block(self) -> str:
        """Render as the plain-text body written to ``book_reviews/*.txt``.

        Header lines are only emitted for fields we actually found, so a
        review with no metadata is written as bare prose.
        """
        header: List[str] = []
        if self.reviewer:
            header.append(f"Reviewer: {self.reviewer}")
        if self.rating:
            header.append(f"Rating: {self.rating}")
        if self.date:
            header.append(f"Date: {self.date}")
        if self.url:
            header.append(f"URL: {self.url}")
        if header:
            return "\n".join(header) + "\n\n" + self.text
        return self.text


@dataclass
class BookMetadata:
    """The assignment's metadata record for one (book, source) pair."""

    isbn13: str
    title: Optional[str] = None
    authors: List[str] = field(default_factory=list)
    publisher: Optional[str] = None
    origin: Optional[str] = None
    date_of_publication: Optional[str] = None
    language: Optional[str] = None
    genres: List[str] = field(default_factory=list)

    def to_json_dict(self) -> Dict[str, Any]:
        """Serialise to the exact key set and order the contract mandates.

        Just the eight fields the assignment asks for. ``genre`` is the
        comma-separated string it specifies; missing scalars are JSON ``null``,
        never the string ``"None"``, never omitted.

        The diagnostics this once carried (``genres`` list, ``_source``,
        ``_source_url``, ``_scraped_at``, ``_edition_*``, ``_warnings``) are gone --
        on a 10 000-book file the warning arrays dominated the payload. The source is
        the filename, and the genre list is ``genre.split(", ")``.

        One real cost: a reader of this file alone can no longer tell that Kobo's
        ``publisher`` may describe a different printing than the ``isbn13`` it is
        filed under. That stays in the run output and the summary.
        """
        return {
            "isbn13": self.isbn13,
            "title": self.title,
            "authors": list(self.authors),
            "publisher": self.publisher,
            "origin": self.origin,
            "date_of_publication": self.date_of_publication,
            "language": self.language,
            "genre": ", ".join(self.genres) if self.genres else None,
        }


@dataclass
class ScrapeResult:
    """Everything one source produced for one book.

    Adapters return this from :meth:`bookscraper.base.BaseSource.scrape` and
    must never raise; partial results with populated ``warnings`` are the
    expected degraded outcome.
    """

    source: str
    isbn13: str
    book_url: Optional[str] = None
    metadata: Optional[BookMetadata] = None
    cover_urls: List[str] = field(default_factory=list)
    blurb: Optional[str] = None
    reviews: List[ReviewItem] = field(default_factory=list)
    genres: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    hint_updates: Optional[BookHint] = None

    def warn(self, message: str) -> None:
        """Append a de-duplicated warning. Adapters should use this freely."""
        if message and message not in self.warnings:
            self.warnings.append(message)

    def has_payload(self) -> bool:
        """True if this source produced anything worth writing to disk."""
        return bool(
            self.metadata
            or self.cover_urls
            or self.blurb
            or self.reviews
            or self.genres
        )
