"""Tests for the shared place-of-publication probe in :mod:`bookscraper.base`.

The probe is what makes the ``origin`` warnings honest: every "layers searched"
clause is built from :attr:`OriginProbe.searched`, so these tests pin the three
properties the warnings depend on.

1. A layer that really carries a publication place is **found** (the field can
   self-heal the day a storefront starts publishing it).
2. Country-*shaped* licensing data -- ``eligibleRegion``, ``ineligibleRegion``,
   ``regionsAllowed``, ``priceCurrency`` -- is **never** mistaken for an origin.
3. Malformed, cyclic, hostile and enormous input **never raises**, because
   ``scrape()`` must not fall over on a page that surprises us.

Runnable either way:

    .venv/bin/python -m pytest tests/test_origin_probe.py -q
    .venv/bin/python tests/test_origin_probe.py

Only synthetic structures appear here -- never a cached page, title or review.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bs4 import BeautifulSoup  # noqa: E402

from bookscraper.base import (  # noqa: E402
    ORIGIN_KEYS,
    ORIGIN_KEY_SPELLINGS,
    BaseSource,
    normalise_key,
)
from bookscraper.models import BookHint, ScrapeResult  # noqa: E402


class _Probe(BaseSource):
    """Minimal concrete adapter: the probe helpers are all we exercise."""

    name = "probe"
    display_name = "Probe"

    def __init__(self) -> None:
        super().__init__(client=None)

    def find_book_url(self, hint: BookHint) -> Optional[str]:  # pragma: no cover
        return None

    def _scrape_into(self, hint: BookHint, result: ScrapeResult) -> None:  # pragma: no cover
        return self.new_result(hint)


def _probe() -> _Probe:
    return _Probe()


def _soup(markup: str) -> BeautifulSoup:
    return BeautifulSoup(markup, "html.parser")


# -- key folding -------------------------------------------------------------

def test_key_spellings_fold_case_and_separators() -> None:
    assert normalise_key("countryOfOrigin") == "countryoforigin"
    assert normalise_key("country_of_origin") == "countryoforigin"
    assert normalise_key("Country of Origin ‏ : ‎") == "countryoforigin"
    assert normalise_key("Country/Region of Origin") == "countryregionoforigin"
    assert normalise_key(None) == ""
    # Every advertised spelling is really in the compared set.
    for spelling in ORIGIN_KEY_SPELLINGS:
        assert normalise_key(spelling) in ORIGIN_KEYS


def test_licensing_keys_are_not_in_the_key_set() -> None:
    for hostile in ("eligibleRegion", "ineligibleRegion", "regionsAllowed",
                    "priceCurrency", "datePublished", "publisher", "places",
                    "availableAtOrFrom", "inLanguage"):
        assert normalise_key(hostile) not in ORIGIN_KEYS


# -- finding a real value ----------------------------------------------------

def test_planted_place_of_publication_is_found() -> None:
    layer = {
        "@type": "Book",
        "name": "A Book",
        "publisher": {"@type": "Organization", "name": "Penguin Books"},
        "placeOfPublication": "London, England",
    }
    value, searched = _probe().probe_origin([("the work blob", layer)])
    assert value == "London, England"
    assert searched == ["the work blob"]


def test_every_advertised_spelling_is_found_in_json() -> None:
    p = _probe()
    for spelling in ORIGIN_KEY_SPELLINGS:
        value, searched = p.probe_origin([("blob", {spelling: "Dublin"})])
        assert value == "Dublin", f"{spelling!r} was not recognised"
        assert searched == ["blob"]


def test_nested_and_object_valued_places_are_found_with_their_path() -> None:
    layer = {
        "workExample": {
            "edition": [{"details": {"publish_places": [{"name": "Toronto"}]}}]
        }
    }
    probe = _probe().probe_origin_detail([("the gizmo blob", layer)])
    assert probe.value == "Toronto"
    assert probe.where is not None
    assert "publish_places" in probe.where
    assert "the gizmo blob" in probe.where


def test_dom_label_row_is_found() -> None:
    soup = _soup(
        """
        <table class="prodDetTable">
          <tr><th>Publisher</th><td>Penguin Classics</td></tr>
          <tr><th>Country of Origin</th><td>Japan</td></tr>
        </table>
        """
    )
    probe = _probe().probe_origin_detail([("the details table", soup)])
    assert probe.value == "Japan"
    assert "Country of Origin" in (probe.where or "")


def test_dom_inline_label_and_meta_tag_are_found() -> None:
    p = _probe()
    inline = _soup(
        '<div class="meta"><li><span>Place of publication: Leipzig</span></li></div>'
    )
    assert p.probe_origin([("dom", inline)])[0] == "Leipzig"

    meta = _soup('<head><meta property="publicationPlace" content="Oslo"></head>')
    assert p.probe_origin([("meta tags", meta)])[0] == "Oslo"

    itemprop = _soup('<span itemprop="publishPlace">Reykjavik</span>')
    assert p.probe_origin([("microdata", itemprop)])[0] == "Reykjavik"


def test_first_layer_with_a_hit_wins_and_searched_stops_there() -> None:
    probe = _probe().probe_origin_detail([
        ("layer one", {"title": "no origin here"}),
        ("layer two", {"placePublished": "Madrid"}),
        ("layer three", {"placeOfPublication": "Never reached"}),
    ])
    assert probe.value == "Madrid"
    assert probe.searched == ["layer one", "layer two"]


# -- refusing look-alikes ----------------------------------------------------

def test_eligible_region_is_not_mistaken_for_origin() -> None:
    """The exact Kobo shape: geo-licensing hanging off a priced checkout offer."""
    work = {
        "@type": "Book",
        "potentialAction": {
            "@type": "ReadAction",
            "expectsAcceptanceOf": {
                "@type": "Offer",
                "price": 6.990,
                "priceCurrency": "USD",
                "eligibleRegion": [{"@type": "Country", "name": "US"}],
                "ineligibleRegion": [
                    {"@type": "Country", "name": code}
                    for code in ("AF", "AL", "DZ", "AD", "AO", "AI", "AG", "AR")
                ],
            },
        },
    }
    value, searched = _probe().probe_origin([("the work blob", work)])
    assert value is None
    assert searched == ["the work blob"]


def test_other_country_shaped_fields_are_not_origins() -> None:
    p = _probe()
    for hostile in (
        {"regionsAllowed": ["US", "CA", "GB", "AU"]},
        {"offers": {"priceCurrency": "USD", "availability": "InStock"}},
        {"details": {"places": [{"name": "Ohio, United States"}]}},
        {"datePublished": "2014-06-26", "publisher": "Penguin Books"},
        {"inLanguage": "en", "countryCode": "US"},
        {"deliveryLocale": "United States"},
    ):
        value, searched = p.probe_origin([("blob", hostile)])
        assert value is None, f"{hostile!r} was wrongly read as an origin"
        assert searched == ["blob"]


def test_implausible_values_are_rejected() -> None:
    p = _probe()
    # A code list, a number, a boolean, markup and an empty string are not places.
    assert p.probe_origin([("blob", {"countryOfOrigin": ["AF"] * 200})])[0] == "AF"
    assert p.probe_origin([("blob", {"countryOfOrigin": 1975})])[0] is None
    assert p.probe_origin([("blob", {"countryOfOrigin": True})])[0] is None
    assert p.probe_origin([("blob", {"countryOfOrigin": ""})])[0] is None
    assert p.probe_origin([("blob", {"countryOfOrigin": "   "})])[0] is None
    assert p.probe_origin([("blob", {"countryOfOrigin": "x" * 400})])[0] is None
    assert p.probe_origin([("blob", {"countryOfOrigin": "<div>London</div>"})])[0] is None
    assert p.probe_origin([("blob", {"countryOfOrigin": "null"})])[0] is None


def test_a_script_island_is_not_read_as_a_dom_label() -> None:
    soup = _soup(
        '<html><body><script>var cfg = {"Country of Origin": "Mars"};</script>'
        "</body></html>"
    )
    assert _probe().probe_origin([("dom", soup)])[0] is None


# -- what "searched" means ---------------------------------------------------

def test_absent_and_empty_layers_are_not_claimed_as_searched() -> None:
    p = _probe()
    value, searched = p.probe_origin([
        ("a layer that was not on the page", None),
        ("an empty blob", {}),
        ("an empty list", []),
        ("a blob with content", {"title": "something"}),
    ])
    assert value is None
    assert searched == ["a blob with content"]

    empty_soup = _soup("")
    assert p.probe_origin([("an empty document", empty_soup)])[1] == []


def test_layers_clause_reports_an_empty_search_honestly() -> None:
    p = _probe()
    assert p.origin_layers_clause([]) == (
        "no parsed layer was available to search on this run"
    )
    assert p.origin_layers_clause(["one"]) == "one"
    assert p.origin_layers_clause(["one", "two"]) == "one and two"
    assert p.origin_layers_clause(["one", "two", "three"]) == "one, two and three"


# -- never raising -----------------------------------------------------------

def test_cyclic_structures_terminate() -> None:
    cyclic: Dict[str, Any] = {"name": "loop"}
    cyclic["self"] = cyclic
    cyclic["kids"] = [cyclic, {"deeper": cyclic}]
    value, searched = _probe().probe_origin([("cyclic", cyclic)])
    assert value is None
    assert searched == ["cyclic"]

    ring: List[Any] = []
    ring.append(ring)
    assert _probe().probe_origin([("ring", ring)])[0] is None


def test_cyclic_structure_still_finds_a_planted_place() -> None:
    cyclic: Dict[str, Any] = {"details": {"placeOfPublication": "Kyoto"}}
    cyclic["parent"] = cyclic
    assert _probe().probe_origin([("cyclic", cyclic)])[0] == "Kyoto"


def test_malformed_and_hostile_layers_never_raise() -> None:
    p = _probe()

    class Exploding(dict):
        def items(self) -> Any:
            raise RuntimeError("boom")

        def __bool__(self) -> bool:
            return True

    class Weird:
        def __repr__(self) -> str:
            raise RuntimeError("no repr for you")

    exploding = Exploding()
    exploding["placeOfPublication"] = "unreachable"

    for payload in (
        exploding,
        {"placeOfPublication": Weird()},
        {None: "London"},
        {"a": {1: 2}, "b": (i for i in range(3))},
        object(),
        "a bare string is not a layer",
        b"bytes are not a layer",
        42,
        set(),
    ):
        value, searched = p.probe_origin([("hostile", payload)])
        assert value is None or isinstance(value, str)
        assert isinstance(searched, list)

    # Malformed *layer* entries (not just payloads) are skipped, not fatal.
    assert p.probe_origin([("only one element",)])[0] is None      # type: ignore[list-item]
    assert p.probe_origin([None])[0] is None                      # type: ignore[list-item]
    assert p.probe_origin(None)[0] is None                        # type: ignore[arg-type]
    assert p.probe_origin([])[0] is None
    assert p.origin_layers_clause(None) == (                      # type: ignore[arg-type]
        "no parsed layer was available to search on this run"
    )


def test_deep_and_wide_structures_are_bounded() -> None:
    p = _probe()
    # 5000 levels deep: must terminate quickly and not recurse to death.
    deep: Dict[str, Any] = {"placeOfPublication": "too deep to matter"}
    for _ in range(5000):
        deep = {"child": deep}
    assert p.probe_origin([("deep", deep)])[0] is None

    # 200k wide: must stop at the node budget rather than chew the page.
    wide = {f"key{i}": i for i in range(200_000)}
    wide["placeOfPublication"] = "Somewhere"
    value, searched = p.probe_origin([("wide", wide)])
    assert searched == ["wide"]
    assert value in (None, "Somewhere")


def test_json_pair_walk_is_bounded_and_paths_are_dotted() -> None:
    p = _probe()
    pairs = list(p.iter_json_pairs({"a": {"b": [{"c": 1}]}}))
    paths = [pair.path for pair in pairs]
    assert "a" in paths and "a.b" in paths and "a.b[0].c" in paths
    for pair in pairs:
        if pair.key == "c":
            assert pair.parent == {"c": 1}

    assert list(p.iter_json_pairs(None)) == []
    assert list(p.iter_json_pairs("string")) == []
    assert len(list(p.iter_json_pairs({f"k{i}": i for i in range(500)}, max_nodes=10))) <= 10
    rooted = list(p.iter_json_pairs({"x": 1}, path="root"))
    assert rooted[0].path == "root.x"


if __name__ == "__main__":  # pragma: no cover - direct invocation convenience
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))


# -- shared title helpers ------------------------------------------------------


def test_sequel_pairs_are_vetoed_identically_for_every_adapter() -> None:
    """``is_sequel_pair`` moved from two private copies into BaseSource.

    Amazon and BookBub each carried the same logic over an identical ordinal table.
    The veto matters: "ready player one" vs "ready player two" scores ~0.88 on raw
    similarity, so without it the wrong book gets filed under the requested ISBN.
    """
    from bookscraper.base import ORDINAL_TOKENS, BaseSource

    sequels = [("ready player one", "ready player two"),
               ("book i", "book ii"),
               ("part 1 of x", "part 2 of x"),
               ("fifth element", "first element")]
    for left, right in sequels:
        assert BaseSource.is_sequel_pair(left, right), f"{left!r} vs {right!r}"

    not_sequels = [("the hobbit", "the hobbit"),      # identical
                   ("dune", "dune messiah"),          # different length
                   ("a b c", "a b d"),                # differs, but not an ordinal
                   ("", "")]                          # empty
    for left, right in not_sequels:
        assert not BaseSource.is_sequel_pair(left, right), f"{left!r} vs {right!r}"

    # Every adapter reaches the same helper, so none can drift again.
    from bookscraper.sources.amazon import AmazonSource
    from bookscraper.sources.bookbub import BookBubSource
    for cls in (AmazonSource, BookBubSource):
        assert cls.is_sequel_pair is BaseSource.is_sequel_pair

    assert ORDINAL_TOKENS["one"] != ORDINAL_TOKENS["two"]
