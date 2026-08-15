"""Auto-discovery of site adapters.

Drop a module in this package that defines a concrete
:class:`~bookscraper.base.BaseSource` subclass with a non-empty ``name`` and it
becomes available to the CLI immediately. There is deliberately **no**
hand-maintained registry: adapter files never edit this file to register
themselves.

A module that fails to import (syntax error, missing optional dependency, ...)
produces a warning and is skipped, so one broken adapter cannot take the rest of
the program down with it.
"""

from __future__ import annotations

import sys
from ..verbosity import verbose
import importlib
import inspect
import pkgutil
from typing import Dict, Type

from ..base import BaseSource

__all__ = ["discover_sources", "PREFERRED_ORDER"]


#: Presentation order only -- *not* a registry. Adapters not listed here are
#: still discovered; they simply sort alphabetically after these. Open Library
#: leads because the pipeline uses it to seed title/author hints for the
#: ISBN-hostile sources (see ``pipeline.SEED_SOURCE``).
PREFERRED_ORDER: tuple = ("openlibrary", "goodreads", "amazon", "bookbub", "kobo", "audible")


def _order_key(name: str) -> tuple:
    try:
        return (0, PREFERRED_ORDER.index(name), name)
    except ValueError:
        return (1, 0, name)


def discover_sources() -> Dict[str, Type[BaseSource]]:
    """Return ``{source_name: adapter_class}`` for every importable adapter.

    Discovery rules:

    * every non-underscore module in ``bookscraper.sources`` is imported;
    * classes are kept when they subclass :class:`BaseSource`, are not
      :class:`BaseSource` itself, are not abstract, define a non-empty ``name``,
      and are *defined* in the module being scanned (so re-exports do not
      produce duplicates);
    * keys are the adapter's own ``name``, lower-cased and stripped, which is
      also what ``--sources`` accepts and what appears in output filenames.

    The returned mapping is ordered by :data:`PREFERRED_ORDER`, then
    alphabetically.
    """
    found: Dict[str, Type[BaseSource]] = {}

    for module_info in pkgutil.iter_modules(__path__):
        short_name = module_info.name
        if short_name.startswith("_"):
            continue
        qualified = f"{__name__}.{short_name}"
        try:
            module = importlib.import_module(qualified)
        except Exception as exc:  # a broken adapter must not break the run
            print('warning: Skipping source module %s: it failed to import (%s: %s)' % (qualified, type(exc).__name__, exc), file=sys.stderr)
            continue

        for _attr, obj in inspect.getmembers(module, inspect.isclass):
            if not issubclass(obj, BaseSource) or obj is BaseSource:
                continue
            if inspect.isabstract(obj):
                if verbose():
                    print('  Ignoring abstract adapter %s.%s' % (qualified, obj.__name__), file=sys.stderr)
                continue
            if obj.__module__ != module.__name__:
                continue  # imported into this module, defined elsewhere
            raw_name = getattr(obj, "name", "") or ""
            key = str(raw_name).strip().lower()
            if not key:
                print("warning: Ignoring %s.%s: it must set a non-empty lowercase 'name' class attribute to be discoverable" % (qualified, obj.__name__), file=sys.stderr)
                continue
            if key in found and found[key] is not obj:
                print('warning: Duplicate source name %r: %s.%s clashes with %s.%s; keeping the first' % (key, qualified, obj.__name__, found[key].__module__, found[key].__name__), file=sys.stderr)
                continue
            found[key] = obj

    ordered = sorted(found.items(), key=lambda kv: _order_key(kv[0]))
    if ordered:
        if verbose():
            print('  Discovered sources: %s' % (', '.join((name for name, _ in ordered)),), file=sys.stderr)
    else:
        if verbose():
            print('  No source adapters found in %s' % (__name__,), file=sys.stderr)
    return dict(ordered)

