"""book-scraper: a polite, source-agnostic book metadata scraping pipeline.

Layout
------
``models``      dataclasses shared by every layer (:class:`~bookscraper.models.BookHint`,
                :class:`~bookscraper.models.ScrapeResult`, ...)
``isbn``        ISBN-10/13 normalisation, checksums and conversion
``csv_input``   reads the run's ISBN list out of a CSV file
``metrics``     one record per (ISBN, source) attempt: drives skip decisions and
                writes the per-source success/failure report to ``<out>/metrics/``
``http_client`` polite, retrying HTTP layer with an optional Selenium fallback
``storage``     writes the five artefact kinds to disk with the assignment's filenames
``base``        :class:`~bookscraper.base.BaseSource` ABC plus parsing helpers
``sources``     per-site adapters, auto-discovered (no registry to maintain)
``pipeline``    orchestration, persistence and the per-source summary table
``batch``       runs the pipeline once per CSV ISBN over one shared HTTP client

Progress and warnings are printed to **stderr** with plain ``print()``; only
``main.py`` and the pipeline's summary renderer write to **stdout**, so
``2>/dev/null`` leaves just the report.
"""

from __future__ import annotations

__version__ = "1.0.0"

__all__ = [
    "__version__",
    "base",
    "batch",
    "csv_input",
    "http_client",
    "isbn",
    "metrics",
    "models",
    "verbosity",
    "pipeline",
    "sources",
    "storage",
]
