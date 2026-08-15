"""What the run tells you afterwards: per-source files, and a closing digest.

Two things go to ``metrics/``:

* ``<source>_isbns.txt`` -- this run's report: which ISBNs that source delivered
  and which it did not, with a reason each. Rewritten whole per run, one file per
  source, so two terminals running ``--sources goodreads`` and ``--sources kobo``
  touch disjoint files by construction.
* ``<source>_no_data.txt`` -- durable state, written by
  :class:`~bookscraper.nodata.NoData`.

Nothing here is load-bearing for a scrape: every writer degrades to a warning.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Sequence, Tuple

from .storage import sanitise, write_text
from .transport import warn

if TYPE_CHECKING:  # pragma: no cover
    from .csv_input import Entry
    from .ledger import NoData
    from .runner import Outcome

WIDTH = 78


@dataclass
class Report:
    """This run's results, held in memory and written out once at the end."""

    directory: Path
    #: source -> [(isbn13, outcome)] for every pair actually attempted.
    rows: Dict[str, List[Tuple[str, "Outcome"]]] = field(default_factory=dict)
    #: source -> pairs skipped because the answer was already on disk.
    skipped: Dict[str, int] = field(default_factory=dict)

    def add(self, entry: "Entry", outcome: "Outcome") -> None:
        self.rows.setdefault(outcome.name, []).append((entry.isbn13, outcome))

    def path_for(self, source: str) -> Path:
        return self.directory / f"{sanitise(source, 'source')}_isbns.txt"

    def write(self) -> List[Path]:
        """One report file per source. Never raises."""
        written: List[Path] = []
        for source, rows in sorted(self.rows.items()):
            try:
                write_text(self.path_for(source), self._render(source, rows))
            except (OSError, ValueError, TypeError) as exc:
                warn(f"warning: could not write {self.path_for(source)}: {exc}")
                continue
            written.append(self.path_for(source))
        if written:
            warn(f"Wrote {len(written)} report file(s) to {self.directory}")
        return written

    @staticmethod
    def _render(source: str, rows: Sequence[Tuple[str, "Outcome"]]) -> str:
        """One source's report: which ISBNs it delivered, and which it did not."""
        good = [i for i, o in rows if o.has_metadata]
        bad = [(i, o) for i, o in rows if not o.has_metadata]
        seconds = sum(o.seconds for _, o in rows)
        rate = len(good) / len(rows) * 100 if rows else 0.0
        lines = ["=" * WIDTH,
                 f"{source}: {len(good)} of {len(rows)} succeeded ({rate:.0f}%), "
                 f"avg {seconds / max(1, len(rows)):.1f}s per book",
                 "=" * WIDTH, ""]

        firm = [(i, o) for i, o in bad if o.trustworthy]
        shaky = [(i, o) for i, o in bad if not o.trustworthy]
        lines.append(f"FAILED ({len(firm)}):")
        lines += [f"  {i}  [{o.status}]  {_reason(o)}" for i, o in firm] or ["  (none)"]
        if shaky:
            # "Audible does not carry this book" and "we never got an answer out of
            # Audible" are different findings; merging them sends a reader hunting
            # for a parser bug that does not exist.
            lines += ["", f"NOT TRUSTWORTHY ({len(shaky)}) -- reported empty, but the "
                      "site was walling us or was", "never successfully reached, so "
                      '"no such book" is not a finding. These are not',
                      f"recorded in {source}_no_data.txt, so they are checked again:"]
            lines += [f"  {i}  {_reason(o)}" for i, o in shaky]
        lines += ["", f"SUCCEEDED ({len(good)}):"]
        lines += [f"  {i}" for i in good] or ["  (none)"]
        return "\n".join(lines + ["", "=" * WIDTH]) + "\n"


def _reason(outcome: "Outcome") -> str:
    """The most specific available account of why nothing came back."""
    if outcome.status == "blocked":
        return "the site was blocking automated access"
    if not outcome.trustworthy:
        return "no host was successfully reached"
    return next((w for w in outcome.warnings), "no metadata parsed from the page")


def digest(books: Sequence[Sequence["Outcome"]], report: Report, nodata: "NoData",
           root: Path, seconds: float, paths: Sequence[Path], *,
           interrupted: bool = False, resume: int = 0) -> str:
    """The closing summary. 10 000 per-book tables are unreadable; this is not."""
    flat = [o for book in books for o in book]
    ok = [book for book in books if any(o.has_metadata for o in book)]
    lines = ["", "=" * WIDTH, "SCRAPE SUMMARY", "=" * WIDTH,
             f"ISBNs scraped   : {len(books)}",
             f"  with metadata : {len(ok)}",
             f"  empty/failed  : {len(books) - len(ok)}",
             f"Files written   : {sum(o.covers + o.reviews for o in flat)}",
             f"  reviews       : {sum(o.reviews for o in flat)}",
             f"  covers        : {sum(o.covers for o in flat)}"]

    if total := sum(report.skipped.values()):
        parts = ", ".join(f"{s} {n}" for s, n in sorted(report.skipped.items()))
        lines.append(f"Already answered: {total} pair(s) not re-fetched ({parts})")
    if shaky := sum(1 for o in flat if o.status == "empty" and not o.trustworthy):
        lines.append(f"Untrusted empty : {shaky} reported empty while the site was "
                     "walling us, so not recorded as absent")
    if unfinished := sum(1 for o in flat if o.incomplete):
        lines.append(f"Incomplete      : {unfinished} book(s) kept on the to-do list "
                     "(record written, an artefact failed), so a re-run retries them")
    if nodata.recorded:
        parts = ", ".join(f"{s} {n}" for s, n in sorted(nodata.recorded.items()))
        lines.append(f"No such book    : {sum(nodata.recorded.values())} pair(s) "
                     f"recorded, so the next run skips them ({parts})")
    lines.append(f"Elapsed         : {_duration(seconds)}")
    if books:
        lines.append(f"  per ISBN      : {_duration(seconds / len(books))} average")

    # The per-source roll-up is the number that tells you a selector has rotted.
    # A single book's empty result never does.
    per_source: Dict[str, Counter] = {}
    for outcome in flat:
        bucket = per_source.setdefault(outcome.name, Counter())
        bucket["seen"] += 1
        bucket["metadata"] += int(outcome.has_metadata)
        bucket["reviews"] += outcome.reviews
        bucket["covers"] += outcome.covers
        bucket[outcome.status] += 1
    if per_source:
        lines += ["", f"{'SOURCE':<12}{'METADATA':>12}{'REVIEWS':>10}{'COVERS':>9}"
                      f"{'BLOCKED':>9}{'SKIPPED':>9}", "-" * 61]
        for name, stats in sorted(per_source.items()):
            score = f"{stats['metadata']}/{stats['seen']}"
            lines.append(f"{name:<12}{score:>12}{stats['reviews']:>10}"
                         f"{stats['covers']:>9}{stats['blocked']:>9}"
                         f"{stats['skipped']:>9}")

    blocked = {o.name for o in flat if o.status == "blocked"}
    if blocked:
        lines += ["", "Sources blocked by anti-bot walls (not circumvented): "
                      + ", ".join(sorted(blocked))]
    lines.append("")
    lines.append(f"Output root     : {root}")
    if paths:
        names = ", ".join(sorted(p.name for p in paths))
        lines.append(f"Report          : {paths[0].parent}/ ({names})")
    if interrupted:
        lines.append(f"Interrupted     : re-run with --start {resume} to continue")
    return "\n".join(lines + ["=" * WIDTH])


def _duration(seconds: float) -> str:
    """``93.4`` -> ``'1m33s'``."""
    total = int(round(max(0.0, seconds)))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h{minutes:02d}m{secs:02d}s"
    return f"{minutes}m{secs:02d}s" if minutes else f"{secs}s"
