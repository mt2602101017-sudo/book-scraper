"""Orchestration: for each ISBN, run each source, persist, record, summarise.

Guarantees everything around this module relies on:

* Open Library runs **first** whenever selected, and its title and authors seed the
  shared :class:`~bookscraper.models.Hint`, so the stores that index no ISBN
  (BookBub, Kobo, Audible) get their search terms from one cheap lookup.
* One source blowing up never stops the others, and one book never stops the run.
* One :class:`~bookscraper.http.HttpClient` serves the whole run, which is what makes
  the courtesy delay, the block registry and the browser the run's rather than one
  book's.

Books are scraped one at a time, deliberately: the pace is set by the per-host
courtesy delay, not the CPU, so concurrency cannot make the crawl politer or much
faster. Shard with ``--start``/``--end`` or ``--sources`` instead.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Sequence, Type

from . import isbn as isbn_utils
from . import persist
from .base import Source, discover
from .csv_input import Entry
from .http import HttpClient
from .metadata import release_caches
from .models import Hint, Result
from .ledger import NoData, Pending
from .report import Report, digest
from .storage import Storage
from .transport import warn

#: The source whose findings seed the shared hint. Open Library, because it is the
#: only one that is ISBN-indexed *and* answers in a single JSON request.
SEED = "openlibrary"


@dataclass
class Config:
    """Everything the CLI collects, in one bundle."""

    out_dir: Path = Path("data")
    #: Report and known-absent lists. Deliberately **not** under ``out_dir``: they
    #: are state about the sites and the run, not scraped artefacts.
    metrics_dir: Path = Path("metrics")
    sources: Optional[Sequence[str]] = None      # None => all discovered
    min_reviews: int = 25
    max_reviews: Optional[int] = None
    min_delay: float = 1.0
    max_delay: float = 2.0
    timeout: int = 25
    retries: int = 3
    browser: str = "auto"
    covers: bool = True
    skip_scraped: bool = True


@dataclass
class Outcome:
    """What one source produced for one book, for the summary."""

    name: str
    #: ok | partial | empty | blocked | skipped
    status: str = "empty"
    book_url: Optional[str] = None
    found: List[str] = field(default_factory=list)
    missing: List[str] = field(default_factory=list)
    covers: int = 0
    reviews: int = 0
    genres: int = 0
    blurb: int = 0
    warnings: List[str] = field(default_factory=list)
    has_metadata: bool = False
    seconds: float = 0.0
    #: False when an ``empty`` cannot be believed -- the site was walling us, or was
    #: never reached at all. Only a trustworthy empty is recorded as an absence.
    trustworthy: bool = True
    #: True when the record was written but an artefact failed transiently, so this
    #: book must be scraped again even though its metadata is on disk.
    incomplete: bool = False


class Runner:
    """Scrapes a list of ISBNs into one shared output tree."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.storage = Storage(config.out_dir)
        self.nodata = NoData(config.metrics_dir)
        #: Books whose record is on disk but whose artefacts are not, so they are
        #: re-scraped rather than skipped. See :mod:`bookscraper.ledger`.
        self.pending = Pending(config.metrics_dir)
        self.report = Report(config.metrics_dir)
        self.client = HttpClient(config.min_delay, config.max_delay,
                                 config.timeout, config.retries, config.browser)
        self.adapters = self._select()

    def _select(self) -> List[Type[Source]]:
        """Resolve ``--sources`` against discovery, seed source first."""
        available = discover()
        wanted = [s.strip().lower() for s in (self.config.sources or []) if s.strip()]
        if not available:
            warn("warning: no source adapters are available in bookscraper.sources")
            return []
        if not wanted:
            chosen = list(available)
        else:
            chosen = [n for n in wanted if n in available]
            for name in (n for n in wanted if n not in available):
                warn(f"warning: unknown source {name!r} -- skipping it. "
                     f"Available: {', '.join(available)}")
        chosen.sort(key=lambda n: (n != SEED, list(available).index(n)))
        return [available[n] for n in chosen]

    # -- one book ------------------------------------------------------------

    def book(self, entry: Entry) -> List[Outcome]:
        """Scrape every selected source for one ISBN. Returns their outcomes."""
        hint = Hint(isbn13=entry.isbn13, isbn10=isbn_utils.to_isbn10(entry.isbn13))
        outcomes: List[Outcome] = []
        for adapter in self.adapters:
            name = adapter.name
            if self.config.skip_scraped and (why := self._answered(name, entry.isbn13)):
                outcomes.append(Outcome(name=name, status="skipped", warnings=[why]))
                self.report.skipped[name] = self.report.skipped.get(name, 0) + 1
                # A skipped seed source would otherwise starve the ISBN-hostile
                # stores of the title they search by, silently turning their hits
                # into misses. Recover it from what we stored.
                self._recover_hint(name, hint)
                continue
            started = time.monotonic()
            outcome = self._one(adapter, hint)
            outcome.seconds = time.monotonic() - started
            outcomes.append(outcome)
            self.report.add(entry, outcome)
            # A trustworthy "the site answered and has no such book" is the one
            # finding the metadata file cannot express, so it gets its own list.
            if outcome.status == "empty" and outcome.trustworthy:
                self.nodata.note(name, entry.isbn13)
            # And a book that is on disk but unfinished has to stay on the to-do
            # list, or the skip check will call it done next run.
            if outcome.incomplete:
                self.pending.note(name, entry.isbn13)
            elif outcome.has_metadata:
                self.pending.discard(name, entry.isbn13)
        return outcomes

    def _answered(self, source: str, isbn13: str) -> Optional[str]:
        """Why this pair needs no fetch, or ``None`` to scrape it."""
        try:
            if self.pending.contains(source, isbn13):
                return None      # on disk, but an artefact is still missing
            if isbn13 in self.storage.meta.scraped(source):
                return f"{self.storage.meta.path_for(source).name} already holds it"
            if self.nodata.contains(source, isbn13):
                return f"{self.nodata.path_for(source).name} records no such book"
        except Exception as exc:  # noqa: BLE001 - a skip check must not end a run
            warn(f"warning: could not check whether {isbn13} was scraped from "
                 f"{source} ({exc}); scraping it")
        return None

    def _recover_hint(self, source: str, hint: Hint) -> None:
        """Fill the shared hint from a stored record when its source was skipped."""
        if source != SEED or (hint.title and hint.authors):
            return
        record = self.storage.meta.record(source, hint.isbn13)
        if not record:
            return
        hint.title = hint.title or (str(record["title"]) if record.get("title") else None)
        if not hint.authors and record.get("authors"):
            hint.authors = [str(a) for a in record["authors"] if a]

    def _one(self, adapter: Type[Source], hint: Hint) -> Outcome:
        """Instantiate, scrape and persist one source. Never raises."""
        outcome = Outcome(name=adapter.name)
        warn(f"--- {adapter.display_name or adapter.name} ---")
        try:
            source = adapter(self.client)
        except Exception as exc:  # noqa: BLE001
            outcome.warnings = [f"could not instantiate: {exc}"]
            return outcome
        source.min_reviews = self.config.min_reviews
        source.max_reviews = self.config.max_reviews

        # Record which hosts this source contacts, so a wall met on an earlier book
        # is still recognised on this one -- see Transport.touched_block.
        self.client.track_hosts()
        result = source.scrape(hint)
        self._merge_hint(hint, result)
        try:
            persist.write(result, outcome, self.storage, self.client, self.config)
        except Exception as exc:  # noqa: BLE001 - persisting must not end the run
            outcome.warnings = list(result.warnings) + [f"persisting failed: {exc}"]
            warn(f"warning: {adapter.name} scraped but persisting failed: {exc}")
            return outcome
        persist.classify(outcome, result, self.client)
        return outcome

    @staticmethod
    def _merge_hint(hint: Hint, result: Result) -> None:
        """Fold a source's findings into the shared hint, never overwriting."""
        if (found := result.hint) is None:
            return
        hint.title = hint.title or found.title
        hint.authors = hint.authors or list(found.authors or [])
        hint.isbn10 = hint.isbn10 or found.isbn10

    # -- the whole run -------------------------------------------------------

    def run(self, entries: Sequence[Entry]) -> int:
        """Scrape every entry in order. Returns the process exit code."""
        if not self.adapters or not entries:
            warn("warning: nothing to do")
            return 1
        self.storage.ensure_dirs()
        warn(f"Scraping {len(entries)} ISBN(s) from "
             f"{', '.join(a.name for a in self.adapters)} into {self.config.out_dir}")

        started, done, results = time.monotonic(), 0, []
        try:
            for position, entry in enumerate(entries, start=1):
                warn(f"===== [{position}/{len(entries)}] {entry.isbn13} =====")
                results.append(self.book(entry))
                done = position
        except KeyboardInterrupt:
            warn(f"warning: interrupted after {done} of {len(entries)} ISBN(s); "
                 "everything already written is intact")
        finally:
            self.client.close()
            # In the finally, so an interrupted run keeps what it learned.
            self.nodata.flush()
            self.pending.flush()
            release_caches()

        paths = self.report.write()
        print(digest(results, self.report, self.nodata, self.config.out_dir,
                     time.monotonic() - started, paths, interrupted=done < len(entries),
                     resume=done))
        flat = [o for book in results for o in book]
        if any(o.has_metadata for o in flat):
            return 0
        # "Everything was already scraped" is a success: the data is on disk from a
        # previous run, and reporting failure would make a re-run of a complete batch
        # look like a total failure.
        return 0 if flat and all(o.status == "skipped" for o in flat) else 1
