"""Writing one source's result to disk, and deciding what that result *was*.

Artefacts are written as each source finishes, so an interrupted run keeps
everything already done. Metadata goes **last**, so the record is only claimed once
every artefact beside it has had its chance to fail.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, List, Optional, Tuple

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

#: Smallest plausible cover. Open Library answers a missing cover with a 43-byte
#: 1x1 transparent GIF and HTTP 200, which sniffs as a valid image and would be
#: filed as artwork. Anything this small is a placeholder, not a book cover.
MIN_COVER_BYTES = 512


def candidates(entry: Any) -> List[str]:
    """The URLs to try for one cover, best first.

    A ``cover_urls`` entry is normally a single URL, but may be a sequence of
    **equivalent** URLs for the same image -- different sizes or CDN routes. They are
    tried in order and the first that downloads wins, so one flaky route does not
    cost the cover. See :attr:`bookscraper.models.Result.cover_urls`.
    """
    if isinstance(entry, str):
        return [entry]
    if isinstance(entry, (list, tuple)):
        return [u for u in entry if isinstance(u, str) and u]
    return []


def download_cover(client: Any, urls: List[str], referer: Optional[str],
                   result: Result) -> Optional[Tuple[bytes, Optional[str]]]:
    """The first of ``urls`` that yields a real image, or ``None``."""
    for url in urls:
        got = client.download(url, referer=referer)
        if got is None:
            result.warn(f"cover download failed: {url}")
            continue
        if len(got[0]) < MIN_COVER_BYTES:
            result.warn(f"cover at {url} is only {len(got[0])} bytes, so it is a "
                        "'no cover available' placeholder rather than artwork")
            continue
        if len(urls) > 1 and url != urls[0]:
            result.warn(f"used the fallback cover {url} because the preferred size "
                        "could not be fetched")
        return got
    return None


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

    wanted = [c for entry in result.cover_urls if (c := candidates(entry))]
    if wanted and config.covers:
        # Numbering restarts at 1, so a previous run's higher-numbered covers would
        # otherwise linger and read back as if they belonged to this run.
        storage.purge("covers", isbn13, source)
        for urls in wanted:
            got = download_cover(client, urls, result.book_url, result)
            if got is None:
                # Recorded, not just warned: the metadata record is about to be
                # written, and without this the skip decision would treat the book
                # as finished and the cover would be lost for good.
                outcome.incomplete = True
                continue
            try:
                storage.cover(isbn13, source, outcome.covers + 1, got[0],
                              content_type=got[1], url=urls[0])
                outcome.covers += 1
            except WRITE_ERRORS as exc:
                result.warn(f"could not write cover {urls[0]}: {exc}")
                outcome.incomplete = True
    elif not wanted:
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
