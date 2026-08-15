"""Orchestration: resolve the ISBN, run each source, persist, summarise.

Guarantees this module provides to everything around it:

* Open Library runs **first** whenever it is selected, and its ``hint_updates``
  (title/authors) are merged into the shared :class:`BookHint`, so the
  ISBN-hostile sources (BookBub, Kobo, Audible) get their title+author search
  terms from one cheap ISBN lookup rather than from whichever store ran first.
* One source blowing up never stops the others: every adapter call is wrapped,
  and a crash becomes a warning row in the summary.
* Artefacts are written as soon as they are parsed, and the metadata JSON is
  written **last** so its ``_warnings`` list includes everything that went wrong
  while persisting.
* Exit status is 0 when at least one source yielded metadata, else 1.

This module and ``main.py`` are the only places allowed to write to stdout, and
only for the human-facing summary.
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple, Type

from . import isbn as isbn_utils
from .base import BaseSource
from .http_client import HttpClient
from .metrics import BookRecord, NoDataIndex, RunReport
from .models import BookHint, BookMetadata, ScrapeResult
from .sources import discover_sources
from .storage import Storage, release_indexes
from .verbosity import verbose

__all__ = ["PipelineConfig", "SourceOutcome", "Pipeline", "run_pipeline"]


#: The source whose results seed the shared hint for everyone else.
#: The source that seeds the shared title/author hint. Open Library, because it
#: is the only one that is ISBN-indexed *and* answers in a single JSON request:
#: the storefronts (BookBub, Kobo, Audible) index no ISBN and can only be
#: searched by title+author, and asking Goodreads for that pair first meant
#: their hit rate depended on whether Goodreads happened to be selected.
SEED_SOURCE = "openlibrary"

#: What a single artefact write is allowed to fail with before it degrades to a
#: warning row instead of taking the run down. ``OSError`` covers the filesystem;
#: ``ValueError`` covers unencodable scraped text (``UnicodeEncodeError`` is a
#: ``ValueError``) and malformed scraped URLs; ``TypeError`` covers a payload
#: shape no serialiser will accept.
_WRITE_ERRORS = (OSError, ValueError, TypeError)


def _expanduser(path: Path) -> Path:
    """``Path.expanduser()`` that survives an unresolvable ``~user``.

    ``expanduser`` raises :class:`RuntimeError` for ``~nosuchuser/x`` (and when
    the home directory cannot be determined at all), which would otherwise
    escape as a raw traceback from a simple ``out_dir`` typo.
    """
    try:
        return Path(path).expanduser()
    except RuntimeError:
        print('warning: Could not expand %r to a home directory; using it literally' % (str(path),), file=sys.stderr)
        return Path(path)


@dataclass
class PipelineConfig:
    """Everything the CLI collects, in one validated bundle."""

    isbn: str
    sources: Optional[Sequence[str]] = None       # None/empty => all discovered
    out_dir: Path = Path("data")
    #: Where the per-source report and known-absent lists go. Deliberately NOT
    #: under ``out_dir``: the absent lists are state about the sites, and the
    #: report is about the run, so neither belongs in the artefact tree that
    #: ``--clean`` may delete.
    metrics_dir: Path = Path("metrics")
    min_reviews: int = 25
    max_reviews: Optional[int] = None
    min_delay: float = 1.0
    max_delay: float = 2.0
    browser: str = "auto"
    filename_style: str = "underscore"
    download_covers: bool = True
    respect_robots: bool = False
    user_agent: Optional[str] = None
    timeout: int = 25
    max_retries: int = 3

    def normalised(self) -> "PipelineConfig":
        """Return a copy with delays ordered, counts sane and paths expanded."""
        low, high = float(self.min_delay), float(self.max_delay)
        if low < 0:
            print('warning: the min_delay setting (%.2f) is negative; using 0' % (low,), file=sys.stderr)
            low = 0.0
        if high < low:
            print('warning: the max_delay setting (%.2f) is below min_delay (%.2f); swapping them' % (high, low), file=sys.stderr)
            low, high = high, low
        max_reviews = self.max_reviews
        if max_reviews is not None and max_reviews < 0:
            print('warning: the max_reviews setting (%s) is negative; treating as unlimited' % (max_reviews,), file=sys.stderr)
            max_reviews = None
        return PipelineConfig(
            isbn=self.isbn,
            sources=list(self.sources) if self.sources else None,
            out_dir=_expanduser(self.out_dir),
            metrics_dir=_expanduser(self.metrics_dir),
            min_reviews=max(0, int(self.min_reviews)),
            max_reviews=max_reviews,
            min_delay=low,
            max_delay=high,
            browser=self.browser,
            filename_style=self.filename_style,
            download_covers=bool(self.download_covers),
            respect_robots=bool(self.respect_robots),
            user_agent=self.user_agent,
            timeout=max(1, int(self.timeout)),
            max_retries=max(0, int(self.max_retries)),
        )


@dataclass
class SourceOutcome:
    """What happened for one source, for the summary table."""

    name: str
    display_name: str
    #: ok | partial | empty | error | blocked | skipped
    #: ``skipped`` means this source's metadata file already held a record for the
    #: book, so nothing was fetched.
    status: str = "pending"
    book_url: Optional[str] = None
    error: Optional[str] = None
    found_fields: List[str] = field(default_factory=list)
    missing_fields: List[str] = field(default_factory=list)
    covers_written: int = 0
    covers_seen: int = 0
    reviews_written: int = 0
    genres_written: int = 0
    blurb_chars: int = 0
    warnings: List[str] = field(default_factory=list)
    files: List[Path] = field(default_factory=list)
    has_metadata: bool = False
    #: hosts that started blocking us *while this source was running*.
    blocked_hosts: Dict[str, str] = field(default_factory=dict)

    @property
    def field_score(self) -> str:
        total = len(self.found_fields) + len(self.missing_fields)
        return f"{len(self.found_fields)}/{total}" if total else "-"


class Pipeline:
    """Runs the selected sources for one ISBN and writes their output."""

    #: Metadata fields reported as found/missing in the summary.
    REPORT_FIELDS: Tuple[str, ...] = (
        "title",
        "authors",
        "publisher",
        "origin",
        "date_of_publication",
        "language",
        "genres",
    )

    def __init__(
        self,
        config: PipelineConfig,
        client: Optional[HttpClient] = None,
        capture_summary: bool = False,
        csv_row: int = 0,
        label: Optional[str] = None,
        report: Optional[RunReport] = None,
        skip_existing: bool = True,
        no_data: Optional[NoDataIndex] = None,
    ) -> None:
        """
        :param client: an existing :class:`HttpClient` to borrow. A batch run
            passes one in so that all its ISBNs share a single session, a single
            browser and -- crucially -- one set of per-host courtesy delays and
            block records; a Pipeline that *borrows* a client never closes it.
            Left ``None`` (the single-ISBN case) it builds and owns its own.
        :param capture_summary: collect the summary table into
            :attr:`summary_text` instead of printing it, for a caller that wants
            the text rather than the side effect (the tests do).
        :param csv_row: the CSV line this ISBN came from, carried into the metrics
            so a failure can be traced back to the input row that asked for it.
        :param label: an optional human label (a title from the CSV) for reports.
        :param report: optional :class:`~bookscraper.metrics.RunReport` to record
            each source's outcome into, for the end-of-run report.
        :param skip_existing: skip a source that already has a record for this book
            in ``book_metadata/<source>_metadata.json``, or that is listed in
            ``metrics/<source>_no_data.txt``. Between the two, every (ISBN, source)
            pair we have an answer for is known. ``False``
            re-fetches everything.
        :param no_data: the :class:`~bookscraper.metrics.NoDataIndex` holding the
            "answered, and does not carry it" lists. Without it only the metadata
            files are consulted, so genuine misses are re-fetched every run.
        """
        self.config = config.normalised()
        self.storage = Storage(
            self.config.out_dir,
            filename_style=self.config.filename_style,
        )
        self._owns_client = client is None
        self.client = client or HttpClient(
            min_delay=self.config.min_delay,
            max_delay=self.config.max_delay,
            timeout=self.config.timeout,
            max_retries=self.config.max_retries,
            browser=self.config.browser,
            user_agent=self.config.user_agent,
            respect_robots=self.config.respect_robots,
        )
        self.hint: Optional[BookHint] = None
        self.outcomes: List[SourceOutcome] = []
        self.capture_summary = bool(capture_summary)
        #: The rendered summary when ``capture_summary`` is set, else ``None``.
        self.summary_text: Optional[str] = None
        self.csv_row = int(csv_row or 0)
        self.label = label
        self.report = report
        self.skip_existing = bool(skip_existing)
        self.no_data = no_data

    def _emit_summary(self, outcomes: Sequence[SourceOutcome]) -> None:
        """Print the summary, or stash it for the caller to print in one piece."""
        if self.capture_summary:
            self.summary_text = format_summary(self, outcomes)
        else:
            print_summary(self, outcomes)

    def _release_client(self) -> None:
        """Close the HTTP client, but only if this Pipeline created it."""
        if self._owns_client:
            self.client.close()

    # -- source selection ----------------------------------------------------

    def select_sources(self) -> List[Tuple[str, Type[BaseSource]]]:
        """Resolve ``--sources`` against auto-discovery, Goodreads first.

        Unknown or not-yet-implemented names produce a clear warning and are
        skipped; they never raise.

        A run that names **no** sources takes only the adapters whose
        ``enabled_by_default`` is true, and says which it left out. Naming a
        source explicitly always runs it, opt-in or not -- so ``--sources
        bookbub`` still works exactly as before.
        """
        available = discover_sources()
        requested = [str(s).strip().lower() for s in (self.config.sources or []) if str(s).strip()]

        if not available:
            print('warning: No source adapters are available in bookscraper.sources, so %s cannot be scraped -- nothing to do. Add an adapter module that subclasses BaseSource.' % ('the requested source(s) ' + ', '.join((repr(r) for r in requested)) if requested else 'anything',), file=sys.stderr)
            return []

        if not requested:
            chosen = [
                name for name, cls in available.items()
                if getattr(cls, "enabled_by_default", True)
            ]
            opt_in = [name for name in available if name not in chosen]
            if opt_in:
                print('Scraping the %d source(s) that are on by default; %s %s off by default and %s skipped -- add %s to scrape %s anyway' % (len(chosen), ', '.join(opt_in), 'is' if len(opt_in) == 1 else 'are', 'was' if len(opt_in) == 1 else 'were', ' '.join((f'--sources {name}' for name in opt_in)), 'it' if len(opt_in) == 1 else 'them'), file=sys.stderr)
            if not chosen:
                print('warning: Every discovered source (%s) is off by default, so a run that names none has nothing to do. Pass --sources with one or more of them explicitly.' % (', '.join(available),), file=sys.stderr)
                return []
        else:
            chosen = []
            for name in requested:
                if name in available:
                    if name not in chosen:
                        chosen.append(name)
                else:
                    print('warning: Unknown source %r -- skipping it. Available sources: %s' % (name, ', '.join(available) or '(none)'), file=sys.stderr)
            if not chosen:
                print('warning: None of the requested sources (%s) are available; available: %s' % (', '.join(requested), ', '.join(available) or '(none)'), file=sys.stderr)
                return []

        # Goodreads always leads so its title/author hints can seed the rest.
        chosen.sort(key=lambda n: (0 if n == SEED_SOURCE else 1,))
        return [(name, available[name]) for name in chosen]

    # -- run -----------------------------------------------------------------

    def run(self) -> int:
        """Execute the whole pipeline. Returns the process exit code (0 or 1)."""
        try:
            isbn13 = isbn_utils.to_isbn13(self.config.isbn)
        except ValueError as exc:
            print('error: %s' % (exc,), file=sys.stderr)
            self._release_client()
            return 1

        self.hint = BookHint(
            isbn13=isbn13,
            isbn10=isbn_utils.isbn13_to_isbn10(isbn13),
        )
        print('Resolved input %r to ISBN-13 %s (%s)' % (self.config.isbn, isbn13, isbn_utils.hyphenate(isbn13)), file=sys.stderr)

        selected = self.select_sources()
        if not selected:
            self._emit_summary([])
            self._release_client()
            return 1

        print('Scraping %d source(s): %s' % (len(selected), ', '.join((n for n, _ in selected))), file=sys.stderr)

        try:
            self.storage.ensure_dirs()
        except OSError as exc:
            print('error: Cannot create the output directories under %s (%s): nothing can be written, so no source was run' % (self.config.out_dir, exc), file=sys.stderr)
            self._emit_summary([])
            self._release_client()
            return 1

        outcomes: List[SourceOutcome] = []
        try:
            for name, cls in selected:
                because = self._already_answered(name, isbn13) if self.skip_existing else None
                if because is not None:
                    outcomes.append(self._skipped_outcome(name, cls, because))
                    if self.report is not None:
                        self.report.note_skipped(name)
                    print('--- %s: skipping %s, %s ---' % (getattr(cls, 'display_name', '') or name, isbn13, because), file=sys.stderr)
                    # A skipped Goodreads would otherwise starve the ISBN-hostile
                    # sources of the title/author they search by, silently turning
                    # their hits into misses. Recover it from what we stored.
                    self._seed_hint_from_disk(name, isbn13)
                    continue
                started = time.monotonic()
                outcome = self._run_source(name, cls)
                outcomes.append(outcome)
                self._record_result(outcome, isbn13, time.monotonic() - started)
        finally:
            self._release_client()

        self.outcomes = outcomes
        self._emit_summary(outcomes)

        succeeded = [o for o in outcomes if o.has_metadata]
        if succeeded:
            return 0
        # "Everything was already scraped" is a success, not a failure: the data is
        # on disk from a previous run. Reporting exit 1 here would make a re-run of
        # a complete batch look like a total failure.
        skipped = [o for o in outcomes if o.status == "skipped"]
        if skipped and len(skipped) == len(outcomes):
            print('%s: every selected source has already been answered; nothing to do' % (isbn13,), file=sys.stderr)
            return 0
        print('warning: No source produced metadata for %s' % (isbn13,), file=sys.stderr)
        return 1

    def _already_answered(self, source: str, isbn13: str) -> Optional[str]:
        """Why this pair needs no fetch, or ``None`` to scrape it.

        Two ways to already have an answer, and the reason is returned rather than a
        bare bool so the summary can say which one it was:

        * a record in ``book_metadata/<source>_metadata.json`` -- the site had the
          book and we stored it;
        * an entry in ``metrics/<source>_no_data.txt`` -- the site was reached, was
          not walling us, and genuinely does not carry it.

        Never raises: an unreadable file means "we do not know", and re-scraping is
        the safe answer to that.
        """
        try:
            if self.storage.has_record(source, isbn13):
                return (f"{self.storage.metadata_path(source).name} already holds a "
                        "record for it")
            if self.no_data is not None and self.no_data.contains(source, isbn13):
                return (f"{self.no_data.path_for(source).name} records that this "
                        "source has no such book")
        except Exception as exc:  # noqa: BLE001 - a skip check must not end a run
            print('warning: could not check whether %s was already scraped from %s (%s); scraping it' % (isbn13, source, exc), file=sys.stderr)
        return None

    def _skipped_outcome(
        self, name: str, cls: Type[BaseSource], because: str
    ) -> SourceOutcome:
        """A summary row for a source we did not run because we have its answer."""
        outcome = SourceOutcome(
            name=name,
            display_name=getattr(cls, "display_name", "") or name,
            status="skipped",
        )
        outcome.warnings = [
            f"not scraped this run: {because}"
        ]
        return outcome

    def _seed_hint_from_disk(self, name: str, isbn13: str) -> None:
        """Fill the shared hint from a stored record when its source was skipped.

        Only the seed source matters here: Audible and BookBub index no ISBN and
        can *only* search by title+author, so a skipped Goodreads would turn their
        hits into misses and quietly change what a re-run produces. Reading the
        title back out of ``<source>_metadata.json`` keeps a resumed run equivalent
        to a fresh one. That file is also what decided to skip in the first place,
        so the hint and the skip now come from the same record.
        """
        if name != SEED_SOURCE or self.hint is None:
            return
        if self.hint.title and self.hint.authors:
            return
        record = self.storage.find_record(name, isbn13)
        if not record:
            return
        learned: List[str] = []
        if not self.hint.title and record.get("title"):
            self.hint.title = str(record["title"])
            learned.append(f"title={self.hint.title!r}")
        if not self.hint.authors and record.get("authors"):
            authors = [str(a) for a in record["authors"] if a]
            if authors:
                self.hint.authors = authors
                learned.append("authors=" + ", ".join(authors))
        if learned and verbose():
            print('  Recovered the shared hint from the stored %s record: %s' % (name, '; '.join(learned)), file=sys.stderr)

    def _record_result(
        self, outcome: SourceOutcome, isbn13: str, seconds: float
    ) -> None:
        """Record one source's outcome for the report. Never raises."""
        record = BookRecord(
            isbn13=isbn13,
            source=outcome.name,
            status=outcome.status,
            has_metadata=outcome.has_metadata,
            csv_row=self.csv_row,
            fields_found=len(outcome.found_fields),
            fields_total=len(outcome.found_fields) + len(outcome.missing_fields),
            missing_fields=list(outcome.missing_fields),
            reviews=outcome.reviews_written,
            covers=outcome.covers_written,
            genres=outcome.genres_written,
            blurb_chars=outcome.blurb_chars,
            seconds=seconds,
            # One field for "why nothing came back", most specific first.
            reason=(outcome.error
                    or next(iter(outcome.blocked_hosts.values()), None)
                    or self._blocked_host_touched()
                    or None),
            # Evidence for BookRecord.suspect_empty: an "empty" seen while a host
            # we spoke to was walling us -- or with no host reached at all -- is not
            # a statement about the catalogue, and the report says so.
            blocked_hosts=self._walled_hosts(),
            requests_made=len(self._contacted_hosts()),
        )
        if self.report is not None:
            self.report.record_book(record)
        # A trustworthy "the site answered and has no such book" is the one finding
        # the metadata file cannot express, so it goes in its own list. suspect_empty
        # is what keeps a wall from being written down as an absence.
        if (self.no_data is not None and record.status == "empty"
                and not record.suspect_empty):
            self.no_data.note(outcome.name, isbn13)

    def _run_source(self, name: str, cls: Type[BaseSource]) -> SourceOutcome:
        """Instantiate, scrape and persist one source. Never raises."""
        display = getattr(cls, "display_name", "") or name
        outcome = SourceOutcome(name=name, display_name=display)
        assert self.hint is not None  # run() sets it before we get here

        print('--- %s ---' % (display,), file=sys.stderr)
        try:
            source = cls(self.client)
        except Exception as exc:
            outcome.status = "error"
            outcome.error = f"{type(exc).__name__}: {exc}"
            print('warning: Could not instantiate %s: %s' % (display, outcome.error), file=sys.stderr)
            return outcome

        # Budget hints; adapters may honour or ignore these.
        source.min_reviews = self.config.min_reviews
        source.max_reviews = self.config.max_reviews
        source.want_covers = self.config.download_covers

        # Snapshot the known blocks so we can attribute any *new* one to this
        # source. Without this, a source that never resolved a book_url reports
        # "empty" even when the site was actively refusing us -- which reads as
        # "the site had no data" instead of "the site blocked us".
        blocks_before = set(self.client.blocks)
        # ...and start recording which hosts this source actually contacts, so a
        # wall discovered on an *earlier* book is still recognised on this one.
        # See _status_for for why the "new block" test alone is not enough.
        self.client.begin_host_tracking()

        try:
            result = source.scrape(self.hint)
        except Exception as exc:
            outcome.status = "error"
            outcome.error = f"{type(exc).__name__}: {exc}"
            outcome.blocked_hosts = self._new_blocks(blocks_before)
            print('warning: %s raised during scrape (this should not happen -- adapters must not raise): %s' % (display, outcome.error), file=sys.stderr)
            return outcome

        outcome.blocked_hosts = self._new_blocks(blocks_before)

        if result is None:
            outcome.status = "empty"
            outcome.error = "adapter returned None instead of a ScrapeResult"
            print('warning: %s returned None instead of a ScrapeResult' % (display,), file=sys.stderr)
            return outcome

        # Persistence is guarded exactly like scrape() above. Cover downloads and
        # artefact writes touch scraped URLs and scraped text, so they can raise
        # things no OSError handler would catch (a malformed netloc -> ValueError
        # from urlparse, a lone surrogate -> UnicodeEncodeError). Without this
        # wrapper such a failure would abort the whole run and skip every
        # remaining source -- the opposite of this module's stated guarantee.
        try:
            self._merge_hint(name, result)
            self._persist(result, outcome)
        except Exception as exc:
            outcome.status = "error"
            outcome.error = f"{type(exc).__name__}: {exc}"
            outcome.warnings = list(result.warnings) + [
                f"persisting {name}'s output failed: {outcome.error}"
            ]
            print('warning: %s scraped successfully but persisting its output failed: %s' % (display, outcome.error), file=sys.stderr)
        return outcome

    def _new_blocks(self, before: set) -> Dict[str, str]:
        """Hosts that started blocking us since ``before`` was snapshotted."""
        return {
            host: reason
            for host, reason in self.client.blocks.items()
            if host not in before
        }

    def _merge_hint(self, name: str, result: ScrapeResult) -> None:
        """Fold a source's ``hint_updates`` into the shared hint (never overwrite)."""
        updates = result.hint_updates
        if updates is None or self.hint is None:
            return
        learned: List[str] = []
        if not self.hint.title and getattr(updates, "title", None):
            self.hint.title = updates.title
            learned.append(f"title={updates.title!r}")
        if not self.hint.authors and getattr(updates, "authors", None):
            self.hint.authors = list(updates.authors)
            learned.append("authors=" + ", ".join(self.hint.authors))
        if not self.hint.isbn10 and getattr(updates, "isbn10", None):
            self.hint.isbn10 = updates.isbn10
            learned.append(f"isbn10={updates.isbn10}")
        if learned:
            print('%s seeded the shared hint: %s' % (name, '; '.join(learned)), file=sys.stderr)

    # -- persistence ---------------------------------------------------------

    def _persist(self, result: ScrapeResult, outcome: SourceOutcome) -> None:
        """Write every artefact this result carries; metadata goes last."""
        source = result.source or outcome.name
        isbn13 = result.isbn13 or (self.hint.isbn13 if self.hint else "")
        outcome.book_url = result.book_url

        # 1. Covers
        cover_urls = [u for u in (result.cover_urls or []) if u]
        outcome.covers_seen = len(cover_urls)
        if cover_urls and not self.config.download_covers:
            print('download_covers is off: skipping %d cover download(s) for %s' % (len(cover_urls), source), file=sys.stderr)
        elif cover_urls:
            # Numbering restarts at 1, so last run's higher-numbered covers would
            # otherwise linger and be read back as if they belonged to this run.
            self.storage.purge("covers", isbn13, source)
            index = 0
            for url in cover_urls:
                downloaded = self.client.download_binary_with_type(
                    url, referer=result.book_url
                )
                if downloaded is None:
                    result.warn(f"cover download failed: {url}")
                    continue
                data, content_type = downloaded
                index += 1
                try:
                    path = self.storage.write_cover(
                        isbn13, source, index, data,
                        content_type=content_type, url=url,
                    )
                except _WRITE_ERRORS as exc:
                    index -= 1
                    result.warn(f"could not write cover {url}: {exc}")
                    print('warning: Could not write cover for %s: %s' % (source, exc), file=sys.stderr)
                    continue
                outcome.files.append(path)
                outcome.covers_written += 1
            if outcome.covers_written == 0:
                result.warn("no cover images could be downloaded")
        else:
            result.warn("no cover image URLs found")

        # 2. Blurb
        blurb = (result.blurb or "").strip()
        if blurb:
            try:
                path = self.storage.write_blurb(isbn13, source, blurb)
                outcome.files.append(path)
                outcome.blurb_chars = len(blurb)
            except _WRITE_ERRORS as exc:
                result.warn(f"could not write blurb: {exc}")
                print('warning: Could not write blurb for %s: %s' % (source, exc), file=sys.stderr)
        else:
            result.warn("no blurb/description found")

        # 3. Reviews
        reviews = [r for r in (result.reviews or []) if r and (r.text or "").strip()]
        cap = self.config.max_reviews
        if cap is not None and len(reviews) > cap:
            print('Capping %s reviews at the max_reviews setting of %d (found %d)' % (source, cap, len(reviews)), file=sys.stderr)
            reviews = reviews[:cap]
        # Same reasoning as covers: numbering restarts at 1 every run, so a run
        # that collects fewer reviews than its predecessor must not leave the
        # predecessor's tail behind.
        if reviews:
            self.storage.purge("reviews", isbn13, source)
        # ``position`` is a *write* counter, not an enumerate() index, so a review
        # that fails to render or write leaves no gap in the 1-based numbering.
        position = 0
        for ordinal, review in enumerate(reviews, start=1):
            try:
                body = review.to_text_block()
            except Exception as exc:
                result.warn(f"could not render review {ordinal}: {exc}")
                continue
            try:
                path = self.storage.write_review(isbn13, source, position + 1, body)
            except _WRITE_ERRORS as exc:
                result.warn(f"could not write review {ordinal}: {exc}")
                print('warning: Could not write review %d for %s: %s' % (ordinal, source, exc), file=sys.stderr)
                continue
            position += 1
            outcome.files.append(path)
            outcome.reviews_written += 1

        target = self.config.min_reviews
        if target and outcome.reviews_written < target:
            message = (
                f"only {outcome.reviews_written} review(s) collected, "
                f"fewer than the requested minimum of {target}"
            )
            result.warn(message)
            print('warning: %s: %s' % (source, message), file=sys.stderr)

        # 4. Genres
        genres = self._genres_for(result)
        if genres:
            try:
                path = self.storage.write_genres(isbn13, source, genres)
                outcome.files.append(path)
                outcome.genres_written = len(genres)
            except _WRITE_ERRORS as exc:
                result.warn(f"could not write genres: {exc}")
                print('warning: Could not write genres for %s: %s' % (source, exc), file=sys.stderr)
        else:
            result.warn("no genres found")

        # 5. Metadata (last, so _warnings is complete)
        metadata = result.metadata
        if metadata is not None:
            if not metadata.genres and genres:
                metadata.genres = list(genres)
            if not metadata.isbn13:
                metadata.isbn13 = isbn13
            try:
                path = self.storage.append_metadata(source, metadata.to_json_dict())
                outcome.files.append(path)
                outcome.has_metadata = True
            except _WRITE_ERRORS as exc:
                result.warn(f"could not write metadata: {exc}")
                print('warning: Could not write metadata for %s: %s' % (source, exc), file=sys.stderr)
        else:
            result.warn("no metadata could be parsed")
            print('warning: %s produced no metadata record' % (source,), file=sys.stderr)

        outcome.found_fields, outcome.missing_fields = self._field_report(metadata, genres)
        outcome.warnings = list(result.warnings)
        outcome.status = self._status_for(outcome, result)

        print('%s: metadata=%s fields=%s covers=%d reviews=%d genres=%d blurb=%d chars warnings=%d' % (source, 'yes' if outcome.has_metadata else 'no', outcome.field_score, outcome.covers_written, outcome.reviews_written, outcome.genres_written, outcome.blurb_chars, len(outcome.warnings)), file=sys.stderr)

    def _genres_for(self, result: ScrapeResult) -> List[str]:
        """Union of ``result.genres`` and ``result.metadata.genres``, deduped."""
        candidates: List[str] = []
        candidates.extend(str(g).strip() for g in (result.genres or []))
        if result.metadata is not None:
            candidates.extend(str(g).strip() for g in (result.metadata.genres or []))
        out: List[str] = []
        seen: set = set()
        for genre in candidates:
            if not genre:
                continue
            key = genre.casefold()
            if key in seen:
                continue
            seen.add(key)
            out.append(genre)
        return out

    def _field_report(
        self, metadata: Optional[BookMetadata], genres: Sequence[str]
    ) -> Tuple[List[str], List[str]]:
        """Split :data:`REPORT_FIELDS` into found/missing for the summary."""
        found: List[str] = []
        missing: List[str] = []
        for name in self.REPORT_FIELDS:
            if metadata is None:
                missing.append(name)
                continue
            if name == "genres":
                value = list(metadata.genres) or list(genres)
            else:
                value = getattr(metadata, name, None)
            (found if value else missing).append(name)
        return found, missing

    def _status_for(self, outcome: SourceOutcome, result: ScrapeResult) -> str:
        """Classify an outcome for the summary's STATUS column.

        ``empty`` and ``blocked`` must not be confused. Neither is skipped any
        more -- neither leaves a metadata record -- but the report reads very
        differently: "Kobo does not sell this 1980s paperback as an ebook" is a
        finding, and "Kobo walled us" is a problem to act on. Merging them once had
        629 WAF-challenged Goodreads books written off as "not on Goodreads"; every
        one resolved on a later attempt.

        The old test asked "did a host *start* blocking during this source's
        turn?" (``outcome.blocked_hosts``). One client serves the whole batch, so a
        host is recorded the **first** time any book meets its wall, and the very
        next victim of that same wall saw no *new* block and was filed as
        ``empty``. Now the question is "was any host this source actually
        contacted blocked?", which is true for the first victim and the
        five-hundredth alike.
        """
        blocked = (
            (self.client.block_reason(result.book_url or "") if result.book_url else None)
            or (next(iter(outcome.blocked_hosts.values()), None))
            or self._blocked_host_touched()
        )
        if not outcome.has_metadata and not result.has_payload():
            return "blocked" if blocked else "empty"
        if outcome.missing_fields or outcome.warnings:
            return "partial"
        return "ok"

    def _contacted_hosts(self) -> List[str]:
        """Hosts the source just spoke to (empty if the client cannot say)."""
        try:
            return sorted(self.client.hosts_contacted())
        except AttributeError:
            return []

    def _walled_hosts(self) -> List[str]:
        """Of the hosts just contacted, those currently known to be blocking us."""
        blocks = self.client.blocks
        return [h for h in self._contacted_hosts() if blocks.get(h)]

    def _blocked_host_touched(self) -> Optional[str]:
        """A block reason for any host this source contacted, or ``None``.

        Deliberately ignores hosts the source never spoke to: a Kobo wall must not
        make an Audible miss look transient.
        """
        try:
            contacted = self.client.hosts_contacted()
        except AttributeError:      # a client without host tracking
            return None
        if not contacted:
            return None
        blocks = self.client.blocks
        for host in contacted:
            reason = blocks.get(host)
            if reason:
                return reason
        return None


# ---------------------------------------------------------------------------
# Human-facing summary (the only stdout in the package's control flow)
# ---------------------------------------------------------------------------

_COLUMNS: Tuple[Tuple[str, str], ...] = (
    ("SOURCE", "<"),
    ("STATUS", "<"),
    ("FIELDS", ">"),
    ("COVERS", ">"),
    ("REVIEWS", ">"),
    ("GENRES", ">"),
    ("BLURB", ">"),
    ("WARN", ">"),
)


def format_summary(pipeline: "Pipeline", outcomes: Sequence[SourceOutcome]) -> str:
    """Render the per-source summary table plus detail blocks as one string."""
    isbn13 = pipeline.hint.isbn13 if pipeline.hint else pipeline.config.isbn
    pretty = isbn_utils.hyphenate(isbn13) if pipeline.hint else isbn13
    width = 78
    lines: List[str] = ["", "=" * width]
    title = f"SCRAPE SUMMARY  {isbn13}"
    if pretty and pretty != isbn13:
        title += f"  ({pretty})"
    if pipeline.hint and pipeline.hint.title:
        title += f"  {pipeline.hint.title!r}"
    lines.append(title)
    lines.append("=" * width)

    if not outcomes:
        lines.append("No sources ran. Nothing was written.")
        lines.append("=" * width)
        return "\n".join(lines)

    rows: List[List[str]] = []
    for outcome in outcomes:
        covers = str(outcome.covers_written)
        if outcome.covers_seen and outcome.covers_written != outcome.covers_seen:
            covers = f"{outcome.covers_written}/{outcome.covers_seen}"
        rows.append([
            outcome.name,
            outcome.status,
            outcome.field_score,
            covers,
            str(outcome.reviews_written),
            str(outcome.genres_written),
            str(outcome.blurb_chars),
            str(len(outcome.warnings)),
        ])

    widths = [
        max(len(header), *(len(row[i]) for row in rows))
        for i, (header, _) in enumerate(_COLUMNS)
    ]
    header_cells = [
        header.ljust(widths[i]) if align == "<" else header.rjust(widths[i])
        for i, (header, align) in enumerate(_COLUMNS)
    ]
    lines.append("  ".join(header_cells))
    lines.append("-" * min(width, len("  ".join(header_cells))))
    for row in rows:
        cells = [
            row[i].ljust(widths[i]) if align == "<" else row[i].rjust(widths[i])
            for i, (_, align) in enumerate(_COLUMNS)
        ]
        lines.append("  ".join(cells))

    lines.append("")
    for outcome in outcomes:
        header = f"{outcome.name} ({outcome.display_name})"
        lines.append(header)
        if outcome.book_url:
            lines.append(f"  url      : {outcome.book_url}")
        if outcome.error:
            lines.append(f"  error    : {outcome.error}")
        if outcome.found_fields:
            lines.append(f"  found    : {', '.join(outcome.found_fields)}")
        if outcome.missing_fields:
            lines.append(f"  missing  : {', '.join(outcome.missing_fields)}")
        if outcome.blocked_hosts:
            for host, reason in sorted(outcome.blocked_hosts.items()):
                lines.append(f"  blocked  : {host}: {reason}")
        if outcome.files:
            lines.append(f"  files    : {len(outcome.files)} written")
        if outcome.warnings:
            lines.append("  warnings :")
            for warning in outcome.warnings:
                lines.append(f"    - {warning}")
        lines.append("")

    blocks = pipeline.client.blocks
    if blocks:
        lines.append("Hosts that blocked automated access (not circumvented):")
        for host, reason in sorted(blocks.items()):
            lines.append(f"  - {host}: {reason}")
        lines.append("")

    total_files = sum(len(o.files) for o in outcomes)
    lines.append(f"Output root : {pipeline.storage.root}")
    lines.append(f"Files written: {total_files}")
    # State the naming convention explicitly, every run. The assignment PDF's
    # paths ("book metadata/", "<isbn13> cp <source> <n>.jpg") are a LaTeX
    # underscore-eating artefact, so underscores are what we write. The other form
    # is still supported by Storage; it is the ``filename_style`` setting in main.py.
    style = pipeline.storage.filename_style
    example = pipeline.storage.dir_name("metadata")
    lines.append(
        f"Naming style: {style} (e.g. {example}/goodreads"
        f"{'_' if style == 'underscore' else ' '}metadata.json)"
    )
    lines.append("=" * width)
    return "\n".join(lines)


def print_summary(pipeline: "Pipeline", outcomes: Sequence[SourceOutcome]) -> None:
    """Print :func:`format_summary` to stdout."""
    print(format_summary(pipeline, outcomes))


def run_pipeline(
    config: PipelineConfig,
    report: Optional[RunReport] = None,
    skip_existing: bool = True,
    no_data: Optional[NoDataIndex] = None,
) -> int:
    """Convenience wrapper: build a :class:`Pipeline`, run it, return the exit code.

    When ``report`` is given, it is written under ``config.out_dir`` before
    returning, so a single-ISBN run produces the same ``metrics/`` a batch does.
    """
    pipeline = Pipeline(config, report=report, skip_existing=skip_existing,
                        no_data=no_data)
    try:
        code = pipeline.run()
    finally:
        if no_data is not None:
            no_data.flush()
        # The indexes served this one crawl; nothing needs them now.
        release_indexes()
    if report is not None:
        report.write_all()
    return code

