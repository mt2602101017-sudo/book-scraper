"""On-disk layout for scraped artefacts: five directories, one naming scheme.

    book_metadata/<source>_metadata.json          (see :mod:`bookscraper.metadata`)
    book_coverpage/<isbn13>_cp_<source>_<n>.<ext>
    book_blurb/<isbn13>_b_<source>_1.txt
    book_reviews/<isbn13>_r_<source>_<n>.txt
    genres/<isbn13>_g_<source>_1.txt

Every path component is sanitised, so ``..`` and separators can never escape the
output root, and all text is UTF-8 with ``\\n`` endings on every platform.
"""

from __future__ import annotations

import re
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, List, Optional

try:  # POSIX only; absence just means no cross-process locking.
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

#: Logical kind -> directory name.
DIRS = {"metadata": "book_metadata", "covers": "book_coverpage",
        "blurb": "book_blurb", "reviews": "book_reviews", "genres": "genres"}
#: Logical kind -> the filename infix numbered artefacts carry.
INFIX = {"covers": "cp", "blurb": "b", "reviews": "r", "genres": "g"}

_UNSAFE = re.compile(r"[^A-Za-z0-9._ -]+")
#: Magic bytes -> extension, so a PNG served from a ``.jpg`` URL is named right.
_MAGIC = ((b"\xff\xd8\xff", "jpg"), (b"\x89PNG\r\n\x1a\n", "png"), (b"GIF87a", "gif"),
          (b"GIF89a", "gif"), (b"BM", "bmp"), (b"II*\x00", "tiff"), (b"MM\x00*", "tiff"))
_CTYPES = {"image/jpeg": "jpg", "image/jpg": "jpg", "image/pjpeg": "jpg",
           "image/png": "png", "image/gif": "gif", "image/webp": "webp",
           "image/avif": "avif", "image/bmp": "bmp", "image/tiff": "tiff",
           "image/svg+xml": "svg"}


@contextmanager
def file_lock(path: Path) -> Iterator[None]:
    """Hold an exclusive cross-process lock on a ``<name>.lock`` sidecar.

    Locks the sidecar rather than the file itself, so the lock does not care
    whether the target is created or replaced inside the block. Degrades to a
    no-op without ``fcntl``: a missing OS lock only costs correctness when two
    processes share an output directory, and refusing to write would be worse.
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
    except OSError:
        if handle is not None:
            handle.close()
        yield
        return
    try:
        yield
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def sanitise(component: Any, fallback: str = "unknown") -> str:
    """Reduce ``component`` to one safe path segment -- no separators, no ``..``."""
    text = _UNSAFE.sub("-", str(component or "").replace("/", "-").replace("\\", "-"))
    text = re.sub(r"-{2,}", "-", text).strip(" .-")
    return (text or fallback)[:120]


def write_text(path: Path, text: str) -> Path:
    """Write UTF-8 with ``\\n`` endings, replacing lone surrogates with U+FFFD.

    Scraped JSON routinely carries a truncated emoji as an unpaired surrogate;
    ``json.loads`` keeps it but a UTF-8 encoder rejects it, so one bad review
    would otherwise take the whole run down from inside ``write()``.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        text = text.encode("utf-16", "surrogatepass").decode("utf-16", "replace")
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    return path


def image_ext(data: bytes, content_type: Optional[str] = None,
              url: Optional[str] = None) -> str:
    """A cover's extension from its real bytes, then headers, then the URL."""
    for magic, ext in _MAGIC:
        if data.startswith(magic):
            return ext
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[4:8] == b"ftyp" and b"avif" in data[8:20]:
        return "avif"
    if data[:256].lstrip().startswith((b"<svg", b"<?xml")) and b"<svg" in data[:512]:
        return "svg"
    subtype = (content_type or "").split(";")[0].strip().lower()
    if subtype in _CTYPES:
        return _CTYPES[subtype]
    match = re.search(r"\.(jpe?g|png|gif|webp|avif|bmp|tiff?|svg)(?:[?#]|$)",
                      url or "", re.IGNORECASE)
    found = match.group(1).lower() if match else "jpg"
    return {"jpeg": "jpg", "tif": "tiff"}.get(found, found)


class Storage:
    """Writes covers, blurbs, reviews and genres beneath ``root``.

    Metadata is handled by :attr:`meta`, a :class:`~bookscraper.metadata.Metadata`
    over the same tree, because it is a queryable record rather than a file drop.
    """

    def __init__(self, root: Path) -> None:
        from .metadata import Metadata

        self.root = Path(root).expanduser()
        self.meta = Metadata(self.root / DIRS["metadata"])

    def dir_for(self, kind: str) -> Path:
        return self.root / DIRS[kind]

    def ensure_dirs(self) -> None:
        """Create the output root and all five artefact directories."""
        for kind in DIRS:
            self.dir_for(kind).mkdir(parents=True, exist_ok=True)

    def _path(self, kind: str, isbn13: str, source: str, n: int, ext: str) -> Path:
        parts = (sanitise(isbn13, "isbn"), INFIX[kind], sanitise(source, "source"),
                 str(max(1, int(n))))
        return self.dir_for(kind) / ("_".join(parts) + f".{ext}")

    def purge(self, kind: str, isbn13: str, source: str) -> int:
        """Delete a previous run's numbered files for this (kind, book, source).

        Numbering restarts at 1 each run, so a run that finds fewer reviews than
        its predecessor would otherwise leave orphans behind for anything
        globbing the directory to read back as if they belonged to this run.
        """
        directory = self.dir_for(kind)
        if not directory.is_dir():
            return 0
        pattern = "_".join((sanitise(isbn13, "isbn"), INFIX[kind],
                            sanitise(source, "source"), "*.*"))
        removed = 0
        for path in sorted(directory.glob(pattern)):
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass
        return removed

    def cover(self, isbn13: str, source: str, n: int, data: bytes, *,
              content_type: Optional[str] = None, url: Optional[str] = None) -> Path:
        """Write one cover image; ``n`` is 1-based."""
        path = self._path("covers", isbn13, source, n,
                          image_ext(data, content_type, url))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def blurb(self, isbn13: str, source: str, text: str) -> Path:
        body = (text or "").strip()
        return write_text(self._path("blurb", isbn13, source, 1, "txt"),
                          body + "\n" if body else "")

    def review(self, isbn13: str, source: str, n: int, text: str) -> Path:
        body = (text or "").strip()
        return write_text(self._path("reviews", isbn13, source, n, "txt"),
                          body + "\n" if body else "")

    def genres(self, isbn13: str, source: str, genres: List[str]) -> Path:
        """One genre per line."""
        text = "\n".join(g for g in (str(x).strip() for x in genres or []) if g)
        return write_text(self._path("genres", isbn13, source, 1, "txt"),
                          text + "\n" if text else "")
