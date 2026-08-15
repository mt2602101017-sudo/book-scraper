"""Source-agnostic containers passed between adapters, the runner and storage.

Nothing here touches the network or the filesystem. :meth:`Book.to_json` is the
on-disk metadata contract: exactly eight keys, in this order, ``null`` rather
than omitted when a source could not find a field.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class Hint:
    """What is known about a book *before* a given source is scraped.

    ``title``/``authors`` start empty and are filled by the first source that
    resolves them (Open Library, in practice), so the storefronts that index no
    ISBN -- Kobo, Audible, BookBub -- have something to search by.
    """

    isbn13: str
    isbn10: Optional[str] = None
    title: Optional[str] = None
    authors: List[str] = field(default_factory=list)


@dataclass
class Review:
    """One reader review. Only ``text`` is guaranteed non-empty."""

    text: str
    reviewer: Optional[str] = None
    rating: Optional[str] = None
    date: Optional[str] = None
    url: Optional[str] = None

    def to_block(self) -> str:
        """Render as written to ``book_reviews/*.txt``: found headers, then prose."""
        pairs = (("Reviewer", self.reviewer), ("Rating", self.rating),
                 ("Date", self.date), ("URL", self.url))
        header = [f"{label}: {value}" for label, value in pairs if value]
        return "\n".join(header) + "\n\n" + self.text if header else self.text


@dataclass
class Book:
    """One (book, source) metadata record -- the assignment's eight fields."""

    isbn13: str
    title: Optional[str] = None
    authors: List[str] = field(default_factory=list)
    publisher: Optional[str] = None
    origin: Optional[str] = None
    date_of_publication: Optional[str] = None
    language: Optional[str] = None
    genres: List[str] = field(default_factory=list)

    #: Fields counted as found/missing in the run summary.
    FIELDS = ("title", "authors", "publisher", "origin",
              "date_of_publication", "language", "genres")

    def to_json(self) -> Dict[str, Any]:
        """The exact key set and order written to ``<source>_metadata.json``."""
        return {
            "isbn13": self.isbn13,
            "title": self.title,
            "authors": list(self.authors),
            "publisher": self.publisher,
            "origin": self.origin,
            "date_of_publication": self.date_of_publication,
            "language": self.language,
            "genre": ", ".join(self.genres) or None,
        }


@dataclass
class Result:
    """Everything one source produced for one book.

    Adapters never raise: a partial result is the expected degraded outcome, so
    ``warnings`` carries what could not be found rather than an exception.
    """

    source: str
    isbn13: str
    book_url: Optional[str] = None
    book: Optional[Book] = None
    cover_urls: List[str] = field(default_factory=list)
    blurb: Optional[str] = None
    reviews: List[Review] = field(default_factory=list)
    genres: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    hint: Optional[Hint] = None

    def warn(self, message: str) -> None:
        """Append a de-duplicated warning."""
        if message and message not in self.warnings:
            self.warnings.append(message)

    def has_payload(self) -> bool:
        """True if anything worth writing to disk came back."""
        return bool(self.book or self.cover_urls or self.blurb
                    or self.reviews or self.genres)
