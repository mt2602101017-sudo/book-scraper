"""On-disk layout for scraped artefacts.

Five artefact kinds, five directories, one naming scheme per ``filename_style``.

The assignment PDF shows ``book metadata/`` and ``<isbn13> cp <source> <n>.jpg``
with *spaces*, which is a LaTeX artefact -- un-escaped underscores are swallowed by
TeX. A file literally named ``goodreads metadata.json`` would be odd, so the
default is ``filename_style='underscore'``, with ``'space'`` available
(``--filename-style space``) to reproduce the PDF byte-for-byte.

Every path component is sanitised, so ``..`` and separators can never escape the
output root. All text is UTF-8 with ``\n`` line endings on every platform.

Metadata accumulates
--------------------
``book_metadata/<source>_metadata.json`` holds **one JSON array with a record per
book**, appended to as the run goes, rather than one object overwritten each time.
That is what makes a 10 000-ISBN batch produce readable files instead of files
describing whichever book finished last.

The append seeks past the closing ``]`` and writes only the new record. Rewriting
the whole array per book would cost ~60 GiB over 10 000 books and slow down as it
grew; this is ~12 MiB and flat. The file is a valid, complete JSON array after
every append, so ``json.load()`` works mid-run.

Appending is a read-modify-write (find the trailing ``]``, truncate, write), so it
has to be serialised against every other writer. Within one run that is free --
requests and writes happen one at a time -- but **two ``main.py`` processes sharing
one output tree** is a supported thing to do, and without a lock both read the same
tail offset and each truncates away the other's record. Measured before the fix:
40 of 80 records vanished and the file still parsed, so there was no crash and no
warning. An ``fcntl.flock`` on a sidecar ``.lock`` file now covers the whole
sequence. It is POSIX-only; on Windows a single run is unaffected, but two
concurrent processes are not protected.
"""

from __future__ import annotations

import sys
from .verbosity import verbose
import json
import os
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Set, Tuple

try:  # POSIX only; absence just means no cross-process locking.
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

__all__ = ["Storage", "FILENAME_STYLES", "release_indexes"]

#: The ISBN-13s each ``<source>_metadata.json`` already holds, keyed by resolved
#: path. This is what the skip decision consults: "is there already a record for
#: this book from this source?" It is read from the file once per source per run
#: and updated in place as records are appended, because asking the file directly
#: for each of 10 000 x 5 pairs would re-parse a growing multi-megabyte array
#: 50 000 times.
_ISBN_INDEX: Dict[str, Set[str]] = {}

#: Full records, keyed by path then by ISBN-13. Built **only** for a source whose
#: records are actually asked for (:meth:`Storage.find_record`), which in practice
#: means Goodreads alone -- it is the one whose stored title/author has to be
#: recovered when it is skipped. The other four never pay for this.
_RECORD_INDEX: Dict[str, Dict[str, Dict[str, Any]]] = {}


def release_indexes() -> None:
    """Drop the in-memory indexes, so the next query re-reads the files.

    Called at the end of a run -- the indexes exist to serve one crawl, and holding
    10 000 parsed records after the last book is written is pointless. Also needed by
    tests, and by anything that rewrites a metadata file behind :class:`Storage`'s
    back (``tools/strip_metadata_keys.py`` does).
    """
    _ISBN_INDEX.clear()
    _RECORD_INDEX.clear()

@contextmanager
def file_lock(path: Path) -> Iterator[None]:
    """Hold an exclusive **cross-process** lock for ``path`` while the body runs.

    Locks a sidecar ``<name>.lock`` rather than the file itself, so the lock does
    not care whether the target is created, replaced or set aside inside the block.
    Blocks until the other process is done -- appends are short, and waiting is
    strictly better than interleaving.

    Degrades to a no-op when ``fcntl`` is unavailable or the lock file cannot be
    opened: a missing OS lock costs correctness only when two *processes* share an
    output directory, and refusing to write at all would be worse.
    """
    if fcntl is None:
        yield
        return
    guard = path.with_name(path.name + ".lock")
    handle = None
    try:
        guard.parent.mkdir(parents=True, exist_ok=True)
        handle = open(guard, "a+")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    except OSError as exc:
        if verbose():
            print('  Could not take a file lock on %s: %s' % (guard, exc), file=sys.stderr)
        if handle is not None:
            handle.close()
        yield
        return
    try:
        yield
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        handle.close()

#: Legal values for ``filename_style`` / ``--filename-style``.
FILENAME_STYLES: Tuple[str, ...] = ("underscore", "space")

#: Logical directory name -> (underscore form, space form).
_DIR_NAMES: Dict[str, Tuple[str, str]] = {
    "metadata": ("book_metadata", "book metadata"),
    "covers": ("book_coverpage", "book coverpage"),
    "blurb": ("book_blurb", "book blurb"),
    "reviews": ("book_reviews", "book reviews"),
    "genres": ("genres", "genres"),
}

#: Anything outside this set becomes a single "-" in a sanitised component.
_UNSAFE_RE = re.compile(r"[^A-Za-z0-9._ -]+")
_COLLAPSE_RE = re.compile(r"-{2,}")

_MAGIC_EXTS: Tuple[Tuple[bytes, str], ...] = (
    (b"\xff\xd8\xff", "jpg"),
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
    (b"BM", "bmp"),
    (b"II*\x00", "tiff"),
    (b"MM\x00*", "tiff"),
)

_CONTENT_TYPE_EXTS: Dict[str, str] = {
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/pjpeg": "jpg",
    "image/png": "png",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/avif": "avif",
    "image/bmp": "bmp",
    "image/tiff": "tiff",
    "image/svg+xml": "svg",
}

_ALLOWED_EXTS = frozenset(
    {"jpg", "jpeg", "png", "gif", "webp", "avif", "bmp", "tiff", "svg"}
)


class Storage:
    """Writes metadata, covers, blurbs, reviews and genres beneath ``root``.

    :param root: output directory (created on demand; ``./data`` by default in
        the CLI).
    :param filename_style: ``'underscore'`` (default, recommended) or
        ``'space'`` to reproduce the assignment PDF literally.
    """

    def __init__(
        self,
        root: Path,
        filename_style: str = "underscore",
    ) -> None:
        if filename_style not in FILENAME_STYLES:
            print("warning: Unknown filename_style %r; using 'underscore' (valid: %s)" % (filename_style, ', '.join(FILENAME_STYLES)), file=sys.stderr)
            filename_style = "underscore"
        self.filename_style = filename_style
        try:
            self.root = Path(root).expanduser()
        except RuntimeError:
            # Unresolvable ``~user`` -- use the literal path rather than crash.
            print('warning: Could not expand %r to a home directory; using it literally' % (str(root),), file=sys.stderr)
            self.root = Path(root)
        self._sep = "_" if filename_style == "underscore" else " "

    # -- layout --------------------------------------------------------------

    def dir_name(self, kind: str) -> str:
        """Return the on-disk directory name for a logical artefact ``kind``."""
        try:
            underscore, spaced = _DIR_NAMES[kind]
        except KeyError as exc:
            raise ValueError(
                f"Unknown artefact kind {kind!r}; expected one of {sorted(_DIR_NAMES)}"
            ) from exc
        return underscore if self.filename_style == "underscore" else spaced

    def dir_for(self, kind: str) -> Path:
        """Return the absolute directory :class:`Path` for ``kind``."""
        return self.root / self.dir_name(kind)

    def ensure_dirs(self) -> None:
        """Create ``root`` and all five artefact directories if absent."""
        for kind in _DIR_NAMES:
            path = self.dir_for(kind)
            path.mkdir(parents=True, exist_ok=True)
        if verbose():
            print('  Output tree ready under %s' % (self.root,), file=sys.stderr)

    @property
    def directories(self) -> List[Path]:
        """All five artefact directories, in a stable order."""
        return [self.dir_for(kind) for kind in _DIR_NAMES]

    # -- sanitising ----------------------------------------------------------

    @staticmethod
    def sanitise(component: str, *, fallback: str = "unknown") -> str:
        """Reduce ``component`` to a safe single path segment.

        Strips directory separators, ``..``, control characters and anything
        outside ``[A-Za-z0-9._ -]``. Never returns an empty string, a value
        containing a separator, or a value that starts with a dot.
        """
        text = "" if component is None else str(component)
        text = text.replace("/", "-").replace("\\", "-").replace("\x00", "")
        text = _UNSAFE_RE.sub("-", text)
        text = _COLLAPSE_RE.sub("-", text).strip(" .-")
        # Defensive: after stripping dots a pure ".."/"." input is now empty.
        if not text or text in {".", ".."}:
            return fallback
        return text[:120]

    def _join(self, *parts: str) -> str:
        """Join filename parts with the active separator (``_`` or space)."""
        return self._sep.join(p for p in parts if p != "")

    @staticmethod
    def scrub_surrogates(text: str) -> Tuple[str, int]:
        """Replace lone UTF-16 surrogates with U+FFFD; return ``(text, n_replaced)``.

        Scraped JSON routinely carries a truncated emoji as an unpaired high
        surrogate (``"\\ud83d"``). ``json.loads`` keeps it, but a UTF-8 encoder
        rejects it, so writing such a string raises :class:`UnicodeEncodeError`
        from deep inside ``open().write()``. Cleaning it here means one bad
        review can never take the run down.
        """
        try:
            text.encode("utf-8")
        except UnicodeEncodeError:
            pass
        else:
            return text, 0
        # Round-tripping through UTF-16 with ``surrogatepass`` lets the lone
        # surrogate through the encoder, and the decoder's ``replace`` then turns
        # exactly that code unit into U+FFFD -- one replacement character per bad
        # code unit, with every other character preserved byte-for-byte. (Encoding
        # straight to UTF-8 with ``replace`` would emit an ASCII '?' instead.)
        try:
            cleaned = text.encode("utf-16", "surrogatepass").decode("utf-16", "replace")
        except (UnicodeError, ValueError):
            cleaned = text.encode("utf-8", "replace").decode("utf-8", "replace")
        replaced = sum(1 for a, b in zip(text, cleaned) if a != b) or 1
        return cleaned, replaced

    def _write_text(self, path: Path, text: str) -> Path:
        """Write UTF-8 with ``\\n`` endings, normalising any CRLF in the payload.

        ``newline='\\n'`` stops Python translating on write, but scraped text
        frequently *contains* literal CRLF; normalising here is what actually
        guarantees ``\\n``-only files. Unencodable lone surrogates are scrubbed
        first -- see :meth:`scrub_surrogates`.
        """
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        text, replaced = self.scrub_surrogates(text)
        if replaced:
            print('warning: Replaced %d unencodable surrogate character(s) with U+FFFD while writing %s (the site served a truncated multi-byte escape)' % (replaced, path.name), file=sys.stderr)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        if verbose():
            print('  Wrote %s (%d chars)' % (path, len(text)), file=sys.stderr)
        return path

    def _write_bytes(self, path: Path, data: bytes) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(data)
        if verbose():
            print('  Wrote %s (%d bytes)' % (path, len(data)), file=sys.stderr)
        return path

    # -- extension sniffing --------------------------------------------------

    @staticmethod
    def guess_image_ext(
        data: bytes,
        content_type: Optional[str] = None,
        url: Optional[str] = None,
        default: str = "jpg",
    ) -> str:
        """Derive a cover-image extension from the real bytes, then headers, then URL."""
        if data:
            for magic, ext in _MAGIC_EXTS:
                if data.startswith(magic):
                    return ext
            if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
                return "webp"
            if len(data) >= 12 and data[4:8] == b"ftyp" and b"avif" in data[8:20]:
                return "avif"
            head = data[:256].lstrip()
            if head.startswith(b"<svg") or head.startswith(b"<?xml"):
                if b"<svg" in data[:512]:
                    return "svg"
        if content_type:
            subtype = content_type.split(";")[0].strip().lower()
            if subtype in _CONTENT_TYPE_EXTS:
                return _CONTENT_TYPE_EXTS[subtype]
        if url:
            match = re.search(r"\.([A-Za-z0-9]{2,5})(?:[?#]|$)", url)
            if match:
                candidate = match.group(1).lower()
                if candidate in _ALLOWED_EXTS:
                    return "jpg" if candidate == "jpeg" else candidate
        return default

    def _normalise_ext(self, ext: Optional[str], default: str = "jpg") -> str:
        """Clean a caller-supplied extension: no dot, lower case, safe charset."""
        if not ext:
            return default
        cleaned = str(ext).strip().lstrip(".").lower()
        cleaned = re.sub(r"[^a-z0-9]", "", cleaned)
        if not cleaned:
            return default
        return "jpg" if cleaned == "jpeg" else cleaned[:8]

    # -- stale-artefact cleanup ----------------------------------------------

    #: Logical kind -> (infix, glob suffix) used by :meth:`purge`.
    _PURGE_PATTERNS: Dict[str, Tuple[str, str]] = {
        "reviews": ("r", ".txt"),
        "covers": ("cp", ".*"),
    }

    def purge(self, kind: str, isbn13: str, source: str) -> int:
        """Delete this run's predecessors for one ``(kind, isbn13, source)`` triple.

        Review and cover files are numbered from 1 upwards each run, so a run
        that finds fewer artefacts than a previous one would otherwise leave
        orphans behind and any consumer globbing the directory would read a
        mixture of old and new. Returns the number of files removed; never
        raises, and never touches another source's or another ISBN's files.
        """
        try:
            infix, suffix = self._PURGE_PATTERNS[kind]
        except KeyError:
            return 0
        directory = self.dir_for(kind)
        if not directory.is_dir():
            return 0
        pattern = self._join(
            self.sanitise(isbn13, fallback="isbn"),
            infix,
            self.sanitise(source, fallback="source"),
            "*",
        ) + suffix
        removed = 0
        try:
            candidates = sorted(directory.glob(pattern))
        except OSError as exc:
            if verbose():
                print('  Could not list %s for purging: %s' % (directory, exc), file=sys.stderr)
            return 0
        for path in candidates:
            try:
                path.unlink()
                removed += 1
            except OSError as exc:
                if verbose():
                    print('  Could not remove stale %s: %s' % (path, exc), file=sys.stderr)
        if removed:
            print('Removed %d stale %s file(s) from a previous run for %s/%s' % (removed, kind, isbn13, source), file=sys.stderr)
        return removed

    # -- writers -------------------------------------------------------------

    def metadata_path(self, source: str) -> Path:
        """Return ``book_metadata/<source>_metadata.json`` for ``source``."""
        src = self.sanitise(source, fallback="source")
        return self.dir_for("metadata") / (self._join(src, "metadata") + ".json")

    def _dump_record(self, source: str, payload: Dict[str, Any]) -> str:
        """Serialise one record, indented two levels for life inside an array.

        ``payload`` is dumped verbatim (key order preserved, UTF-8 kept as real
        characters rather than ``\\uXXXX`` escapes) so that
        :meth:`bookscraper.models.BookMetadata.to_json_dict` fully controls the
        document shape.
        """
        try:
            text = json.dumps(payload, ensure_ascii=False, indent=2,
                              sort_keys=False, default=str)
        except (TypeError, ValueError) as exc:
            print('warning: Metadata for %s is not JSON-serialisable (%s); falling back to a string-coerced dump' % (source, exc), file=sys.stderr)
            text = json.dumps({k: str(v) for k, v in dict(payload).items()},
                              ensure_ascii=False, indent=2)
        # Indent the whole object by two spaces so the array reads naturally.
        return "\n".join("  " + line for line in text.splitlines())

    def append_metadata(self, source: str, payload: Dict[str, Any]) -> Path:
        """Append one book's record to ``book_metadata/<source>_metadata.json``.

        The file is a single JSON array and stays valid after every append, so
        ``json.load()`` works mid-run. Appending truncates the trailing ``"\n]\n"``
        and writes ``",\n<record>\n]\n"``, so the cost is one record rather than the
        whole array -- re-dumping per book would be ~60 GiB across 10 000 ISBNs.

        Serialised on an ``fcntl`` file lock, because the user may run two
        processes against one output tree.

        A structurally unusable file (truncated by ``kill -9``, hand-edited) is
        moved aside to ``<name>.corrupt-N.json`` rather than silently discarded.

        **Re-scraping a book replaces its record** instead of adding a second one:
        ``--start`` resume deliberately re-runs a few already-finished books, and
        duplicates would corrupt every count taken from the file.
        """
        path = self.metadata_path(source)
        record = self._dump_record(source, payload)
        incoming = str(payload.get("isbn13") or "").strip()
        # Only update an index that has already been built. Creating one here would
        # leave a set holding this single ISBN that then *looks* authoritative, and
        # every other book would be reported as unscraped.
        if incoming:
            known = _ISBN_INDEX.get(str(path))
            if known is not None:
                known.add(incoming)
            records = _RECORD_INDEX.get(str(path))
            if records is not None:
                records[incoming] = payload

        # A file lock, so a second main.py process cannot truncate our record.
        with file_lock(path):
            path.parent.mkdir(parents=True, exist_ok=True)
            if incoming and self._replace_record(path, source, incoming, payload):
                return path
            if not path.exists() or path.stat().st_size == 0:
                self._write_text(path, f"[\n{record}\n]\n")
                if verbose():
                    print('  Started %s with its first record' % (path.name,), file=sys.stderr)
                return path

            tail_at = self._array_tail_offset(path)
            if tail_at is None:
                salvaged = self._set_aside(path)
                print('warning: %s was not a readable JSON array, so it could not be appended to; it has been kept as %s and a fresh file started. Nothing already on disk was deleted.' % (path.name, salvaged.name if salvaged else '(could not be moved)'), file=sys.stderr)
                self._write_text(path, f"[\n{record}\n]\n")
                return path

            with open(path, "r+", encoding="utf-8", newline="") as handle:
                handle.seek(tail_at)
                handle.truncate()
                # tail_at points just past the last record, so an empty array
                # (``[]``) needs no comma while a populated one does.
                separator = "" if self._array_is_empty(path, tail_at) else ","
                handle.write(f"{separator}\n{record}\n]\n")
        if verbose():
            print('  Appended a record to %s' % (path.name,), file=sys.stderr)
        return path

    def _replace_record(
        self, path: Path, source: str, isbn13: str, payload: Dict[str, Any]
    ) -> bool:
        """Rewrite an existing record for ``isbn13`` in place. ``True`` if it did.

        Only touches the file when a record for this ISBN is genuinely already
        there, so the ordinary append path stays untouched (and cheap) for the
        common case of a book seen for the first time. Caller holds the lock.
        """
        if not path.is_file():
            return False
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return False  # unusable; let the append path set it aside
        if not isinstance(data, list):
            return False

        replaced = False
        for index, existing in enumerate(data):
            if isinstance(existing, dict) and \
                    str(existing.get("isbn13") or "").strip() == isbn13:
                data[index] = payload
                replaced = True
                break
        if not replaced:
            return False

        body = ",\n".join(self._dump_record(source, item) for item in data)
        self._write_text(path, f"[\n{body}\n]\n")
        if verbose():
            print('  Replaced the existing %s record for %s rather than duplicating it' % (source, isbn13), file=sys.stderr)
        return True

    def _array_tail_offset(self, path: Path) -> Optional[int]:
        """Byte offset just past the final record, or ``None`` if unusable.

        Reads only the tail of the file, so this stays cheap on a large array.
        Returns the offset of the closing ``]``'s position, having stripped the
        whitespace before it.
        """
        try:
            size = path.stat().st_size
            with open(path, "rb") as handle:
                window = min(size, 4096)
                handle.seek(size - window)
                tail = handle.read(window)
        except OSError as exc:
            if verbose():
                print('  Could not read the tail of %s: %s' % (path, exc), file=sys.stderr)
            return None

        stripped = tail.rstrip()
        if not stripped.endswith(b"]"):
            return None
        # Offset of the ']' within the file, then back over any whitespace.
        end = size - (len(tail) - stripped.rindex(b"]"))
        while end > 0:
            try:
                with open(path, "rb") as handle:
                    handle.seek(end - 1)
                    previous = handle.read(1)
            except OSError:
                return None
            if previous not in b" \t\r\n":
                break
            end -= 1
        return end

    @staticmethod
    def _array_is_empty(path: Path, tail_at: int) -> bool:
        """True when the array holds no records yet (the file is just ``[``)."""
        try:
            with open(path, "rb") as handle:
                head = handle.read(max(0, tail_at)).strip()
        except OSError:
            return False
        return head in (b"[", b"")

    def _set_aside(self, path: Path) -> Optional[Path]:
        """Rename an unusable file out of the way. Returns the new path or ``None``."""
        for index in range(1, 1000):
            candidate = path.with_name(f"{path.stem}.corrupt-{index}{path.suffix}")
            if candidate.exists():
                continue
            try:
                os.replace(path, candidate)
            except OSError as exc:
                if verbose():
                    print('  Could not set aside %s: %s' % (path, exc), file=sys.stderr)
                return None
            return candidate
        return None

    def read_metadata(self, source: str) -> List[Dict[str, Any]]:
        """Return every record in ``<source>_metadata.json`` (``[]`` if absent).

        Provided so callers and tests never have to know how the file is built.
        """
        path = self.metadata_path(source)
        if not path.is_file():
            return []
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError) as exc:
            print('warning: Could not read %s: %s' % (path, exc), file=sys.stderr)
            return []
        if isinstance(data, list):
            return [record for record in data if isinstance(record, dict)]
        # Tolerate a single-object file from an older run.
        return [data] if isinstance(data, dict) else []

    def scraped_isbns(self, source: str) -> Set[str]:
        """Every ISBN-13 ``<source>_metadata.json`` already holds.

        The metadata file *is* the record of what has been scraped -- there is no
        separate ledger -- so this is the whole basis of the skip decision. Read
        once per source per run and cached (see :data:`_ISBN_INDEX`).

        One consequence worth being clear about: a source that answered honestly
        and had **no such book** writes no record, so it is indistinguishable here
        from a source that was never asked, and it will be asked again on the next
        run. That is the cost of deriving the skip decision from the output instead
        of from a separate log of attempts.
        """
        path = self.metadata_path(source)
        key = str(path)
        known = _ISBN_INDEX.get(key)
        if known is None:
            known = {
                str(record.get("isbn13") or "").strip()
                for record in self.read_metadata(source)
            }
            known.discard("")
            _ISBN_INDEX[key] = known
            if verbose():
                print('  %s already holds %d record(s)' % (path.name, len(known)), file=sys.stderr)
        return known

    def has_record(self, source: str, isbn13: str) -> bool:
        """True when ``<source>_metadata.json`` already carries this book."""
        return str(isbn13).strip() in self.scraped_isbns(source)

    def find_record(self, source: str, isbn13: str) -> Optional[Dict[str, Any]]:
        """The stored record for one book, or ``None``.

        Like :meth:`scraped_isbns`, the file is parsed once per source per run and
        the result kept (see :data:`_RECORD_INDEX`) -- this is asked once per *book*
        on a warm run, so re-reading a 10 000-record array each time cost ~70 s of
        pure JSON parsing before it was indexed.
        """
        wanted = str(isbn13).strip()
        if not wanted:
            return None
        path = self.metadata_path(source)
        key = str(path)
        records = _RECORD_INDEX.get(key)
        if records is None:
            records = {}
            for record in self.read_metadata(source):
                found = str(record.get("isbn13") or "").strip()
                if found:
                    records[found] = record
            _RECORD_INDEX[key] = records
            if verbose():
                print('  Indexed %d record(s) from %s for lookup' % (len(records), path.name), file=sys.stderr)
        return records.get(wanted)

    def write_cover(
        self,
        isbn13: str,
        source: str,
        n: int,
        data: bytes,
        ext: str = "jpg",
        *,
        content_type: Optional[str] = None,
        url: Optional[str] = None,
    ) -> Path:
        """Write ``book_coverpage/<isbn13>_cp_<source>_<n>.<ext>``.

        ``n`` is 1-based. The extension is derived from the image's magic bytes
        (then ``content_type``, then ``url``) so a PNG served from a ``.jpg``
        URL is still named correctly; an explicit non-default ``ext`` is only
        used when sniffing finds nothing.
        """
        sniffed = self.guess_image_ext(
            data, content_type=content_type, url=url,
            default=self._normalise_ext(ext, "jpg"),
        )
        suffix = self._normalise_ext(sniffed, "jpg")
        name = self._join(
            self.sanitise(isbn13, fallback="isbn"),
            "cp",
            self.sanitise(source, fallback="source"),
            str(self._index(n)),
        ) + f".{suffix}"
        return self._write_bytes(self.dir_for("covers") / name, data)

    def write_blurb(self, isbn13: str, source: str, text: str) -> Path:
        """Write ``book_blurb/<isbn13>_b_<source>_1.txt`` (always index 1)."""
        name = self._join(
            self.sanitise(isbn13, fallback="isbn"),
            "b",
            self.sanitise(source, fallback="source"),
            "1",
        ) + ".txt"
        body = (text or "").strip()
        return self._write_text(self.dir_for("blurb") / name, body + "\n" if body else "")

    def write_review(self, isbn13: str, source: str, n: int, text: str) -> Path:
        """Write ``book_reviews/<isbn13>_r_<source>_<n>.txt``. ``n`` is 1-based."""
        name = self._join(
            self.sanitise(isbn13, fallback="isbn"),
            "r",
            self.sanitise(source, fallback="source"),
            str(self._index(n)),
        ) + ".txt"
        body = (text or "").strip()
        return self._write_text(self.dir_for("reviews") / name, body + "\n" if body else "")

    def write_genres(self, isbn13: str, source: str, genres: List[str]) -> Path:
        """Write ``genres/<isbn13>_g_<source>_1.txt``, one genre per line."""
        name = self._join(
            self.sanitise(isbn13, fallback="isbn"),
            "g",
            self.sanitise(source, fallback="source"),
            "1",
        ) + ".txt"
        lines = [str(g).strip() for g in (genres or []) if str(g).strip()]
        text = "\n".join(lines)
        return self._write_text(
            self.dir_for("genres") / name, text + "\n" if text else ""
        )

    # -- helpers -------------------------------------------------------------

    # (There was a _warn_if_clobbering_other_isbn() here. It warned that writing a
    # second book's metadata destroyed the first, because the mandated filename
    # carries no ISBN. append_metadata() removes the problem rather than reporting
    # it -- records accumulate -- so the warning went with it.)

    @staticmethod
    def _index(n: int) -> int:
        """Coerce ``n`` to a sane 1-based index."""
        try:
            value = int(n)
        except (TypeError, ValueError):
            return 1
        return value if value >= 1 else 1

