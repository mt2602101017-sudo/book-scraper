"""Writing one source's result to disk, and deciding what that result *was*.

Artefacts are written as each source finishes, so an interrupted run keeps
everything already done. Metadata goes **last**, so the record is only claimed once
every artefact beside it has had its chance to fail.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, List

from .models import Book, Result
from .parse import dedupe
from .transport import warn

if TYPE_CHECKING:  # pragma: no cover
    from .runner import Config, Outcome
    from .storage import Storage

#: What one artefact write may fail with before it degrades to a warning. OSError
#: covers the filesystem; ValueError covers unencodable scraped text
#: (UnicodeEncodeError is one) and malformed scraped URLs; TypeError covers a payload
#: shape no serialiser will accept.
WRITE_ERRORS = (OSError, ValueError, TypeError)


def genres_of(result: Result) -> List[str]:
    """Union of the result's and the record's genres, order preserved."""
    found = list(result.genres or [])
    if result.book is not None:
        found += list(result.book.genres or [])
    return [g for g in dedupe(str(x).strip() for x in found) if g]


def write(result: Result, outcome: "Outcome", storage: "Storage", client: Any,
          config: "Config") -> None:
    """Write every artefact this result carries, recording counts on ``outcome``."""
    source, isbn13 = result.source, result.isbn13
    outcome.book_url = result.book_url

    urls = [u for u in result.cover_urls if u]
    if urls and config.covers:
        # Numbering restarts at 1, so a previous run's higher-numbered covers would
        # otherwise linger and read back as if they belonged to this run.
        storage.purge("covers", isbn13, source)
        for url in urls:
            if (got := client.download(url, referer=result.book_url)) is None:
                result.warn(f"cover download failed: {url}")
                continue
            try:
                storage.cover(isbn13, source, outcome.covers + 1, got[0],
                              content_type=got[1], url=url)
                outcome.covers += 1
            except WRITE_ERRORS as exc:
                result.warn(f"could not write cover {url}: {exc}")
    elif not urls:
        result.warn("no cover image URLs found")

    if blurb := (result.blurb or "").strip():
        try:
            storage.blurb(isbn13, source, blurb)
            outcome.blurb = len(blurb)
        except WRITE_ERRORS as exc:
            result.warn(f"could not write blurb: {exc}")
    else:
        result.warn("no blurb/description found")

    reviews = [r for r in result.reviews if r and r.text.strip()]
    if config.max_reviews is not None:
        reviews = reviews[:config.max_reviews]
    if reviews:
        storage.purge("reviews", isbn13, source)
    for review in reviews:
        try:
            # A write counter, not an enumerate index, so a review that fails to
            # write leaves no gap in the 1-based numbering.
            storage.review(isbn13, source, outcome.reviews + 1, review.to_block())
            outcome.reviews += 1
        except (*WRITE_ERRORS, AttributeError) as exc:
            result.warn(f"could not write a review: {exc}")
    if config.min_reviews and outcome.reviews < config.min_reviews:
        result.warn(f"only {outcome.reviews} review(s) collected, fewer than the "
                    f"requested minimum of {config.min_reviews}")

    genres = genres_of(result)
    if genres:
        try:
            storage.genres(isbn13, source, genres)
            outcome.genres = len(genres)
        except WRITE_ERRORS as exc:
            result.warn(f"could not write genres: {exc}")
    else:
        result.warn("no genres found")

    book = result.book
    if book is not None:
        book.genres = book.genres or genres
        book.isbn13 = book.isbn13 or isbn13
        try:
            storage.meta.append(source, book.to_json())
            outcome.has_metadata = True
        except WRITE_ERRORS as exc:
            result.warn(f"could not write metadata: {exc}")
    else:
        result.warn("no metadata could be parsed")

    for name in Book.FIELDS:
        value = genres if name == "genres" else getattr(book, name, None)
        (outcome.found if value else outcome.missing).append(name)


def classify(outcome: "Outcome", result: Result, client: Any) -> None:
    """Set ``status`` and ``trustworthy`` from what came back.

    ``empty`` and ``blocked`` must not be confused: "Kobo does not sell this 1980s
    paperback as an ebook" is a finding, and "Kobo walled us" is a problem to act on
    -- and only the first may ever be recorded as an absence.
    """
    outcome.warnings = list(result.warnings)
    blocked = (client.block_reason(result.book_url or "") or client.touched_block())
    if not outcome.has_metadata and not result.has_payload():
        outcome.status = "blocked" if blocked else "empty"
        # Zero hosts reached means the site was never actually asked, so "no such
        # book" cannot be a finding about it either.
        outcome.trustworthy = not blocked and client.contacted() > 0
    elif outcome.missing or outcome.warnings:
        outcome.status = "partial"
    else:
        outcome.status = "ok"
    warn(f"{outcome.name}: metadata={'yes' if outcome.has_metadata else 'no'} "
         f"fields={len(outcome.found)}/{len(outcome.found) + len(outcome.missing)} "
         f"covers={outcome.covers} reviews={outcome.reviews} "
         f"genres={outcome.genres} warnings={len(outcome.warnings)}")
