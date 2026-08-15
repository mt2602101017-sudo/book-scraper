"""Every adapter must call the shared :class:`BaseSource` helpers correctly.

Written because of a bug that cost a whole source. When ``_source_url`` was dropped
from :class:`~bookscraper.models.BookMetadata`, ``BaseSource.new_metadata`` lost its
second parameter -- but ``kobo.py`` kept calling ``new_metadata(hint, result.book_url)``.
Every Kobo scrape then raised ``TypeError: new_metadata() takes 2 positional arguments
but 3 were given``, which ``BaseSource.scrape`` dutifully caught and turned into
``"unexpected TypeError while scraping ...; returning a partial result"``. So Kobo
returned an empty record for **every** book, exit status stayed 0, and the whole
test suite stayed green -- the adapter had simply stopped working.

The lesson is that the wrapper which makes one bad adapter harmless also makes a
broken call site invisible, so the call sites need checking statically. These tests
parse each adapter with :mod:`ast` -- no network, no browser, no fixtures -- and
check every call to a shared helper against the real signature.

    .venv/bin/python -m pytest tests/test_adapter_signatures.py -q
"""

from __future__ import annotations

import ast
import inspect
import sys
from pathlib import Path
from typing import Dict, List, Tuple

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from bookscraper.base import BaseSource  # noqa: E402
from bookscraper.sources import discover_sources  # noqa: E402

#: Helpers an adapter is expected to call. Each is checked by arity, because that
#: is the failure mode that slipped through: a call that is one argument off.
_SHARED_HELPERS = (
    "new_metadata", "new_result", "probe_origin", "probe_origin_detail",
    "seed_from_openlibrary", "search_terms", "clean_text", "dedupe", "iso_date",
    "origin_unavailable", "origin_layers_clause", "absolutise", "select_text",
)


def _adapter_files() -> List[Path]:
    files = []
    for name in discover_sources():
        path = _ROOT / "bookscraper" / "sources" / f"{name}.py"
        assert path.exists(), f"no module on disk for the {name!r} source"
        files.append(path)
    assert files, "no adapters were discovered at all"
    return files


def _self_calls(path: Path) -> List[Tuple[str, int, int, List[str]]]:
    """``(method, lineno, positional_count, keywords)`` for every ``self.X(...)``."""
    out = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if (isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name)
                and fn.value.id == "self"):
            positional = sum(1 for a in node.args if not isinstance(a, ast.Starred))
            out.append((fn.attr, node.lineno, positional,
                        [k.arg for k in node.keywords if k.arg]))
    return out


def _limits() -> Dict[str, Tuple[int, int, bool, set]]:
    """``helper -> (min_positional, max_positional, takes_varargs, valid_keywords)``."""
    limits = {}
    for helper in _SHARED_HELPERS:
        fn = getattr(BaseSource, helper, None)
        if fn is None:
            continue
        params = list(inspect.signature(fn).parameters.values())
        if params and params[0].name in {"self", "cls"}:
            params = params[1:]          # `self` is supplied by the bound call
        positional = [p for p in params
                      if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
        required = sum(1 for p in positional if p.default is p.empty)
        varargs = any(p.kind is p.VAR_POSITIONAL for p in params)
        names = {p.name for p in params if p.kind is not p.VAR_POSITIONAL}
        takes_kwargs = any(p.kind is p.VAR_KEYWORD for p in params)
        limits[helper] = (required, len(positional), varargs,
                          names if not takes_kwargs else set())
    return limits


def test_every_adapter_calls_shared_helpers_with_the_right_arity() -> None:
    """The exact bug: ``new_metadata(hint, result.book_url)`` against a 1-arg helper."""
    limits, problems = _limits(), []
    assert "new_metadata" in limits, "the regression's own helper must be covered"

    for path in _adapter_files():
        for method, line, positional, keywords in _self_calls(path):
            if method not in limits:
                continue
            low, high, varargs, valid = limits[method]
            supplied = positional + len([k for k in keywords if k in valid or not valid])
            if positional > high and not varargs:
                problems.append(
                    f"{path.name}:{line}: self.{method}() got {positional} positional "
                    f"argument(s); it accepts at most {high}"
                )
            elif supplied < low:
                problems.append(
                    f"{path.name}:{line}: self.{method}() got {supplied} argument(s); "
                    f"it requires {low}"
                )
            for keyword in keywords:
                if valid and keyword not in valid:
                    problems.append(
                        f"{path.name}:{line}: self.{method}() got an unexpected "
                        f"keyword {keyword!r}; valid: {sorted(valid)}"
                    )
    assert not problems, "adapter/helper signature mismatches:\n  " + "\n  ".join(problems)


def test_no_adapter_calls_a_helper_that_does_not_exist() -> None:
    """A renamed helper must not leave a call site behind.

    ``BaseSource.scrape`` catches ``AttributeError`` the same way it caught the
    ``TypeError``, so a stale name is just as invisible at runtime.
    """
    problems = []
    for path in _adapter_files():
        module = ast.parse(path.read_text(encoding="utf-8"))
        defined = {n.name for n in ast.walk(module)
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        assigned = {t.attr for n in ast.walk(module) if isinstance(n, ast.Assign)
                    for t in n.targets
                    if isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name)
                    and t.value.id == "self"}
        for method, line, _positional, _keywords in _self_calls(path):
            if method in defined or method in assigned or hasattr(BaseSource, method):
                continue
            problems.append(f"{path.name}:{line}: self.{method}() is defined nowhere")
    assert not problems, "calls to non-existent methods:\n  " + "\n  ".join(problems)


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc}")
            else:
                print(f"ok   {name}")
    sys.exit(1 if failures else 0)
