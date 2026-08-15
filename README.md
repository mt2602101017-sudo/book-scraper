# book-scraper

Scrapes metadata, cover images, blurbs, reviews and genres for every ISBN in a CSV,
from six sources: Open Library, Goodreads, Amazon, BookBub, Kobo and Audible.

```bash
python -m venv .venv && .venv/bin/python -m pip install -r requirements.txt
.venv/bin/python main.py 2602101017.csv
```

You also need Google Chrome or Firefox on the machine: Kobo and BookBub produce
nothing without a browser (see [Why a browser](#why-a-browser)). Selenium 4.6+
fetches the matching driver itself.

## Usage

```bash
python main.py 2602101017.csv                     # every ISBN in the CSV
python main.py 2602101017.csv --start 0 --end 20  # just the first 20
python main.py 2602101017.csv --sources goodreads,amazon
python main.py 9780143127550                      # one ISBN
python main.py 0143127551                         # ISBN-10 is converted for you
```

The positional argument is a CSV **or** a single ISBN; which one it is is decided by
whether it names a file on disk, so neither form needs a flag.

Three flags, because they are the only things that genuinely vary per run: which
books (`--start`/`--end`) and which sites (`--sources`). Everything else — delays,
timeouts, review targets — lives in `SETTINGS` at the top of `main.py`, where a
reader sees all of it at once. The scraper grew 28 flags while it was being built,
and every one of them was a way for one run to differ from the tested one.

Rows are a **half-open** range, like Python slicing, so shards tile exactly:

```bash
# three terminals, no row scraped twice
python main.py books.csv --start 0    --end 3300
python main.py books.csv --start 3300 --end 6600
python main.py books.csv --start 6600
```

Splitting by site instead means the two runs barely share a host, so neither waits
on the other's courtesy delay:

```bash
python main.py books.csv --sources goodreads,amazon
python main.py books.csv --sources kobo,audible
```

Exit status: `0` if at least one source produced metadata, `1` if none did, `2` for
bad usage (a failed ISBN checksum, an unreadable CSV). Progress and warnings go to
stderr, so `python main.py books.csv 2>/dev/null` leaves just the summary.

## Output

All books share one flat tree under `./data`:

```
data/book_metadata/<source>_metadata.json      one JSON array, a record per book
data/book_coverpage/<isbn13>_cp_<source>_<n>.jpg
data/book_blurb/<isbn13>_b_<source>_1.txt
data/book_reviews/<isbn13>_r_<source>_<n>.txt
data/genres/<isbn13>_g_<source>_1.txt
```

Each metadata record is exactly eight fields, with `null` (never `"None"`, never
omitted) where a source published nothing:

```json
{
  "isbn13": "9780143127550",
  "title": "Everything I Never Told You",
  "authors": ["Celeste Ng"],
  "publisher": "Penguin Books",
  "origin": null,
  "date_of_publication": "2015-05-12",
  "language": "English",
  "genre": "Fiction, Book Club, Mystery, Contemporary"
}
```

Metadata **accumulates**. The mandated filename carries no ISBN, so overwriting
would leave six files describing whichever book finished last. Each append seeks past
the trailing `]` and writes one record — re-dumping the whole array per book costs
~60 GiB across 10 000 books, this is ~12 MiB and flat — and the file is a valid JSON
array after every append, so `json.load()` works mid-run. Re-scraping a book replaces
its record rather than adding a second one. An `fcntl` lock covers the whole sequence,
so two `main.py` processes can share one output tree.

## Resuming

Nothing already answered is fetched twice. Three things together cover every
(ISBN, source) pair:

- a record in `book_metadata/<source>_metadata.json` — the site had the book;
- an entry in `metrics/<source>_no_data.txt` — the site answered and genuinely does
  not carry it;
- an entry in `metrics/<source>_incomplete.txt` — the record was written but an
  artefact failed, so the book is **not** finished and gets scraped again.

The second list exists because no shape of "record present" can mean "absent". On the
shipped 10 000-ISBN CSV that is 20 007 of 49 975 pairs, and re-crawling them costs
roughly 29 h per run.

The third exists because "record present" does not mean "complete" either. A cover
download that times out used to leave a metadata record behind, which the skip check
then read as done — so the cover was lost permanently. Now the book stays on the
to-do list until its artefacts land, and drops off by itself once they do (or once it
turns out the source has no cover to give).

If you have output from before that was tracked, one pass recovers it:

```bash
python main.py books.csv --retry-incomplete
```

That queues every book whose record is on disk but whose cover is not. Books that
genuinely have no cover are re-checked once and then drop off the list.

Only a **trustworthy** empty is recorded: one where the site was actually reached and
no host the source contacted was walling us. That guard matters — skipping it once
wrote off 629 WAF-challenged Goodreads books as "not on Goodreads", and every one
resolved on a later attempt. `empty` and `blocked` are reported separately for the
same reason: "Kobo does not sell this 1980s paperback as an ebook" is a finding,
"Kobo walled us" is a problem to act on.

Ctrl-C is safe. Everything finished is on disk, and the digest tells you the
`--start` to resume from.

## Politeness and what is not done

- A randomised delay is slept **before every request, per host**, so interleaved
  adapters cannot hammer one site and a run cannot open with a burst. The default is
  1–2 s; hosts that ask for less traffic get their own limit in
  `transport.HOST_LIMITS`, which is where to add one if a site starts pushing back:

  | Host | Delay | Timeout | Why |
  | --- | --- | --- | --- |
  | `openlibrary.org` | 2.5–4 s | 30 s | answers HTTP 503 under sustained load |
  | `covers.openlibrary.org` | 3.5–5 s | 45 s | ~100 cover requests per IP per 5 min |
  | `archive.org` | 3.5–5 s | 60 s | serves `-L` covers out of a ZIP, on demand |

- Transient failures are retried with exponential backoff, honouring `Retry-After`.
  After three consecutive failures a host is **paused** (30 s, lengthening to a
  120 s cap) instead of being retried per URL, because burning the retry budget on
  every book against a host that is refusing connections helps nobody.
- Bot walls — CAPTCHA interstitials, Cloudflare challenges, AWS WAF's HTTP-202
  JavaScript challenge — are **detected and recorded, never fought**. No CAPTCHA is
  solved, forged or routed around; a walled page is a fetch failure, and the report
  says which host it was.
- No credentials are used anywhere, and no member-only content is retrieved. An
  earlier version signed in to Amazon to page past the ~13 reviews the detail page
  embeds; that breached Amazon's Conditions of Use and is gone.
- Reviews and blurbs are copyrighted third-party text and covers are licensed
  artwork, so `.gitignore` keeps `data/` out of version control.

## Why a browser

Four sources work on `requests` + BeautifulSoup alone. Two do not, and no amount of
header tuning changes that — Cloudflare gates both on the **TLS fingerprint**
(Python's OpenSSL versus Chrome's BoringSSL), and both render the parts we want
client-side:

| Source | Plain `requests` | Why |
| --- | --- | --- |
| Kobo | 0 of 3 pages | HTTP 403 + `cf-mitigated: challenge` on every path |
| BookBub | 0 of 3 pages | challenged on every path including `/robots.txt` |

Both go straight to the browser rather than spending a guaranteed 403 first. Selenium
stays a *soft* dependency in code — imported lazily on the first rendered fetch — so
without it the scraper warns and degrades to four sources instead of failing.

## What each source can deliver

Measured over the shipped 10 000-ISBN CSV:

| Source | Records | Reviews | Covers | Notes |
| --- | --- | --- | --- | --- |
| Goodreads | 9 977 | 138 308 | 43 358 | best all round; whole page in one request |
| Amazon | 9 961 | 68 700 | 20 635 | ISBN-10 *is* the ASIN |
| Kobo | 3 556 | 1 095 | 1 887 | ebook catalogue only |
| Audible | 1 724 | 17 713 | 2 867 | audiobook editions only |
| BookBub | 351 | 0 | 371 | promo listings; no publisher, date or language |
| Open Library | — | 0 | — | runs first, to seed the others |

Three caveats worth knowing:

- **`origin` is null everywhere.** No storefront publishes a place of publication;
  it is null in all 25 571 records scraped so far. The probe still runs on every
  source, so the field self-heals if one starts publishing it, and the warning names
  the layers actually searched. It is never inferred from the publisher's imprint,
  the storefront locale or the delivery country — that would be a guess wearing a
  scraped value's clothes.
- **Audible describes an audiobook.** Its `publisher` is the audio imprint, its date
  the audio release, its language the narration language. Narrators are never merged
  into authors.
- **Open Library publishes subject headings, not genres,** so its `genre` mixes true
  genres with topical headings and Library of Congress strings. They are reported
  verbatim; filtering them would be an editorial guess rather than a scrape.
- **Most Open Library records have no cover at all.** Of 91 records in one 381-book
  run with no cover file, 83 had no cover to fetch — the catalogue simply does not
  hold one. So a low cover count against Open Library is usually the catalogue, not
  a fault; the remaining 8 were genuine download failures, and those are what the
  size fallback and the to-do list now catch.

Open Library runs first because it is the only source that is ISBN-indexed *and*
answers in one JSON request. Kobo, Audible and BookBub have no ISBN lookup at all and
can only be searched by title and author, so one cheap lookup seeds all three.

## Architecture

```
main.py                 the CLI: three flags plus SETTINGS
bookscraper/
  models.py             the dataclasses every layer shares
  isbn.py               ISBN-10/13 normalisation, checksums, conversion
  csv_input.py          reads the run's ISBN list
  transport.py          the polite request engine: delays, retries, block recording
  blocks.py             recognising bot walls (and never fighting them)
  render.py             optional headless-browser rendering
  http.py               HttpClient: typed fetchers over the transport
  parse.py              text and DOM helpers
  extract.py            JSON-LD, embedded JSON blobs, dates, lists
  match.py              is this actually the right book?
  covers.py             collecting covers without keeping four copies of one
  languages.py          language codes to names
  origin.py             the place-of-publication probe
  storage.py            the five artefact directories
  metadata.py           the accumulating <source>_metadata.json arrays
  nodata.py             the durable "no such book" lists
  base.py               the Source contract, plus adapter auto-discovery
  persist.py            writing one result, and classifying what it was
  runner.py             orchestration
  report.py             the per-source reports and the closing digest
  sources/              one adapter per site, auto-discovered
tests/                  no network; a fake client stands in for every fetch
```

Adding a source means dropping a module in `bookscraper/sources/` that subclasses
`Source` and sets a `name`. There is no registry to edit. Three rules an adapter must
honour:

1. **Scraping never raises.** `Source.scrape` is the net; a missing selector appends
   a warning and carries on, because a partial result is a success.
2. **No scraped value is ever hard-coded.** Every field comes from a live parse.
3. **A warning is derived from the page, not asserted about it.** If a warning names
   the layers it searched, the code must have searched them on this run.

Books are scraped one at a time on purpose. The pace is set by the per-host courtesy
delay, not the CPU, so concurrency cannot make the crawl politer or much faster — a
thread pool here bought 1.2× while requiring a locked delay clock, a lock around the
single browser and captured-and-replayed output. Sharding by rows or by sites gets
the same speedup without any of that.

## Tests

```bash
.venv/bin/python -m pytest tests -q
```

88 tests, no network access: every fetch goes through a fake client. They cover the
things that would silently corrupt output rather than crash — the metadata array
staying valid after every append, a wall never being recorded as an absence, a
sequel never being filed under its predecessor's ISBN, and every adapter returning a
result rather than raising however badly a fetch went.
