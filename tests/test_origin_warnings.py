"""Every ``origin`` warning must be produced by a search that really happened.

These are offline tests: each adapter's origin method is called with synthetic
layers, so they pin the *shape of the claim* rather than any live page content.
What they enforce:

* a layer is only named as searched when it was actually there to search;
* nothing about live page content is hand-counted (no "47-entry" literals);
* Kobo's geo-licensing rejection reports the path and count it measured;
* Amazon consults **both** detail layouts, and merging changes no other field;
* a planted place of publication makes the field self-heal on all five sources.

Runnable either way:

    .venv/bin/python -m pytest tests/test_origin_warnings.py -q
    .venv/bin/python tests/test_origin_warnings.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bs4 import BeautifulSoup  # noqa: E402

from bookscraper.models import ScrapeResult  # noqa: E402
from bookscraper.sources.amazon import AmazonSource, _Page  # noqa: E402
from bookscraper.sources.audible import AudibleSource  # noqa: E402
from bookscraper.sources.bookbub import BookBubSource, _Record  # noqa: E402
from bookscraper.sources.goodreads import GoodreadsSource, _BookPage  # noqa: E402
from bookscraper.sources.kobo import KoboSource  # noqa: E402

_ISBN = "9780143127550"


def _result(source: str) -> ScrapeResult:
    return ScrapeResult(source=source, isbn13=_ISBN)


def _soup(markup: str) -> BeautifulSoup:
    return BeautifulSoup(markup, "html.parser")


def _origin_warning(result: ScrapeResult) -> str:
    hits = [w for w in result.warnings if w.startswith("origin")]
    assert hits, f"no origin warning in {result.warnings!r}"
    return hits[0]


# The Kobo shape verified live: geo-licensing on a priced Offer inside a
# checkout ReadAction. The list length here is arbitrary test data -- the point
# is that the adapter *counts* it rather than remembering a number.
_INELIGIBLE = ["AF", "AL", "DZ", "AD", "AO", "AI", "AG"]
_KOBO_WORK: Dict[str, Any] = {
    "@type": "Book",
    "potentialAction": {
        "@type": "ReadAction",
        "expectsAcceptanceOf": {
            "@type": "Offer",
            "price": 6.990,
            "priceCurrency": "USD",
            "eligibleRegion": [{"@type": "Country", "name": "US"}],
            "ineligibleRegion": [
                {"@type": "Country", "name": code} for code in _INELIGIBLE
            ],
        },
    },
}


def _kobo_book() -> Dict[str, Any]:
    return {"@type": "Book", "name": "A Book", "workExample": _KOBO_WORK,
            "publisher": {"name": "Penguin Books"}}


# -- Kobo --------------------------------------------------------------------

def test_kobo_counts_the_ineligible_regions_and_reports_the_real_path() -> None:
    kobo = KoboSource(client=None)
    result = _result("kobo")
    value = kobo._extract_origin(
        _soup("<html><body><div id='x'>Language: English</div></body></html>"),
        _KOBO_WORK, _kobo_book(), {}, {"language": "English"}, "Penguin Books", result,
    )
    warning = _origin_warning(result)
    assert value is None
    # Counted, not remembered.
    assert f"({len(_INELIGIBLE)} entries)" in warning
    # The real nesting: the regions hang off the Offer, not off potentialAction.
    assert "potentialAction.expectsAcceptanceOf.ineligibleRegion" in warning
    assert "potentialAction.expectsAcceptanceOf.eligibleRegion" in warning
    assert "priced Offer" in warning
    # The substantive point survives.
    assert "sales-territory geo-licensing" in warning
    assert "not where the book was published" in warning


def test_kobo_says_so_when_the_region_lists_are_not_on_the_page() -> None:
    kobo = KoboSource(client=None)
    result = _result("kobo")
    kobo._extract_origin(_soup("<html><body><p>x</p></body></html>"), {},
                         {"@type": "Book", "name": "A Book"}, {}, {}, None, result)
    warning = _origin_warning(result)
    assert "found no eligibleRegion/ineligibleRegion pair" in warning
    assert "entries)" not in warning


def test_kobo_only_claims_layers_that_existed() -> None:
    kobo = KoboSource(client=None)
    result = _result("kobo")
    # No gizmo blob, no DOM rows: neither may be claimed as searched.
    kobo._extract_origin(_soup("<html><body><p>x</p></body></html>"), {}, {}, {},
                         {}, None, result)
    warning = _origin_warning(result)
    assert "data-kobo-gizmo-config googleBook blob" not in warning
    assert "bookitem-secondary-metadata DOM rows" not in warning
    assert "the page DOM and og:/meta tags" in warning


def test_kobo_self_heals_when_a_place_of_publication_appears() -> None:
    kobo = KoboSource(client=None)
    result = _result("kobo")
    book = _kobo_book()
    book["placeOfPublication"] = "Toronto, Canada"
    value = kobo._extract_origin(_soup("<html><body><p>x</p></body></html>"),
                                 _KOBO_WORK, book, {}, {}, None, result)
    assert value == "Toronto, Canada"
    joined = " ".join(result.warnings)
    assert "'Toronto, Canada' was read from Kobo's page" in joined
    assert "placeOfPublication" in joined
    assert "is null" not in joined


# -- Audible -----------------------------------------------------------------

def test_audible_names_only_the_country_fields_this_run_found() -> None:
    audible = AudibleSource(client=None)
    result = _result("audible")
    soup = _soup('<html><body><input id="reviewsCountry" value="US"></body></html>')
    audiobook = {"@type": "Audiobook", "name": "A Book",
                 "offers": {"@type": "Offer", "priceCurrency": "USD"}}
    details = {"language": "english", "regionsAllowed": ["US", "CA", "GB"]}
    assert audible._pick_origin(result, soup, audiobook, details) is None
    warning = _origin_warning(result)
    assert "regionsAllowed (3 entries)" in warning       # counted on this run
    assert "priceCurrency" in warning
    assert "#reviewsCountry" in warning
    assert "the Audiobook/Product JSON-LD" in warning


def test_audible_omits_country_fields_that_are_not_on_the_page() -> None:
    audible = AudibleSource(client=None)
    result = _result("audible")
    assert audible._pick_origin(result, _soup("<html><body><p>x</p></body></html>"),
                                {"@type": "Audiobook", "name": "A Book"}, {}) is None
    warning = _origin_warning(result)
    assert "regionsAllowed" not in warning
    assert "#reviewsCountry" not in warning
    assert "no country-shaped value on the page at all" in warning
    # The component JSON was empty, so it cannot be claimed as searched.
    assert "the embedded component JSON" not in warning


def test_audible_self_heals() -> None:
    audible = AudibleSource(client=None)
    result = _result("audible")
    value = audible._pick_origin(
        result, _soup("<html><body><p>x</p></body></html>"),
        {"@type": "Audiobook", "countryOfOrigin": "Ireland"}, {})
    assert value == "Ireland"
    assert "is null" not in " ".join(result.warnings)


# -- BookBub -----------------------------------------------------------------

def test_bookbub_keeps_its_row_probe_and_adds_the_shared_one() -> None:
    bookbub = BookBubSource(client=None)
    result = _result("bookbub")
    record = _Record(url="u", slug="s",
                     soup=_soup('<div class="book-panel">Length: 304 Pages</div>'),
                     data={"title": "A Book"})
    assert bookbub._origin(record, {"length": "304 Pages"}, result) is None
    warning = _origin_warning(result)
    assert "'place of publication'" in warning          # the label probe
    assert "the parsed detail rows" in warning          # the shared probe
    assert "data-book-json book JSON payload" in warning


def test_bookbub_distinguishes_selector_decay_from_an_absent_field() -> None:
    bookbub = BookBubSource(client=None)
    bookbub._detail_panel_found = False
    result = _result("bookbub")
    record = _Record(url="u", slug="s", soup=_soup("<div>nothing here</div>"), data={})
    assert bookbub._origin(record, {}, result) is None
    warning = _origin_warning(result)
    assert "selector/layout change on BookBub's side" in warning
    assert "not evidence that the field is absent" in warning


def test_bookbub_self_heals_from_a_detail_row() -> None:
    bookbub = BookBubSource(client=None)
    result = _result("bookbub")
    record = _Record(url="u", slug="s", soup=_soup("<div/>"), data={})
    value = bookbub._origin(record, {"place of publication": "Edinburgh"}, result)
    assert value == "Edinburgh"
    assert "is null" not in " ".join(result.warnings)


def test_bookbub_self_heals_from_the_book_json() -> None:
    bookbub = BookBubSource(client=None)
    result = _result("bookbub")
    record = _Record(url="u", slug="s", soup=_soup("<div/>"),
                     data={"publicationPlace": "Cape Town"})
    assert bookbub._origin(record, {}, result) == "Cape Town"


# -- Goodreads ---------------------------------------------------------------

def test_goodreads_probes_for_a_real_key_and_keeps_the_setting_note() -> None:
    goodreads = GoodreadsSource(client=None)
    result = _result("goodreads")
    page = _BookPage(url="u", soup=_soup("<html><body><h1>A Book</h1></body></html>"),
                     book={"details": {"isbn": "0143127551"}},
                     work={"details": {"places": [{"name": "Ohio"}]}})
    assert goodreads._origin(page, result) is None
    warning = _origin_warning(result)
    assert "the __NEXT_DATA__ Apollo cache's work record" in warning
    assert "place-of-publication key or label was found" in warning
    setting = [w for w in result.warnings if "SETTING" in w]
    assert setting and "Ohio" in setting[0]


def test_goodreads_self_heals_and_does_not_use_places() -> None:
    goodreads = GoodreadsSource(client=None)
    result = _result("goodreads")
    page = _BookPage(
        url="u", soup=_soup("<html><body><h1>A Book</h1></body></html>"),
        book={"details": {"isbn": "0143127551", "placeOfPublication": "New York"}},
        work={"details": {"places": [{"name": "Ohio"}]}},
    )
    assert goodreads._origin(page, result) == "New York"
    joined = " ".join(result.warnings)
    assert "'New York' was read from Goodreads' page" in joined
    assert "Ohio" not in joined


# -- Amazon: both detail layouts really are consulted ------------------------

_AMAZON_BULLETS = """
<div id="detailBullets_feature_div"><ul>
  <li><span class="a-list-item">
    <span class="a-text-bold">Publisher &#8207; : &#8206;</span>
    <span>Penguin Books (June 26, 2014)</span></span></li>
  <li><span class="a-list-item">
    <span class="a-text-bold">Language &#8207; : &#8206;</span>
    <span>English</span></span></li>
  <li><span class="a-list-item">
    <span class="a-text-bold">Paperback &#8207; : &#8206;</span>
    <span>304 pages</span></span></li>
  <li><span class="a-list-item">
    <span class="a-text-bold">ISBN-10 &#8207; : &#8206;</span>
    <span>0143127551</span></span></li>
  <li><span class="a-list-item">
    <span class="a-text-bold">ISBN-13 &#8207; : &#8206;</span>
    <span>978-0143127550</span></span></li>
  <li><span class="a-list-item">
    <span class="a-text-bold">Publication date &#8207; : &#8206;</span>
    <span>June 26, 2014</span></span></li>
</ul></div>
"""

_AMAZON_TABLE = """
<table class="prodDetTable">
  <tr><th>Publisher</th><td>A Different Imprint</td></tr>
  <tr><th>Country of Origin</th><td>Japan</td></tr>
  <tr><th>Item Weight</th><td>9.6 ounces</td></tr>
</table>
"""


def _amazon_page(markup: str) -> _Page:
    return _Page(url="https://www.amazon.com/dp/0143127551", html=markup,
                 soup=_soup(markup))


def test_amazon_reads_a_label_only_the_table_carries() -> None:
    """The defect this fixes: the table used to be skipped whenever bullets parsed."""
    amazon = AmazonSource(client=None)
    page = _amazon_page(f"<html><body>{_AMAZON_BULLETS}{_AMAZON_TABLE}</body></html>")
    result = _result("amazon")
    assert amazon._origin(page, result) == "Japan"
    assert any('"Country of Origin" detail bullet' in w for w in result.warnings)


def test_amazon_merge_changes_no_other_field() -> None:
    """The bullet list wins on conflict, so merging can only *add* labels."""
    amazon = AmazonSource(client=None)
    bullets_only = amazon._bullets_for(_amazon_page(
        f"<html><body>{_AMAZON_BULLETS}</body></html>"))
    merged = AmazonSource(client=None)._bullets_for(_amazon_page(
        f"<html><body>{_AMAZON_BULLETS}{_AMAZON_TABLE}</body></html>"))

    for label, value in bullets_only.items():
        assert merged[label] == value, f"{label} changed value when merging"
    assert merged["Publisher"] == "Penguin Books (June 26, 2014)"   # not the table's
    assert set(merged) - set(bullets_only) == {"Country of Origin", "Item Weight"}
    # No label is duplicated: dict keys are unique and every value is a single read.
    assert len(merged) == len(bullets_only) + 2
    assert merged["ISBN-13"] == "978-0143127550"
    assert merged["Language"] == "English"
    assert merged["Publication date"] == "June 26, 2014"


def test_amazon_table_only_layout_still_works() -> None:
    amazon = AmazonSource(client=None)
    bullets = amazon._bullets_for(_amazon_page(f"<html><body>{_AMAZON_TABLE}</body></html>"))
    assert bullets["Publisher"] == "A Different Imprint"
    assert any("alternate 'Product details' table layout" in n for n in amazon._notes)


def test_amazon_warning_reports_the_merged_map_and_the_delivery_locale() -> None:
    amazon = AmazonSource(client=None)
    markup = (f"<html><body>{_AMAZON_BULLETS}"
              '<div id="glow-ingress-line2">Mumbai 400001</div></body></html>')
    result = _result("amazon")
    assert amazon._origin(_amazon_page(markup), result) is None
    warning = _origin_warning(result)
    assert "merged detail map" in warning
    assert "'Product details' table" in warning
    assert "6 label(s) this run" in warning              # counted, not asserted
    assert "'Mumbai 400001'" in warning                  # read, not remembered
    assert "not where the book was published" in warning


def test_amazon_omits_the_delivery_clause_when_the_page_has_none() -> None:
    amazon = AmazonSource(client=None)
    result = _result("amazon")
    assert amazon._origin(_amazon_page(f"<html><body>{_AMAZON_BULLETS}</body></html>"),
                          result) is None
    assert "#glow-ingress-line2" not in _origin_warning(result)


def test_amazon_probe_finds_an_alternative_spelling_in_the_dom() -> None:
    amazon = AmazonSource(client=None)
    markup = ("<html><body>"
              '<div id="detailBullets_feature_div"><ul><li><span class="a-list-item">'
              '<span class="a-text-bold">Language : </span><span>English</span>'
              "</span></li></ul></div>"
              '<div><span>Place of publication: Bombay</span></div></body></html>')
    result = _result("amazon")
    assert amazon._origin(_amazon_page(markup), result) == "Bombay"


# -- no hand-counted page facts anywhere -------------------------------------

def test_no_origin_warning_carries_a_hardcoded_page_count() -> None:
    """A number in an origin warning must have been counted on this run."""
    kobo, audible, bookbub = (KoboSource(client=None),
                              AudibleSource(client=None),
                              BookBubSource(client=None))
    goodreads, amazon = (GoodreadsSource(client=None),
                         AmazonSource(client=None))
    warnings: List[str] = []

    result = _result("kobo")
    kobo._extract_origin(_soup("<html><body><p>x</p></body></html>"), _KOBO_WORK,
                         _kobo_book(), {}, {}, None, result)
    warnings.append(_origin_warning(result))

    result = _result("audible")
    audible._pick_origin(result, _soup("<html><body><p>x</p></body></html>"),
                         {"@type": "Audiobook", "name": "A Book"}, {})
    warnings.append(_origin_warning(result))

    result = _result("bookbub")
    bookbub._origin(_Record(url="u", slug="s", soup=_soup("<div/>"), data={}),
                    {}, result)
    warnings.append(_origin_warning(result))

    result = _result("goodreads")
    goodreads._origin(_BookPage(url="u", soup=_soup("<html><p>x</p></html>")), result)
    warnings.append(_origin_warning(result))

    result = _result("amazon")
    amazon._origin(_amazon_page("<html><body><p>x</p></body></html>"), result)
    warnings.append(_origin_warning(result))

    # The counts that legitimately appear are the probe's own key-spelling total
    # and figures measured from the synthetic page above. Nothing may claim the
    # old hand-counted "47-entry ineligibleRegion list".
    for warning in warnings:
        assert "47" not in warning
        for number in re.findall(r"\b\d+\b", warning):
            assert number in {"13", "0", "1", str(len(_INELIGIBLE))}, (
                f"unexplained number {number!r} in {warning!r}"
            )


if __name__ == "__main__":  # pragma: no cover - direct invocation convenience
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
