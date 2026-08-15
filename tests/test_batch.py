"""Offline tests for sequential batching, the courtesy delay, and cleanup.

No network and no browser: :class:`HttpClient` is exercised through
:meth:`_throttle` alone, and the batch runner is driven with a stub that replaces
``_run_one``. What these pin down is the part that is easy to get quietly wrong:

* the courtesy delay is real, randomised, and tracked **per host**, so waiting out
  one site never charges another for it -- the whole politeness claim rests on it;
* a run scrapes every ISBN exactly once, in file order, and survives a book that
  raises;
* an interrupt accounts for every ISBN exactly once and hands back a ``--start``
  that re-scrapes at worst the book it landed in and skips nothing;
* cleanup deletes only what it should, refuses dangerous targets, and leaves the
  ISBNs a resumed run is *not* touching alone.

This file replaced ``test_batch_parallel.py`` when the thread pool was removed.
Its assertions about pool size, per-thread throttling and captured summary tables
went with it; everything else here is the same guarantee, tested against a
sequential runner.

Runnable either way:

    .venv/bin/python -m pytest tests/test_batch.py -q
    .venv/bin/python tests/test_batch.py
"""

from __future__ import annotations

import io
import contextlib
import sys
import tempfile
import time
from pathlib import Path
from typing import List

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bookscraper import batch as B  # noqa: E402
from bookscraper import isbn as I  # noqa: E402
from bookscraper.csv_input import CsvIsbns, IsbnEntry  # noqa: E402
from bookscraper.http_client import HttpClient  # noqa: E402
from bookscraper.pipeline import PipelineConfig  # noqa: E402


def _entries(count: int) -> List[IsbnEntry]:
    """``count`` distinct, checksum-valid entries with real row numbers."""
    out = []
    for n in range(count):
        body = f"978014312{n:03d}"
        out.append(
            IsbnEntry(isbn13=body + I.isbn13_check_digit(body), row=n + 2)
        )
    return out


def _config(root: Path, **kw) -> B.BatchConfig:
    kw.setdefault("clean", False)
    return B.BatchConfig(base=PipelineConfig(isbn="", out_dir=root), **kw)


def _run(entries, config) -> B.BatchReport:
    """Run a batch with its stdout swallowed (the digest is not under test)."""
    source = CsvIsbns(path=Path("fake.csv"), entries=list(entries))
    with contextlib.redirect_stdout(io.StringIO()):
        return B.run_batch(source, config)


# -- the courtesy delay --------------------------------------------------------


def test_consecutive_requests_to_one_host_are_spaced_by_the_delay(
    tmp_path: Path,
) -> None:
    """The politeness guarantee, measured rather than asserted in prose."""
    delay = 0.25
    client = HttpClient(min_delay=delay, max_delay=delay, browser="never")
    stamps: List[float] = []
    for _ in range(4):
        client._throttle("https://one.example/page")
        stamps.append(time.monotonic())

    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    # Allow a small scheduling tolerance below the nominal delay.
    assert all(gap >= delay * 0.9 for gap in gaps), (
        f"same-host requests were not spaced by ~{delay}s: {gaps}"
    )
    client.close()


def test_the_delay_clock_is_kept_per_host_not_globally(tmp_path: Path) -> None:
    """Waiting out Goodreads must not also charge Kobo for the wait.

    Asserted on the state rather than on wall clock: with one request in flight at
    a time the two waits are paid serially either way, so timing cannot tell a
    per-host clock from a global one -- but a single shared deadline would show up
    here as one entry instead of three.
    """
    client = HttpClient(min_delay=0.01, max_delay=0.02, browser="never")
    for host in ("a.example", "b.example", "c.example"):
        client._throttle(f"https://{host}/p")
    assert set(client._next_allowed_at) == {"a.example", "b.example", "c.example"}, (
        "each host must carry its own next-allowed timestamp"
    )
    client.close()


def test_the_delay_is_randomised_not_a_fixed_sleep(tmp_path: Path) -> None:
    """A fixed sleep is a recognisable machine fingerprint; the range is the point."""
    client = HttpClient(min_delay=0.05, max_delay=0.35, browser="never")
    client._throttle("https://one.example/p")
    deadlines = []
    for _ in range(8):
        before = client._next_allowed_at["one.example"]
        client._throttle("https://one.example/p")
        deadlines.append(client._next_allowed_at["one.example"] - before)
    assert len(set(round(d, 3) for d in deadlines)) > 1, (
        f"every delay was identical, so it is not randomised: {deadlines}"
    )
    assert all(0.05 <= d <= 0.35 + 0.01 for d in deadlines), deadlines
    client.close()


def test_a_block_is_recorded_once_and_readable_afterwards(tmp_path: Path) -> None:
    """One wall met by many books is one warning and one registry entry."""
    client = HttpClient(min_delay=0, max_delay=0, browser="never")
    for _ in range(5):
        client._record_block("https://walled.example/x", "HTTP 403 wall")
    assert client.blocks == {"walled.example": "HTTP 403 wall"}
    assert client.block_reason("https://walled.example/other") == "HTTP 403 wall"
    assert client.block_reason("https://fine.example/x") is None
    client.close()


# -- the batch -----------------------------------------------------------------


def _stub_run_one(state: dict):
    """Replace ``_run_one`` with a fast stub that records what ran, and when."""

    def fake(entry, config, client, run_report=None, skip_existing=True,
             no_data=None):
        state["live"] = state.get("live", 0) + 1
        state["peak"] = max(state.get("peak", 0), state["live"])
        state.setdefault("seen", []).append(entry.isbn13)
        state["live"] -= 1
        book = B.BookOutcome(entry=entry, out_dir=Path("/nonexistent"))
        book.exit_code = 0
        book.seconds = 0.01
        return book

    return fake


def test_every_isbn_runs_exactly_once_and_in_file_order(tmp_path: Path) -> None:
    entries = _entries(12)
    state: dict = {}
    original = B._run_one
    B._run_one = _stub_run_one(state)
    try:
        report = _run(entries, _config(tmp_path))
    finally:
        B._run_one = original

    assert len(report.books) == 12
    assert state["seen"] == [e.isbn13 for e in entries], "file order is the contract"
    assert len(set(state["seen"])) == 12, "no ISBN may be scraped twice"
    assert [b.isbn13 for b in report.books] == [e.isbn13 for e in entries]
    assert state["peak"] == 1, "two books must never be in flight at once"


def test_a_crashing_book_does_not_stop_the_batch(tmp_path: Path) -> None:
    entries = _entries(6)
    doomed = entries[2].isbn13
    original = B._run_one

    def fake(entry, config, client, run_report=None, skip_existing=True,
             no_data=None):
        if entry.isbn13 == doomed:
            raise RuntimeError("selector exploded")
        book = B.BookOutcome(entry=entry, out_dir=Path("/nonexistent"))
        book.exit_code = 0
        return book

    B._run_one = fake
    try:
        report = _run(entries, _config(tmp_path))
    finally:
        B._run_one = original

    assert len(report.books) == 6, "the crashed book still gets a row"
    assert len(report.succeeded) == 5
    failed = [b for b in report.books if not b.ok]
    assert len(failed) == 1 and failed[0].isbn13 == doomed
    assert "selector exploded" in (failed[0].error or "")
    assert report.exit_code() == 0, "five good books still mean success"


def test_an_interrupt_is_reported_and_gives_a_safe_resume_point(
    tmp_path: Path,
) -> None:
    """Ctrl-C must account for every ISBN and skip none on resume."""
    entries = _entries(10)
    counter = {"n": 0}
    original = B._run_one

    def fake(entry, config, client, run_report=None, skip_existing=True,
             no_data=None):
        counter["n"] += 1
        if counter["n"] == 4:
            raise KeyboardInterrupt
        book = B.BookOutcome(entry=entry, out_dir=Path("/nonexistent"))
        book.exit_code = 0
        return book

    B._run_one = fake
    try:
        report = _run(entries, _config(tmp_path))
    finally:
        B._run_one = original

    assert report.interrupted, "an interrupt must be reported"
    assert report.resume_at is not None

    completed = {b.isbn13 for b in report.books}
    not_attempted = {e.isbn13 for e in report.not_attempted}
    assert not completed & not_attempted, "an ISBN cannot be both done and pending"
    assert completed | not_attempted == {e.isbn13 for e in entries}, (
        "every ISBN must be accounted for after an interrupt"
    )

    # The resume point may re-scrape the book the interrupt landed in, but must
    # never skip one: everything before it has to have completed.
    skipped = [
        e.isbn13 for e in entries[: report.resume_at] if e.isbn13 not in completed
    ]
    assert skipped == [], (
        f"--start {report.resume_at} would silently skip un-scraped ISBNs: {skipped}"
    )
    # And the book that was interrupted mid-scrape is retried, not lost.
    assert entries[3].isbn13 in not_attempted


def test_stop_on_error_abandons_the_unstarted_isbns(tmp_path: Path) -> None:
    entries = _entries(10)
    original = B._run_one

    def fake(entry, config, client, run_report=None, skip_existing=True,
             no_data=None):
        book = B.BookOutcome(entry=entry, out_dir=Path("/nonexistent"))
        # The second row yields nothing, which under --stop-on-error ends the run.
        book.exit_code = 1 if entry.row == 3 else 0
        return book

    B._run_one = fake
    try:
        report = _run(entries, _config(tmp_path, continue_on_error=False))
    finally:
        B._run_one = original

    assert len(report.books) == 2, "the batch should stop on the failing book"
    accounted = {b.isbn13 for b in report.books} | {
        e.isbn13 for e in report.not_attempted
    }
    assert accounted == {e.isbn13 for e in entries}


# -- cleanup -------------------------------------------------------------------


def _populate(root: Path, isbn: str) -> Path:
    """Create a plausible previous-run directory for one ISBN."""
    directory = root / isbn
    (directory / "book_reviews").mkdir(parents=True, exist_ok=True)
    (directory / "book_reviews" / f"{isbn}_r_goodreads_99.txt").write_text("old")
    (directory / "book_metadata").mkdir(parents=True, exist_ok=True)
    (directory / "book_metadata" / "goodreads_metadata.json").write_text("{}")
    return directory


def test_clean_removes_only_the_isbns_this_run_will_scrape(tmp_path: Path) -> None:
    """The ISBNs a resumed run skips must keep their output -- that is the point."""
    entries = _entries(4)
    scraping, untouched = entries[:2], entries[2:]
    for entry in entries:
        _populate(tmp_path, entry.isbn13)

    removed, problems = B.prepare_output_dirs(scraping, _config(tmp_path, clean=True, flat=False))
    assert removed == 2 and problems == []
    for entry in scraping:
        assert not (tmp_path / entry.isbn13).exists()
    for entry in untouched:
        assert (tmp_path / entry.isbn13 / "book_metadata").is_dir(), (
            "an ISBN this run is not scraping must not be cleaned"
        )


def test_clean_leaves_stale_numbered_files_no_chance_to_survive(tmp_path: Path) -> None:
    entry = _entries(1)[0]
    stale = _populate(tmp_path, entry.isbn13) / "book_reviews" / \
        f"{entry.isbn13}_r_goodreads_99.txt"
    assert stale.exists()
    B.prepare_output_dirs([entry], _config(tmp_path, clean=True, flat=False))
    assert not stale.exists(), (
        "a previous run's higher-numbered review would blend into this run"
    )


def test_flat_clean_touches_only_the_five_artefact_dirs(tmp_path: Path) -> None:
    (tmp_path / "book_reviews").mkdir()
    (tmp_path / "book_reviews" / "old.txt").write_text("x")
    (tmp_path / "book_metadata").mkdir()
    keep_dir = tmp_path / "my_notes"
    keep_dir.mkdir()
    (keep_dir / "notes.md").write_text("mine")
    keep_file = tmp_path / "my-notes.csv"
    keep_file.write_text("something I keep here\n")

    removed, problems = B.prepare_output_dirs([], _config(tmp_path, flat=True, clean=True))
    assert removed == 2 and problems == []
    assert not (tmp_path / "book_reviews").exists()
    assert keep_dir.is_dir(), "an unrelated directory must be left alone"
    assert (keep_dir / "notes.md").read_text() == "mine"
    assert keep_file.is_file(), "a file kept beside the artefact dirs must survive"
    assert tmp_path.is_dir(), "the output root itself is never deleted"


def test_flat_clean_is_never_the_implicit_default(tmp_path: Path) -> None:
    """A shared-directory clean must be opted into, not inherited.

    Regression: cleaning defaulted on for the single-ISBN/``--flat`` form, whose
    targets carry no ISBN, so a plain ``main.py <isbn>`` silently deleted whatever
    *other* book had been scraped into ``--out``. It ate a previous run's output
    once; ``BatchConfig.clean`` must therefore default to False and ``main.py``
    must only set it from an explicit ``--clean``.
    """
    assert B.BatchConfig(base=PipelineConfig(isbn="")).clean is False, (
        "BatchConfig.clean must default to False so no caller deletes by accident"
    )

    # And run_batch must honour that: an existing shared tree survives a run that
    # did not ask for cleaning. (prepare_output_dirs itself always deletes -- it is
    # the callers that gate it -- so the guarantee under test is run_batch's.)
    (tmp_path / "book_reviews").mkdir()
    victim = tmp_path / "book_reviews" / "9999999999999_r_goodreads_1.txt"
    victim.write_text("someone else's book")

    original = B._run_one
    B._run_one = _stub_run_one({})
    try:
        report = _run(_entries(2), _config(tmp_path, flat=True))  # clean defaults off
    finally:
        B._run_one = original

    assert report.dirs_cleaned == 0
    assert victim.is_file() and victim.read_text() == "someone else's book", (
        "a run that did not ask to clean must not touch another book's output"
    )


def test_clean_refuses_dangerous_targets(tmp_path: Path) -> None:
    """Deletion is the one irreversible act here; the guards must hold."""
    assert B._refuse_to_delete(Path(tmp_path.anchor or "/")) is not None
    home = Path.home()
    assert B._refuse_to_delete(home) is not None
    assert B._refuse_to_delete(Path.cwd()) is not None
    assert B._refuse_to_delete(Path.cwd().parent) is not None
    a_file = tmp_path / "a-file.txt"
    a_file.write_text("x")
    assert B._refuse_to_delete(a_file) is not None
    # A plain scratch directory is fine.
    safe = tmp_path / "9780143127550"
    safe.mkdir()
    assert B._refuse_to_delete(safe) is None


def test_clean_reports_a_refusal_instead_of_deleting(tmp_path: Path) -> None:
    """A refused target is surfaced, not silently skipped."""
    config = _config(tmp_path, flat=True, clean=True)
    # Point a target at the working directory, which _refuse_to_delete rejects.
    original = B.clean_targets
    B.clean_targets = lambda entries, cfg: [Path.cwd()]
    try:
        removed, problems = B.prepare_output_dirs([], config)
    finally:
        B.clean_targets = original
    assert removed == 0
    assert len(problems) == 1 and "refusing to delete" in problems[0]


def test_run_batch_cleans_before_scraping_when_asked(tmp_path: Path) -> None:
    entries = _entries(3)
    for entry in entries:
        _populate(tmp_path, entry.isbn13)
    state: dict = {}
    original = B._run_one
    B._run_one = _stub_run_one(state)
    try:
        report = _run(entries, _config(tmp_path, clean=True, flat=False))
    finally:
        B._run_one = original
    assert report.dirs_cleaned == 3
    assert report.clean_problems == []


def test_clean_is_a_no_op_when_nothing_exists(tmp_path: Path) -> None:
    removed, problems = B.prepare_output_dirs(_entries(3), _config(tmp_path, clean=True, flat=False))
    assert (removed, problems) == (0, [])


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
