"""Two processes, one output directory: does anything get lost?

These spawn **real subprocesses**, because a lock inside one process cannot help
here at all: the two writers are two separate ``main.py`` runs. This is the only
concurrency the project still has -- books within a run are scraped one at a time --
so it is the only place a lock is needed.

The motivating failure: two ``main.py`` runs in two terminals sharing one
``--out``. ``append_metadata`` is a read-modify-write (find the trailing ``]``,
truncate, write), so both processes read the same tail offset and each truncated
away the other's record. **40 of 80 records vanished and the file still parsed** --
no crash, no warning, just half the data gone. An :func:`fcntl.flock` now serialises
the whole sequence across processes.

Also covered: report filenames being kept apart per source selection, so the second
process does not overwrite the first's manifest.

The ledger tests that used to live here went with ``scrape_ledger.jsonl``: the skip
decision now reads the metadata file, and that file's append is exactly what the
first three tests below already cover.

Runnable either way:

    .venv/bin/python -m pytest tests/test_multiprocess.py -q
    .venv/bin/python tests/test_multiprocess.py
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import List

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_ROOT = str(Path(__file__).resolve().parent.parent)


def _run_parallel(script: str, args_list: List[List[str]]) -> None:
    """Run ``script`` once per argument list, all concurrently, and wait."""
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as handle:
        handle.write(script)
        path = handle.name
    try:
        processes = [
            subprocess.Popen(
                [sys.executable, path, *args],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            for args in args_list
        ]
        for process in processes:
            _out, err = process.communicate(timeout=120)
            assert process.returncode == 0, (
                f"worker failed ({process.returncode}): {err.decode()[-800:]}"
            )
    finally:
        Path(path).unlink(missing_ok=True)


_APPEND_WORKER = f"""
import sys
sys.path.insert(0, {_ROOT!r})
from pathlib import Path
from bookscraper.storage import Storage
root, source, tag, count = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
storage = Storage(Path(root))
storage.ensure_dirs()
for index in range(count):
    storage.append_metadata(source, {{
        "isbn13": tag + "-" + str(index).zfill(3),
        # A payload big enough that a naive append interleaves rather than fitting
        # inside one atomic write.
        "blurb": "y" * 300,
    }})
"""


def _records(root: Path, source: str) -> List[dict]:
    with open(root / "book_metadata" / f"{source}_metadata.json", encoding="utf-8") as h:
        return json.load(h)


def test_two_processes_appending_the_same_source_lose_nothing(tmp_path: Path) -> None:
    """The regression this module exists for: 40 of 80 records used to vanish."""
    per_process = 40
    _run_parallel(_APPEND_WORKER, [
        [str(tmp_path), "goodreads", "A", str(per_process)],
        [str(tmp_path), "goodreads", "B", str(per_process)],
    ])

    records = _records(tmp_path, "goodreads")
    isbns = {r["isbn13"] for r in records}
    from_a = sum(1 for i in isbns if i.startswith("A"))
    from_b = sum(1 for i in isbns if i.startswith("B"))

    assert len(records) == per_process * 2, (
        f"expected {per_process * 2} records, found {len(records)} -- one process "
        "truncated away the other's writes"
    )
    assert from_a == per_process and from_b == per_process, (
        f"lopsided result (A={from_a}, B={from_b}): writes were lost, not interleaved"
    )


def test_two_processes_on_different_sources_are_independent(tmp_path: Path) -> None:
    _run_parallel(_APPEND_WORKER, [
        [str(tmp_path), "goodreads", "A", "30"],
        [str(tmp_path), "amazon", "B", "30"],
    ])
    assert len(_records(tmp_path, "goodreads")) == 30
    assert len(_records(tmp_path, "amazon")) == 30


def test_the_file_stays_parseable_throughout(tmp_path: Path) -> None:
    """Never a half-written array, even under contention from four processes."""
    _run_parallel(_APPEND_WORKER, [
        [str(tmp_path), "kobo", tag, "15"] for tag in ("A", "B", "C", "D")
    ])
    records = _records(tmp_path, "kobo")      # raises if malformed
    assert len(records) == 60
    assert len({r["isbn13"] for r in records}) == 60


# -- report filenames ----------------------------------------------------------


def test_reports_for_different_sources_do_not_collide(tmp_path: Path) -> None:
    """One directory serves every run, because the filename carries the source.

    This replaced a ``metrics.<sources>/`` directory suffix: the report used to bundle
    all five sources into one ``isbns_by_source.txt``, so a ``--sources kobo`` run
    would overwrite a ``--sources goodreads`` one. Per-source filenames remove the
    collision by construction.
    """
    from bookscraper.metrics import BookRecord, RunReport

    first = RunReport(tmp_path)
    first.record_book(BookRecord(isbn13="9780143127550", source="goodreads",
                                 status="partial", has_metadata=True))
    wrote_first = first.write_all()

    second = RunReport(tmp_path)
    second.record_book(BookRecord(isbn13="9780143127550", source="kobo",
                                  status="empty", requests_made=2))
    wrote_second = second.write_all()

    assert [p.name for p in wrote_first] == ["goodreads_isbns.txt"]
    assert [p.name for p in wrote_second] == ["kobo_isbns.txt"]
    assert wrote_first[0].parent == wrote_second[0].parent == tmp_path
    # The first run's report is still intact after the second run wrote its own.
    assert "9780143127550" in wrote_first[0].read_text(encoding="utf-8")
    assert "SUCCEEDED (1)" in wrote_first[0].read_text(encoding="utf-8")
    assert "FAILED (1)" in wrote_second[0].read_text(encoding="utf-8")


def _run_all() -> int:
    failures = 0
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    for name, fn in tests:
        with tempfile.TemporaryDirectory() as directory:
            try:
                fn(Path(directory))
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc}")
            except Exception as exc:  # noqa: BLE001 - report, do not mask
                failures += 1
                print(f"ERROR {name}: {type(exc).__name__}: {exc}")
            else:
                print(f"ok   {name}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(_run_all())
