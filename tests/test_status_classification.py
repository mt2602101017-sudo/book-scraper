"""Offline tests for ``empty`` vs ``blocked``, the report's most important word.

No network: :class:`HttpClient` is used only for its block bookkeeping and its
host tracking, with ``browser='never'`` and zero delays.

Why this file exists
--------------------
A real 9 995-ISBN Goodreads run filed **629 books as ``empty``** -- "the site
answered and genuinely has no such book" -- when they had in fact been
WAF-challenged. Every one of the six retried later resolved with 6/7 fields.

Neither status is skipped now (neither leaves a metadata record, so both are
re-attempted), but the distinction still decides what the report *claims*, and
that is worth as much: "Kobo does not sell this 1980s paperback as an ebook" is a
finding about a catalogue, while "Kobo walled us" is a problem to act on. A report
that merges them sends a reader hunting for a parser bug that does not exist.

The cause was the question being asked. ``outcome.blocked_hosts`` holds hosts that
started blocking *during this source's turn*, but one :class:`HttpClient` serves the
whole batch, so a wall is recorded the **first** time any book hits it. Every later
victim of the same wall saw no *new* block and was filed ``empty``. The test below
reproduces exactly that shape.

Runnable either way:

    .venv/bin/python -m pytest tests/test_status_classification.py -q
    .venv/bin/python tests/test_status_classification.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bookscraper.http_client import HttpClient  # noqa: E402
from bookscraper.models import BookMetadata, ScrapeResult  # noqa: E402
from bookscraper.pipeline import (  # noqa: E402)
    Pipeline,
    PipelineConfig,
    SourceOutcome,
)


_ISBN = "9780143127550"
_WAF = "AWS WAF challenge response (HTTP 202, x-amzn-waf-action: challenge)"


def _client() -> HttpClient:
    return HttpClient(min_delay=0, max_delay=0, browser="never")


def _pipeline(client: HttpClient, root: Path) -> Pipeline:
    return Pipeline(
        PipelineConfig(isbn=_ISBN, out_dir=root),
        client=client,
    )


def _touch(client: HttpClient, url: str) -> None:
    """Record a contact with ``url``'s host, as a real fetch would."""
    client.begin_host_tracking()
    client._throttle(url)


def _classify(pipeline: Pipeline, source: str, *, new_blocks: dict | None = None,
              metadata: bool = False) -> str:
    outcome = SourceOutcome(name=source, display_name=source.title())
    outcome.blocked_hosts = dict(new_blocks or {})
    outcome.has_metadata = metadata
    result = ScrapeResult(source=source, isbn13=_ISBN)
    if metadata:
        result.metadata = BookMetadata(isbn13=_ISBN, title="A Book")
    return pipeline._status_for(outcome, result)


# -- the regression -------------------------------------------------------------


def test_a_wall_found_on_an_earlier_book_still_reads_as_blocked(tmp_path: Path) -> None:
    """The 629-book bug, reproduced exactly.

    An earlier book records the wall; this book hits the same wall, so nothing is
    *newly* blocked. It must still be ``blocked``, not ``empty``.
    """
    client = _client()
    try:
        # An earlier book already discovered the wall.
        client._record_block("https://www.goodreads.com/book/isbn/other", _WAF)
        pipeline = _pipeline(client, tmp_path)

        # This book contacts the same, already-known-blocked host and gets nothing.
        _touch(client, f"https://www.goodreads.com/book/isbn/{_ISBN}")
        status = _classify(pipeline, "goodreads", new_blocks={})

        assert status == "blocked", (
            "a book blocked by a wall discovered earlier was filed as 'empty', "
            "reporting a wall as a fact about the catalogue"
        )
    finally:
        client.close()


def test_the_first_victim_of_a_wall_is_still_blocked(tmp_path: Path) -> None:
    """The case the old code did get right must keep working."""
    client = _client()
    try:
        pipeline = _pipeline(client, tmp_path)
        _touch(client, f"https://www.goodreads.com/book/isbn/{_ISBN}")
        status = _classify(
            pipeline, "goodreads", new_blocks={"www.goodreads.com": _WAF}
        )
        assert status == "blocked"
    finally:
        client.close()


# -- what must NOT change ------------------------------------------------------


def test_a_genuine_absence_is_still_empty(tmp_path: Path) -> None:
    """Audible legitimately does not carry ~83 % of this project's CSV.

    Those are real answers about a real catalogue and must read as ``empty``, not as
    a failure to get an answer.
    """
    client = _client()
    try:
        pipeline = _pipeline(client, tmp_path)
        _touch(client, "https://www.audible.com/search?keywords=x")
        assert _classify(pipeline, "audible", new_blocks={}) == "empty"
    finally:
        client.close()


def test_another_sources_wall_does_not_contaminate_this_one(tmp_path: Path) -> None:
    """A Kobo block must not make an Audible miss look transient.

    This is why the check is "a host *I contacted*" rather than "any known block".
    """
    client = _client()
    try:
        client._record_block("https://www.kobo.com/us/en/search", "HTTP 403 wall")
        pipeline = _pipeline(client, tmp_path)
        _touch(client, "https://www.audible.com/search?keywords=x")
        assert _classify(pipeline, "audible", new_blocks={}) == "empty"
    finally:
        client.close()


def test_a_source_that_contacted_nothing_is_empty(tmp_path: Path) -> None:
    """No contact means no evidence of a block, whatever else is known."""
    client = _client()
    try:
        client._record_block("https://www.goodreads.com/x", _WAF)
        pipeline = _pipeline(client, tmp_path)
        client.begin_host_tracking()          # tracking on, but nothing fetched
        assert _classify(pipeline, "goodreads", new_blocks={}) == "empty"
    finally:
        client.close()


def test_a_successful_scrape_is_never_blocked(tmp_path: Path) -> None:
    """Metadata in hand outranks any wall met along the way.

    Goodreads is often WAF-challenged on the static path and then succeeds through
    the browser; that book has real data and is a success, not a block.
    """
    client = _client()
    try:
        client._record_block("https://www.goodreads.com/x", _WAF)
        pipeline = _pipeline(client, tmp_path)
        _touch(client, f"https://www.goodreads.com/book/isbn/{_ISBN}")
        status = _classify(pipeline, "goodreads", new_blocks={}, metadata=True)
        assert status in ("ok", "partial"), f"got {status!r}"
    finally:
        client.close()


# -- host tracking itself ------------------------------------------------------


def test_host_tracking_records_every_contacted_host(tmp_path: Path) -> None:
    client = _client()
    try:
        client.begin_host_tracking()
        for url in ("https://www.kobo.com/a", "https://ratingsapi.kobo.com/b",
                    "https://cdn.kobo.com/c", "https://www.kobo.com/d"):
            client._throttle(url)
        assert client.hosts_contacted() == {
            "www.kobo.com", "ratingsapi.kobo.com", "cdn.kobo.com",
        }
    finally:
        client.close()


def test_tracking_resets_per_source(tmp_path: Path) -> None:
    """Each source starts clean, or one source's hosts would leak into the next."""
    client = _client()
    try:
        client.begin_host_tracking()
        client._throttle("https://www.goodreads.com/a")
        assert client.hosts_contacted() == {"www.goodreads.com"}

        client.begin_host_tracking()
        client._throttle("https://www.audible.com/b")
        assert client.hosts_contacted() == {"www.audible.com"}
    finally:
        client.close()


def test_nothing_is_tracked_before_it_is_asked_for(tmp_path: Path) -> None:
    """A client nobody asked to track must not accumulate state."""
    client = _client()
    try:
        client._throttle("https://www.goodreads.com/a")
        assert client.hosts_contacted() == set()
    finally:
        client.close()


def _run_all() -> int:
    failures = 0
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    for name, fn in tests:
        with tempfile.TemporaryDirectory() as directory:
            try:
                fn(Path(directory))
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc}")
            except Exception as exc:  # noqa: BLE001 - report, do not mask
                failures += 1
                print(f"ERROR {name}: {type(exc).__name__}: {exc}")
            else:
                print(f"ok   {name}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(_run_all())
