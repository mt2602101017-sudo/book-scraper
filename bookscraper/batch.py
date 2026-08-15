"""Run the pipeline once per ISBN read from a CSV, and report on the whole run.

A batch is not just a ``for`` loop around ``run_pipeline``. Four things must hold
across a multi-thousand-row file that do not arise for a single ISBN.

**One HTTP client for the whole batch.** Per-host delays, block detection and the
Selenium browser all live on :class:`HttpClient`. A fresh one per ISBN would reset
the delay clock, re-discover every block, and start a browser per book. The batch
builds one, lends it to every :class:`Pipeline`, and closes it at the end.

**One flat output tree.** All books share ``book_metadata/``, ``book_coverpage/``,
``book_blurb/``, ``book_reviews/`` and ``genres/``. Cover, blurb, review and genre
filenames carry the ISBN so they cannot collide; the metadata filename does *not*,
which is why ``Storage.append_metadata`` **accumulates** records into one array per
source instead of overwriting -- that is what makes a flat layout work for 10 000
books. ``flat=False`` gives each book its own tree instead.

**Stopping early must not lose work.** Artefacts are written as each ISBN
completes and Ctrl-C is caught, so an interrupted batch keeps everything finished.

**A batch-level report.** 10 000 per-book tables are unreadable, so the batch closes
with a digest, and the per-source list of which ISBNs delivered and which did not
goes to ``metrics/isbns_by_source.txt``.

One ISBN at a time
------------------
Books are scraped sequentially, in file order, and each book runs its sources in
order. This is a deliberate simplification: the run's pace is set by the per-host
courtesy delay, not by the CPU, so concurrency cannot make the crawl politer or
much faster -- one host can only be asked so often however many workers want it.

A thread pool used to sit here (``--workers``, default 4). It bought ~1.2x wall
clock on a measured 4-ISBN, 2-source run while requiring a locked per-host
reservation clock, a lock around the single browser, thread-local host tracking,
a lock on every metadata append, and captured-and-replayed summary tables so four
threads' output did not interleave. Sequential execution deletes all of that, and
the sequential ordering is what the whole design already depended on anyway:
Goodreads must run before the ISBN-hostile sources so its title/author hint can
seed them.

Two ways to go faster are still available and do not need threads: split the file
across terminals with ``--start``/``--end``, or split the sites with
``--sources``. Both are documented in the README, and the reports are named after
the sources they cover so concurrent runs do not overwrite each other's.
"""

from __future__ import annotations

import sys
from .verbosity import verbose
import shutil
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from .csv_input import CsvIsbns, IsbnEntry, RowProblem
from .http_client import HttpClient
from .metrics import NoDataIndex, RunReport
from .pipeline import Pipeline, PipelineConfig, SourceOutcome
from .storage import release_indexes

__all__ = [
    "BatchConfig",
    "BookOutcome",
    "BatchReport",
    "run_batch",
    "select_entries",
    "prepare_output_dirs",
    "clean_targets",
    "format_batch_report",
]


@dataclass
class BatchConfig:
    """How to run a batch: the per-ISBN settings plus the batch-only knobs.

    :param base: the single-ISBN configuration. Its ``isbn`` is ignored (each
        entry supplies its own) and its ``out_dir`` is the batch *root*.
    :param flat: share one artefact tree across every ISBN (the default and the
        assignment's layout). Safe because the accumulating metadata writer keeps
        one record per book, and every other filename carries the ISBN. Set
        ``False`` gives each book its own
        ``<out>/<isbn13>/`` tree instead.
    :param start: skip this many ISBNs first, so an interrupted batch can be
        resumed without re-scraping what already succeeded.
    :param end: stop *before* this index, so ``start=100, end=200`` scrapes exactly
        the hundred ISBNs 100..199 -- a half-open range, matching Python slicing and
        :func:`range`, so consecutive shards (0-1000, 1000-2000) tile the file with
        no overlap and no gap. ``None`` means "to the end of the file". An ``end``
        past the last row is clamped rather than treated as an error, so
        ``--end 99999`` on a 9 995-row file is simply "everything".
    :param continue_on_error: keep going when one ISBN produces nothing. Off
        means a single failure ends the batch, which is occasionally what you
        want while debugging a selector.
    :param clean: delete this run's output directories before scraping into them,
        so a re-run cannot blend with a previous run's artefacts. Defaults to
        ``False``: with ``flat`` set the targets are *shared* directories carrying
        no ISBN, so cleaning them can destroy an unrelated book's output, and that
        must always be an explicit choice. ``main.py`` turns it on by default only
        for the per-ISBN layout, where each target is a directory this run is about
        to rewrite anyway. See :func:`prepare_output_dirs`.
    """

    base: PipelineConfig
    flat: bool = True
    start: int = 0
    end: Optional[int] = None
    continue_on_error: bool = True
    clean: bool = False
    #: Collect per-request and per-book metrics into ``<out>/metrics/``.
    collect_metrics: bool = True
    #: Skip a source that already has a record for the book in
    #: ``book_metadata/<source>_metadata.json``. ``False``
    #: re-fetches everything.
    skip_scraped: bool = True


@dataclass
class BookOutcome:
    """What the pipeline produced for one ISBN."""

    entry: IsbnEntry
    out_dir: Path
    exit_code: int = 1
    outcomes: List[SourceOutcome] = field(default_factory=list)
    #: Set when the run itself failed rather than merely finding nothing.
    error: Optional[str] = None
    seconds: float = 0.0

    @property
    def isbn13(self) -> str:
        return self.entry.isbn13

    @property
    def ok(self) -> bool:
        """True when at least one source produced a metadata record."""
        return self.exit_code == 0

    @property
    def files_written(self) -> int:
        return sum(len(o.files) for o in self.outcomes)

    @property
    def reviews_written(self) -> int:
        return sum(o.reviews_written for o in self.outcomes)

    @property
    def covers_written(self) -> int:
        return sum(o.covers_written for o in self.outcomes)


@dataclass
class BatchReport:
    """Everything a batch produced, for the closing digest and for callers."""

    root: Path
    books: List[BookOutcome] = field(default_factory=list)
    #: CSV rows that yielded no ISBN, carried through from the reader.
    skipped_rows: List[RowProblem] = field(default_factory=list)
    duplicates: List[str] = field(default_factory=list)
    #: ISBNs in the file that this run did not reach (outside the range, Ctrl-C).
    not_attempted: List[IsbnEntry] = field(default_factory=list)
    interrupted: bool = False
    blocked_hosts: dict = field(default_factory=dict)
    seconds: float = 0.0
    #: Output directories deleted before scraping, and anything that stopped one
    #: being deleted.
    dirs_cleaned: int = 0
    clean_problems: List[str] = field(default_factory=list)
    #: ``--start`` value that resumes this run, set when interrupted.
    resume_at: Optional[int] = None
    #: The report files written at the end of the run.
    metrics_paths: List[Path] = field(default_factory=list)
    #: The run's report, for the digest's "already answered" line.
    run_report: Optional[RunReport] = None
    #: The known-absent lists, for the digest's "no such book" line.
    no_data: Optional[NoDataIndex] = None

    @property
    def succeeded(self) -> List[BookOutcome]:
        return [b for b in self.books if b.ok]

    @property
    def failed(self) -> List[BookOutcome]:
        return [b for b in self.books if not b.ok]

    def exit_code(self) -> int:
        """0 when at least one ISBN produced metadata, else 1.

        Deliberately the same contract as a single-ISBN run: the CLI documents
        exit 1 as "no source produced metadata", and a batch where every book
        came back empty is exactly that case.
        """
        return 0 if self.succeeded else 1


def _out_dir_for(config: BatchConfig, entry: IsbnEntry) -> Path:
    """Where one ISBN's five artefact directories live."""
    if config.flat:
        return config.base.out_dir
    return config.base.out_dir / entry.isbn13


#: Directory names :func:`prepare_output_dirs` is willing to delete inside a flat
#: output root. Anything else there belongs to the user, not to us.
_ARTEFACT_DIR_NAMES = frozenset({
    "book_metadata", "book metadata",
    "book_coverpage", "book coverpage",
    "book_blurb", "book blurb",
    "book_reviews", "book reviews",
    "genres",
})


def _refuse_to_delete(path: Path) -> Optional[str]:
    """Return why ``path`` must not be deleted, or ``None`` when it is safe.

    Deleting is the one irreversible thing this program does, so the target has
    to be provably a scratch output directory rather than something with a user's
    life in it. Filesystem root, a home directory, and anything at or above the
    working directory are all refused outright.
    """
    try:
        resolved = path.resolve()
    except (OSError, RuntimeError) as exc:
        return f"it cannot be resolved to a real path ({exc})"
    if resolved.parent == resolved:
        return "it is the filesystem root"
    try:
        if resolved == Path.home().resolve():
            return "it is your home directory"
    except (OSError, RuntimeError):
        pass  # No determinable home directory; the checks below still apply.
    try:
        cwd = Path.cwd().resolve()
    except OSError:
        cwd = None
    if cwd is not None and (resolved == cwd or resolved in cwd.parents):
        return "it is the working directory or a parent of it"
    if resolved.exists() and not resolved.is_dir():
        return "it is a file, not a directory"
    return None


def clean_targets(
    entries: Sequence[IsbnEntry], config: BatchConfig
) -> List[Path]:
    """The directories :func:`prepare_output_dirs` would delete, in order.

    Separate from the deletion itself so a caller can report the exact list a
    real run would remove instead of describing it in prose that could drift.
    """
    if config.flat:
        return [config.base.out_dir / name for name in sorted(_ARTEFACT_DIR_NAMES)]
    # dict.fromkeys keeps file order while collapsing the (already impossible)
    # duplicate ISBN case.
    return list(dict.fromkeys(_out_dir_for(config, e) for e in entries))


def prepare_output_dirs(
    entries: Sequence[IsbnEntry],
    config: BatchConfig,
) -> Tuple[int, List[str]]:
    """Delete the output directories this run is about to write into.

    Numbering restarts at 1 every run, so without this a re-run that finds fewer
    covers or reviews than its predecessor leaves the predecessor's higher-numbered
    files behind, and anything globbing the directory reads a blend of two runs.
    ``Storage.purge`` already handles that per (kind, isbn, source); this is the
    coarser guarantee that a whole ISBN's directory starts empty, which also
    catches artefacts from a source that is no longer being scraped.

    Scope is deliberately narrow. Returns ``(directories_removed, problems)``.

    * **Not flat:** exactly the ``<out>/<isbn13>/`` directories for the ISBNs in
      ``entries`` -- so an ISBN this run is not scraping (outside ``--start``/``--end``)
      keeps everything it had, which is what makes resuming a batch safe.
    * **Flat:** only the five known artefact directories inside ``<out>``. A file
      the user keeps next to them is left alone.
    * Never the output root itself, and never anything :func:`_refuse_to_delete`
      objects to.
    * The manifest and skipped-rows CSVs are *not* removed here; they are
      overwritten at the end of the run.
    """
    problems: List[str] = []
    removed = 0
    targets = clean_targets(entries, config)

    # A flat clean (and the single-ISBN form, which is a flat batch of one) deletes
    # whole artefact directories that may hold a *different* book's output, since
    # nothing in those paths is keyed by ISBN. Say so before doing it: the per-ISBN
    # layout makes this impossible, so the warning is specific to opting out of it.
    if config.flat:
        doomed = [t for t in targets if t.exists()]
        if doomed:
            print("warning: Cleaning %d shared artefact director%s under %s: any previous book's covers, reviews, blurbs and genres in there will be deleted, because those paths carry no ISBN. Set BatchConfig.clean=False to keep them, or scrape into a fresh out_dir." % (len(doomed), 'y' if len(doomed) == 1 else 'ies', config.base.out_dir), file=sys.stderr)

    for target in targets:
        if not target.exists():
            continue
        refusal = _refuse_to_delete(target)
        if refusal is not None:
            message = f"refusing to delete {target}: {refusal}"
            print('error: %s' % (message,), file=sys.stderr)
            problems.append(message)
            continue
        try:
            shutil.rmtree(target)
        except OSError as exc:
            message = f"could not delete {target}: {exc}"
            print('warning: %s' % (message,), file=sys.stderr)
            problems.append(message)
            continue
        removed += 1
        if verbose():
            print('  Removed previous output directory %s' % (target,), file=sys.stderr)

    if removed:
        print('Cleaned %d existing output director%s under %s before scraping' % (removed, 'y' if removed == 1 else 'ies', config.base.out_dir), file=sys.stderr)
    return removed, problems


def select_entries(
    entries: Sequence[IsbnEntry],
    config: BatchConfig,
) -> Tuple[List[IsbnEntry], List[IsbnEntry]]:
    """Apply ``--start``/``--end``. Returns ``(to_run, not_attempted)``.

    The range is **half-open**, ``[start, end)``, like Python slicing: so
    ``--start 1000 --end 2000`` is exactly the thousand ISBNs 1000..1999, and the
    next shard starts at 2000 with no overlap and no gap. Rows past the end of the
    file are clamped, not an error.

    There used to be a ``--limit N`` as well, meaning "N rows from ``--start``".
    It was removed: it expressed the same slice less clearly (``--start 2000
    --limit 2000`` vs ``--start 2000 --end 4000``), and having two flags bound the
    same edge meant defining which one won when they disagreed.

    Public so a caller can report exactly the slice a real run would scrape,
    rather than re-deriving the arithmetic and drifting from it.
    """
    total = len(entries)
    start = max(0, int(config.start))
    if start >= total and entries:
        print('warning: --start %d skips past all %d ISBN(s) in the file; nothing to do'
              % (start, total), file=sys.stderr)
        return [], list(entries)

    stop = total
    reason = ""
    if config.end is not None:
        end = int(config.end)
        if end <= start:
            print('warning: --end %d is not past --start %d, so the range is empty; '
                  'nothing to do' % (end, start), file=sys.stderr)
            return [], list(entries)
        if end > total:
            print('--end %d is past the last of %d ISBN(s); stopping at the end of '
                  'the file' % (end, total), file=sys.stderr)
        stop = min(end, total)
        reason = "--end %d" % end
    if start:
        print('--start %d: skipping the first %d ISBN(s)' % (start, start),
              file=sys.stderr)
    if reason and stop - start < total - start:
        print('%s: scraping %d of the %d ISBN(s) from row %d onwards'
              % (reason, stop - start, total - start, start), file=sys.stderr)

    return list(entries[start:stop]), list(entries[:start]) + list(entries[stop:])


def run_batch(
    source: CsvIsbns,
    config: BatchConfig,
) -> BatchReport:
    """Scrape every ISBN in ``source``, one at a time. Never raises.

    Ctrl-C is caught: the report comes back with ``interrupted=True``, every
    finished ISBN's artefacts already on disk, and the rest recorded as not
    attempted so ``--start`` can resume without re-scraping what succeeded.
    """
    report = BatchReport(
        root=config.base.out_dir,
        skipped_rows=list(source.problems),
        duplicates=list(source.duplicates),
    )
    started = time.monotonic()

    to_run, report.not_attempted = select_entries(source.entries, config)
    if not to_run:
        report.seconds = time.monotonic() - started
        print(format_batch_report(report, config))
        return report

    if config.clean:
        report.dirs_cleaned, report.clean_problems = prepare_output_dirs(to_run, config)

    # Collects each source's outcome and writes the end-of-run report. It holds no
    # skip state: that comes from the metadata files themselves, per ISBN.
    run_report = RunReport(config.base.metrics_dir, enabled=config.collect_metrics)
    report.run_report = run_report

    # Absence is the one answer the metadata files cannot hold, so it gets its own
    # per-source list. Not governed by --no-metrics: this is state the next run
    # reads, not a report about this one.
    no_data = NoDataIndex(config.base.metrics_dir, enabled=config.skip_scraped)
    report.no_data = no_data

    client = HttpClient(
        min_delay=config.base.min_delay,
        max_delay=config.base.max_delay,
        timeout=config.base.timeout,
        max_retries=config.base.max_retries,
        browser=config.base.browser,
        user_agent=config.base.user_agent,
        respect_robots=config.base.respect_robots,
    )

    print('Batch: %d ISBN(s) from %s into %s (%s layout, one at a time)' % (len(to_run), source.path, config.base.out_dir, 'flat/shared' if config.flat else 'one directory per ISBN'), file=sys.stderr)

    try:
        _execute(to_run, config, client, report, run_report, no_data)
    finally:
        report.blocked_hosts = client.blocks
        client.close()
        # Merge the run's newly-found absences into the lists on disk. In the
        # finally, so an interrupted batch keeps what it learned.
        no_data.flush()
        # The metadata indexes exist to answer "already scraped?" during the crawl.
        # Holding 10 000 parsed records after the last book is pointless.
        release_indexes()

    report.seconds = time.monotonic() - started
    # The report goes last: it describes the whole run, including anything that
    # failed while persisting above.
    report.metrics_paths = run_report.write_all()
    print(format_batch_report(report, config))
    return report


def _execute(
    to_run: Sequence[IsbnEntry],
    config: BatchConfig,
    client: HttpClient,
    report: BatchReport,
    run_report: Optional[RunReport] = None,
    no_data: Optional[NoDataIndex] = None,
) -> None:
    """Run every entry in file order, recording each result into ``report``."""
    total = len(to_run)
    try:
        for position, entry in enumerate(to_run, start=1):
            print('===== [%d/%d] %s =====' % (position, total, entry.describe()), file=sys.stderr)
            try:
                book = _run_one(entry, config, client, run_report=run_report,
                                skip_existing=config.skip_scraped,
                                no_data=no_data)
            except KeyboardInterrupt:
                raise
            except Exception as exc:  # noqa: BLE001 - a book, not the batch
                # _run_one already contains its own failures; this is the outer
                # guarantee, so a future change in there cannot silently cost the
                # remaining 9 999 books their run.
                book = BookOutcome(entry=entry, out_dir=_out_dir_for(config, entry))
                book.error = f"{type(exc).__name__}: {exc}"
                print('warning: %s failed unexpectedly: %s' % (entry.isbn13, book.error), file=sys.stderr)
            report.books.append(book)
            print('----- [%d/%d] %s: %s in %.0fs -----' % (position, total, book.isbn13, 'metadata written' if book.ok else book.error or 'no metadata', book.seconds), file=sys.stderr)
            if not book.ok and not config.continue_on_error:
                print('error: %s produced no metadata and --stop-on-error is set; abandoning the remaining %d ISBN(s)' % (entry.isbn13, total - position), file=sys.stderr)
                report.not_attempted.extend(to_run[position:])
                return
    except KeyboardInterrupt:
        # The book in flight was abandoned mid-scrape, so it is *not* in
        # report.books and _note_interrupt will list it as not attempted -- which
        # is what makes the resume point safe.
        _note_interrupt(report, to_run, config)


def _note_interrupt(
    report: BatchReport,
    to_run: Sequence[IsbnEntry],
    config: BatchConfig,
    already_flagged: bool = False,
) -> None:
    """Record an interrupt and say how to resume.

    The resume point is the **first ISBN that did not complete**, so nothing that
    ran is re-scraped and nothing that did not run is skipped. Running one book at
    a time means that is simply the book Ctrl-C landed in: everything before it
    finished and everything after it never started.
    """
    report.interrupted = True
    completed = {book.isbn13 for book in report.books}
    first_missing = len(to_run)
    for index, entry in enumerate(to_run):
        if entry.isbn13 not in completed:
            first_missing = index
            break
    for entry in to_run[first_missing:]:
        if entry.isbn13 not in completed and entry not in report.not_attempted:
            report.not_attempted.append(entry)
    report.resume_at = config.start + first_missing
    if not already_flagged:
        print('warning: Interrupted after %d of %d ISBN(s).' % (len(completed), len(to_run)), file=sys.stderr)
    print('warning: Everything already written is intact; re-run with --start %d to continue.' % (report.resume_at,), file=sys.stderr)


def _run_one(
    entry: IsbnEntry,
    config: BatchConfig,
    client: HttpClient,
    run_report: Optional[RunReport] = None,
    skip_existing: bool = True,
    no_data: Optional[NoDataIndex] = None,
) -> BookOutcome:
    """Run the pipeline for one ISBN. Only ``KeyboardInterrupt`` escapes.

    Everything else becomes a failed :class:`BookOutcome`, because one book's
    unexpected crash must not cost the other 9 999 their run.
    """
    out_dir = _out_dir_for(config, entry)
    book = BookOutcome(entry=entry, out_dir=out_dir)
    started = time.monotonic()
    try:
        pipeline = Pipeline(
            replace(config.base, isbn=entry.isbn13, out_dir=out_dir),
            client=client,
            csv_row=entry.row,
            label=entry.label,
            report=run_report,
            skip_existing=skip_existing,
            no_data=no_data,
        )
        book.exit_code = pipeline.run()
        book.outcomes = list(pipeline.outcomes)
    except KeyboardInterrupt:
        raise
    except Exception as exc:  # noqa: BLE001 - one book must not kill the batch
        book.exit_code = 1
        book.error = f"{type(exc).__name__}: {exc}"
        print('warning: %s failed unexpectedly: %s' % (entry.isbn13, book.error), file=sys.stderr)
    book.seconds = time.monotonic() - started
    return book


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def _duration(seconds: float) -> str:
    """``93.4`` -> ``'1m33s'``; short enough for a summary line."""
    total = int(round(max(0.0, seconds)))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h{minutes:02d}m{secs:02d}s"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def format_batch_report(report: BatchReport, config: BatchConfig) -> str:
    """Render the closing batch digest as one string."""
    width = 78
    lines: List[str] = ["", "=" * width, "BATCH SUMMARY", "=" * width]

    attempted = len(report.books)
    ok = report.succeeded
    failed = report.failed
    lines.append(f"ISBNs scraped   : {attempted}")
    lines.append(f"  with metadata : {len(ok)}")
    lines.append(f"  empty/failed  : {len(failed)}")
    if report.skipped_rows:
        # Each row was already reported with its line number and reason as the CSV
        # was read, so only the count belongs here.
        lines.append(
            f"CSV rows skipped: {len(report.skipped_rows)} (no usable ISBN)"
        )
    if report.duplicates:
        lines.append(f"Duplicates      : {len(report.duplicates)} (scraped once each)")
    if report.not_attempted:
        why = "interrupted" if report.interrupted else "outside --start/--end"
        lines.append(f"Not attempted   : {len(report.not_attempted)} ({why})")

    lines.append(f"Files written   : {sum(b.files_written for b in report.books)}")
    lines.append(f"  reviews       : {sum(b.reviews_written for b in report.books)}")
    lines.append(f"  covers        : {sum(b.covers_written for b in report.books)}")
    if report.run_report is not None and getattr(report.run_report, "suspect_empties", None):
        total = sum(report.run_report.suspect_empties.values())
        parts = ", ".join(f"{s} {n}"
                          for s, n in sorted(report.run_report.suspect_empties.items()))
        lines.append(
            f"Untrusted empty : {total} reported empty while the site was walling "
            f"us, so not recorded as absent ({parts})"
        )
    if report.run_report is not None and report.run_report.total_skipped:
        summary = report.run_report.summarise()
        if summary:
            lines.append(f"Already answered: {summary} (so not re-fetched)")
    if report.no_data is not None and getattr(report.no_data, "recorded", None):
        added = report.no_data.recorded
        total = sum(added.values())
        parts = ", ".join(f"{s} {n}" for s, n in added.items())
        lines.append(
            f"No such book    : {total} pair(s) recorded in "
            f"metrics/<source>_no_data.txt, so the next run skips them ({parts})"
        )
    if report.dirs_cleaned:
        lines.append(
            f"Dirs cleaned    : {report.dirs_cleaned} (deleted before scraping)"
        )
    lines.append(f"Elapsed         : {_duration(report.seconds)}")
    if attempted:
        lines.append(
            f"  per ISBN      : {_duration(report.seconds / attempted)} average"
        )

    # Per-source roll-up: which of the five sites actually delivered, across the
    # whole file. This is the number that tells you a selector has rotted -- a
    # single book's empty result never does.
    per_source: dict = {}
    for book in report.books:
        for outcome in book.outcomes:
            bucket = per_source.setdefault(
                outcome.name, {"metadata": 0, "reviews": 0, "covers": 0, "seen": 0}
            )
            bucket["seen"] += 1
            bucket["metadata"] += 1 if outcome.has_metadata else 0
            bucket["reviews"] += outcome.reviews_written
            bucket["covers"] += outcome.covers_written
    if per_source:
        lines.append("")
        lines.append(
            f"{'SOURCE':<12}{'METADATA':>12}{'REVIEWS':>10}{'COVERS':>9}"
        )
        lines.append("-" * 43)
        for name in sorted(per_source):
            stats = per_source[name]
            score = f"{stats['metadata']}/{stats['seen']}"
            lines.append(
                f"{name:<12}{score:>12}{stats['reviews']:>10}{stats['covers']:>9}"
            )

    if failed:
        lines.append("")
        shown = failed[:20]
        lines.append(f"ISBNs with no metadata ({len(failed)}):")
        for book in shown:
            detail = f" -- {book.error}" if book.error else ""
            row = f" (csv row {book.entry.row})" if book.entry.row else ""
            lines.append(f"  - {book.isbn13}{row}{detail}")
        if len(failed) > len(shown):
            lines.append(f"  ... and {len(failed) - len(shown)} more")

    if report.blocked_hosts:
        lines.append("")
        lines.append("Hosts that blocked automated access (not circumvented):")
        for host, reason in sorted(report.blocked_hosts.items()):
            lines.append(f"  - {host}: {reason}")

    lines.append("")
    lines.append(f"Output root     : {report.root}")
    lines.append(
        "Layout          : "
        + ("flat -- all books share one artefact tree" if config.flat
           else "one self-contained tree per ISBN")
    )
    if report.metrics_paths:
        lines.append(
            f"Report          : {report.metrics_paths[0].parent}/ "
            f"({', '.join(sorted(p.name for p in report.metrics_paths))})"
        )
    if report.clean_problems:
        lines.append("")
        lines.append("Output directories that could not be cleaned first:")
        for problem in report.clean_problems[:10]:
            lines.append(f"  - {problem}")
        if len(report.clean_problems) > 10:
            lines.append(f"  ... and {len(report.clean_problems) - 10} more")
        lines.append(
            "  those directories may hold a blend of this run and a previous one"
        )
    if report.interrupted:
        resume = report.resume_at
        if resume is None:
            resume = config.start + len(report.books)
        lines.append(
            f"Interrupted     : re-run with --start {resume} to continue where this "
            "stopped"
        )
    lines.append("=" * width)
    return "\n".join(lines)

