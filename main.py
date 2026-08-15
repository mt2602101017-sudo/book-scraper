#!/usr/bin/env python3
"""Command-line entry point for the book scraper.

    python main.py 2602101017.csv                    # every ISBN in the CSV
    python main.py 2602101017.csv --start 0 --end 20 # ...just the first 20
    python main.py 2602101017.csv --sources goodreads,amazon
    python main.py 9780143127550                     # one ISBN
    python main.py 0143127551                        # ISBN-10 is converted for you
    python main.py                                   # prompts for a CSV path or ISBN

The positional argument is a **CSV file or a single ISBN**; which one it is is
decided by whether it names a file on disk (or carries a ``.csv``/``.tsv``/
``.txt`` suffix), so neither form needs a flag.

Output goes under ``./data`` in one shared tree -- ``book_metadata/``,
``book_coverpage/``, ``book_blurb/``, ``book_reviews/``, ``genres/`` -- with each
source's metadata accumulating into a single ``<source>_metadata.json`` array.
The run report and the resume ledger go under ``./metrics``.

Exit status
    0  at least one source produced metadata (for at least one ISBN)
    1  none did
    2  bad usage -- e.g. the ISBN failed its checksum, or the CSV is unreadable

Progress and warnings are printed to stderr; only the summary tables go to
stdout, so ``python main.py <input> 2>/dev/null`` gives you just the report.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from pathlib import Path
from typing import List, Optional, Sequence

from bookscraper import csv_input
from bookscraper import isbn as isbn_utils
from bookscraper.batch import BatchConfig, run_batch
from bookscraper.metrics import NoDataIndex, RunReport
from bookscraper.pipeline import PipelineConfig, run_pipeline
from bookscraper.sources import discover_sources
from bookscraper.verbosity import set_verbose

#: Everything that used to be a flag. These are settings, not choices: each value
#: is the one found to work while the adapters were built, so exposing it only
#: invited a run that differed from the tested one. Change them here.
SETTINGS = {
    "out_dir": "data",          # the shared artefact tree
    "metrics_dir": "metrics",   # report + resume ledger; outside out_dir on purpose
    "min_reviews": 25,          # the assignment's target; a shortfall warns, never fails
    "max_reviews": None,        # no cap: take whatever a site will give
    "browser": "auto",          # Selenium only where a site needs it (Kobo, BookBub)
    "min_delay": 1.0,           # per-host courtesy delay, randomised in this range
    "max_delay": 2.0,
    "timeout": 25,
    "retries": 3,
    "download_covers": True,
    "filename_style": "underscore",
    "respect_robots": False,
    "user_agent": None,         # HttpClient's desktop-Chrome default
    "isbn_column": None,        # auto-detected from the CSV header or its data
    "verbose": False,           # flip to True to see every fallback as it fires
}


def build_parser() -> argparse.ArgumentParser:
    """Three flags. Everything else is a constant -- see :data:`SETTINGS`.

    The scraper grew 28 flags while it was being built, most of them there to let
    one run differ from another while a site was still being figured out. Now that
    each adapter's working path is known they are settings, not choices, so they
    live in ``SETTINGS`` where a reader sees all of them at once.

    What is left is the part that genuinely varies per invocation: which books
    (``--start``/``--end``) and which sites (``--sources``).
    """
    parser = argparse.ArgumentParser(
        prog="main.py",
        description=(
            "Scrape book metadata, covers, blurbs, reviews and genres for every ISBN "
            "in a CSV (or one ISBN given directly) and write them under ./data."
        ),
        epilog=(
            "Rows are a half-open range: --start 0 --end 100 is the first hundred, "
            "and the next shard starts at 100. Sources: " + ", ".join(discover_sources())
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "input", nargs="?", metavar="CSV_OR_ISBN",
        help="a CSV holding a column of ISBNs, or a single ISBN-10/13",
    )
    parser.add_argument(
        "--start", type=int, default=0, metavar="N",
        help="skip the first N ISBNs",
    )
    parser.add_argument(
        "--end", type=int, default=None, metavar="N",
        help="stop before row N (half-open, so shards tile without overlap)",
    )
    parser.add_argument(
        "--sources", default="all", metavar="LIST",
        help="comma-separated source slugs, or 'all'",
    )
    return parser


def parse_sources(
    raw: Optional[str], parser: Optional[argparse.ArgumentParser] = None
) -> Optional[List[str]]:
    """Turn ``--sources`` into a list of slugs, or ``None`` meaning "all".

    ``None`` is returned only for a genuinely empty value and for the explicit
    ``all`` / ``*`` literals. A *non-empty* value that yields no usable names
    (``','``, ``';;'``) is a usage error: mapping it to "all" silently scraped every
    live site when the user was trying to narrow the run down. With a ``parser``
    this exits 2; without one (unit tests) it raises ``ValueError``.
    """
    if raw is None:
        return None
    text = raw.strip()
    if not text or text.lower() in {"all", "*"}:
        return None
    names = [part.strip().lower() for part in text.replace(";", ",").split(",") if part.strip()]
    if not names:
        message = (
            f"--sources {raw!r} contains no source names. Pass a comma-separated "
            f"list of slugs ({', '.join(discover_sources())}), or 'all'."
        )
        if parser is not None:
            parser.error(message)  # exits 2
        raise ValueError(message)
    return names


def build_config(args: argparse.Namespace, parser: argparse.ArgumentParser) -> PipelineConfig:
    """One :class:`PipelineConfig` from ``SETTINGS`` plus the three flags.

    ``isbn`` is left empty: the batch runner overwrites it per row, and the
    single-ISBN path fills it in with :func:`dataclasses.replace`.
    """
    return PipelineConfig(
        isbn="",
        sources=parse_sources(args.sources, parser),
        out_dir=Path(SETTINGS["out_dir"]),
        metrics_dir=Path(SETTINGS["metrics_dir"]),
        min_reviews=SETTINGS["min_reviews"],
        max_reviews=SETTINGS["max_reviews"],
        min_delay=SETTINGS["min_delay"],
        max_delay=SETTINGS["max_delay"],
        browser=SETTINGS["browser"],
        filename_style=SETTINGS["filename_style"],
        download_covers=SETTINGS["download_covers"],
        respect_robots=SETTINGS["respect_robots"],
        user_agent=SETTINGS["user_agent"],
        timeout=SETTINGS["timeout"],
        max_retries=SETTINGS["retries"],
    )


def prompt_for_input() -> Optional[str]:
    """Ask for a CSV path or an ISBN. Returns ``None`` if the user gives up."""
    try:
        answer = input("Enter a CSV file of ISBNs, or a single ISBN: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    # A path pasted from a shell or a file manager often keeps its quotes.
    return answer.strip("'\"") or None


def run_single(raw_isbn: str, base: PipelineConfig) -> int:
    """Scrape one ISBN. Returns the process exit code."""
    try:
        isbn13 = isbn_utils.to_isbn13(raw_isbn)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        # A bare word with no digits is far likelier to be a mistyped filename
        # than a mistyped ISBN, so say both things.
        if not any(ch.isdigit() for ch in raw_isbn):
            print(
                f"hint: {raw_isbn!r} is neither a readable file nor an ISBN. Pass a "
                "CSV path (e.g. 2602101017.csv) or a 10-/13-digit ISBN.",
                file=sys.stderr,
            )
        return 2

    # Nothing is deleted first. The single-ISBN form writes into the same shared
    # directories as a batch, and those paths carry no ISBN, so clearing them would
    # destroy whatever *other* book was scraped there before. (An earlier version
    # did exactly that and ate a previous run's output; hence this note.)
    report = RunReport(base.metrics_dir)
    no_data = NoDataIndex(base.metrics_dir)
    try:
        return run_pipeline(replace(base, isbn=isbn13), report=report, no_data=no_data)
    except KeyboardInterrupt:
        print("warning: interrupted; partial output may already be on disk.", file=sys.stderr)
        no_data.flush()          # keep what was gathered before the interrupt
        report.write_all()
        return 1


def run_csv(raw_path: str, args: argparse.Namespace, base: PipelineConfig) -> int:
    """Read the CSV and scrape every ISBN in it. Returns the process exit code."""
    try:
        found = csv_input.read_isbns(raw_path, column=SETTINGS["isbn_column"])
    except csv_input.CsvInputError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if not found.entries:
        # An empty or wrong-column file is a usage error, not a scrape that found
        # nothing: exit 2 so a script can tell the two apart.
        print(f"error: no usable ISBN in {found.path}", file=sys.stderr)
        for problem in found.problems[:10]:
            print(f"  {problem}", file=sys.stderr)
        if len(found.problems) > 10:
            print(f"  ... and {len(found.problems) - 10} more", file=sys.stderr)
        return 2

    print(found.summarise(), file=sys.stderr)
    # A 10 000-row file can carry hundreds of bad rows; printing every one buries
    # the run. A few go to stderr, all of them go to the CSV in ./metrics.
    shown = 20 if SETTINGS["verbose"] else 5
    for problem in found.problems[:shown]:
        print(f"warning: {found.path.name}: {problem}", file=sys.stderr)
    if len(found.problems) > shown:
        print(
            f"warning: {found.path.name}: {len(found.problems) - shown} further row(s) "
            "were skipped; see the skipped-rows list in ./metrics",
            file=sys.stderr,
        )

    batch = BatchConfig(base=base, start=max(0, args.start), end=args.end)
    return run_batch(found, batch).exit_code()


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Parse arguments and run. Returns the process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)
    set_verbose(SETTINGS["verbose"])

    raw_input_value = args.input or prompt_for_input()
    if not raw_input_value:
        parser.error("no CSV file or ISBN supplied")  # exits 2

    base = build_config(args, parser)

    if csv_input.looks_like_csv_path(raw_input_value):
        return run_csv(raw_input_value, args, base)

    for flag, value in (("--start", args.start or None), ("--end", args.end)):
        if value is not None:
            print(f"warning: {flag} applies to a CSV input; ignoring it", file=sys.stderr)
    return run_single(raw_input_value, base)


if __name__ == "__main__":
    sys.exit(main())
