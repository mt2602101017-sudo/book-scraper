"""Smoke tests that actually invoke ``main.py``, over no network.

These exist because of a real gap. Replacing the ``logging`` framework with plain
``print()`` calls left two orphaned ``log`` names -- one in ``configure_output``,
one in the CSV branch of ``main()``. Both were ``NameError`` at *runtime*, both
were on the two commonest paths (a single ISBN, and a CSV), and **the whole 177-test
suite passed anyway**, because every test called library functions directly and
none ever ran ``main()``.

So this file checks the entry points a marker will actually type, and asserts the
conventions that are easy to break silently:

* a usage error is a message and exit 2, never a traceback;
* progress and warnings go to **stderr**, so ``2>/dev/null`` leaves a clean report;
* the CLI carries exactly the three documented flags -- a fourth one creeping back
  in means a setting escaped ``SETTINGS`` again.

Every case here fails *before* any fetch (a bad ISBN, a missing file, ``--help``),
so no socket is opened and no artefact is written.

Runnable either way:

    .venv/bin/python -m pytest tests/test_cli_smoke.py -q
    .venv/bin/python tests/test_cli_smoke.py
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

#: The flags the CLI is documented to have, and the only ones it may have.
_EXPECTED_FLAGS = {"--help", "--start", "--end", "--sources"}


def _run(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    """Invoke ``main.py`` in a subprocess, exactly as a user would."""
    return subprocess.run(
        [sys.executable, str(_ROOT / "main.py"), *args],
        capture_output=True, text=True, timeout=120, cwd=str(cwd or _ROOT),
    )


def _assert_ran(proc: subprocess.CompletedProcess, what: str) -> None:
    """Fail loudly on a traceback, whatever the exit code says."""
    combined = proc.stdout + proc.stderr
    assert "Traceback" not in combined, f"{what} raised:\n{combined[-1500:]}"
    for bad in ("NameError", "AttributeError", "TypeError", "ImportError"):
        assert bad not in combined, f"{what} hit {bad}:\n{combined[-1500:]}"


# -- the entry points ----------------------------------------------------------


def test_help_runs(tmp_path: Path) -> None:
    proc = _run("--help")
    _assert_ran(proc, "--help")
    assert proc.returncode == 0
    assert "--sources" in proc.stdout


def test_help_names_every_source(tmp_path: Path) -> None:
    """``--list-sources`` is gone, so ``--help`` has to carry the slugs instead.

    Discovery is dynamic, so this asks the package what it found rather than
    hard-coding six names -- a seventh adapter must not fail this test, but an
    adapter missing from the epilog must.
    """
    from bookscraper.sources import discover_sources

    proc = _run("--help")
    for slug in discover_sources():
        assert slug in proc.stdout, f"--help never mentions the {slug!r} source"


def test_the_cli_has_exactly_three_flags(tmp_path: Path) -> None:
    """Guards the reduction itself: 28 flags became 3, and must stay 3.

    Every removed flag became an entry in ``SETTINGS``, so a new ``--foo`` here
    means a setting was re-exposed and a run can once again differ from the tested
    one. ``--help`` is argparse's own and does not count against the three.
    """
    import re

    proc = _run("--help")
    found = set(re.findall(r"--[a-z][a-z-]+", proc.stdout))
    assert found == _EXPECTED_FLAGS, f"CLI flags drifted: {sorted(found)}"


def test_the_single_isbn_path_runs(tmp_path: Path) -> None:
    """Regression: ``configure_output`` NameError'd before any scraping began.

    A non-numeric argument is neither a file nor an ISBN, so it is rejected at
    exit 2 -- which is *after* the output setup, so this still exercises the code
    that used to blow up, without opening a socket.
    """
    proc = _run("nonsense-not-an-isbn")
    _assert_ran(proc, "a single ISBN")
    assert proc.returncode == 2, "expected the documented usage error"
    assert "hint:" in proc.stderr, "a bare word should suggest it may be a filename"


def test_a_bad_isbn_still_exits_two_cleanly(tmp_path: Path) -> None:
    """A usage error must be a message, never a traceback."""
    proc = _run("9780143127551")  # last digit wrong: fails the checksum
    _assert_ran(proc, "a bad checksum")
    assert proc.returncode == 2
    assert "error:" in proc.stderr.lower()


def test_a_missing_csv_exits_two_cleanly(tmp_path: Path) -> None:
    proc = _run(str(tmp_path / "nope.csv"))
    _assert_ran(proc, "a missing CSV")
    assert proc.returncode == 2


def test_an_empty_csv_exits_two_cleanly(tmp_path: Path) -> None:
    """A wrong-column or empty file is bad usage, not a scrape that found nothing."""
    path = tmp_path / "books.csv"
    path.write_text("Title,Author\nDune,Herbert\n", encoding="utf-8")
    proc = _run(str(path))
    _assert_ran(proc, "a CSV with no ISBN column")
    assert proc.returncode == 2
    assert "no usable ISBN" in proc.stderr


# -- the output conventions ----------------------------------------------------


def test_errors_go_to_stderr_not_stdout(tmp_path: Path) -> None:
    """``2>/dev/null`` must leave a clean report, so diagnostics belong on stderr."""
    proc = _run("9780143127551")
    _assert_ran(proc, "stream split check")
    assert "error:" in proc.stderr.lower()
    assert proc.stdout.strip() == "", f"diagnostics leaked onto stdout: {proc.stdout!r}"


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
