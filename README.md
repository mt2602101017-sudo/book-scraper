# book-scraper

Given a **CSV file of ISBNs** (or a single ISBN), this program scrapes five things from six
book websites for each one and writes them to disk:

1. **Metadata** — title, author(s), publisher, origin, date of publication, language, genre
2. **Cover page image(s)** — numbered, so multiple editions each get their own file
3. **Blurb** / publisher description
4. **Reviews** — one plain-text file per review
5. **Genres** — one file per source, one genre per line

Sources: **Open Library**, **Goodreads**, **Amazon**, **BookBub**, **Kobo**, **Audible**. All
six are on by default. They are run in that order because the first three can be looked up by
ISBN directly while the last three cannot, and Open Library — a free, ISBN-indexed JSON API —
resolves the ISBN to a title+author that the storefronts can then be *searched* with. See
[How a book is resolved](#how-a-book-is-resolved).

```bash
.venv/bin/python main.py 2602101017.csv                # every ISBN in the CSV
.venv/bin/python main.py 2602101017.csv --end 20       # …just the first 20
.venv/bin/python main.py 9780143127550                 # one ISBN, no CSV needed
.venv/bin/python main.py 2602101017.csv --sources goodreads,amazon
```

**There are exactly three flags** — `--start`, `--end` and `--sources`. Everything that used to
be a flag is now a constant in the `SETTINGS` dict at the top of `main.py`. See
[Usage](#usage).

The input is a CSV whose ISBN column is found automatically; ISBNs are scraped **one at a time,
in file order**, through one shared rate-limited HTTP client, into one flat tree, with each
source's metadata **accumulating** into a single `<source>_metadata.json` array; the run ends
with a batch digest and a `metrics/` directory. See
[CSV input](#csv-input-the-normal-way-to-run-this),
[Layout](#layout-one-flat-tree-with-metadata-accumulating),
[One ISBN at a time](#one-isbn-at-a-time-and-why-there-is-no---workers) and [Metrics](#metrics).

Nothing is cached or stubbed. Every value in every output file comes from a live parse
performed during that run. When a site does not publish a field, the field is written as JSON
`null` **and** the reason is recorded in the run's logs and metrics — it is never guessed or
silently inferred.

One field needs a caveat up front. **`origin` (country/place of publication) was published by
none of the six sources on any run recorded here, so it is `null` in every metadata file.**
That is a *result*, not an assumption: every adapter runs a real search on every run —
Amazon's "Country of Origin" detail bullet and BookBub's detail rows, plus one shared probe
(`BaseSource.probe_origin`) that walks each parsed layer of each site looking for thirteen
spellings of "place of publication" — and each warning names the layers **the probe reports
having searched**, so the sentence cannot outlive the code and the field fills itself in the day
a source starts publishing it. It is never cross-filled between sources either: each record's
`origin` must come from the page that record claims, so a value found on one site is not copied
into another site's file. See [Field availability](#field-availability).

---

## Install

Python 3.10+ (developed and verified on CPython 3.12).

```bash
cd book-scraper
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

That installs `requests`, `beautifulsoup4`, `lxml` **and `selenium`**.

You also need a real browser (Google Chrome or Firefox) on the machine. Selenium 4.6+ fetches
the matching driver itself, so no `chromedriver` on `PATH` is required.

### Why selenium is a required dependency, not an extra

The brief allows Selenium **where page content is JavaScript-rendered**, and that is exactly
and only what it is used for here. **Kobo produces nothing without a browser**: it sits behind
a Cloudflare TLS-fingerprint gate no header tuning can pass, and its book body is client-side
rendered. **BookBub is the same**: Cloudflare-walled and client-side rendered. Install without
selenium and a default run writes **4** metadata JSONs instead of 6, so selenium belongs in
`requirements.txt` rather than in the optional extras.

It is nonetheless still a *soft* dependency **in code**: `selenium` is imported lazily inside
`HttpClient._ensure_driver()` and **nowhere else**, so if it or a browser driver is missing,
`HttpClient` prints one clear warning, returns `None`, and every adapter degrades to
`requests` + BeautifulSoup rather than crashing. Setting `SETTINGS["browser"] = "never"` demonstrates this — zero
tracebacks, one explanatory warning per affected source, and Amazon still returns a full
6/7-field record through the static path alone.

Only the adapters that genuinely need JavaScript declare it (`prefers_browser = True`: Kobo,
BookBub) and those two go **straight** to the browser, because a plain request to either host
is a measured 0-for-3. Open Library, Goodreads, Amazon and Audible never open a browser at all
on the normal path — Open Library is a pure JSON API, and the other three are static-first and
escalate only when a plain fetch is actively blocked.

```bash
.venv/bin/python -m pip install -r requirements-optional.txt   # pytest, brotli
```

---

## Usage

```bash
.venv/bin/python main.py 2602101017.csv                  # CSV of ISBNs (the normal case)
.venv/bin/python main.py 9780143127550                   # a single ISBN-13
.venv/bin/python main.py 0143127551                      # ISBN-10, auto-converted
.venv/bin/python main.py 978-0-14-312755-0               # hyphens/spaces fine
.venv/bin/python main.py                                 # prompts interactively
.venv/bin/python main.py --help                          # also lists the source slugs
```

The positional argument is **a CSV file or a single ISBN** — no flag distinguishes them. It is
read as a file when it names something on disk or carries a `.csv` / `.tsv` / `.txt` / `.psv`
suffix, and as an ISBN otherwise. A `.csv` that does not exist is therefore a *usage error*
(exit 2), never something fed to the ISBN parser.

### Flags

There are three, and they are the three things that genuinely change between runs: **which
books**, and **which sites**.

| Flag | Default | Meaning |
|---|---|---|
| `CSV_OR_ISBN` | *(prompted)* | Path to a CSV holding a column of ISBNs, **or** one ISBN-10/13 in any punctuation. Every ISBN is checksum-validated; ISBN-10s are converted to ISBN-13 first. |
| `--start N` | `0` | Skip the first N ISBNs — how you resume an interrupted batch. |
| `--end N` | *end of file* | Stop **before** row N, so `--start 100 --end 200` is exactly rows 100–199. Half-open like Python slicing, so consecutive shards tile the file with no overlap. Clamped to the file length. |
| `--sources LIST` | `all` | Comma-separated slugs: `openlibrary,goodreads,amazon,bookbub,kobo,audible`. `all` means every discovered adapter. Unknown names warn and are skipped; a non-empty value naming *nothing* valid is a usage error rather than a silent "all". |

```bash
# resume a batch that stopped after 1 200 ISBNs
.venv/bin/python main.py 2602101017.csv --start 1200

# one explicit slice: rows 2000-3999, nothing else
.venv/bin/python main.py 2602101017.csv --start 2000 --end 4000

# shard the file across terminals -- the ranges tile exactly, no row twice
.venv/bin/python main.py 2602101017.csv --start 0    --end 5000
.venv/bin/python main.py 2602101017.csv --start 5000 --end 10000

# or split the sites instead, so the two runs barely share a host
.venv/bin/python main.py 2602101017.csv --sources goodreads,amazon
.venv/bin/python main.py 2602101017.csv --sources openlibrary,kobo,audible
```

### Everything else lives in `SETTINGS`

This CLI had **28 flags**. Almost all of them existed so that one run could differ from another
while a site was still being figured out — `--browser`, `--min-delay`, `--timeout`,
`filename_style`, the `max_reviews` setting and so on. Once each adapter's working path was known they
stopped being *choices* and became *settings*: there is one value that works, and a run that
uses a different one is a run that has not been tested.

So they live in a dict at the top of `main.py`, where a reader sees all of them at once and
`grep` finds them in one place:

```python
SETTINGS = {
    "out_dir": "data",          # the shared artefact tree
    "metrics_dir": "metrics",   # report + resume state; outside out_dir on purpose
    "min_reviews": 25,          # the assignment's target; a shortfall warns, never fails
    "max_reviews": None,        # no cap: take whatever a site will give
    "browser": "auto",          # Selenium only where a site needs it (Kobo, BookBub)
    "min_delay": 1.0,           # per-host courtesy delay, randomised in this range
    "max_delay": 2.0,
    "timeout": 25,
    "retries": 3,
    "download_covers": True,
    "filename_style": "underscore",
    "respect_robots": False,
    "user_agent": None,         # HttpClient's desktop-Chrome default
    "isbn_column": None,        # auto-detected from the CSV header or its data
    "verbose": False,           # flip to True to see every fallback as it fires
}
```

Two consequences worth knowing:

* **`--dry-run` is gone.** Validating the CSV without scraping was useful while the input was
  unknown; the CSV is now known. `--start 0 --end 0` selects no rows if you want the header
  parse without the fetching.
* **Nothing is ever deleted.** `--clean` is gone and `BatchConfig.clean` defaults to `False`.
  The flat layout shares directories across books, so clearing them would destroy *earlier*
  books' covers and reviews. (An earlier version defaulted this ON and ate a previous run's
  output — hence the default, and a regression test.) Re-running is safe because already-answered
  pairs are skipped; see [Skipping what is already scraped](#skipping-what-is-already-scraped).

A guard test (`tests/test_cli_smoke.py::test_the_cli_has_exactly_three_flags`) parses `--help`
and fails if a fourth flag appears, so a setting cannot quietly become a choice again.

**Exit status:** `0` at least one source produced metadata *for at least one ISBN* · `1` none
did · `2` bad usage (a failed ISBN checksum, an unreadable CSV, or a CSV with no usable ISBN in
it). Progress and warnings print to **stderr**, the summary tables to **stdout**, so
`main.py <input> 2>/dev/null` gives you just the report.

The last of those is worth stating plainly: **an empty or wrong-column CSV exits 2, not 1.**
"I read your file and none of it was an ISBN" is a usage problem the caller can fix, and a
script must be able to tell it apart from "I scraped 9 995 books and the sites gave me
nothing".

---

## BookBub, and the render-wait fix it forced

BookBub was **off by default for a while**, and is on again. The episode is kept here because
what it turned up was a general bug, not a BookBub one.

A thread pool recovered idle time but barely moved wall clock, so I profiled a
single 5-source ISBN (`9780821716595`) instead of tuning the pool further. (The pool was
[removed afterwards](#one-isbn-at-a-time-and-why-there-is-no---workers).) The courtesy delay
turned out not to be the bottleneck at all:

| Phase | Time | Share |
|---|---|---|
| Browser rendering | 90.7 s | **85 %** |
| Throttle sleeping | 12.6 s | 12 % |
| Static HTTP requests | 14.9 s | — |
| **Wall** | **106.5 s** | |

Broken down per source, one adapter accounted for three quarters of the run and returned
nothing:

| Source | Time | Result |
|---|---|---|
| goodreads | 4.5 s | 6/7 fields |
| amazon | 5.8 s | 6/7 fields |
| **bookbub** | **73.5 s** | **0/7 — nothing** |
| kobo | 11.2 s | 6/7 fields |
| audible | 3.0 s | 0/7 |

The cause is **selector decay, not the site being slow.** Every BookBub render waits
`RENDER_WAIT_SECONDS = 20` for `[data-book-json]`, and that attribute is no longer on live
book pages — I loaded one directly and confirmed `[data-book-json]` is absent while `h1` and
`og:title` are present. Three renders × 20 s ≈ 64 s of pure timeout, for pages that then
parsed fine from the DOM already sitting there. The wait was never load-bearing: on timeout
the code logged and parsed anyway.

Measured effect of taking BookBub out of the default set at the time:

| Scope | Before | After | Gain |
|---|---|---|---|
| 1 ISBN, sequential | 98.4 s | **33.7 s** | 2.9x |
| 3 ISBNs (then 3 workers) | 4m16s | **51 s** | 5.0x |

**Nothing was ever deleted or commented out** — it was one class attribute,
`enabled_by_default = False`, honoured centrally by `Pipeline.select_sources()`. Once the
render-wait fix below landed, the 20-second timeouts that made BookBub expensive were gone, so
the attribute came off and **BookBub is on by default again**. The opt-out mechanism itself
stays (and is still tested with a stub in `tests/test_source_defaults.py`), because the next
adapter that needs disabling should not have to reinvent it.

What BookBub returns is genuinely thin — title, authors, genres and a blurb; no publisher, no
publication date, no language, and aggregate ratings rather than review text. Those are
[absent from the site](#field-availability), not missed by the parser, and each one is warned
about individually.

### The general fix that came with it

The same rot could hit any adapter, so `HttpClient._await_selector` no longer issues one long
`WebDriverWait`. It polls in 0.25 s steps and, **once `document.readyState` is `complete`**,
gives the selector only `RENDER_SETTLE_SECONDS` (3 s) more before concluding it is not on the
page. A page that is *genuinely still loading* keeps the caller's full `wait_seconds` budget,
so nothing that used to work stops working — only the hopeless case gets cheap.

Verified non-destructive: BookBub on `9780553259100` produced **byte-identical metadata**
before and after (3/7 fields, 6 genres, 340-char blurb) in **25.1 s instead of 57.7 s**. The
`tests/test_source_defaults.py` cases assert both halves — a removed selector gives up fast,
a late-arriving one is still caught, and the caller's timeout stays a hard ceiling.

---

## CSV input: the normal way to run this

```bash
.venv/bin/python main.py 2602101017.csv
```

The file that ships with this project is one column, 10 000 data rows:

```csv
Isbn-13
9780821716595
9780312954154
…
```

but nothing about that shape is assumed. What the reader
(`bookscraper/csv_input.py`) handles, all of it exercised by `tests/test_csv_input.py`:

| Input reality | What happens |
|---|---|
| Header `Isbn-13`, `ISBN`, `ISBN13`, `EAN`, `barcode`, … | Recognised after case and punctuation are stripped, so `Isbn-13` == `isbn13`. |
| No header at all | The first row is detected as data (it parses as an ISBN) and used. |
| A header that names no ISBN column (`first,second,third`) | The column that **actually contains the most valid ISBNs** is used, and the choice is logged. |
| A header that *lies* — `isbn` over empty cells, real ISBNs in `ean13` | The data wins over the header; the discrepancy is logged. |
| the `isbn_column` setting given | Honoured even if it turns out to be the wrong column, so the per-row errors describe *your* choice instead of silently retargeting. |
| ISBN-10s in the column | Converted to ISBN-13 (`0143127551` → `9780143127550`). |
| `978-0-14-312755-0`, `ISBN-13: 978 0 14 312755 0`, quoted cells | Normalised. |
| `;`-, tab- or `|`-separated file | Delimiter sniffed, with a counting fallback for single-column files (which `csv.Sniffer` refuses). |
| UTF-8 BOM, CRLF endings, cp1252 bytes | Handled. A file that is neither UTF-8 nor cp1252 is read as latin-1 **and says so**. |
| Blank lines, `#` comments | Skipped, and not counted as data rows. |
| The same book twice (even as ISBN-10 *and* ISBN-13) | Scraped once; the duplicate is counted and logged. |
| A bad checksum, or the literal text `Invalid ISBN-10` | **Skipped with its physical row number**, never scraped and never silently dropped. |

That last row is the one that matters most on the shipped file, which contains five
`Invalid ISBN-10` cells. They are reported individually:

```
row 1353: 'Invalid ISBN-10' normalised to 'INVALIDISBN10', which is 13 digits but does not
          start with the Bookland prefix 978 or 979, so it is not an ISBN-13
```

…so a 5-in-10 000 data-quality problem is traceable back to the line you can open and fix
rather than showing up as a count. The batch digest repeats them at the end (first ten, then a
count), and all of them are listed in the run's `metrics/` report.

### Check before you commit to 9 995 books

```bash
.venv/bin/python main.py 2602101017.csv --start 0 --end 0
```

```
==============================================================================
DRY RUN  2602101017.csv
==============================================================================
ISBN column     : column 1 ('Isbn-13')
Delimiter       : ','
Header          : Isbn-13
Data rows read  : 10000
Valid ISBNs     : 9995
Rows skipped    : 5
Would scrape    : 9995
Output root     : data
Layout          : data/<isbn13>/book_metadata/...
```

`--start 0 --end 0` opens no socket and writes no file. Use it with the `isbn_column` setting to confirm a
column choice on an unfamiliar export before spending hours of crawl time on it.

### One HTTP client for the whole batch

A batch is not a shell `for` loop around the single-ISBN program, and the difference is not
cosmetic. Per-host courtesy delays, block detection and the Selenium browser all live on one
`HttpClient`; the batch builds **one** and lends it to every book (`Pipeline(..., client=…)`,
which then does not close what it does not own). A process per ISBN would instead:

* reset the per-host delay clock on every book, so five hosts get hit as fast as the loop turns
  — the opposite of the politeness this project documents;
* re-discover each site's block from scratch;
* start and tear down a **browser per book** — seconds of startup, thousands of times over.

### One ISBN at a time, and why there is no `--workers`

Books are scraped **sequentially, in file order**, and each book runs its sources in order.
There is no thread pool and no concurrency inside a run.

That is a deliberate simplification, and it costs almost nothing, because **the per-host
courtesy delay — not the CPU — sets the pace.** One host can only politely be asked so often
however many workers want it, so concurrency cannot make the crawl faster in the way it first
appears to; what a pool recovers is only the time a sequential run spends *idle*, waiting out
one site's delay while the others sit untouched. Measured, when the pool still existed
(4 ISBNs, `--sources goodreads,amazon`):

| Mode | Wall clock |
|---|---|
| 1 worker | 58 s |
| 4 workers | 48 s |

**~1.2x wall clock**, because four workers were queueing on two hosts. For that, the pool
required:

* a locked per-host *reservation* clock, so N threads sleeping "their own" 1–2 s could not
  still land N requests per second on one site;
* an exclusive lock around the single Selenium browser — one browser cannot be driven by two
  threads, and interleaved `driver.get()` / `driver.page_source` calls would hand one book's DOM
  to another book's parser and write the wrong book's data to disk;
* thread-local tracking of which hosts each book had contacted;
* a per-path lock on every accumulating metadata append;
* per-book summary tables captured and replayed under an output lock, because four threads
  printing 30-line tables to one stdout interleave them into gibberish.

All of that is gone. The ordering the design already depended on is now simply true rather than
enforced: Open Library runs before the ISBN-hostile sources so its title/author hint can seed
them, and no two writers ever touch the same path.

**Two ways to overlap work remain, and neither needs threads:**

```bash
# shard the file across terminals -- the ranges tile exactly, no row twice
.venv/bin/python main.py 2602101017.csv --start 0    --end 5000
.venv/bin/python main.py 2602101017.csv --start 5000 --end 10000

# or split the sites, so the two runs barely share a host
.venv/bin/python main.py 2602101017.csv --sources goodreads,amazon
.venv/bin/python main.py 2602101017.csv --sources kobo,audible
```

Both are covered in [Running two commands at once](#running-two-commands-at-once-two-terminals-one-output-tree),
and the reports are named after the sources they cover so concurrent runs do not overwrite each
other's. Note the caveat there: politeness does **not** compose across processes, so two
terminals on the same host hit it at twice the rate.

`--workers` was accepted-and-ignored for a while so old scripts would not break. It is now
gone with the rest of the flags, so `--workers 4` is a plain usage error (exit 2) naming the
three flags that remain. Shard with `--start`/`--end`, or split sites with `--sources`.

### Cleaning up before a run

Cover and review files are numbered from 1 upwards **each run**, so a re-run that finds fewer
artefacts than its predecessor would otherwise leave the predecessor's higher-numbered files
behind, and anything globbing the directory would read a blend of two runs. `Storage.purge`
already handles that per (kind, ISBN, source); cleaning adds the coarser guarantee that a
directory **starts empty**, which also clears artefacts from a source you are no longer
scraping.

Deletion is the one irreversible thing this program does, so **whether it happens by default
depends on whether it can only ever affect the book being scraped**:

| Layout | Targets | Cleans by default? |
|---|---|---|
| Flat (the default) | the five shared artefact directories in `out_dir` | **No** — those hold *every* book, so cleaning them discards earlier runs' work. |
| `BatchConfig(flat=False)` | `out_dir/<isbn13>/` for the ISBNs *this run will scrape* | **Yes** — each target is a directory this run is about to rewrite anyway. |

That first row is a lesson learned the hard way: cleaning was briefly on by default for the
shared layout, and a plain `main.py <isbn>` into `data/` silently deleted a previous run's 139
files. It now requires setting `BatchConfig.clean` in code — there is no CLI flag for it any more —
and even then logs a warning naming what is about to go.
`tests/test_batch.py::test_flat_clean_is_never_the_implicit_default` pins the default
shut.

Note that with the flat layout you rarely want cleaning at all: metadata records are *replaced*
per ISBN rather than duplicated, and every other filename carries its ISBN, so a re-run overwrites
its own files and leaves everyone else's alone.

Cleaning **never** touches:

* an ISBN outside `--start`/`--end` — which is what makes resuming a batch safe;
* files or folders you keep beside the artefact directories (only the five known names go);
* the output root itself, the filesystem root, your home directory, or the working directory or
  any parent of it — those are refused with a message, surfaced in the batch digest rather than
  swallowed;
* `metrics/`, which sits outside the artefact tree and is written at the end of the run.

`clean_targets()` reports exactly which directories a real run would delete.

### Layout: one flat tree, with metadata accumulating

Every book writes into the same five directories — the assignment's four, plus `genres/` for
Task 5. No per-ISBN subdirectories:

```
data/
├── book_metadata/                       # 1 file per source, MANY records each
│   ├── goodreads_metadata.json          #   [ {book 1}, {book 2}, … ]
│   ├── amazon_metadata.json
│   ├── kobo_metadata.json
│   └── audible_metadata.json
├── book_coverpage/                      # <isbn13>_cp_<source>_<n>.jpg
│   ├── 9780821716595_cp_goodreads_1.jpg
│   ├── 9780312954154_cp_amazon_1.jpg
│   └── …
├── book_blurb/                          # <isbn13>_b_<source>_1.txt
├── book_reviews/                        # <isbn13>_r_<source>_<n>.txt
├── genres/                              # <isbn13>_g_<source>_1.txt
└── metrics/                             # see Metrics, below
    ├── <source>_isbns.txt               #   this run's report, per source
    └── <source>_no_data.txt             #   ISBNs the source answered about and lacks
```

Covers, blurbs, reviews and genres all carry the ISBN in the filename, so books cannot collide.
**The metadata filename does not** — `<source>_metadata.json` has no ISBN in it — which is
exactly why that file **accumulates** instead of being overwritten:

```json
[
  {
    "isbn13": "9780821716595",
    "title": "American Rebellion",
    …
  },
  {
    "isbn13": "9780312954154",
    "title": "Once upon a Crime",
    …
  }
]
```

One valid JSON array per source, one record per book, loadable with a plain `json.load()` — and
valid *after every append*, not just when the batch finishes, so you can read it mid-run.

Two things worth knowing about how that is written (`Storage.append_metadata`):

* **It appends in place**, by seeking past the trailing `]` and writing only the new record.
  Re-dumping the whole array per book would be ~60 GiB of writes across a 10 000-ISBN batch and
  would get slower as the file grew; this is ~12 MiB and flat.
* **Re-scraping a book replaces its record** rather than adding a second one. That is not
  hypothetical: an interrupted run re-scrapes the book Ctrl-C landed in, and a plain re-run
  repeats everything. Silent duplicates would corrupt every count taken from
  the file, so a matching `isbn13` is rewritten in place. Three identical runs of four books
  leave four records, not twelve — asserted in `tests/test_storage_metadata.py`.

Within one run the appends happen one at a time, so nothing needs locking; **across two
`main.py` processes** sharing one `out_dir` they do, and an `fcntl.flock` provides it (see
[Running two commands at once](#running-two-commands-at-once-two-terminals-one-output-tree)). A file
damaged by a `kill -9` is moved aside to `<name>.corrupt-N.json` and a fresh array started —
never silently discarded.

`BatchConfig(flat=False)` opts into the older `<out>/<isbn13>/` layout, where each book gets a
self-contained tree.

Two files that used to sit here are gone. `batch_manifest.csv` held one row per (ISBN, source)
with `status`, `fields_found`, `missing_fields`, the artefact counts and the `book_url`; it
duplicated `metrics/book_results.csv` almost column for column, and both are now replaced by the
per-book summary tables printed during the run plus `metrics/<source>_isbns.txt`.
`batch_skipped_rows.csv` listed the CSV rows that yielded no ISBN — a pure function of the input
file, rewritten identically every run, and now reported in the digest instead.

The cost of dropping the manifest, stated plainly: **there is no machine-readable per-pair record
any more.** Which fields a given (ISBN, source) pair produced, and the URL it came from, appear
only in the run's stdout. If you need that back for a pipeline, the manifest is the thing to
re-add — the data is all on `SourceOutcome` already.

### Skipping what is already scraped

**The skip decision has no state of its own: it reads the output.** Before running a source for
a book, the pipeline asks whether `book_metadata/<source>_metadata.json` already holds a record
with that `isbn13`. If it does, the source is skipped; if it does not, the source runs.

```
Already scraped : 2 (ISBN, source) pair(s) skipped: a metadata record was already on disk:
                  goodreads 2
```

The unit is the **(ISBN, source) pair**, not the book: if Goodreads has a record but Kobo does
not, only Kobo runs. To force a re-fetch, delete the source's record from
`book_metadata/<source>_metadata.json` and its line from
`metrics/<source>_no_data.txt`; those two files *are* the skip decision.

Measured on 2 ISBNs: **23 s cold, 0 s warm.** A re-scrape *replaces* the record rather than
adding a second one, so re-running is idempotent.

#### Absence is an answer too, so it is recorded

A metadata record is keyed on the book *existing*, so no shape of "record present"
can mean "this site does not carry it". A source that answered honestly and had no
such book would therefore leave nothing behind and be asked again forever. Measured
on the shipped CSV that is **20 007 of 49 975 pairs**, and re-crawling them costs
**~29 h per full run** of the four default sources (Kobo 24 h, Audible 4.6 h) — 187 h
with BookBub included.

So absence gets its own list, one plain file of ISBNs per source:

```
data/metrics/audible_no_data.txt      8271 ISBNs audible answered about and does not carry
data/metrics/kobo_no_data.txt         3944
data/metrics/bookbub_no_data.txt      7758
data/metrics/amazon_no_data.txt         34
```

The file holds **nothing but ISBN-13s, one per line, sorted** — no header, no comments,
no columns. Reading it is `set(text.split())`, so there is no format to parse or to get
wrong, and hand-editing it is safe.

Between the two, every pair we have an answer for is known:

| Previous outcome | Where the answer lives | Re-run behaviour |
|---|---|---|
| `ok` / `partial` | `book_metadata/<source>_metadata.json` | **Skip** |
| `empty`, and the site was really reached | `metrics/<source>_no_data.txt` | **Skip** |
| `empty`, but we were walled or never got through | nowhere | Re-fetch (correct: we learned nothing) |
| `blocked` — 403 wall or CAPTCHA | nowhere | Re-fetch |
| `error` — the adapter crashed | nowhere | Re-fetch |

On the shipped CSV that covers **45 571 of 49 975 pairs (91 %)**.

Three properties of the list are load-bearing:

* **Only a trustworthy empty is recorded.** `status == "empty"` *and*
  `BookRecord.suspect_empty` false — the site was actually reached, and no host this
  source contacted was walling us. Without that guard this file would recreate the
  failure it is modelled on: 629 WAF-challenged Goodreads books were once written off
  as "not on Goodreads", and every one resolved on a later attempt. Verified: a Kobo
  run under `SETTINGS["browser"] = "never"` gets a 403 wall, produces five empties, and records
  **zero** of them.
* **Merged, never truncated.** `metrics/` is otherwise rewritten whole each run, so a
  `--end 100` slice would leave 100 entries where 10 000 had been. The list is
  re-read under an `fcntl` lock and unioned on write, so a concurrent process's
  additions survive too.
* **Not a report.** `<source>_isbns.txt` is the report; this is state the
  next run's skip decision reads.

An absence is a fact about a date, not a permanent one — Kobo may add an ebook next
year. The file is a plain ISBN list with a `#` header, so deleting a line (or the
file) re-opens that book for checking, and deleting a line from it re-opens that pair.

#### Where this came from

An earlier version kept a `scrape_ledger.jsonl`: one appended JSON line per
*attempt*, read on startup. It cost a second file to keep consistent with the first,
an `fcntl` lock, a JSON-Lines parser, a startup reconciliation that adopted
pre-ledger output, a `SKIPPABLE` set defining which outcomes counted as settled, and
a deliberate divergence in which an untrustworthy `empty` was written to the ledger
as `blocked` so it stayed retryable while the report still showed `empty`. What
replaced it stores **one bit per pair** instead of a status vocabulary and a history,
and the skip decision now reads the same files a human would.

To seed the lists from an old ledger:

```bash
python tools/backfill_no_data.py data/_retired_reports/scrape_ledger.jsonl data --dry-run
python tools/backfill_no_data.py data/_retired_reports/scrape_ledger.jsonl data
```

It carries over only the winning `empty` lines — `ok`/`partial` pairs already have a
metadata record, and `blocked`/`error` told us nothing. No trustworthiness has to be
re-derived, because the ledger writer had already downgraded a suspect `empty` to
`blocked` before writing it.

> **One subtlety worth knowing.** Open Library seeds the title/author that BookBub, Kobo and
> Audible search by — they index no ISBN and *cannot* search any other way. If Open Library is
> skipped, the hint is recovered from its stored metadata record instead, so a resumed run
> produces the same results as a fresh one. That record is also what decided to skip, so the
> hint and the skip now come from the same place. Without it, skipping the seed would silently
> turn Audible's hits into misses — a test asserts it, keyed to `pipeline.SEED_SOURCE` rather
> than to a source name, because that choice has already changed once.

Because these are honest per-pair decisions, a fully-skipped run exits **0**, not 1: the data is
on disk from earlier, so "nothing to do" is success.

### Running two commands at once (two terminals, one output tree)

**Supported, and safe** — including the common case of splitting sources across terminals:

```bash
# terminal 1
.venv/bin/python main.py 2602101017.csv --sources goodreads,amazon
# terminal 2, at the same time
.venv/bin/python main.py 2602101017.csv --sources kobo,audible
```

Verified with two real concurrent `main.py` runs: both metadata files complete, separate
reports.

This needs a lock that spans processes — the only concurrency this project still has, and so the
only lock left in it. `append_metadata` is a read-modify-write (find the trailing `]`, truncate, write), so
two processes read the same tail offset and each truncated away the other's record. Measured
before the fix: **40 of 80 records vanished, and the file still parsed** — no crash, no warning,
just half the data gone. An `fcntl.flock` on a sidecar `.lock` file now serialises the whole
sequence across processes; `tests/test_multiprocess.py` spawns real subprocesses and fails
without it.

| Path | Two processes | Why |
|---|---|---|
| `book_metadata/<source>_metadata.json` | safe | Per source, and file-locked for the same-source case |
| covers / blurbs / reviews / genres | safe | ISBN **and** source are in every filename |
| `metrics/` | safe | Every filename carries its source, so two runs on different `--sources` touch different files |

That last row is why `--sources goodreads` writes `metrics/goodreads_isbns.txt` while
`--sources kobo` writes `metrics/kobo_isbns.txt` — one directory, no collision. Two runs
covering the *same* source do overwrite each other's report; split by source (or by
`--start`/`--end`) and they never overlap.

Two caveats worth stating:

* **Politeness does not compose across processes.** Each process has its own `HttpClient` and so
  its own per-host delay clock. Two terminals scraping Goodreads hit it at *twice* the rate one
  would — the delay is enforced per process, not per machine. If you split by source (the case
  above) the hosts barely overlap and this is a non-issue; if both runs cover the same sources,
  raise the `min_delay`/`max_delay` settings or just use one command.
* **The same ISBNs and the same sources in both terminals is just duplicated work.** Nothing
  corrupts, but they will race to scrape the same pairs. Split by `--sources` (or by `--start`
  / `--end`) so the two runs do not overlap.

`fcntl` is POSIX-only. A single command is unaffected on any platform, since it writes one
record at a time; on Windows, concurrent *processes* sharing one `out_dir` are not protected.

### Interrupting and resuming

Artefacts are written as each ISBN completes and `Ctrl-C` is caught, so an interrupted batch
keeps everything already finished and tells you how to continue:

```
Interrupted     : re-run with --start 1200 to continue where this stopped
```

The resume point is the **first ISBN that did not complete**, which running one book at a time
makes simply the book Ctrl-C landed in: everything before it finished, and everything after it
never started. That book is re-scraped rather than assumed done, which is the right trade
against silently leaving a hole in the output. The tests assert that no un-scraped ISBN is ever
before the resume point, and that every ISBN ends up either completed or reported as not
attempted — never both, never neither.

`--start N` / `--end N` partition the ISBN list without dropping or repeating one. One book
crashing unexpectedly costs that book only: the failure is reported with its exception in the
digest and the batch moves on (`continue_on_error=False` would stop it instead).

### The batch digest

Per-ISBN summary tables still print — they are the per-book record — but 10 000 of them are
unreadable alone, so the run closes with:

```
==============================================================================
BATCH SUMMARY
==============================================================================
ISBNs scraped   : 3
  with metadata : 3
  empty/failed  : 0
CSV rows skipped: 5 (no usable ISBN)
Not attempted   : 9992 (outside --start/--end)
Files written   : 21
  reviews       : 9
  covers        : 0
Elapsed         : 1m41s
  per ISBN      : 34s average

SOURCE          METADATA   REVIEWS   COVERS
-------------------------------------------
amazon               3/3         3        0
audible              0/3         0        0
bookbub              1/3         0        0
goodreads            3/3         6        0
kobo                 1/3         0        0
```

The per-source roll-up is the number that tells you a **selector has rotted**: `goodreads 0/500`
is unmistakable, whereas one book's empty result never distinguishes a broken parser from a
book the site does not stock.

> **Crawl time is the real constraint.** The per-host courtesy delay, not the CPU, sets the
> pace, which is why there is no `--workers` any more: concurrency could not raise the rate any
> single site sees without making the crawl impolite. All 9 995 rows of the shipped CSV is a
> multi-day crawl. Use `--end` while iterating, `--sources` to narrow the sites, `--start` to
> resume, and two terminals on disjoint shards or sources if you need overlap. Raising the `min_delay`/`max_delay` settings is the polite
> direction to move; lowering them is not.

---

## Metrics

One directory, `<out>/metrics/`, holding two files per source:

| File | Kind | Lifetime |
|---|---|---|
| `<source>_isbns.txt` | **report** — which ISBNs that source delivered and which it did not, with a reason per miss | rewritten each run |
| `<source>_no_data.txt` | **state** — the known-absent list the skip decision reads | merged across runs |

Both are keyed by source **in the filename**, and that is what lets one directory serve every
run. There used to be a `metrics.goodreads/` / `metrics.amazon/` split, because a single
`isbns_by_source.txt` bundling all five sources would be overwritten by the next run covering
different ones. Splitting the report per source removes the collision, and the whole suffix
concept went with it: two terminals running `--sources goodreads` and `--sources kobo` into one
`out_dir` now touch disjoint files by construction. (Two runs covering the *same* source still
overwrite each other's report — last writer wins, as before. The `_no_data` list is safe either
way: it merges under an `fcntl` lock.)

The report answers what the per-book tables cannot: across the whole file, which ISBNs did this
source deliver, and which did it not. The directory sits outside the artefact tree, so cleaning
never touches it.

`<source>_isbns.txt` is the report. It is **not** the same as `<source>_no_data.txt`, which is state
rather than a report. See [Absence is an answer too](#absence-is-an-answer-too-so-it-is-recorded).

`metrics/kobo_isbns.txt`:

```
==============================================================================
kobo: 1 of 3 succeeded (33%), avg 22.2s per book
==============================================================================

FAILED (2):
  9780553259100  [empty]  no metadata parsed from the page

NOT TRUSTWORTHY (1) -- reported empty, but the site was walling us
or was never successfully reached, so "no such book" is not a finding.
These are not recorded in kobo_no_data.txt, so they are checked again:
  9780312954154  walled by www.kobo.com

SUCCEEDED (1):
  9780821716595
```

Two distinctions in there carry the weight:

* **`[empty]` vs `[blocked]`.** `empty` means the site answered and genuinely does not carry the
  book — Kobo sells ebooks and Audible sells audiobooks, so a print-only 1980s paperback
  legitimately is not there. `blocked` means we were walled. Only the second is a problem you can
  act on, and a report that merged them would send you hunting for a parser bug that does not
  exist.
* **`NOT TRUSTWORTHY`.** An `empty` recorded while a host this source actually contacted was
  walling us — or with no host reached at all — is not a statement about the catalogue, and is
  listed separately rather than counted as a finding.

The **per-source roll-up in the batch digest** is what tells you a selector has rotted
(`goodreads 0/500` is unmistakable). Earlier versions also wrote `summary.txt`, `requests.csv`,
`errors.csv`, `book_results.csv`, `failures_by_source.txt` and `metrics.json` here, plus a
`batch_manifest.csv` beside them; those were request-level diagnostics from while the scraper was
being built, plus the same per-pair numbers in three more shapes. They are gone — this file
answers the per-source question and the digest carries the totals.

> **A report failure never costs you a scrape.** Every writer degrades to a warning, and
> recording one result is a list append. If `metrics/` cannot be created the run continues and
> says so.

---

## Output layout

Whether you scrape one ISBN or 10 000, this is the tree (with `BatchConfig(flat=False)`, it appears
inside each `<isbn13>/` instead):

```
data/
├── book_metadata/                       # 1 file per source, a record per book
│   ├── goodreads_metadata.json
│   ├── amazon_metadata.json
│   ├── kobo_metadata.json
│   ├── audible_metadata.json
│   └── bookbub_metadata.json            # only with --sources ...,bookbub
├── book_coverpage/                      # <isbn13>_cp_<source>_<n>.jpg   (n is 1-based)
│   ├── 9780143127550_cp_goodreads_1.jpg … _6.jpg
│   ├── 9780143127550_cp_amazon_1.jpg … _2.jpg
│   └── …
├── book_blurb/                          # <isbn13>_b_<source>_1.txt
│   └── 9780143127550_b_goodreads_1.txt
├── book_reviews/                        # <isbn13>_r_<source>_<n>.txt  (one file per review)
│   ├── 9780143127550_r_goodreads_1.txt … _30.txt
│   └── …
└── genres/                              # <isbn13>_g_<source>_1.txt   (one genre per line)
    └── 9780143127550_g_goodreads_1.txt
```

Numbering is **1-based and contiguous** — a failed download or write never leaves a gap,
because the index is a write counter rather than a loop index. Every path component is
sanitised, so a hostile source name or ISBN can never escape the output root. All text files
are UTF-8 with `\n`-only line endings, on every platform. Cover extensions come from the
image's real **magic bytes** (then `Content-Type`, then the URL), so a PNG served from a
`.jpg` URL is still named `.png`.

### `book_metadata/<source>_metadata.json`

A JSON **array**, one element per book scraped from that source. Each element has exactly these
keys, in this order:

```json
{
  "isbn13": "9780143127550",
  "title": "Everything I Never Told You",
  "authors": ["Celeste Ng"],
  "publisher": "Penguin Books",
  "origin": null,
  "date_of_publication": "2015-05-12",
  "language": "English",
  "genre": "Fiction, Book Club, Contemporary, Mystery, …"
}
```

* `genre` is the **comma-separated string** the assignment asks for
  ("Genre, (Separated by comma)"), and is `null` when no genres were found. For the list form,
  either `genre.split(", ")` or read `genres/<isbn13>_g_<source>_1.txt`, which holds one per line.
* Missing scalars are JSON `null` — never the string `"None"`, never omitted.
* The **source is the filename** (`goodreads_metadata.json`), so it is not repeated in every
  record.

`tests/test_storage_metadata.py::test_the_record_holds_exactly_the_contract_keys` pins this key
set and order, so drift in either direction fails the suite.

#### What used to be here, and where it went

Earlier versions also wrote `genres` (list), `_source`, `_source_url`, `_scraped_at`,
`_edition_isbn13`, `_edition_matches_requested` and `_warnings`. They are gone — on a
10 000-book file the `_warnings` arrays alone dominated the payload. Nothing is lost that is not
recorded elsewhere:

| Was | Now |
|---|---|
| `_warnings` | The run's logs and the per-source summary table |
| `_source` | The filename |
| `genres` (list) | `genre.split(", ")`, or `genres/<isbn13>_g_<source>_1.txt` |
| `_scraped_at`, `_source_url` | The logs, and the `url` line in each source's summary block |

One real cost is worth stating: `_edition_isbn13` / `_edition_matches_requested` made an
**edition mismatch machine-readable**. `isbn13` is always the ISBN you asked for, but Kobo sells
the ebook and Audible the audiobook, so `publisher`, `date_of_publication` and `language` can
describe a *different* printing. A consumer reading only this file can no longer detect that; it
is still reported in the logs and in the summary's warning rows. Nothing on disk exposes it
programmatically any more.

To bring files written by an older version into this shape:

```bash
python tools/strip_metadata_keys.py data --dry-run   # report what would change
python tools/strip_metadata_keys.py data             # rewrite, backing each file up
```

It backs every file up to `<name>.pre-strip.json` first, refuses to write output it cannot
re-parse, and preserves every surviving value byte-for-byte (verified across 1 014 records: 0
differences).

### Why one metadata file *per source*, holding every book

The assignment names the file `book_metadata/<source>_metadata.json`, with no ISBN in it. This
implementation honours that filename literally — and resolves the obvious tension it creates.

With one book per file, scraping a second ISBN would overwrite the first book's record while its
ISBN-named cover/blurb/review/genre files accumulated around it: you would end up with one book's
metadata and 10 000 books' reviews. So the file holds a **JSON array with one record per book**,
appended to as the run goes. The mandated filename is kept, no book clobbers another, and a
consumer that wants a single book just filters on `isbn13`.

The alternative — a directory per ISBN — is still available via `BatchConfig(flat=False)`, but it is no
longer the default, because it answers a question the accumulating file no longer asks.

---

## Underscores vs spaces: `filename_style`

The assignment PDF renders the required paths as `book metadata/`, `book coverpage/` and
`<isbn13> cp <source> <n>.jpg` — with **spaces**. That is a LaTeX artefact: an unescaped `_`
is swallowed by TeX, so `book_metadata` *prints* as `book metadata`. A file literally named
`goodreads metadata.json` would be bizarre, and spaces in paths break naive shell pipelines
downstream.

So this project **defaults to underscores** and offers the spaced form as an explicit opt-in:

```bash
# set SETTINGS["filename_style"] = "space" in main.py, then:
.venv/bin/python main.py 9780143127550
# -> "book metadata/goodreads metadata.json"
# -> "book coverpage/9780143127550 cp goodreads 1.jpg"
# -> "book reviews/9780143127550 r goodreads 1.txt"
```

Both styles come out of the same code path and both have been verified byte-for-byte. If a
marker insists on the PDF's literal form, pass the flag; nothing else changes.

## Why five directories when the brief lists four

Section 4 of the assignment enumerates only `book metadata/`, `book coverpage/`,
`book blurb/` and `book reviews/` — it **omits a directory for genres** — while Task 5
explicitly requires scraping the book's genres. Rather than silently drop a required task or
bury genres inside the metadata file only, this implementation creates **all five**
directories: the four named in section 4, plus `genres/` holding
`<isbn13>_g_<source>_1.txt` with one genre per line. The genres are *also* in the metadata
JSON as both `genre` (comma-separated string) and `genres` (list), so whichever shape a
marker expects is there.

---

## Per-source capability matrix

Measured on a real run of `9780143127550` (Celeste Ng, *Everything I Never Told You*).
"Task" numbers are the assignment's five tasks.

| Source | 1. Metadata | 2. Covers | 3. Blurb | 4. Reviews | 5. Genres | Needs a browser? |
|---|---|---|---|---|---|---|
| **openlibrary** | 5/7 fields | 1 | *none* † | **0** (real ceiling ‡) | 39 | No |
| **goodreads** | 6/7 fields | **6** (multi-edition) | 749 chars | **30** | 10 | No |
| **amazon** | 6/7 fields | 2 | 749 chars | **13** (hard ceiling ‡) | 6 | No |
| **bookbub** | 3/7 fields | 1 | 1446 chars | **0** (real ceiling ‡) | 18 | **Yes** |
| **kobo** | 6/7 fields | 2 | 1446 chars | **48** | 3 | **Yes** |
| **audible** | 6/7 fields | 2 | 1446 chars | **25** | 10 | No |

All six are on by default.

† Open Library is a **bibliographic catalogue, not a storefront**, and that shapes its row in
both directions. It is the only source that answers from the ISBN alone in a single documented
JSON request — no HTML, no browser, no title-matching guesswork — which is why it seeds the
title+author the storefronts are searched with. But it publishes **no reader reviews at all**,
and carries a description on only some editions (none on this one). Its 39 "genres" are Open
Library *subject headings*, which mix real genres ("Fiction") with topical ones ("Grief") and
Library-of-Congress strings; they are reported as found, with a warning saying exactly that,
because trimming them would mean inventing a taxonomy Open Library does not publish. Its two
missing metadata fields here are `language` (absent from this edition record) and `origin`.

The missing 7th field is `origin` in every row, on every ISBN scraped so far: **no source
published a place of publication on any run**, so the observed ceiling is 6/7.
(BookBub's 3/7 additionally reflects the publisher, date and language it genuinely does not
publish.) The field is not wired shut, though — every run searches for it, and a run would report
7/7 the moment a search succeeded. The most likely place for that to happen is Amazon's "Country
of Origin" detail bullet, which Amazon prints on physical-goods listings but not on the book pages
seen here. Nothing about the field is hard-coded, and nothing about it comes from off-storefront
data.

> ### ‡ Task 4 (≥25 reviews per source) is met by 3 of the 5 sources. This is a hard limit.
>
> | Source | Reviews | Why |
> |---|---|---|
> | goodreads | ~30 ✅ | fine |
> | kobo | 10–100 ✅ | fine (varies with the edition's review count) |
> | audible | 25 ✅ | fine |
> | **amazon** | **13, always** ❌ | Only the reviews embedded in the detail page are reachable. `/product-reviews/<asin>` and `/portal/customer-reviews/<asin>` both redirect to the `/ax/claim` sign-in wall, in a real browser too. Exceeding 13 requires an **authenticated session**, which this scraper deliberately does not create. The supported route is Amazon **PA-API 5**. |
> | **bookbub** | **0, always** ❌ | BookBub exposes only aggregate ratings (`averageRating` / `ratingsCount`) to anonymous clients. Member review *text* is behind sign-in. |
>
> Both shortfalls are warned per run and are **not** padded from another site. Total review
> yield is therefore ~116 files, not the ~125 the task implies.

### Which fields each source can actually deliver

| Field | goodreads | amazon | bookbub | kobo | audible |
|---|---|---|---|---|---|
| title | yes | yes (verbatim, incl. publisher SEO stuffing) | yes | yes (+ subtitle) | yes |
| authors | yes | yes | yes | yes | yes (narrators kept **out**) |
| publisher | yes | yes | **never published** | yes (ebook imprint) | yes (**audio** imprint) |
| origin | **`null`** — searched for, not found; `work.details.places` is the story's *setting* | **`null`** — the "Country of Origin" bullet is looked up in both detail layouts but book pages omit it (physical goods have it) | **`null`** — no such detail row and no such key; a deals site, not a bibliographic database | **`null`** — the only country data found is `eligibleRegion`/`ineligibleRegion`, i.e. sales-territory geo-licensing | **`null`** — the only country data found is `regionsAllowed` (distribution allowlist), `priceCurrency` and `#reviewsCountry` (the storefront) |
| date_of_publication | yes (this edition) | yes (this printing) | **never published** | yes (**ebook** release) | yes (**audio** release) |
| language | yes (edition language) | yes | **`null`** — BookBub has no per-book language field | yes | narration language |
| genres | yes (10) | browse nodes — noisy | yes (18, marketing tags filtered) | yes (3, coarse) | yes (10, `/tag/<kind>/` classified) |

<a id="field-availability"></a>
#### `origin`: searched for on every source, published by none

`origin` is a **storefront-only** field. The assignment requires every value to be extracted
programmatically from the live page, so a place of publication fetched from a bibliographic API
would not be Goodreads/Amazon/BookBub/Kobo/Audible data at all — it would be a sixth source
wearing their name in the `_source` key. This project therefore attempts a real extraction on
each site and, when the search comes back empty, writes `null` plus a warning that says exactly
what was searched. An honest `null` beats a plausible value with a false provenance.

**The warning is derived, not asserted.** One shared probe does the looking for all five
adapters:

```python
BaseSource.probe_origin(layers: Sequence[Tuple[str, Any]]) -> Tuple[Optional[str], List[str]]
```

Each adapter hands it `(name, payload)` pairs for the layers it has *already parsed on this run* —
JSON blobs and `bs4` DOM/meta fragments alike — and gets back the value found (or `None`) together
with **the list of layers it actually searched**. That list is what the "layers searched" clause in
every `origin` warning is built from, so a layer that was missing from the page is never claimed as
searched, and the sentence cannot drift away from the code. `probe_origin_detail()` additionally
reports *where* a hit came from, so a found value can be warned with its provenance. The probe
recognises these spellings, case- and separator-insensitively (`countryOfOrigin`,
`country_of_origin` and `Country of Origin ‏ : ‎` are one entry):

`placeOfPublication`, `publicationPlace`, `publishPlace`, `publish_places`, `placePublished`,
`countryOfOrigin`, `country_of_origin`, `publicationCountry`, `publish_country`, and the visible
labels `Place of publication`, `Country of origin`, `Country/Region of Origin`, `Published in`.

Matching is exact on the folded token and never a substring, which is why `eligibleRegion`,
`ineligibleRegion`, `regionsAllowed`, `priceCurrency` and `datePublished` cannot be mistaken for an
origin. The walk is bounded in depth and node count, is cycle-safe, and never raises — an
unexpected blob degrades to "searched, found nothing" instead of taking a scrape down
(`tests/test_origin_probe.py`).

What each adapter searches, and what it refuses to use:

| Source | Searched (per run, reported by the probe) | Rejected, and why |
|---|---|---|
| goodreads | `__NEXT_DATA__` Apollo cache work record, book record (`book.details`), JSON-LD `Book` blocks, book DOM + `og:`/meta, and any already-fetched legacy `/work/editions/` listing — all parsed for the other fields | `work.details.places` — that is the story's **setting** (for this ISBN, "Ohio"), not where the book was published |
| amazon | the `"Country of Origin"` / `"Country/Region of Origin"` label in the **merged** detail map (`#detailBullets_feature_div` bullets **plus** `Product details` table rows — see below), then the shared probe over that map and the page DOM | `#glow-ingress-line2` — the **delivery** locale (where Amazon would ship a copy), quoted in the warning as read from the page |
| bookbub | every `ORIGIN_LABELS` detail row (`country`, `country of origin`, `place of publication`, `published in`, `origin`) in the parsed detail panel, then the shared probe over the detail rows, the `data-book-json` payload and the page DOM | nothing to reject; the panel is read and simply has no such row — and "panel read, no row" is warned **differently** from "panel not found", so selector decay is never reported as a fact about BookBub |
| kobo | the `data-kobo-gizmo-config` `googleBook` blob, its `workExample` record, the `googleProduct` blob, the injected `ld+json`, the `bookitem-secondary-metadata` DOM rows and the page DOM + `og:`/meta | `potentialAction.expectsAcceptanceOf.eligibleRegion` / `.ineligibleRegion` — **sales-territory geo-licensing**, i.e. where the ebook may be *sold*. They hang off a *priced* `Offer` inside the checkout `ReadAction`; the adapter locates that path and counts the entries per run, so the warning quotes a measured count, never a remembered one |
| audible | the `Audiobook`/`Product` JSON-LD, the embedded component JSON, and the page DOM (`adbl-metadata` slots, `og:`/meta) | whichever country-shaped fields *that run* finds: `regionsAllowed` (a distribution **allowlist** of ISO codes, counted per run), `priceCurrency` and `#reviewsCountry` (the storefront this run forced with `overrideBaseCountry`) |

Amazon's merge is part of this honesty: the `Product details` table used to be consulted **only**
when `#detailBullets_feature_div` was missing, so "looked in both layouts" was false whenever the
bullet list parsed — a `Country of Origin` row printed only in the table would have been on the page
and reported as `null`. Both layouts are now always parsed and merged into one map (bullet list wins
on conflict, so no other field can change value; the table can only add labels), which also makes
`publisher`, `language`, `date`, `ISBN` and page count more robust.

And universally: **`origin` is never inferred from the publisher's imprint**, the storefront
locale or the delivery country. "Penguin Books" is a company, not a place; inferring "London" or
"New York" from it is a guess, not a scraped value. Earlier revisions of this project got this
wrong twice — first a hostname→country table that emitted the literal `"United States"` for every
book on amazon.com, then an off-storefront edition lookup that put another site's data in a
storefront's record. Both are gone. `origin` is `null`, with reasons.

## How a book is resolved

An ISBN is not a universal key. Only three of the six sources can be looked up by one:

| | Looked up by ISBN | How the book is found |
|---|---|---|
| **openlibrary** | yes | `/api/books?bibkeys=ISBN:<isbn>` — a documented JSON endpoint |
| **goodreads** | yes | `/book/isbn/<isbn>` redirects to the book page |
| **amazon** | yes | the ISBN is a valid ASIN for most print editions |
| **bookbub** | **no** | publishes no ISBN at all; slug built from title + author |
| **kobo** | **no** | indexes the EAN of *its own* EPUB, not arbitrary print ISBNs |
| **audible** | **no** | indexes ASINs of audiobook editions; title + author search |

So the bottom three need a title and an author before they can search at all, and the pipeline
gets that from **one** place: Open Library runs first (`pipeline.SEED_SOURCE`) and its
title/author is merged into a shared `BookHint` that the rest of the run reads.

**This used to be Goodreads' job**, with Open Library as a per-adapter fallback when Goodreads
had not run. That was worse in two ways: the storefronts' hit rate depended on whether Goodreads
happened to be selected, and the fallback existed as **three near-identical private copies**
(`kobo.py`, `audible.py`, `bookbub.py`) that had quietly drifted apart — only BookBub's tried
`/search.json`, only Audible's passed the ISBN-10 too, only BookBub's kept the subtitle. There
is now one `BaseSource.seed_from_openlibrary()`, which is the union of all three, so every
adapter gets the best of what any one of them had.

Checked before switching, rather than assumed. For 40 books where Kobo or Audible had already
matched a product, both candidate seeds were scored against the title the store actually
returned, using the adapter's own scorer and its own 0.85 floor:

| Seed | Usable | Had a title at all |
|---|---|---|
| Goodreads | 38/40 | — |
| **Open Library** | **39/40** | **40/40** |

Open Library was not worse; it was marginally better (one book scored 0.65 from Goodreads and
0.90 from Open Library). The single below-floor case failed identically for **both** seeds, so
it is a pre-existing store mismatch, not a regression.

**No field is ever populated from the seed.** The seed is a search string and nothing else.
Every value in a storefront's record — title, authors, publisher, date, language, genres, blurb,
reviews, covers — is parsed out of that storefront's own page, and `origin` is left `null`
rather than filled in from Open Library. Open Library's *own* record is written to
`openlibrary_metadata.json`, separately, like any other source's.

### Honest caveats, per source

**Goodreads** — the richest source, and the one that seeds `title`/`authors` for the
ISBN-hostile sources, so the pipeline always runs it **first**. Primary path is the page's own
`__NEXT_DATA__`/Apollo cache; DOM, JSON-LD, `og:` meta and the legacy `/work/editions/` page
are layered fallbacks. Publisher, edition date and language do **not** exist in the book
page's DOM at all. `origin` has come back `null` on every run: the probe searches the Apollo cache,
the JSON-LD, the DOM and any fetched editions listing, and the one field that looks like an answer,
`work.details.places`, is the story's *setting* (for this ISBN, "Ohio") — reported as its own note
and deliberately not used. Goodreads is now behind
AWS WAF and intermittently answers **HTTP 202 with a JavaScript challenge**; that signature is
detected and reported, and recovered through the browser when one is available — never solved.
Beyond the 30 reviews embedded in the page, deeper pagination uses Goodreads' own anonymous
GraphQL endpoint, whose endpoint and API key are **re-derived at runtime** from the page's own
JS bundle (Goodreads rotates that key, so nothing is hardcoded); those reviews carry no
permalink. Goodreads' ToS prohibits automated collection — robots.txt permission is not
consent. `/search` is robots-*disallowed* and is only ever used as a last-resort fallback,
loudly warned.

**Amazon** — treat as **best-effort enrichment, never a required source**. Its Conditions of
Use forbid scraping, and its detail-page layout is A/B tested constantly, so selector
longevity is low. **13 reviews is the hard anonymous ceiling**: both documented review-listing
routes redirect to the `/ap/signin` wall and the XHR endpoints answer 401/404, so exceeding it
would need an authenticated session — deliberately not attempted, even with a real browser
(verified: the browser hits the same wall, so it is an *auth* wall, not a rendering problem).
`origin` is the one field Amazon could in principle supply — its physical-goods listings carry a
"Country of Origin" bullet, which is looked up in the merged map built from **both** detail layouts
and used verbatim when present — but the book pages seen here omit it, so the field comes back
`null`; the `#glow-ingress-line2` delivery locale is quoted in the warning as read from the page and
**never** substituted for it. Genres are publisher-chosen browse nodes, weak.
`date_of_publication` is *this printing's* date. Cover 2 comes from a sibling print edition
with a different ISBN, warned per cover. The Akamai `bm-verify` interstitial is detected,
backed off from, and never solved. Which reviews Amazon embeds is geo-localised by egress IP,
so the mix (and their language) varies run to run.

**BookBub** — an ebook *deals* site, not a bibliographic database. It publishes **no ISBN
anywhere**, so matching is title+author only and every acceptance is reported with its
similarity scores; a different *edition* of the right book can legitimately win. It publishes
**no publisher, no publication date and no place of publication** — those are genuinely absent
from the site, not selectors we failed to find — and the adapter distinguishes "the detail panel
was read and has no such row" from "the detail panel itself could not be found", so a CSS rename
upstream is reported as selector decay rather than as a property of BookBub. `language` is
emitted as **`null`**: `<html lang="en-US">` describes the storefront, not the book, and
reporting it would have made every book "en" forever. **Reviews: 0, and 25 is unreachable** —
only aggregate ratings are exposed anonymously (this run: average 4.20 from 2,148 ratings) and
review text is behind sign-in. The count is reported honestly and padded with nothing. BookBub's
`description` field is also sometimes duplicated ~2.6x upstream, restarting mid-word; that is
detected, truncated at the first repetition, and named in a warning.
Requires a browser; slow (~10-25 s per page).
Cloudflare's aggressiveness is IP/ASN/geo-sensitive, so a static probe runs first to *prove*
the block rather than assume it.

**Kobo** — `www.kobo.com` returns HTTP 403 + `cf-mitigated: challenge` to plain `requests`
regardless of headers (it is a TLS/JA3 fingerprint check), so a browser is mandatory; the
sibling hosts `ratingsapi.kobo.com` and `cdn.kobo.com` are not gated and are used directly.
Kobo indexes the EAN of **its own EPUB edition**, not arbitrary print ISBNs, so discovery
usually falls back to a title+author search — and that search path is robots-*disallowed*
(the `respect_robots` setting correctly blocks it). Everything it reports therefore describes the
**ebook** edition: for this ISBN Kobo's own ISBN is `9781101634615`, surfaced in the warnings.
`origin` comes back `null`: the shared probe searches every layer this run parsed (the warning lists
them), and the only country data it finds is `potentialAction.expectsAcceptanceOf.eligibleRegion` /
`.ineligibleRegion` on a priced `Offer` — storefront sales licensing, not a place of publication —
whose path and entry count the adapter measures per run and names in the warning as discarded. Genres are coarse (3). Slowest source:
roughly a minute per book. `SEED_HINT_FROM_OPEN_LIBRARY = True` (module constant) lets a
standalone Kobo run resolve the ISBN to a **search query** via openlibrary.org — only the query;
every reported field is still parsed from Kobo, and no field (least of all `origin`) is ever
taken from openlibrary.org. Set it to `False` for a strictly single-site adapter.

**Audible** — ISBN-hostile: it indexes ASINs and has no ISBN lookup, so discovery is a
title+author search seeded by Goodreads (or, standalone, by Open Library purely to build the
query — no Open Library value ever enters the record). The wrong-book guard is load-bearing:
Audible also carries an *Everything I Never Told You* by a different author, correctly
rejected. Everything it reports describes an **audiobook edition**: `publisher` is the audio
imprint (`Penguin Audio`), `date_of_publication` is the audio release date, `language` is the
narration language. Narrators are extracted but never merged into `authors`. `origin` comes back
`null`: the shared probe searches the JSON-LD, the embedded component JSON and the page DOM (the
warning lists whichever of those the run actually parsed), and the country-shaped fields it does
find — `regionsAllowed` (a distribution allowlist, counted per run), `priceCurrency` and
`#reviewsCountry` (the storefront) — are located on the page as the warning is built, then named and
rejected. Reviews are
request-bound: the XHR `pageSize` is capped at 5 server-side, so
25 reviews cost 5 requests; a transient HTTP 503 leaky-bucket throttle stops pagination early
with an honest count. From a non-US IP, `www.audible.com` silently geo-redirects; that is
defeated with `ipRedirectOverride`+`overrideBaseCountry` and asserted on the final response URL.

### Known limitations that apply everywhere

* **`origin` came back `null` on every source and every ISBN scraped so far.** Each adapter runs a
  real search each run and logs which layers the probe reports having searched
  and which look-alike value it refused (setting, delivery locale, sales territory, distribution
  allowlist) — every one of those located on the page as the sentence is built. It is never
  inferred from the marketplace, the storefront locale or the publisher's imprint, and it is never
  back-filled from a bibliographic API — the field would then describe a host that is not the
  `_source`. So 6/7 is the observed ceiling, not a wired-in one: a run reports 7/7 as soon as a
  search succeeds.
* **`language` is the language of the *edition/narration*, not necessarily of original
  composition.** No source publishes "language the work was written in". Where script
  detection suggests a translation, that is warned rather than corrected.
* **Edition drift.** Only Goodreads and Amazon can confirm the exact ISBN. Kobo sells one
  EPUB per work, Audible one audiobook, BookBub one record per deal — so publisher, date and
  cover may belong to a *different* edition of the same work. Every such case is warned **and**
  reported in the run's logs and the summary's warning rows (it used to also be exposed as
  `_edition_isbn13` / `_edition_matches_requested` in the JSON; those keys were removed).
* **Re-running is idempotent per (ISBN, source).** Reviews and covers are numbered from 1 each
  run, so the previous run's higher-numbered files are deleted before the new set is written —
  otherwise a throttled run that collected 5 reviews would leave 25 stale files behind that a
  directory glob would read back as real data.
* **Review counts are site ceilings, not effort ceilings.** BookBub 0 and Amazon 13 cannot be
  raised without authenticating, which this project refuses to do. the `min_reviews` setting therefore
  warns rather than failing.
* **Review text is user-generated work owned by its authors.** Full bodies are stored here for
  the educational purpose of the exercise; for anything public, store an excerpt plus the
  permalink instead.
* **Selector longevity is low** on Amazon, Kobo and BookBub. Every parse step is layered and
  warns when it falls back, so decay shows up as warnings rather than as silently wrong data.
* **Blurbs can be byte-identical across sources.** For this ISBN, Kobo and Audible return the
  same 1,447-character publisher description. That is the same marketing copy distributed to
  every retailer, independently parsed each time — not a shared cache.
* **Re-running for a second ISBN overwrites `<source>_metadata.json`** (see above).

---

## What a run looks like

The pipeline prints one summary table plus a per-source detail block. `STATUS` is
`ok` / `partial` / `empty` / `blocked` / `error`; `FIELDS` is found/total metadata fields.
Almost every honest run is `partial`, because no storefront has yet answered the `origin` search and
the caveats are real.

```
==============================================================================
SCRAPE SUMMARY  9780143127550  (978-0-14-312755-0)  'Everything I Never Told You'
==============================================================================
SOURCE     STATUS   FIELDS  COVERS  REVIEWS  GENRES  BLURB  WARN
----------------------------------------------------------------
goodreads  partial     6/7       6       30      10    749     3
amazon     partial     6/7       2       13       6    749     7
bookbub    partial     3/7       1        0      18   1446     9
kobo       partial     6/7       2       48       3   1446     7
audible    partial     6/7       2       25      10   1446     5

Hosts that blocked automated access (not circumvented):
  - www.bookbub.com: anti-bot interstitial detected (matched 'just a moment...', body only 6187 chars)
  - www.kobo.com: HTTP 403 wall

Output root : /Users/…/book-scraper/data
Files written: 144
Naming style: underscore (e.g. book_metadata/goodreads_metadata.json)
```

One source failing never stops the others: **both** the adapter call and the persistence step
are wrapped, and a crash becomes an `error` row rather than a traceback. `COVERS` shows
`written/seen` when they differ (for example `0/1` under `download_covers=False`).

`WARN` varies between runs because the warnings describe **what that run observed** — the run
above did not hit Goodreads' AWS WAF challenge, so those warnings (and the third blocked-host
line) are absent; the previous run did. The same is true of the `origin` warnings: the layer list,
the region counts and the delivery locale they quote are measured on that run, so they change with
the page instead of describing it from memory. The `6/7` columns have been stable across every ISBN
tried, because the missing field is always `origin` — but they are a measurement too, and a run
prints `7/7` the moment a search finds a place of publication.

---

## Architecture

```
main.py                     CLI: CSV-or-ISBN routing, the 3 flags, SETTINGS
bookscraper/
  models.py                 BookHint, ReviewItem, BookMetadata, ScrapeResult (+ the JSON contract)
  isbn.py                   normalise / validate / convert / hyphenate ISBNs (pure stdlib)
  metrics.py                RunReport: collects each (ISBN, source) outcome and writes
                            metrics/<source>_isbns.txt. NoDataIndex: the durable
                            per-source lists of ISBNs a site answered about and does
                            not carry -- the half of the skip decision the metadata
                            files cannot express
  csv_input.py              read the ISBN list out of a CSV: delimiter/encoding/column
                            detection, per-row validation with physical row numbers
  verbosity.py              the verbose setting. Progress/warnings are plain
                            print(..., file=sys.stderr) calls at the point of use;
                            there is no logging framework.
  http_client.py            polite retrying HTTP layer + optional lazy Selenium;
                            per-host courtesy delay, block detection, one browser
                            reused for the whole run
  storage.py                the on-disk layout, both filename styles, path sanitising,
                            and has_record(): "is this book already stored?", read
                            from the metadata file itself and indexed once per run
  base.py                   BaseSource ABC + shared parsing helpers (clean_text, jsonld,
                            probe_origin, …); enabled_by_default marks opt-in adapters
  pipeline.py               orchestration, persistence, the per-ISBN summary table
  batch.py                  one Pipeline per CSV ISBN, in file order, over one
                            shared HttpClient; pre-run cleanup, batch digest,
                            interrupt/resume
  sources/
    __init__.py             auto-discovery (pkgutil + inspect) — there is NO registry
    goodreads.py  amazon.py  bookbub.py  kobo.py  audible.py
tests/test_isbn.py          ISBN checksum tests (pytest, or run the file directly)
tests/test_csv_input.py     CSV shapes, column detection, bad rows, --start/--end slicing
tests/test_batch.py         the per-host courtesy delay is real, randomised and kept
                            per host, every ISBN runs exactly once in file order,
                            interrupts stay accounted for, cleanup refuses to overreach
tests/test_multiprocess.py  real subprocesses sharing one tree: appends lose nothing,
                            and two sources' reports cannot collide
tests/test_skip_and_report.py  a source with a record on disk is not asked again; a
                            trustworthy "no such book" is recorded and skipped next
                            run while a walled or unreached one is not; the absent
                            list merges rather than truncates; deleting a
                            both; a skipped Goodreads still seeds the hint
tests/test_status_classification.py  empty vs blocked is decided by whether a host this
                            source actually contacted is walled, not by whether the wall
                            was new -- it decides what the report claims
tests/test_storage_metadata.py  the metadata record holds exactly the contract keys, a
                            re-scrape replaces rather than duplicates, and the seek-based
                            append survives non-ASCII
tests/test_source_defaults.py  the default source set excludes bookbub while --sources
                            bookbub still runs it; a removed wait_css selector gives up
                            fast but a still-loading page keeps its full budget
tests/test_origin_probe.py  the shared origin probe: finds a planted place, refuses
                            eligibleRegion, survives malformed/cyclic/huge input
tests/test_origin_warnings.py  every origin warning is derived from a real search, and
                            Amazon's two detail layouts are both consulted
tests/test_cli_smoke.py     --help, the three-flag guard and the usage-error exit
                            codes all still work
```

**Adding a source** takes one file: drop a module in `bookscraper/sources/` defining a
`BaseSource` subclass with a unique lowercase `name`, and it appears in `--sources` and
`--help` immediately. No registry to edit. A module that fails to import warns and is
skipped rather than taking the run down.

**Retiring one takes one line**, and does not mean deleting it: set
`enabled_by_default = False` on the class and it drops out of default runs while staying
discoverable and still working under an explicit `--sources <name>`. That is how BookBub is
handled — the cost/benefit changed under us, so the switch flipped, but ~1 600 lines of working
parser and its documented findings are still there for whoever repairs the selectors.

Three rules every adapter obeys: **`scrape()` never raises** (a missing selector appends to
`result.warnings` and carries on), **no scraped value is ever hard-coded**, and **a warning is
derived from the page rather than asserted about it** — if a warning names the layers it searched or
counts something on the page, the code did that searching and that counting on this run. A
hand-written claim about live page content is a hard-coded scraped value wearing prose.

The pipeline enforces the first rule from the outside too: *both* `source.scrape()` and the
persistence step are wrapped in `except Exception`, so neither a parser bug nor an unwritable
artefact (a malformed scraped URL, a lone UTF-16 surrogate in a truncated emoji) can abort the
run and skip the remaining sources. `batch.py` adds the same containment one level up: one
ISBN's unexpected crash is reported in the digest and the batch continues, because one bad
book must not cost the other 9 994 their run.

Run the tests:

```bash
.venv/bin/python -m pytest tests -q       # or run any test file directly, e.g.:
.venv/bin/python tests/test_origin_probe.py
```

---

## Politeness, legality and ToS

**This project is for educational use.** Please read this section before pointing it at
anything.

* **Rate limiting is enforced, not advertised.** A fresh
  `random.uniform(min_delay, max_delay)` sleep is applied **before every outbound request**,
  tracked **per host**, so interleaving sources cannot bypass it and even the first contact
  with a host is throttled. Instrumented evidence from a real run
  (`min_delay=1.0, max_delay=2.0`, 8 requests across 3 hosts): every same-host gap was
  ≥ 1.00 s (min 1.32 s, max 3.28 s, mean 2.05 s), and the gaps were randomised rather than a
  fixed sleep. Retries use exponential backoff with jitter and honour `Retry-After`.
* **A CSV multiplies the request count, and the delay is what holds.** Batch mode changes the
  volume, not the manners: every book goes through the **same shared `HttpClient`**, so the
  per-host delay clock, the retry budget and the block records carry across the whole file
  rather than resetting per ISBN. This is the concrete reason batching is not a shell `for`
  loop over the single-ISBN program — a process per ISBN *would* reset the clock and hammer
  five hosts as fast as the loop turns. That said, scale is the operator's judgement call:
  9 995 ISBNs × 5 sources is ~50 000 requests, and pointing that at five storefronts is a
  different act from scraping one book. Use `--end` unless you mean it, and raise
  the `min_delay`/`max_delay` settings rather than lower them.
* **There is no concurrency inside a run to reason about.** One request goes out at a time, in
  the order the adapters ask for it, each preceded by its host's courtesy delay. If you want a
  gentler crawl, raise the `min_delay`/`max_delay` settings. If you run two commands at once, note that
  the delay is enforced per *process*, so two terminals on the same host double its rate.
* **No CAPTCHA is ever solved, forged or routed around.** Cloudflare managed challenges, AWS
  WAF's HTTP-202 JavaScript challenge, Akamai `bm-verify` interstitials and 403 walls are
  *detected, named in a warning, and degraded from*. Where a real browser gets through, it is
  because the browser runs the site's own challenge script normally — no token forging, no TLS
  impersonation (`curl_cffi` and friends are deliberately not used), no third-party unblocking
  or residential-proxy service, and no paid CAPTCHA-solving API.
* **No authentication, ever.** Nothing here logs in, reuses a session cookie, or crosses a
  paywall. That is precisely why Amazon stops at 13 reviews and BookBub at 0 — those ceilings
  are reported honestly rather than climbed.
* **robots.txt.** the `respect_robots` setting makes the client fetch and obey each host's `robots.txt`.
  It is **off by default** so the tool's real reach stays visible; with it on, some fallback
  paths are correctly refused (Goodreads `/search`, Kobo's title search). Where a
  robots-disallowed path is used as a last resort, the adapter says so in a warning. Note that
  robots.txt permission is *not* the same thing as ToS permission.
* **Terms of Service.** Goodreads, Amazon and Audible (all Amazon-owned) prohibit automated
  collection in their Conditions of Use; Kobo and BookBub restrict it too. Running this
  against those sites is a ToS risk the operator accepts. Nothing here is authenticated or
  paywalled, and no anti-bot control is circumvented, but that does not make it authorised.
* **For Amazon data at any scale, the compliant route is the official
  [Amazon Product Advertising API](https://webservices.amazon.com/paapi5/documentation/)**
  (PA-API 5), which serves titles, authors, images, browse nodes and more under a licence
  intended for the purpose. Open Library, Google Books and the Library of Congress offer free,
  documented, scrape-free APIs for bibliographic metadata — if what you actually need is a place
  of publication, that is where to get it. This project does **not** use them for `origin`,
  because `origin` must come from the page the record claims. Open Library *is* scraped here, as
  a source in its own right, but its values are never copied into a storefront's record.
  Prefer all of these over scraping for anything beyond a one-off exercise.
* **Copyright.** Blurbs are publisher marketing copy and reviews are user-generated works
  owned by their authors. Storing them locally for study is one thing; redistributing them is
  another. Cover images are likewise licensed artwork.
