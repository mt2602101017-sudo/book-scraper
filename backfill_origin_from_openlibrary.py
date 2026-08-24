#!/usr/bin/env python
"""Copy Open Library's place of publication into the storefront metadata files.

Open Library carries ``publish_places`` for roughly half the corpus, and no
storefront publishes a place of publication at all -- so this fills a field that
would otherwise stay null. Read the warning below before using ``--apply``.

WHAT THIS BREAKS
----------------
The project's stated rule is that every value in ``<source>_metadata.json`` was
scraped from *that* source. Writing Open Library's value into
``amazon_metadata.json`` makes that false: a consumer reading the Amazon record
would reasonably believe Amazon published the origin, and Amazon does not.

It also contradicts the record's own warnings. An Amazon run emits, per book,
"origin ... is null: the Country of Origin detail bullet is absent ..." -- a
statement derived from the page it actually parsed. Filling the field afterwards
leaves that warning describing a state that no longer matches the data.

So this is a deliberate policy change, not a bug fix. Two safer shapes exist and
``--mode`` selects between them:

* ``sidecar`` (default) -- write ``origin_from_openlibrary.json``, a standalone
  ISBN -> place map with its provenance in the filename. Storefront records are
  left exactly as scraped. Nothing is misattributed.
* ``inplace`` -- what was asked for: set ``origin`` on the matching records in
  the named storefront files. Requires ``--apply``, backs each file up first.

Values are copied **verbatim**. Open Library spells the same city many ways
("New York", "New York, N.Y", "New York, NY", "New York, USA"); normalising them
would be an editorial guess, which is the same class of mistake as inferring a
place from the publisher's imprint.

    python backfill_origin_from_openlibrary.py                     # dry run, sidecar
    python backfill_origin_from_openlibrary.py --mode sidecar --apply
    python backfill_origin_from_openlibrary.py --mode inplace --apply
"""

from __future__ import annotations

import argparse
import collections
import json
import shutil
from pathlib import Path
from typing import Any, Dict, List

from bookscraper.storage import file_lock

DEFAULT_DIR = Path("data/book_metadata")
SOURCE = "openlibrary"


def load(path: Path) -> List[Dict[str, Any]]:
    with path.open(encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, list):
        raise ValueError(f"{path}: expected a JSON array")
    return data


def origins_from(path: Path) -> Dict[str, str]:
    """``{isbn13: place}`` for every Open Library record that has a place."""
    found: Dict[str, str] = {}
    for record in load(path):
        if not isinstance(record, dict):
            continue
        isbn13, origin = record.get("isbn13"), record.get("origin")
        if isbn13 and origin not in (None, "", "None"):
            found[str(isbn13)] = str(origin)
    return found


def rewrite(path: Path, records: List[Dict[str, Any]]) -> None:
    """Replace the whole array under the same lock a normal append would take.

    The metadata files are appended to in place by live runs, so a bulk rewrite
    has to hold the same cross-process lock or it can land in the middle of one.
    """
    with file_lock(path):
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(records, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        tmp.replace(path)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", type=Path, default=DEFAULT_DIR)
    ap.add_argument("--targets", default="amazon,goodreads",
                    help="comma-separated storefronts to fill (inplace mode)")
    ap.add_argument("--mode", choices=("sidecar", "inplace"), default="sidecar")
    ap.add_argument("--apply", action="store_true",
                    help="actually write; without it this is a dry run")
    ap.add_argument("--overwrite", action="store_true",
                    help="also replace origins that are already set (inplace)")
    args = ap.parse_args()

    ol_path = args.dir / f"{SOURCE}_metadata.json"
    if not ol_path.is_file():
        return print(f"missing {ol_path}") or 1

    origins = origins_from(ol_path)
    total = len(load(ol_path))
    print(f"{ol_path.name}: {total} records, {len(origins)} with an origin "
          f"({100.0 * len(origins) / total:.1f}%)")
    spellings = collections.Counter(origins.values())
    print(f"  {len(spellings)} distinct spellings; top 5: "
          f"{', '.join(f'{v!r}x{n}' for v, n in spellings.most_common(5))}")

    if args.mode == "sidecar":
        out = args.dir / "origin_from_openlibrary.json"
        payload = [{"isbn13": i, "origin": o, "origin_source": SOURCE}
                   for i, o in sorted(origins.items())]
        print(f"\nsidecar -> {out}: {len(payload)} entries, storefront files untouched")
        if args.apply:
            with out.open("w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2)
                fh.write("\n")
            print("  written.")
        else:
            print("  dry run; pass --apply to write.")
            print(f"  sample: {payload[:2]}")
        return 0

    print("\n*** inplace mode: storefront records will claim an origin they did "
          "not publish ***")
    for name in [t.strip() for t in args.targets.split(",") if t.strip()]:
        path = args.dir / f"{name}_metadata.json"
        if not path.is_file():
            print(f"\n{name}: no {path.name}, skipped")
            continue
        records = load(path)
        fill = keep = absent = 0
        for record in records:
            if not isinstance(record, dict):
                continue
            isbn13 = str(record.get("isbn13") or "")
            new = origins.get(isbn13)
            if new is None:
                absent += 1
            elif record.get("origin") in (None, "", "None") or args.overwrite:
                record["origin"] = new
                fill += 1
            else:
                keep += 1
        print(f"\n{path.name}: {len(records)} records")
        print(f"  would fill origin        : {fill}")
        print(f"  already set, left alone  : {keep}")
        print(f"  no Open Library origin   : {absent}")
        if args.apply:
            backup = path.with_suffix(path.suffix + ".bak")
            shutil.copy2(path, backup)
            rewrite(path, records)
            print(f"  written; backup at {backup.name}")
        else:
            print("  dry run; pass --apply to write.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
