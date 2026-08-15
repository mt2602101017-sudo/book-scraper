#!/usr/bin/env python3
"""Seed ``metrics/<source>_no_data.txt`` from a retired ``scrape_ledger.jsonl``.

The ledger recorded one JSON line per (ISBN, source) attempt, later line winning.
A line whose status is ``empty`` means the site was reached, answered, and had no
such book -- and it means that *reliably*, because the ledger writer already
downgraded an untrustworthy empty to ``blocked`` before writing it. So the pairs
worth carrying forward are exactly the winning ``empty`` lines, and nothing here has
to re-derive trustworthiness.

``ok`` / ``partial`` lines are deliberately **not** carried over: those pairs have a
real record in ``book_metadata/<source>_metadata.json``, which is where the skip
decision already finds them. ``blocked`` and ``error`` are not carried over either --
neither told us anything about the book.

    python tools/backfill_no_data.py data/_retired_reports/scrape_ledger.jsonl metrics
    python tools/backfill_no_data.py <ledger> <metrics-dir> --dry-run

Merges with whatever is already in the lists; it never truncates.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bookscraper.metrics import NoDataIndex  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("ledger", type=Path, help="path to scrape_ledger.jsonl")
    parser.add_argument("metrics_dir", type=Path,
                        help="the --metrics-dir of the runs (default: ./metrics)")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would be written, write nothing")
    args = parser.parse_args(argv)

    if not args.ledger.is_file():
        print(f"error: {args.ledger} does not exist", file=sys.stderr)
        return 2

    winning: dict = {}
    lines = bad = 0
    with open(args.ledger, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            lines += 1
            try:
                record = json.loads(line)
                key = (str(record["isbn13"]).strip(),
                       str(record["source"]).strip().lower())
                winning[key] = str(record["status"]).strip().lower()
            except (ValueError, KeyError, TypeError):
                bad += 1

    absent = sorted(k for k, status in winning.items() if status == "empty")
    per_source = Counter(source for _, source in absent)
    statuses = Counter(winning.values())

    print(f"{args.ledger}: {lines} line(s), {len(winning)} distinct pair(s)"
          + (f", {bad} unreadable" if bad else ""))
    print(f"  statuses: {dict(statuses.most_common())}")
    print()
    print(f"{'source':<12}{'no-such-book':>14}")
    for source, count in sorted(per_source.items()):
        print(f"{source:<12}{count:>14}")
    print(f"{'TOTAL':<12}{len(absent):>14}")

    if args.dry_run:
        print("\n--dry-run: nothing written.")
        return 0

    index = NoDataIndex(args.metrics_dir)
    before = {source: index.count(source) for source in per_source}
    for isbn13, source in absent:
        index.note(source, isbn13)
    written = index.flush()

    print()
    for path in written:
        source = path.name[: -len("_no_data.txt")]
        print(f"  {path}  {before.get(source, 0)} -> {index.count(source)}")
    if not written:
        print("  nothing new to write (the lists already hold all of it)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
