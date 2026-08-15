#!/usr/bin/env python3
"""Strip the diagnostic keys from metadata JSONs written by an earlier version.

    python tools/strip_metadata_keys.py data              # rewrite in place
    python tools/strip_metadata_keys.py data --dry-run    # report only

``BookMetadata.to_json_dict`` now emits just the eight fields the assignment asks
for. Files written before that change also carry ``genres``, ``_source``,
``_source_url``, ``_scraped_at``, ``_edition_isbn13``,
``_edition_matches_requested`` and ``_warnings``; this brings them into line so a
directory is not half one shape and half the other.

Safety
------
* Every file is **backed up** to ``<name>.pre-strip.json`` before being touched,
  unless that backup already exists (so re-running cannot overwrite the original).
* A file is rewritten only if stripping actually changes it, and only after the new
  content has been re-parsed successfully.
* The key **order** of the surviving fields is preserved, and no value is altered.
* Anything unreadable is reported and skipped, never rewritten.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

#: Keys removed from the on-disk contract.
DROPPED: Tuple[str, ...] = (
    "genres",
    "_source",
    "_source_url",
    "_scraped_at",
    "_edition_isbn13",
    "_edition_matches_requested",
    "_warnings",
)

#: The keys that remain, in the order they are written.
KEPT: Tuple[str, ...] = (
    "isbn13",
    "title",
    "authors",
    "publisher",
    "origin",
    "date_of_publication",
    "language",
    "genre",
)


def strip_record(record: Dict[str, Any]) -> Dict[str, Any]:
    """Return ``record`` without the dropped keys, in the canonical order.

    Keys that are neither kept nor dropped are preserved at the end rather than
    silently discarded -- this script's job is to remove a known list, not to
    enforce a schema on data it does not recognise.
    """
    out: Dict[str, Any] = {key: record[key] for key in KEPT if key in record}
    for key, value in record.items():
        if key not in KEPT and key not in DROPPED:
            out[key] = value
    return out


def process(path: Path, dry_run: bool) -> Tuple[str, int]:
    """Strip one file. Returns ``(outcome, records_touched)``."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as exc:
        return f"unreadable ({exc})", 0

    records = data if isinstance(data, list) else [data]
    if not all(isinstance(r, dict) for r in records):
        return "unexpected shape; skipped", 0

    stripped = [strip_record(r) for r in records]
    touched = sum(
        1 for before, after in zip(records, stripped) if before.keys() != after.keys()
    )
    if not touched:
        return "already clean", 0
    if dry_run:
        return f"would strip {touched} record(s)", touched

    backup = path.with_name(f"{path.stem}.pre-strip{path.suffix}")
    if not backup.exists():
        try:
            backup.write_text(
                path.read_text(encoding="utf-8"), encoding="utf-8", newline="\n"
            )
        except OSError as exc:
            return f"could not write backup ({exc}); left untouched", 0

    payload = stripped if isinstance(data, list) else stripped[0]
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    # Re-parse before committing: never replace good data with something unreadable.
    try:
        json.loads(text)
    except ValueError as exc:  # pragma: no cover - defensive
        return f"refused to write unparseable output ({exc})", 0
    try:
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
    except OSError as exc:
        return f"could not write ({exc})", 0
    return f"stripped {touched} record(s), backup at {backup.name}", touched


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("root", nargs="?", default="data",
                        help="output root holding book_metadata/ (default: data)")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would change and write nothing")
    args = parser.parse_args(argv)

    directory = Path(args.root) / "book_metadata"
    if not directory.is_dir():
        print(f"error: {directory} is not a directory", file=sys.stderr)
        return 2

    files = sorted(
        p for p in directory.glob("*_metadata.json")
        if not p.stem.endswith(".pre-strip")
    )
    if not files:
        print(f"No metadata files under {directory}")
        return 0

    print(f"{'dry run: ' if args.dry_run else ''}{len(files)} file(s) in {directory}")
    print(f"dropping: {', '.join(DROPPED)}\n")
    total = 0
    for path in files:
        outcome, touched = process(path, args.dry_run)
        total += touched
        print(f"  {path.name:34} {outcome}")
    print(f"\n{total} record(s) {'would be' if args.dry_run else ''} updated")
    return 0


if __name__ == "__main__":
    sys.exit(main())
