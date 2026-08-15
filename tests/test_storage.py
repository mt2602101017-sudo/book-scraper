"""The output tree: filenames, the accumulating metadata array, absence lists."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bookscraper.metadata import release_caches  # noqa: E402
from bookscraper.ledger import NoData, Pending  # noqa: E402
from bookscraper.storage import Storage, image_ext, sanitise  # noqa: E402


# -- storage ----------------------------------------------------------------

def test_filenames_match_the_mandated_scheme(tmp_path: Path) -> None:
    store = Storage(tmp_path)
    store.ensure_dirs()
    assert store.cover("9780143127550", "goodreads", 1, b"\xff\xd8\xff junk").name \
        == "9780143127550_cp_goodreads_1.jpg"
    assert store.blurb("9780143127550", "goodreads", "x").name \
        == "9780143127550_b_goodreads_1.txt"
    assert store.review("9780143127550", "goodreads", 3, "x").name \
        == "9780143127550_r_goodreads_3.txt"
    genres = store.genres("9780143127550", "goodreads", ["Fiction", "Classics"])
    assert genres.name == "9780143127550_g_goodreads_1.txt"
    assert genres.read_text() == "Fiction\nClassics\n"
    assert store.meta.path_for("goodreads").name == "goodreads_metadata.json"


def test_metadata_accumulates_and_stays_valid_after_every_append(tmp_path: Path) -> None:
    release_caches()
    store = Storage(tmp_path)
    for index, value in enumerate(["9780143127550", "9780062316097", "9780316769488"]):
        store.meta.append("goodreads", {"isbn13": value, "title": f"T{index}"})
        # json.load must work mid-run, after each append.
        assert len(json.load(open(store.meta.path_for("goodreads")))) == index + 1


def test_rescraping_replaces_a_record_rather_than_duplicating_it(tmp_path: Path) -> None:
    release_caches()
    store = Storage(tmp_path)
    store.meta.append("amazon", {"isbn13": "9780143127550", "title": "first"})
    store.meta.append("amazon", {"isbn13": "9780062316097", "title": "other"})
    store.meta.append("amazon", {"isbn13": "9780143127550", "title": "second"})
    records = json.load(open(store.meta.path_for("amazon")))
    assert len(records) == 2
    assert [r for r in records if r["isbn13"] == "9780143127550"][0]["title"] == "second"


def test_the_skip_index_reads_what_is_on_disk(tmp_path: Path) -> None:
    release_caches()
    store = Storage(tmp_path)
    store.meta.append("kobo", {"isbn13": "9780143127550", "title": "T"})
    release_caches()
    assert store.meta.scraped("kobo") == {"9780143127550"}
    assert store.meta.record("kobo", "9780143127550")["title"] == "T"
    assert store.meta.scraped("audible") == set()


def test_purge_removes_only_this_book_and_source(tmp_path: Path) -> None:
    store = Storage(tmp_path)
    store.ensure_dirs()
    store.review("9780143127550", "goodreads", 1, "a")
    store.review("9780143127550", "goodreads", 2, "b")
    store.review("9780143127550", "amazon", 1, "c")
    store.review("9780062316097", "goodreads", 1, "d")
    assert store.purge("reviews", "9780143127550", "goodreads") == 2
    remaining = sorted(p.name for p in store.dir_for("reviews").iterdir())
    assert remaining == ["9780062316097_r_goodreads_1.txt",
                         "9780143127550_r_amazon_1.txt"]


def test_a_lone_surrogate_does_not_take_the_run_down(tmp_path: Path) -> None:
    store = Storage(tmp_path)
    store.ensure_dirs()
    written = store.review("9780143127550", "goodreads", 1, "emoji \ud83d truncated")
    assert "�" in written.read_text(encoding="utf-8")


def test_sanitise_cannot_escape_the_output_root() -> None:
    for hostile in ("../../etc/passwd", "..", ".", "", "a/b\\c"):
        cleaned = sanitise(hostile, "safe")
        assert "/" not in cleaned and "\\" not in cleaned
        assert cleaned not in ("", ".", "..")


def test_image_extension_comes_from_the_bytes_first() -> None:
    assert image_ext(b"\x89PNG\r\n\x1a\n", "image/jpeg", "x.jpg") == "png"
    assert image_ext(b"", "image/webp", None) == "webp"
    assert image_ext(b"", None, "https://x/y.PNG?v=2") == "png"
    assert image_ext(b"", None, None) == "jpg"


def test_no_data_merges_rather_than_overwrites(tmp_path: Path) -> None:
    NoData(tmp_path).path_for("kobo").write_text("9780143127550\n", encoding="utf-8")
    index = NoData(tmp_path)
    index.note("kobo", "9780062316097")
    index.flush()
    # A --end 100 slice must not truncate entries it never looked at.
    on_disk = set((tmp_path / "kobo_no_data.txt").read_text().split())
    assert on_disk == {"9780143127550", "9780062316097"}
    assert NoData(tmp_path).contains("kobo", "9780143127550") is True
    assert NoData(tmp_path).contains("kobo", "9780316769488") is False




def test_the_pending_list_merges_and_removes_across_runs(tmp_path: Path) -> None:
    """Durable state: a --end 100 slice must not truncate what it never looked at."""
    first = Pending(tmp_path)
    first.note("kobo", "9780143127550")
    first.note("kobo", "9780062316097")
    first.flush()

    second = Pending(tmp_path)
    assert second.contains("kobo", "9780143127550") is True
    second.discard("kobo", "9780143127550")     # this one got finished
    second.note("kobo", "9780316769488")        # this one just failed
    second.flush()

    on_disk = set((tmp_path / "kobo_incomplete.txt").read_text().split())
    assert on_disk == {"9780062316097", "9780316769488"}
    # A discard takes effect immediately, before any flush.
    assert second.contains("kobo", "9780143127550") is False


def test_the_two_ledgers_do_not_share_a_file(tmp_path: Path) -> None:
    NoData(tmp_path).path_for("amazon").write_text("1\n", encoding="utf-8")
    assert Pending(tmp_path).path_for("amazon").name == "amazon_incomplete.txt"
    assert NoData(tmp_path).path_for("amazon").name == "amazon_no_data.txt"
    assert Pending(tmp_path).contains("amazon", "1") is False
