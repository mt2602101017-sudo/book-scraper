"""The run's report: which books each source delivered, and which it did not.

This module used to be a *ledger* as well -- a durable ``scrape_ledger.jsonl`` of
every (ISBN, source) attempt, read back on startup to decide what not to re-fetch.
That job now belongs to the output itself: ``Storage.has_record`` asks
``book_metadata/<source>_metadata.json`` whether it already holds the book, and the
pipeline skips the source if it does. One fact, one place.

The one thing the output cannot express
--------------------------------------
A metadata record is keyed on the book *existing*, so no shape of "record present"
can mean "this site does not carry it". A source that answered honestly and had no
such book therefore leaves nothing behind, and is indistinguishable from one that
was never asked. Measured on the shipped CSV that is 20 007 of 49 975 pairs, and
re-crawling them costs ~29 h per full run of the four default sources (Kobo 24 h,
Audible 4.6 h) -- 187 h if BookBub is included.

So absence is recorded too, in :class:`NoDataIndex`: one plain list of ISBNs per
source, at ``<out>/metrics/<source>_no_data.txt``. That is the whole of what came
back when the ledger was removed -- one bit per pair instead of a status vocabulary,
a per-attempt history, an adoption pass and a retryability rule.

Only a **trustworthy** empty is recorded. ``status == "empty"`` plus
:attr:`BookRecord.suspect_empty` being false means the site was actually reached and
no host this source contacted was walling us. Without that guard this file would
recreate the failure it is modelled on: 629 WAF-challenged Goodreads books were once
written off as "not on Goodreads", and every one resolved on a later attempt.

What this module writes
-----------------------
Everything goes in one metrics directory -- ``./metrics/`` by default, **outside**
the ``out_dir`` artefact tree (both are set in ``main.py``'s ``SETTINGS``). Two files per
source:

* ``<source>_isbns.txt`` -- **this run's report**: the ISBNs that failed with a reason
  each, and the ISBNs that succeeded. Rewritten whole each run. The per-attempt CSV
  and the JSON rollup that used to sit beside it were the same numbers in two more
  shapes, and ``batch_manifest.csv`` was a third; all three are gone. The per-book
  summary tables printed during the run carry the per-pair detail.
* ``<source>_no_data.txt`` -- **durable state**, merged rather than overwritten, so a
  ``--end 100`` slice cannot truncate the entries it never looked at.

Both classes are handed the directory itself rather than an output root, so the
caller decides where reports live and a test can point them at a temp path.

Both are keyed by source *in the filename*, which is what lets one directory serve
every run. There used to be a ``metrics.goodreads/`` / ``metrics.amazon/`` split,
because a single ``isbns_by_source.txt`` bundling all five sources would have been
overwritten by the next run covering different ones. Splitting the report per source
removes the collision, and with it the whole suffix concept. Two terminals running
``--sources goodreads`` and ``--sources kobo`` into one output tree now touch disjoint
files by construction.

(Two runs covering the *same* source still overwrite each other's report -- last
writer wins, exactly as before, since the suffix never distinguished those either.
The ``_no_data`` list is safe regardless: it merges under an ``fcntl`` lock.)

Nothing here is load-bearing for a scrape: every writer degrades to a warning, and
recording one result is a list append.
"""

from __future__ import annotations

import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from .storage import file_lock
from .verbosity import verbose

__all__ = [
    "METRICS_DIR_NAME",
    "NO_DATA_SUFFIX",
    "REPORT_SUFFIX",
    "BookRecord",
    "NoDataIndex",
    "RunReport",
]

#: Directory the end-of-run report goes into.
METRICS_DIR_NAME = "metrics"

#: Filename tail of the per-source "answered, and does not carry it" lists.
NO_DATA_SUFFIX = "_no_data.txt"

#: Filename tail of the per-source run report.
REPORT_SUFFIX = "_isbns.txt"


@dataclass
class BookRecord:
    """What one (ISBN, source) attempt produced.

    ``status`` uses the pipeline's own vocabulary (``ok`` / ``partial`` / ``empty``
    / ``blocked`` / ``error`` / ``skipped``) so the report never disagrees with the
    table printed during the run.
    """

    isbn13: str
    source: str
    status: str = "pending"
    has_metadata: bool = False
    csv_row: int = 0
    fields_found: int = 0
    fields_total: int = 0
    missing_fields: List[str] = field(default_factory=list)
    reviews: int = 0
    covers: int = 0
    genres: int = 0
    blurb_chars: int = 0
    seconds: float = 0.0
    #: Why nothing came back -- an exception, a block reason, or simply empty.
    reason: Optional[str] = None
    #: Hosts that were walling us while this attempt ran, if any. An ``empty`` with
    #: a value here is **not trustworthy**: see :meth:`suspect_empty`.
    blocked_hosts: List[str] = field(default_factory=list)
    #: How many outbound requests this attempt made. Zero alongside ``empty`` means
    #: the site was never actually asked, so "no such book" cannot be a finding.
    requests_made: int = 0

    @property
    def succeeded(self) -> bool:
        return bool(self.has_metadata)

    @property
    def suspect_empty(self) -> bool:
        """True when this ``empty`` may really be a block in disguise.

        No longer changes what gets retried -- an ``empty`` leaves no metadata
        record, so it is re-asked next run either way. It changes what the report
        *claims*: "Audible does not carry this book" and "we never got an answer out
        of Audible" are different findings, and merging them would send a reader
        hunting for a parser bug that does not exist.
        """
        if self.status != "empty":
            return False
        return bool(self.blocked_hosts) or self.requests_made == 0


class NoDataIndex:
    """The ISBNs each source answered about and genuinely does not carry.

    The complement of ``book_metadata/<source>_metadata.json``: between them, "have
    we already asked this site about this book?" has an answer for every pair.

    Read lazily per source and cached, so 10 000 skip checks cost one file read.
    Written by :meth:`flush` at the end of a run, **merged** with whatever is
    already on disk -- this is durable state, and a partial run (``--end 100``) must
    not truncate the entries it did not look at.

    The file is nothing but ISBN-13s, one per line, sorted. No header, no comments,
    no columns: reading it is ``set(text.split())`` and there is no format to parse or
    to get wrong. What the file *means* is documented in the README and named by the
    filename.
    """

    def __init__(self, directory: Path, enabled: bool = True) -> None:
        #: Where the lists live. Given explicitly, not derived from ``out_dir``: the
        #: lists are state about the *sites*, not artefacts of one output tree.
        self.directory = Path(directory)
        #: ``False`` makes every query miss and every write a
        #: no-op, so the caller needs no conditionals.
        self.enabled = bool(enabled)
        #: source -> ISBNs already on disk (loaded on first use).
        self._known: Dict[str, Set[str]] = {}
        #: source -> ISBNs this run discovered, not yet written.
        self._added: Dict[str, Set[str]] = {}
        #: source -> how many this run actually wrote. Kept separately because
        #: :meth:`flush` clears ``_added``, and the batch digest renders *after* the
        #: flush -- reading ``_added`` there reported nothing every time.
        self._recorded: Dict[str, int] = {}

    # -- layout --------------------------------------------------------------

    def path_for(self, source: str) -> Path:
        """``metrics/<source>_no_data.txt`` for ``source``."""
        safe = re.sub(r"[^a-z0-9._-]+", "-", str(source or "source").strip().lower())
        return self.directory / f"{safe or 'source'}{NO_DATA_SUFFIX}"

    # -- reading -------------------------------------------------------------

    def _load(self, source: str) -> Set[str]:
        """The ISBNs on disk for ``source``, read once. Never raises."""
        known = self._known.get(source)
        if known is not None:
            return known
        known = set()
        path = self.path_for(source)
        try:
            if path.is_file():
                # One ISBN per line and nothing else, so split() is the whole reader.
                known = set(path.read_text(encoding="utf-8").split())
        except (OSError, ValueError) as exc:
            print(f"warning: could not read {path} ({exc}); every {source} miss will "
                  "be re-checked this run", file=sys.stderr)
            known = set()
        self._known[source] = known
        if known and verbose():
            print(f"  {path.name}: {len(known)} known-absent ISBN(s)", file=sys.stderr)
        return known

    def contains(self, source: str, isbn13: str) -> bool:
        """True when ``source`` has already answered that it has no such book."""
        if not self.enabled:
            return False
        wanted = str(isbn13).strip()
        if not wanted:
            return False
        source = str(source).strip().lower()
        return wanted in self._load(source) or wanted in self._added.get(source, set())

    def count(self, source: str) -> int:
        """How many ISBNs are known absent for ``source`` (disk + this run)."""
        source = str(source).strip().lower()
        return len(self._load(source) | self._added.get(source, set()))

    # -- writing -------------------------------------------------------------

    def note(self, source: str, isbn13: str) -> None:
        """Remember that ``source`` answered and has no such book.

        Call this **only** for a trustworthy empty -- see the module docstring. Held
        in memory until :meth:`flush`, so one run appends once per source rather
        than rewriting a 10 000-line file per book.
        """
        if not self.enabled:
            return
        isbn13 = str(isbn13).strip()
        source = str(source).strip().lower()
        if not isbn13 or not source:
            return
        if isbn13 in self._load(source):
            return
        self._added.setdefault(source, set()).add(isbn13)

    @property
    def added(self) -> Dict[str, int]:
        """source -> how many new absences are found but not yet written."""
        return {source: len(isbns) for source, isbns in sorted(self._added.items())
                if isbns}

    @property
    def recorded(self) -> Dict[str, int]:
        """source -> how many new absences this run wrote, for the digest."""
        return {source: count for source, count in sorted(self._recorded.items())
                if count}

    def flush(self) -> List[Path]:
        """Merge this run's findings into the lists on disk. Never raises."""
        if not self.enabled or not self._added:
            return []
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            print(f"warning: could not create {self.directory}, so {sum(len(v) for v in self._added.values())} "
                  f"known-absent ISBN(s) were not saved and will be re-checked: {exc}",
                  file=sys.stderr)
            return []

        written: List[Path] = []
        for source, isbns in sorted(self._added.items()):
            if not isbns:
                continue
            path = self.path_for(source)
            try:
                # Re-read under the lock rather than trusting the cached copy: a
                # second main.py process may have added entries since we loaded it,
                # and a merge must not drop them.
                with file_lock(path):
                    on_disk: Set[str] = set()
                    if path.is_file():
                        on_disk = set(path.read_text(encoding="utf-8").split())
                    merged = on_disk | isbns
                    path.write_text("\n".join(sorted(merged)) + "\n",
                                    encoding="utf-8", newline="\n")
            except (OSError, ValueError) as exc:
                print(f"warning: could not write {path} ({exc}); {len(isbns)} miss(es) "
                      "will be re-checked next run", file=sys.stderr)
                continue
            self._known[source] = merged
            self._recorded[source] = self._recorded.get(source, 0) + len(isbns)
            written.append(path)
            print(f"Recorded {len(isbns)} new known-absent ISBN(s) in {path.name} "
                  f"({len(merged)} total)", file=sys.stderr)
        self._added.clear()
        return written


class RunReport:
    """This run's results, held in memory, written out once at the end.

    One instance per run, shared by every book. Nothing is read back and nothing
    persists between runs -- the metadata files are the durable record.
    ``enabled=False`` makes the writers no-ops so callers need no conditionals.
    """

    def __init__(self, directory: Path, enabled: bool = True) -> None:
        #: Where the report files go. Same reasoning as :class:`NoDataIndex`.
        self.directory = Path(directory)
        self.enabled = bool(enabled)
        #: This run's own results.
        self._books: List[BookRecord] = []
        #: source -> pairs skipped this run because a record was already on disk.
        self.skipped: Dict[str, int] = {}
        #: source -> empties this run that the report will flag as untrustworthy.
        self.suspect_empties: Dict[str, int] = {}

    # -- recording -----------------------------------------------------------

    def record_book(self, book: BookRecord) -> None:
        """Keep one result for the report. Never raises."""
        if not self.enabled:
            return
        self._books.append(book)
        if book.suspect_empty:
            self.suspect_empties[book.source] = (
                self.suspect_empties.get(book.source, 0) + 1
            )

    def note_skipped(self, source: str) -> None:
        """Count a skip, for the run digest."""
        self.skipped[source] = self.skipped.get(source, 0) + 1

    @property
    def total_skipped(self) -> int:
        return sum(self.skipped.values())

    def summarise(self) -> str:
        """One digest line, or ``''`` when nothing was skipped.

        Deliberately does not name *which* of the two reasons applied -- a stored
        record, or a recorded absence -- because a run mixes both. The per-source
        summary block names the reason for each book.
        """
        if not self.total_skipped:
            return ""
        parts = ", ".join(f"{s} {n}" for s, n in sorted(self.skipped.items()))
        return (f"{self.total_skipped} (ISBN, source) pair(s) skipped, because we "
                f"already had the answer: {parts}")

    # -- reporting -----------------------------------------------------------

    def _rollup(self) -> Dict[str, Dict[str, Any]]:
        """Per-source totals plus the ISBNs that succeeded and failed."""
        by_source: Dict[str, List[BookRecord]] = defaultdict(list)
        for book in self._books:
            by_source[book.source].append(book)
        out: Dict[str, Dict[str, Any]] = {}
        for source, records in sorted(by_source.items()):
            good = [r for r in records if r.succeeded]
            bad = [r for r in records if not r.succeeded]
            seconds = sum(r.seconds for r in records)
            out[source] = {
                "attempted": len(records),
                "succeeded": len(good),
                "failed": len(bad),
                "statuses": dict(Counter(r.status for r in records).most_common()),
                "reviews": sum(r.reviews for r in records),
                "covers": sum(r.covers for r in records),
                "average_seconds": round(seconds / len(records), 2) if records else 0.0,
                "suspect_empties": len([r for r in bad if r.suspect_empty]),
                "successful_isbns": [r.isbn13 for r in good],
                "failed_isbns": [
                    {"isbn13": r.isbn13, "csv_row": r.csv_row, "status": r.status,
                     "trustworthy": not r.suspect_empty,
                     "reason": (r.reason
                                or ("blocked" if r.status == "blocked" else None)
                                or (f"walled by {', '.join(r.blocked_hosts)}"
                                    if r.blocked_hosts else None)
                                or ("no host was successfully reached"
                                    if r.requests_made == 0 else None)
                                or "no metadata parsed from the page")}
                    for r in bad
                ],
            }
        return out

    def path_for(self, source: str) -> Path:
        """``<metrics-dir>/<source>_isbns.txt`` for ``source``."""
        safe = re.sub(r"[^a-z0-9._-]+", "-", str(source or "source").strip().lower())
        return self.directory / f"{safe or 'source'}{REPORT_SUFFIX}"

    def write_all(self) -> List[Path]:
        """Write one report file per source under ``metrics/``. Never raises.

        One file per source rather than one bundling them all, so a run covering
        ``--sources kobo`` cannot overwrite the report of a run covering
        ``--sources goodreads``. That is what removed the old ``metrics.<sources>/``
        directory suffix.
        """
        if not self.enabled:
            return []
        rollup = self._rollup()
        if not rollup:
            return []
        directory = self.directory
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            print(f"warning: could not create {directory}, so no report was "
                  f"written: {exc}", file=sys.stderr)
            return []

        written: List[Path] = []
        for source, stats in rollup.items():
            safe = re.sub(r"[^a-z0-9._-]+", "-", source.strip().lower()) or "source"
            path = directory / f"{safe}{REPORT_SUFFIX}"
            try:
                path.write_text(self._render(source, stats), encoding="utf-8",
                                newline="\n")
            except (OSError, ValueError, TypeError) as exc:
                print(f"warning: could not write {path}: {exc}", file=sys.stderr)
                continue
            written.append(path)
        if written:
            print(f"Wrote {len(written)} report file(s) to {directory}",
                  file=sys.stderr)
        return written

    def _render(self, source: str, stats: Dict[str, Any]) -> str:
        """One source's report: which ISBNs it delivered, and which it did not."""
        width = 78
        rate = (stats["succeeded"] / stats["attempted"] * 100
                if stats["attempted"] else 0.0)
        lines = [
            "=" * width,
            f"{source}: {stats['succeeded']} of {stats['attempted']} succeeded "
            f"({rate:.0f}%), avg {stats['average_seconds']:.1f}s per book",
            "=" * width,
            "",
            f"FAILED ({stats['failed']}):",
        ]
        firm = [e for e in stats["failed_isbns"] if e["trustworthy"]]
        shaky = [e for e in stats["failed_isbns"] if not e["trustworthy"]]
        lines += ([f"  {e['isbn13']}  [{e['status']}]  {e['reason']}"
                   for e in firm] or ["  (none)"])
        if shaky:
            lines += [
                "",
                f"NOT TRUSTWORTHY ({len(shaky)}) -- reported empty, but the site was "
                "walling us",
                "or was never successfully reached, so \"no such book\" is not a "
                "finding.",
                "These are not recorded in " + source + NO_DATA_SUFFIX + ", so they "
                "are checked again:",
            ]
            lines += [f"  {e['isbn13']}  {e['reason']}" for e in shaky]
        lines += ["", f"SUCCEEDED ({stats['succeeded']}):"]
        lines += ([f"  {i}" for i in stats["successful_isbns"]] or ["  (none)"])
        lines += ["", "=" * width]
        return "\n".join(lines) + "\n"
