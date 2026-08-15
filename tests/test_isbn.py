"""Checksum-level tests for :mod:`bookscraper.isbn`.

Runnable either way:

    .venv/bin/python -m pytest tests/test_isbn.py -q
    .venv/bin/python tests/test_isbn.py

Only ISBNs appear as literals here -- never a cached title, blurb or review.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bookscraper import isbn as I  # noqa: E402


def test_normalize_strips_punctuation_and_uppercases_x() -> None:
    assert I.normalize("978-0-14-312755-0") == "9780143127550"
    assert I.normalize("  0 143 127551 ") == "0143127551"
    assert I.normalize("isbn-10: 0-306-40615-2") == "0306406152"
    assert I.normalize("030640615x") == "030640615X"
    # En dash / em dash / non-breaking space, as pasted from a web page.
    assert I.normalize("978–0–14–312755–0") == "9780143127550"


def test_isbn10_validity() -> None:
    assert I.is_valid_isbn10("0143127551")
    assert I.is_valid_isbn10("0-14-312755-1")
    assert I.is_valid_isbn10("0062316095")
    assert I.is_valid_isbn10("0306406152")
    # A genuine X check digit.
    assert I.is_valid_isbn10("043942089X")
    assert not I.is_valid_isbn10("0143127550")   # wrong check digit
    assert not I.is_valid_isbn10("014312755")    # too short
    assert not I.is_valid_isbn10("9780143127550")
    assert not I.is_valid_isbn10("")


def test_isbn13_validity() -> None:
    assert I.is_valid_isbn13("9780143127550")
    assert I.is_valid_isbn13("978-0-14-312755-0")
    assert I.is_valid_isbn13("9780062316097")
    assert not I.is_valid_isbn13("9780143127551")   # wrong check digit
    assert not I.is_valid_isbn13("9770143127550")   # not a Bookland prefix
    assert not I.is_valid_isbn13("0143127551")      # that is an ISBN-10
    assert not I.is_valid_isbn13("")


def test_check_digit_helpers() -> None:
    assert I.isbn13_check_digit("978014312755") == "0"
    assert I.isbn13_check_digit("978006231609") == "7"
    assert I.isbn10_check_digit("014312755") == "1"
    assert I.isbn10_check_digit("006231609") == "5"
    assert I.isbn10_check_digit("043942089") == "X"


def test_round_trip_conversion() -> None:
    assert I.isbn10_to_isbn13("0143127551") == "9780143127550"
    assert I.isbn13_to_isbn10("9780143127550") == "0143127551"
    assert I.isbn13_to_isbn10("9780062316097") == "0062316095"
    assert I.isbn10_to_isbn13("0062316095") == "9780062316097"
    # 979-prefixed ISBN-13s have no ISBN-10 equivalent.
    assert I.isbn13_to_isbn10("9791234567896") is None
    assert I.isbn13_to_isbn10("nonsense") is None


def test_to_isbn13_accepts_both_forms() -> None:
    for form in ("9780143127550", "978-0-14-312755-0", "0143127551", "0-14-312755-1",
                 "ISBN-13: 978 0 14 312755 0"):
        assert I.to_isbn13(form) == "9780143127550"
    assert I.to_isbn13("9780062316097") == "9780062316097"
    assert I.to_isbn13("0062316095") == "9780062316097"


def _expect_value_error(raw: object, *, must_mention: str) -> str:
    try:
        I.to_isbn13(raw)  # type: ignore[arg-type]
    except ValueError as exc:
        message = str(exc)
        assert must_mention.lower() in message.lower(), (
            f"message for {raw!r} should mention {must_mention!r}, got: {message}"
        )
        assert len(message) > 20, f"message for {raw!r} is not helpful: {message}"
        return message
    raise AssertionError(f"to_isbn13({raw!r}) should have raised ValueError")


def test_invalid_inputs_raise_helpful_value_errors() -> None:
    _expect_value_error("9780143127551", must_mention="checksum")
    _expect_value_error("0143127550", must_mention="checksum")
    _expect_value_error("123", must_mention="10 or 13")
    _expect_value_error("", must_mention="No ISBN found")
    _expect_value_error(None, must_mention="No ISBN supplied")
    _expect_value_error("abcdefghij", must_mention="9 digits")
    _expect_value_error("9770143127550", must_mention="978")
    _expect_value_error(12345, must_mention="string")


def test_hyphenate_is_best_effort_and_never_raises() -> None:
    assert I.hyphenate("9780143127550") == "978-0-14-312755-0"
    assert I.hyphenate("9780062316097") == "978-0-06-231609-7"
    # Unknown/odd input degrades instead of raising.
    assert I.hyphenate("not-an-isbn") == "NOTANISBN"
    assert I.hyphenate("") == ""


def test_variants_are_ordered_and_deduped() -> None:
    got = I.variants("0143127551")
    assert got[0] == "9780143127550"
    assert "0143127551" in got
    assert "978-0-14-312755-0" in got
    assert len(got) == len(set(got))
    assert I.variants("nope") == []


def _run_all() -> int:
    failures = 0
    tests = [(name, fn) for name, fn in sorted(globals().items())
             if name.startswith("test_") and callable(fn)]
    for name, fn in tests:
        try:
            fn()
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
