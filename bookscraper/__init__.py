"""book-scraper: a polite, source-agnostic book metadata scraping pipeline.

    models      dataclasses shared by every layer
    isbn        ISBN-10/13 normalisation, checksums, conversion
    csv_input   reads the run's ISBN list out of a CSV
    transport   the polite request engine: delays, retries, block recording
    blocks      recognising bot walls (and never fighting them)
    render      optional headless-browser rendering
    http        HttpClient: typed fetchers over the transport
    parse       text, DOM and identity-matching helpers
    extract     JSON-LD, embedded JSON blobs, dates and lists
    origin      the place-of-publication probe
    storage     writes the five artefact kinds with the mandated filenames
    metadata    the accumulating <source>_metadata.json arrays
    nodata      the durable "this site has no such book" lists
    base        the Source contract, plus adapter auto-discovery
    sources     per-site adapters, auto-discovered
    runner      orchestration and persistence
    report      the per-source report files and the closing digest

Progress and warnings go to **stderr**; only the closing digest goes to stdout,
so ``2>/dev/null`` leaves just the report.
"""

from __future__ import annotations

__version__ = "2.0.0"
