"""Offline tests for the skip decision and the end-of-run report.

No network. The skip decision has no state of its own any more: it asks
``book_metadata/<source>_metadata.json`` whether it already holds the book. What
these pin down:

* a source with a record on disk is not asked again, and one without a record is;
* the decision is per **(ISBN, source)**, not per book;
* the index behind it stays correct as records are appended during a run, and is
  read from the file (not remembered from a previous process);
* ``--rescrape`` overrides it;
* a **trustworthy** empty is recorded in ``metrics/<source>_no_data.txt`` and skipped
  next run, while an untrustworthy one (the site was walling us, or was never
  reached) is **not** recorded, because a wall must never be written down as an
  absence -- that is the 629-book failure mode;
* the absent list is **merged, not truncated**, so a ``--end 100`` slice cannot
  destroy the 9 895 entries it did not look at, and it holds nothing but ISBNs so
  reading it needs no parser;
* an unreadable metadata file re-scrapes rather than crashing;
* skipping the seed source still leaves the ISBN-hostile sources a title/author
  hint -- otherwise a resumed run silently produces *different* results from a
  fresh one;
* the report separates a genuine miss from one recorded while the site was walling
  us, and writes exactly one file.

This file replaced ``test_ledger_metrics.py`` when ``scrape_ledger.jsonl`` was
removed.

Runnable either way:

    .venv/bin/python -m pytest tests/test_skip_and_report.py -q
    .venv/bin/python tests/test_skip_and_report.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from typing import List

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bookscraper.base import BaseSource  # noqa: E402
from bookscraper.metrics import BookRecord, NoDataIndex, RunReport  # noqa: E402
from bookscraper.models import BookMetadata  # noqa: E402
from bookscraper.pipeline import SEED_SOURCE, Pipeline, PipelineConfig  # noqa: E402
from bookscraper.storage import Storage, release_indexes  # noqa: E402


_ISBN = "9780143127550"
_OTHER = "9780062316097"


class _StubSource(BaseSource):
    """A source that always succeeds, and counts how often it was asked."""

    name = "stub"
    display_name = "Stub"
    calls: List[str] = []

    def find_book_url(self, hint):  # pragma: no cover - discovery not under test
        return None

    def _scrape_into(self, hint, result) -> None:
        type(self).calls.append(hint.isbn13)
        result.metadata = BookMetadata(isbn13=hint.isbn13, title="Stub Book")


class _EmptySource(_StubSource):
    """A source that is reached, answers, and has no such book.

    It contacts a host, because that is what makes the miss *trustworthy*: a run
    that never reached the site has learned nothing about the catalogue.
    """

    name = "hollow"
    display_name = "Hollow"
    host = "https://hollow.example/search?q=x"

    def _scrape_into(self, hint, result) -> None:
        type(self).calls.append(hint.isbn13)
        self.client._throttle(type(self).host)   # the request that came back empty
        # No metadata, no artefacts: exactly what a genuine miss looks like.


class _UnreachedSource(_StubSource):
    """A source that produced nothing without ever reaching the site."""

    name = "silent"
    display_name = "Silent"

    def _scrape_into(self, hint, result) -> None:
        type(self).calls.append(hint.isbn13)


def _pipeline(root: Path, source=_StubSource, isbn: str = _ISBN,
              skip_existing: bool = True, report: RunReport | None = None,
              no_data: NoDataIndex | None = None) -> Pipeline:
    config = PipelineConfig(isbn=isbn, out_dir=root, min_reviews=0,
                            download_covers=False, browser="never",
                            min_delay=0, max_delay=0)
    pipeline = Pipeline(config, capture_summary=True, report=report,
                        skip_existing=skip_existing, no_data=no_data)
    pipeline.select_sources = lambda: [(source.name, source)]  # type: ignore[assignment]
    return pipeline


def _run(root: Path, **kw) -> Pipeline:
    pipeline = _pipeline(root, **kw)
    pipeline.run()
    return pipeline


# -- the skip decision ---------------------------------------------------------


def test_a_source_with_a_record_is_not_asked_again(tmp_path: Path) -> None:
    release_indexes()
    _StubSource.calls = []

    first = _run(tmp_path)
    assert _StubSource.calls == [_ISBN], "the first run must actually scrape"
    assert first.outcomes[0].has_metadata

    second = _run(tmp_path)
    assert _StubSource.calls == [_ISBN], "the second run must not re-scrape"
    assert second.outcomes[0].status == "skipped"
    assert second.outcomes[0].warnings, "a skip must explain itself in the summary"
    assert "stub_metadata.json" in second.outcomes[0].warnings[0]


def test_the_decision_is_per_isbn_not_per_file(tmp_path: Path) -> None:
    """One book being present must not skip a different book."""
    release_indexes()
    _StubSource.calls = []
    _run(tmp_path, isbn=_ISBN)
    _run(tmp_path, isbn=_OTHER)
    assert _StubSource.calls == [_ISBN, _OTHER], "a second book must still be scraped"

    records = Storage(tmp_path).read_metadata("stub")
    assert sorted(r["isbn13"] for r in records) == sorted([_ISBN, _OTHER])


def test_the_decision_is_per_source(tmp_path: Path) -> None:
    """A goodreads record must not skip kobo."""
    release_indexes()
    storage = Storage(tmp_path)
    storage.ensure_dirs()
    storage.append_metadata("stub", {"isbn13": _ISBN, "title": "Stub Book"})

    assert storage.has_record("stub", _ISBN) is True
    assert storage.has_record("hollow", _ISBN) is False, (
        "one source's record must not settle another source"
    )


def test_a_record_written_this_run_is_seen_immediately(tmp_path: Path) -> None:
    """The cached index must not go stale behind an append inside one run."""
    release_indexes()
    storage = Storage(tmp_path)
    storage.ensure_dirs()
    assert storage.has_record("stub", _ISBN) is False
    storage.append_metadata("stub", {"isbn13": _ISBN, "title": "Stub Book"})
    assert storage.has_record("stub", _ISBN) is True


def test_the_index_is_read_from_the_file_not_remembered(tmp_path: Path) -> None:
    """A fresh process must see what a previous one wrote."""
    release_indexes()
    storage = Storage(tmp_path)
    storage.ensure_dirs()
    storage.append_metadata("stub", {"isbn13": _ISBN, "title": "Stub Book"})

    release_indexes()                       # stands in for a new process
    assert Storage(tmp_path).has_record("stub", _ISBN) is True


def test_rescrape_overrides_an_existing_record(tmp_path: Path) -> None:
    release_indexes()
    _StubSource.calls = []
    _run(tmp_path)
    _run(tmp_path, skip_existing=False)
    assert _StubSource.calls == [_ISBN, _ISBN], "--rescrape must re-fetch"

    records = Storage(tmp_path).read_metadata("stub")
    assert len(records) == 1, "a re-scrape replaces the record, never duplicates it"


def test_a_source_with_no_such_book_is_recorded_and_then_skipped(tmp_path: Path) -> None:
    """The one answer the metadata file cannot hold gets its own list.

    Without this, ~40 % of the shipped CSV's pairs (Audible misses ~90 %, Kobo ~59 %)
    are re-fetched on every run -- ~29 h per full run of the four default sources.
    """
    release_indexes()
    _EmptySource.calls = []
    index = NoDataIndex(tmp_path)

    first = _run(tmp_path, source=_EmptySource, no_data=index)
    assert first.outcomes[0].status == "empty"
    assert index.contains("hollow", _ISBN), "a trustworthy empty must be recorded"
    written = index.flush()
    assert [p.name for p in written] == ["hollow_no_data.txt"]
    assert written[0].parent == tmp_path, (
        "the directory is given to the constructor, not derived from --out"
    )
    # Nothing but ISBNs, so the reader is set(text.split()) and there is no format
    # to get wrong.
    body = written[0].read_text(encoding="utf-8")
    assert body == f"{_ISBN}\n", repr(body)

    second = _run(tmp_path, source=_EmptySource, no_data=NoDataIndex(tmp_path))
    assert _EmptySource.calls == [_ISBN], "the site must not be asked twice"
    assert second.outcomes[0].status == "skipped"
    assert "no such book" in second.outcomes[0].warnings[0]


def test_an_empty_from_a_walled_site_is_not_recorded_as_absent(tmp_path: Path) -> None:
    """A wall must never be written down as an absence -- the 629-book failure mode."""
    release_indexes()
    _EmptySource.calls = []
    index = NoDataIndex(tmp_path)
    pipeline = _pipeline(tmp_path, source=_EmptySource, no_data=index)
    # The host this source is about to contact is already known to be walling us.
    pipeline.client._record_block(_EmptySource.host, "HTTP 403 wall")
    pipeline.run()

    assert pipeline.outcomes[0].status == "blocked", "a wall is not an empty"
    assert not index.contains("hollow", _ISBN), (
        "an empty seen while walled is not a statement about the catalogue"
    )
    assert index.flush() == [], "nothing to write means no file"


def test_an_empty_with_no_request_made_is_not_recorded(tmp_path: Path) -> None:
    """If the site was never reached, it cannot have answered."""
    release_indexes()
    _UnreachedSource.calls = []
    index = NoDataIndex(tmp_path)
    pipeline = _run(tmp_path, source=_UnreachedSource, no_data=index)
    assert pipeline.outcomes[0].status == "empty"
    assert not index.contains("silent", _ISBN), (
        "requests_made == 0 means the site never answered, so there is nothing to record"
    )


def test_the_absent_list_is_merged_not_truncated(tmp_path: Path) -> None:
    """A partial run must not destroy entries it never looked at.

    Regression risk: metrics/ is otherwise rewritten whole every run, so a
    ``--end 100`` slice would leave 100 entries where 10 000 had been.
    """
    first = NoDataIndex(tmp_path)
    for n in range(5):
        first.note("kobo", f"978000000000{n}")
    first.flush()

    second = NoDataIndex(tmp_path)
    second.note("kobo", "9789999999999")
    second.flush()

    third = NoDataIndex(tmp_path)
    assert third.count("kobo") == 6, "the earlier entries must survive"
    assert third.contains("kobo", "9780000000000")
    assert third.contains("kobo", "9789999999999")


def test_rescrape_ignores_the_absent_list(tmp_path: Path) -> None:
    release_indexes()
    _EmptySource.calls = []
    index = NoDataIndex(tmp_path)
    index.note("hollow", _ISBN)
    index.flush()

    _run(tmp_path, source=_EmptySource, no_data=NoDataIndex(tmp_path, enabled=False),
         skip_existing=False)
    assert _EmptySource.calls == [_ISBN], "--rescrape must re-check a known absence"


def test_the_absent_list_survives_a_damaged_file(tmp_path: Path) -> None:
    """An unreadable list means "we do not know", which re-checks rather than skips."""
    (tmp_path / "kobo_no_data.txt").write_bytes(b"\xff\xfe not utf-8 at all")
    index = NoDataIndex(tmp_path)
    assert index.contains("kobo", _ISBN) is False


def test_blank_lines_and_stray_whitespace_are_tolerated(tmp_path: Path) -> None:
    """The reader is ``set(text.split())``, so hand-editing the file is safe."""
    (tmp_path / "kobo_no_data.txt").write_text(
        "9780143127550\n\n   9780062316097   \n\n", encoding="utf-8")
    index = NoDataIndex(tmp_path)
    assert index.count("kobo") == 2
    assert index.contains("kobo", "9780143127550")
    assert index.contains("kobo", "9780062316097")


def test_a_fully_skipped_run_exits_zero(tmp_path: Path) -> None:
    """The data is on disk; reporting failure would be wrong."""
    release_indexes()
    _StubSource.calls = []
    _run(tmp_path)
    assert _pipeline(tmp_path).run() == 0, (
        "everything already scraped is success, not failure"
    )


def test_an_unreadable_metadata_file_scrapes_rather_than_crashing(tmp_path: Path) -> None:
    release_indexes()
    _StubSource.calls = []
    storage = Storage(tmp_path)
    storage.ensure_dirs()
    storage.metadata_path("stub").write_text("{ this is not json", encoding="utf-8")

    pipeline = _run(tmp_path)
    assert _StubSource.calls == [_ISBN], "an unusable record file must not skip"
    assert pipeline.outcomes[0].has_metadata


def test_skipping_the_seed_source_still_seeds_the_hint(tmp_path: Path) -> None:
    """Otherwise a resumed run quietly produces different results from a fresh one.

    BookBub, Kobo and Audible index no ISBN and can only search by title+author,
    which the seed source supplies. If a skipped seed left the hint empty, their
    hits would silently become misses.

    Keyed to ``pipeline.SEED_SOURCE``, not to a literal name: the seed moved from
    Goodreads to Open Library, and hard-coding it here made this test pass while
    testing the wrong source.
    """
    release_indexes()
    storage = Storage(tmp_path)
    storage.ensure_dirs()
    storage.append_metadata(SEED_SOURCE, {
        "isbn13": _ISBN,
        "title": "Everything I Never Told You",
        "authors": ["Celeste Ng"],
    })

    class _Seed(_StubSource):
        name = SEED_SOURCE
        display_name = SEED_SOURCE.title()

    pipeline = _run(tmp_path, source=_Seed)
    assert pipeline.outcomes[0].status == "skipped"
    assert pipeline.hint is not None
    assert pipeline.hint.title == "Everything I Never Told You", (
        "the hint must be recovered from the stored record when the seed is skipped"
    )
    assert pipeline.hint.authors == ["Celeste Ng"]


# -- the report ----------------------------------------------------------------


def test_the_report_separates_trustworthy_from_suspect(tmp_path: Path) -> None:
    report = RunReport(tmp_path)
    report.record_book(BookRecord(isbn13="REALMISS", source="audible",
                                  status="empty", requests_made=3))
    report.record_book(BookRecord(isbn13="WALLED", source="audible", status="empty",
                                  requests_made=2,
                                  blocked_hosts=["www.audible.com"]))
    written = report.write_all()

    assert [p.name for p in written] == ["audible_isbns.txt"], (
        "one report file per source, so two --sources runs cannot collide"
    )
    text = written[0].read_text(encoding="utf-8")
    assert "NOT TRUSTWORTHY (1)" in text
    assert "WALLED" in text.split("NOT TRUSTWORTHY")[1]
    # The genuine miss stays in the plain FAILED list.
    assert "REALMISS" in text.split("NOT TRUSTWORTHY")[0]
    assert "walled by www.audible.com" in text
    assert report.suspect_empties == {"audible": 1}


def test_suspicion_applies_only_to_empties(tmp_path: Path) -> None:
    """A successful scrape that met a wall on the way is still a success."""
    assert not BookRecord(isbn13=_ISBN, source="goodreads", status="partial",
                          has_metadata=True, requests_made=0,
                          blocked_hosts=["www.goodreads.com"]).suspect_empty
    assert not BookRecord(isbn13=_ISBN, source="kobo", status="blocked",
                          requests_made=0).suspect_empty
    assert BookRecord(isbn13=_ISBN, source="kobo", status="empty",
                      requests_made=0).suspect_empty


def test_the_report_counts_skips_for_the_digest(tmp_path: Path) -> None:
    report = RunReport(tmp_path)
    assert report.summarise() == "", "nothing skipped means nothing to report"
    report.note_skipped("goodreads")
    report.note_skipped("goodreads")
    report.note_skipped("audible")
    assert report.total_skipped == 3
    summary = report.summarise()
    assert "3 (ISBN, source) pair(s) skipped" in summary
    assert "goodreads 2" in summary and "audible 1" in summary


def test_a_disabled_report_writes_nothing(tmp_path: Path) -> None:
    directory = tmp_path / "metrics"
    report = RunReport(directory, enabled=False)
    report.record_book(BookRecord(isbn13=_ISBN, source="stub", status="ok",
                                  has_metadata=True))
    assert report.write_all() == []
    assert not directory.exists()


def test_the_report_survives_an_unwritable_directory(tmp_path: Path) -> None:
    """A reporting failure must never cost a scrape."""
    blocker = tmp_path / "metrics"
    blocker.write_text("I am a file, not a directory", encoding="utf-8")
    report = RunReport(blocker)
    report.record_book(BookRecord(isbn13=_ISBN, source="stub", status="ok",
                                  has_metadata=True))
    assert report.write_all() == [], "it degrades to a warning, it does not raise"


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
