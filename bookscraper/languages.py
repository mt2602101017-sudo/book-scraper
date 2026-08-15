"""Language codes to English names, shared by the sources that publish codes.

Not scraped data -- a lookup table -- so it lives in one place rather than being
copied into three adapters. Kobo and BookBub publish BCP-47 primary subtags,
Open Library publishes MARC / ISO 639-2**B** codes, which is why both spellings of
the dual-code languages are here: without ``fre``, ``ger``, ``chi`` and ``dut``,
French, German, Chinese and Dutch all come back as bare codes.

Emitting ``"en"`` where another source says ``"English"`` makes one source look
like a different language to anything grouping on the field, which is the whole
reason to normalise at all.
"""

from __future__ import annotations

from typing import Any, Optional

NAMES = {
    "en": "English", "eng": "English",
    "fr": "French", "fre": "French", "fra": "French",
    "de": "German", "ger": "German", "deu": "German",
    "es": "Spanish", "spa": "Spanish",
    "it": "Italian", "ita": "Italian",
    "nl": "Dutch", "dut": "Dutch", "nld": "Dutch",
    "pt": "Portuguese", "por": "Portuguese",
    "ru": "Russian", "rus": "Russian",
    "ja": "Japanese", "jpn": "Japanese",
    "zh": "Chinese", "chi": "Chinese", "zho": "Chinese",
    "ko": "Korean", "kor": "Korean",
    "ar": "Arabic", "ara": "Arabic",
    "hi": "Hindi", "hin": "Hindi",
    "he": "Hebrew", "heb": "Hebrew",
    "el": "Greek", "gre": "Greek", "ell": "Greek",
    "sv": "Swedish", "swe": "Swedish",
    "da": "Danish", "dan": "Danish",
    "no": "Norwegian", "nb": "Norwegian", "nor": "Norwegian",
    "fi": "Finnish", "fin": "Finnish",
    "pl": "Polish", "pol": "Polish",
    "cs": "Czech", "cze": "Czech", "ces": "Czech",
    "tr": "Turkish", "tur": "Turkish",
    "hu": "Hungarian", "hun": "Hungarian",
    "ro": "Romanian", "rum": "Romanian", "ron": "Romanian",
    "uk": "Ukrainian", "ukr": "Ukrainian",
    "ca": "Catalan", "cat": "Catalan",
    "id": "Indonesian", "ind": "Indonesian",
    "th": "Thai", "tha": "Thai",
    "vi": "Vietnamese", "vie": "Vietnamese",
}


def name_of(raw: Any) -> Optional[str]:
    """``"en-GB"`` -> ``"English"``; an already-spelled-out value passes through.

    An unknown code comes back unchanged rather than as ``None``, because what the
    site published is more useful than nothing.
    """
    found = str(raw or "").strip()
    if not found:
        return None
    # Only short, code-shaped values are looked up: a real name is left alone.
    if len(found) <= 5 or "-" in found or "_" in found:
        code = found.replace("_", "-").split("-")[0].lower()
        return NAMES.get(code, found)
    return found.title() if found.islower() else found
