#!/usr/bin/env python3
"""Command-line entry point for the book scraper.

    python main.py 2602101017.csv                     # every ISBN in the CSV
    python main.py 2602101017.csv --start 0 --end 20  # ...just the first 20
    python main.py 2602101017.csv --sources goodreads,amazon
    python main.py 9780143127550                      # one ISBN
    python main.py 0143127551                         # ISBN-10 is converted

The positional argument is a **CSV file or a single ISBN**; which one it is is
decided by whether it names a file on disk (or carries a ``.csv``/``.tsv``/
``.txt`` suffix), so neither form needs a flag.

Output goes under ``./data`` in one shared tree -- ``book_metadata/``,
``book_coverpage/``, ``book_blurb/``, ``book_reviews/``, ``genres/`` -- with each
source's metadata accumulating into one ``<source>_metadata.json`` array. The run
report and the resume state go under ``./metrics``.

Exit status: 0 if at least one source produced metadata, 1 if none did, 2 for bad
usage (a failed ISBN checksum, an unreadable CSV). Progress and warnings go to
stderr, so ``python main.py <input> 2>/dev/null`` leaves just the summary.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Optional, Sequence

from bookscraper import csv_input
from bookscraper import isbn as isbn_utils
from bookscraper.base import discover
from bookscraper.csv_input import Entry
from bookscraper.runner import Config, Runner

#: Everything that used to be a flag. These are settings, not choices: each value
#: is the one found to work while the adapters were built, so exposing it only
#: invited a run that differed from the tested one. The scraper grew 28 flags this
#: way; what is left below is the part that genuinely varies per invocation.
SETTINGS = {
    "out_dir": "data",         # the shared artefact tree
    "metrics_dir": "metrics",  # report + resume state; outside out_dir on purpose
    "min_reviews": 25,         # the target; a shortfall warns, never fails
    "max_reviews": None,       # no cap: take whatever a site will give
    "browser": "auto",         # Selenium only where a site needs it (Kobo, BookBub)
    "min_delay": 1.0,          # per-host courtesy delay, randomised in this range
    "max_delay": 2.0,
    "timeout": 25,
    "retries": 3,
    "covers": True,
    "skip_scraped": True,      # False re-fetches everything
}


def build_parser() -> argparse.ArgumentParser:
    """Three flags: which books, and which sites."""
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="Scrape book metadata, covers, blurbs, reviews and genres for "
                    "every ISBN in a CSV (or one ISBN given directly) into ./data.",
        epilog="Rows are a half-open range: --start 0 --end 100 is the first "
               "hundred, and the next shard starts at 100. Sources: "
               + ", ".join(discover()))
    parser.add_argument("input", nargs="?", metavar="CSV_OR_ISBN",
                        help="a CSV holding a column of ISBNs, or one ISBN-10/13")
    parser.add_argument("--start", type=int, default=0, metavar="N",
                        help="skip the first N ISBNs")
    parser.add_argument("--end", type=int, default=None, metavar="N",
                        help="stop before row N (half-open, so shards tile exactly)")
    parser.add_argument("--sources", default="all", metavar="LIST",
                        help="comma-separated source slugs, or 'all'")
    return parser


def parse_sources(raw: str, parser: argparse.ArgumentParser) -> Optional[List[str]]:
    """``--sources`` as a list of slugs, or ``None`` meaning all.

    ``None`` only for a genuinely empty value and the explicit ``all``/``*``. A
    non-empty value yielding no names (``','``) is a usage error: mapping it to
    "all" once scraped every live site when the user meant to narrow the run.
    """
    stripped = (raw or "").strip()
    if not stripped or stripped.lower() in ("all", "*"):
        return None
    names = [p.strip().lower() for p in stripped.replace(";", ",").split(",") if p.strip()]
    if not names:
        parser.error(f"--sources {raw!r} contains no source names. Pass a "
                     f"comma-separated list ({', '.join(discover())}), or 'all'.")
    return names


def config_for(args: argparse.Namespace, parser: argparse.ArgumentParser) -> Config:
    return Config(
        out_dir=Path(SETTINGS["out_dir"]),
        metrics_dir=Path(SETTINGS["metrics_dir"]),
        sources=parse_sources(args.sources, parser),
        min_reviews=SETTINGS["min_reviews"], max_reviews=SETTINGS["max_reviews"],
        min_delay=SETTINGS["min_delay"], max_delay=SETTINGS["max_delay"],
        timeout=SETTINGS["timeout"], retries=SETTINGS["retries"],
        browser=SETTINGS["browser"], covers=SETTINGS["covers"],
        skip_scraped=SETTINGS["skip_scraped"])


def entries_from_csv(raw: str, args: argparse.Namespace) -> List[Entry]:
    """The ISBNs to scrape, sliced by ``--start``/``--end``. Exits 2 if unusable."""
    # A missing, unreadable or ISBN-free file is a *usage* error, not a scrape that
    # found nothing, so it exits 2 and a script can tell the two apart.
    try:
        found = csv_input.read_isbns(raw)
    except csv_input.CsvError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    if not found.entries:
        print(f"error: no usable ISBN in {found.path}", file=sys.stderr)
        for row, reason in found.problems[:10]:
            print(f"  row {row}: {reason}", file=sys.stderr)
        raise SystemExit(2)

    print(found.summarise(), file=sys.stderr)
    for row, reason in found.problems[:5]:
        print(f"warning: {found.path.name} row {row}: {reason}", file=sys.stderr)
    if len(found.problems) > 5:
        print(f"warning: {len(found.problems) - 5} further row(s) were skipped",
              file=sys.stderr)

    start = max(0, args.start)
    end = len(found.entries) if args.end is None else min(args.end, len(found.entries))
    if start >= len(found.entries):
        print(f"warning: --start {start} skips past all {len(found.entries)} ISBN(s)",
              file=sys.stderr)
    return found.entries[start:max(start, end)]


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    raw = args.input
    if not raw:
        try:
            raw = input("Enter a CSV file of ISBNs, or a single ISBN: ").strip("'\" ")
        except (EOFError, KeyboardInterrupt):
            print()
            raw = ""
    if not raw:
        parser.error("no CSV file or ISBN supplied")  # exits 2

    if csv_input.looks_like_path(raw):
        entries = entries_from_csv(raw, args)
    else:
        for flag, value in (("--start", args.start), ("--end", args.end)):
            if value:
                print(f"warning: {flag} applies to a CSV input; ignoring it",
                      file=sys.stderr)
        try:
            entries = [Entry(isbn13=isbn_utils.to_isbn13(raw))]
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            if not any(ch.isdigit() for ch in raw):
                # A bare word with no digits is likelier a mistyped filename.
                print(f"hint: {raw!r} is neither a readable file nor an ISBN. Pass "
                      "a CSV path (e.g. 2602101017.csv) or a 10-/13-digit ISBN.",
                      file=sys.stderr)
            return 2

    return Runner(config_for(args, parser)).run(entries)


if __name__ == "__main__":
    sys.exit(main())
