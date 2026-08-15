"""Parsing, structured extraction, identity matching, covers and the origin probe."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bookscraper import extract, match, origin, parse  # noqa: E402
from bookscraper.covers import Covers  # noqa: E402
from bookscraper.languages import name_of  # noqa: E402
from bookscraper.models import Book, Review  # noqa: E402


# -- parsing ----------------------------------------------------------------

def test_text_collapses_whitespace_but_keeps_paragraphs() -> None:
    assert parse.text("  a &amp;  b \r\n\n\n c ") == "a & b\n\nc"
    assert parse.text(None) == ""


def test_html_text_turns_breaks_and_blocks_into_line_breaks() -> None:
    body = parse.html_text("<p>one</p><p>two<br>three</p><script>x()</script>")
    assert "one" in body and "two" in body and "three" in body
    assert "x()" not in body


@pytest.mark.parametrize("raw,expected", [
    ("January 5, 2016", "2016-01-05"), ("June 2016", "2016-06"), ("2016", "2016"),
    ("Published: 5th January 2016", "2016-01-05"), ("2016-01-05", "2016-01-05"),
])
def test_iso_date_normalises_what_it_can(raw: str, expected: str) -> None:
    assert extract.iso_date(raw) == expected


def test_iso_date_keeps_unparseable_text_rather_than_losing_it() -> None:
    assert extract.iso_date("sometime soon") == "sometime soon"
    assert extract.iso_date("") is None


def test_split_list_and_dedupe() -> None:
    assert extract.split_list("Fiction, Fantasy & Young Adult") == \
        ["Fiction", "Fantasy", "Young Adult"]
    assert extract.split_list([{"name": "Fiction"}, "fiction "]) == ["Fiction"]
    assert parse.dedupe(["Fiction", " fiction ", "Classics"]) == ["Fiction", "Classics"]


# -- identity matching ------------------------------------------------------

def test_sequel_titles_are_vetoed_and_editions_are_not() -> None:
    assert match.is_sequel("Ready Player One", "Ready Player Two") is True
    assert match.is_sequel("Dune", "Dune Messiah") is False
    assert match.is_sequel("Educated", "Educated") is False
    assert match.title_score("Ready Player One", "Ready Player Two") == 0.0


def test_authors_agree_tolerates_initials_and_accents() -> None:
    assert match.authors_agree(["Celeste Ng"], ["celeste ng"]) is True
    assert match.authors_agree(["Charlotte Brontë"], ["Charlotte Bronte"]) is True
    assert match.authors_agree(["J.R.R. Tolkien"], ["John Ronald Reuel Tolkien"]) is True
    assert match.authors_agree(["Celeste Ng"], ["Ajay K Pandey"]) is False
    # Nothing on either side means nothing to disagree about.
    assert match.authors_agree([], ["Anyone"]) is True


def test_a_two_letter_surname_is_still_matchable() -> None:
    """Taking the last token blindly yields "ng", which made Celeste Ng unmatchable."""
    assert match.surname("Celeste Ng") == "celeste"
    assert match.surname("Charlotte Brontë") == "bronte"


def test_containment_is_asymmetric_so_a_sequel_never_looks_confident() -> None:
    # Dropping a subtitle is the same book...
    assert match.title_score("Educated: A Memoir", "Educated") >= 0.85
    # ...but adding words is probably the sequel, and must stay capped.
    assert match.title_score("Dune", "Dune Messiah", additive_ceiling=0.80) <= 0.80
    assert match.title_score("Everything I Never Told You", "Everything I Never Told You") == 1.0


def test_derivative_works_are_rejected_but_not_real_titles() -> None:
    assert match.is_derivative("Study Guide: Educated by Tara Westover") is True
    assert match.is_derivative("Summary of Sapiens") is True
    assert match.is_derivative("Dune (Boxed Set)") is True
    assert match.is_derivative("Educated: A Memoir") is False
    # A marker already in what we asked for must not reject the real book.
    assert match.is_derivative("A Guide to Birdsong", "A Guide to Birdsong") is False


def test_best_ranks_and_breaks_ties_towards_the_shorter_title() -> None:
    ranked = match.best("Sapiens", ["Yuval Noah Harari"], [
        ("Sapiens: A Graphic History", ["Yuval Noah Harari"], "graphic"),
        ("Sapiens", ["Yuval Noah Harari"], "book"),
        ("Summary of Sapiens", ["Some Hack"], "junk"),
    ])
    assert [payload for _, payload in ranked][0] == "book"
    assert "junk" not in [payload for _, payload in ranked]


def test_an_author_disagreement_rejects_a_perfect_title() -> None:
    """Audible carries another author's book under this exact title."""
    ranked = match.best("Everything I Never Told You", ["Celeste Ng"], [
        ("Everything I Never Told You", ["Ajay K Pandey"], "imposter"),
        ("Everything I Never Told You", ["Celeste Ng"], "real"),
    ])
    assert [payload for _, payload in ranked] == ["real"]


# -- covers and languages ---------------------------------------------------

def test_covers_dedupe_by_artwork_identity_not_by_url() -> None:
    found = Covers("https://x/", key=lambda u: u.rsplit("/", 1)[-1].split(".")[0],
                   limit=3)
    assert found.add("https://cdn/a._SL500_.jpg") is True
    assert found.add("https://other-cdn/a._SL75_.jpg") is False   # same artwork
    assert found.add("https://cdn/b.jpg") is True
    assert found.add("https://cdn/no-cover.jpg") is False         # a placeholder
    assert len(found) == 2


def test_covers_respect_their_limit_and_upgrade_urls() -> None:
    found = Covers("https://x/", upgrade=lambda u: u.replace("_small", "_full"), limit=1)
    found.extend(["https://cdn/a_small.jpg", "https://cdn/b.jpg"])
    assert found.urls == ["https://cdn/a_full.jpg"]
    assert found.full is True


@pytest.mark.parametrize("raw,expected", [
    ("en", "English"), ("en-GB", "English"), ("eng", "English"),
    ("fre", "French"), ("ger", "German"), ("chi", "Chinese"), ("dut", "Dutch"),
    ("english", "English"), ("Portuguese", "Portuguese"), ("xyz", "xyz"),
])
def test_language_codes_become_names_in_both_code_families(raw: str,
                                                           expected: str) -> None:
    assert name_of(raw) == expected


def test_no_language_is_none_not_a_guess() -> None:
    assert name_of("") is None
    assert name_of(None) is None


def test_jsonld_flattens_graphs_and_filters_by_type() -> None:
    html = ('<script type="application/ld+json">'
            '{"@graph":[{"@type":"Book","name":"B"},{"@type":"Person","name":"P"}]}'
            "</script>")
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    assert [b["name"] for b in extract.jsonld(soup, "Book")] == ["B"]
    assert len(extract.jsonld(soup)) == 3   # the wrapper plus both members


# -- origin -----------------------------------------------------------------

def test_a_planted_place_of_publication_is_found_with_its_layer() -> None:
    found, searched = origin.probe([("json", {"work": {"publish_places": ["London"]}})])
    assert found == "London"
    assert searched == ["json"]


def test_licensing_and_locale_fields_are_never_read_as_an_origin() -> None:
    for key in ("eligibleRegion", "ineligibleRegion", "regionsAllowed",
                "priceCurrency", "datePublished", "publisher"):
        found, _ = origin.probe([("json", {key: "United States"})])
        assert found is None, key


def test_a_details_table_label_is_read() -> None:
    from bs4 import BeautifulSoup
    soup = BeautifulSoup("<table><tr><th>Country of Origin ‏ : ‎</th>"
                         "<td>United Kingdom</td></tr></table>", "html.parser")
    found, _ = origin.probe([("dom", soup)])
    assert found == "United Kingdom"


def test_absent_layers_are_not_claimed_as_searched() -> None:
    found, searched = origin.probe([("empty", {}), ("none", None), ("real", {"a": 1})])
    assert found is None
    assert searched == ["real"]
    assert "no parsed layer" in origin.clause([])


def test_a_cyclic_blob_terminates_and_is_still_searched() -> None:
    node: dict = {"placeOfPublication": "Paris"}
    node["self"] = node
    found, _ = origin.probe([("json", node)])
    assert found == "Paris"


# -- models -----------------------------------------------------------------

def test_the_metadata_contract_is_exactly_eight_keys_in_order() -> None:
    payload = Book(isbn13="9780143127550", title="T", authors=["A"],
                   genres=["Fiction", "Classics"]).to_json()
    assert list(payload) == ["isbn13", "title", "authors", "publisher", "origin",
                            "date_of_publication", "language", "genre"]
    assert payload["genre"] == "Fiction, Classics"
    assert payload["publisher"] is None      # null, never omitted, never "None"


def test_a_review_renders_only_the_headers_it_has() -> None:
    assert Review(text="body").to_block() == "body"
    rendered = Review(text="body", reviewer="R", rating="5/5").to_block()
    assert rendered == "Reviewer: R\nRating: 5/5\n\nbody"
