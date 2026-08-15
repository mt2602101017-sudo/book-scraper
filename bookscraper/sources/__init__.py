"""Per-site adapters, auto-discovered by :func:`bookscraper.base.discover`.

Drop a module here that defines a concrete :class:`~bookscraper.base.Source`
subclass with a non-empty ``name`` and it becomes available to the CLI at once.
There is deliberately no registry to maintain. Modules whose names start with an
underscore hold shared helpers and are skipped.
"""
