"""Parsers and import helpers for provenance-aware MT/LXX edition text.

Only Septuagint rows are stored in ``biblical_version_verses``. Masoretic rows
are built at query time from the STEPBible TAHOT ``verses`` table, so the MT is
never duplicated in the database.
"""

from __future__ import annotations

import json
import re
import sqlite3
import zlib
from pathlib import Path
from typing import Iterator


LXX_COMMIT = "c91f6b1e8fb3ba37df701e6ae31f675ace71a2b2"
LXX_SOURCE_URL = f"https://github.com/OpenScriptorium/lxx-morph/tree/{LXX_COMMIT}"
LXX_LICENSE = (
    "CC BY 4.0 (morphology); Rahlfs Greek text public domain; "
    "Brenton translation public domain"
)
LXX_TRANSLATION_LABEL = "Brenton English Septuagint translation"
BRENTON_SOURCE_URL = "https://ebible.org/details.php?id=eng-Brenton"

MT_EDITION = "STEPBible TAHOT / Westminster Leningrad Codex"
MT_LICENSE = "CC BY 4.0"
MT_SOURCE_URL = "https://github.com/STEPBible/STEPBible-Data"
MT_TRANSLATION_LABEL = "STEPBible word glosses (interlinear, not a translation)"
MT_VERSIFICATION_METHOD = "TAHOT English/Hebrew versification"

# Field order of the compact per-word arrays stored in the morphology column.
LXX_MORPHOLOGY_FIELDS = ("surface", "lemma", "parsing", "pos", "confidence", "source")

MT_BOOK_CODES = {
    "genesis": "Gen", "exodus": "Exo", "leviticus": "Lev", "numbers": "Num",
    "deuteronomy": "Deu", "joshua": "Jos", "judges": "Jdg", "ruth": "Rut",
    "1-samuel": "1Sa", "2-samuel": "2Sa", "1-kings": "1Ki", "2-kings": "2Ki",
    "1-chronicles": "1Ch", "2-chronicles": "2Ch", "ezra": "Ezr",
    "nehemiah": "Neh", "esther": "Est", "job": "Job", "psalms": "Psa",
    "proverbs": "Pro", "ecclesiastes": "Ecc", "song-of-solomon": "Sng",
    "isaiah": "Isa", "jeremiah": "Jer", "lamentations": "Lam", "ezekiel": "Ezk",
    "daniel": "Dan", "hosea": "Hos", "joel": "Jol", "amos": "Amo",
    "obadiah": "Oba", "jonah": "Jon", "micah": "Mic", "nahum": "Nam",
    "habakkuk": "Hab", "zephaniah": "Zep", "haggai": "Hag",
    "zechariah": "Zec", "malachi": "Mal",
}

LXX_SOURCE_BOOKS = frozenset({
    "1-chronicles", "1-esdras", "1-kings", "1-maccabees", "1-samuel",
    "2-chronicles", "2-esdras", "2-kings", "2-maccabees", "2-samuel",
    "3-maccabees", "4-maccabees", "amos", "baruch",
    "bel-and-the-dragon-theodotion", "bel-and-the-dragon", "daniel-theodotion",
    "daniel", "deuteronomy", "ecclesiastes", "esther-greek", "exodus",
    "ezekiel", "genesis", "habakkuk", "haggai", "hosea", "isaiah",
    "jeremiah-lxx", "job-lxx", "joel", "jonah", "joshua-vaticanus-b",
    "joshua", "judges-vaticanus-b", "judges", "judith", "lamentations",
    "letter-of-jeremiah", "leviticus", "malachi", "micah", "nahum", "numbers",
    "obadiah", "odes", "proverbs", "psalms-lxx", "psalms-of-solomon", "ruth",
    "sirach", "song-of-solomon", "susanna-theodotion", "susanna",
    "tobit-sinaiticus", "tobit", "wisdom", "zechariah", "zephaniah",
})


LXX_BOOK_CODES = {
    **MT_BOOK_CODES,
    "1-esdras": "1Es", "2-esdras": "2Es", "1-maccabees": "1Ma",
    "2-maccabees": "2Ma", "3-maccabees": "3Ma", "4-maccabees": "4Ma",
    "baruch": "Bar", "bel-and-the-dragon": "Bel",
    "bel-and-the-dragon-theodotion": "Bel", "daniel-theodotion": "Dan",
    "esther-greek": "EsG", "jeremiah-lxx": "Jer", "job-lxx": "Job",
    "joshua-vaticanus-b": "Jos", "judges-vaticanus-b": "Jdg",
    "letter-of-jeremiah": "Lje", "odes": "Ode", "psalms-lxx": "Psa",
    "psalms-of-solomon": "Pss", "sirach": "Sir", "susanna": "Sus",
    "susanna-theodotion": "Sus", "tobit": "Tob", "tobit-sinaiticus": "Tob",
    "judith": "Jdt", "wisdom": "Wis",
}

# Brenton translates Codex Vaticanus (B) and, for Daniel, Susanna and Bel,
# Theodotion. Its English is attached only to the Rahlfs file of the same
# recension; the Old Greek and Alexandrinus (A) files stay untranslated.
BRENTON_BOOK_CODES = {
    "genesis": "GEN", "exodus": "EXO", "leviticus": "LEV", "numbers": "NUM",
    "deuteronomy": "DEU", "joshua-vaticanus-b": "JOS",
    "judges-vaticanus-b": "JDG", "ruth": "RUT",
    "1-samuel": "1SA", "2-samuel": "2SA", "1-kings": "1KI", "2-kings": "2KI",
    "1-chronicles": "1CH", "2-chronicles": "2CH", "1-esdras": "1ES",
    "1-maccabees": "1MA", "2-maccabees": "2MA", "3-maccabees": "3MA",
    "4-maccabees": "4MA", "amos": "AMO", "baruch": "BAR",
    "bel-and-the-dragon-theodotion": "BEL", "daniel-theodotion": "DAG",
    "ecclesiastes": "ECC", "esther-greek": "ESG", "ezekiel": "EZK",
    "habakkuk": "HAB", "haggai": "HAG", "hosea": "HOS", "isaiah": "ISA",
    "jeremiah-lxx": "JER", "job-lxx": "JOB", "joel": "JOL", "jonah": "JON",
    "judith": "JDT", "lamentations": "LAM", "letter-of-jeremiah": "LJE",
    "malachi": "MAL", "micah": "MIC", "nahum": "NAM", "obadiah": "OBA",
    "proverbs": "PRO", "psalms-lxx": "PSA", "sirach": "SIR",
    "song-of-solomon": "SNG", "susanna-theodotion": "SUS", "tobit": "TOB",
    "wisdom": "WIS", "zechariah": "ZEC", "zephaniah": "ZEP",
}
# 2 Esdras is translated by Brenton's separate Ezra and Nehemiah books.
REQUIRED_BRENTON_BOOKS = frozenset(BRENTON_BOOK_CODES.values()) | {"EZR", "NEH"}

_EDITION_SUFFIXES = {
    "daniel": "Old Greek",
    "daniel-theodotion": "Theodotion",
    "susanna": "Old Greek",
    "susanna-theodotion": "Theodotion",
    "bel-and-the-dragon": "Old Greek",
    "bel-and-the-dragon-theodotion": "Theodotion",
    "joshua": "Alexandrinus A text",
    "judges": "Alexandrinus A text",
    "joshua-vaticanus-b": "Vaticanus B recension",
    "judges-vaticanus-b": "Vaticanus B recension",
    "tobit-sinaiticus": "Sinaiticus recension",
}

_OT_CODES = tuple(MT_BOOK_CODES.values())
_REF_RE = re.compile(r"^(\d+):(\d+)([a-z]?)$", re.IGNORECASE)
_USFM_NOTE_RE = re.compile(r"\\(?:f|x)\s.*?\\(?:f|x)\*", re.DOTALL)
# Closing character markers (\add*, \+it*) never consume the following space;
# opening markers (\add, \+it) consume the one delimiter space after them.
_USFM_CLOSE_MARKER_RE = re.compile(r"\\\+?[a-z0-9-]+\*", re.IGNORECASE)
_USFM_OPEN_MARKER_RE = re.compile(r"\\\+?[a-z0-9-]+\s?", re.IGNORECASE)


def _split_ref(reference: str) -> tuple[int, int, str]:
    match = _REF_RE.match(reference)
    if not match:
        raise ValueError(f"Unsupported edition reference: {reference}")
    return int(match.group(1)), int(match.group(2)), match.group(3).lower()


def _clean_usfm(text: str) -> str:
    text = _USFM_NOTE_RE.sub("", text)
    text = _USFM_CLOSE_MARKER_RE.sub("", text)
    text = _USFM_OPEN_MARKER_RE.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


def parse_brenton_usfm(directory: Path) -> dict[tuple[str, str], str]:
    """Read eBible's Brenton USFM files into (book code, chapter:verse) text."""
    translations: dict[tuple[str, str], str] = {}
    for path in sorted(directory.glob("*.usfm")):
        content = path.read_text(encoding="utf-8-sig")
        id_match = re.search(r"^\\id\s+(\S+)", content, re.MULTILINE)
        if not id_match:
            continue
        book = id_match.group(1)
        chapter: str | None = None
        current_key: tuple[str, str] | None = None
        for line in content.splitlines():
            chapter_match = re.match(r"^\\c\s+(\d+)", line)
            if chapter_match:
                chapter = chapter_match.group(1)
                current_key = None
                continue
            verse_match = re.match(r"^\\v\s+(\d+[a-z]?(?:-\d+[a-z]?)?)\s*(.*)", line)
            if verse_match and chapter:
                current_key = (book, f"{chapter}:{verse_match.group(1)}")
                translations[current_key] = _clean_usfm(verse_match.group(2))
                continue
            if current_key and line and not re.match(r"^\\(?:c|s|ms|mt|r|d|b|ib)\b", line):
                continuation = _clean_usfm(line)
                if continuation:
                    translations[current_key] = f"{translations[current_key]} {continuation}".strip()
    return translations


def load_lxx_pairings(path: Path) -> dict[tuple[str, str], list[dict]]:
    """Index the explicit LXX→MT versification map by Greek book and verse."""
    pairings: dict[tuple[str, str], list[dict]] = {}
    with path.open(encoding="utf-8") as source:
        for line in source:
            record = json.loads(line)
            pairings.setdefault((record["grk_book"], record["grk_ref"]), []).append(record)
    return pairings


def _detokenize(words: list[dict]) -> str:
    text = " ".join(word["surface"] for word in words)
    text = re.sub(r"\s+([,.;:!?·])", r"\1", text)
    text = re.sub(r"([‘“(])\s+", r"\1", text)
    return text.strip()


def _morphology(words: list[dict]) -> bytes:
    """Store word morphology as zlib-compressed positional JSON arrays.

    Each word is ``[surface, lemma, parsing, pos, confidence, source]`` (see
    LXX_MORPHOLOGY_FIELDS); compression shrinks the column about threefold.
    """
    compact = [[word.get(key) or "" for key in LXX_MORPHOLOGY_FIELDS] for word in words]
    return zlib.compress(json.dumps(compact, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), 9)


def expand_lxx_morphology(value: bytes | None) -> list[dict]:
    """Expand stored morphology into per-word dicts."""
    if not value:
        return []
    return [
        {key: item for key, item in zip(LXX_MORPHOLOGY_FIELDS, word) if item}
        for word in json.loads(zlib.decompress(value))
    ]


def _edition_name(slug: str) -> str:
    suffix = _EDITION_SUFFIXES.get(slug)
    return f"Rahlfs Septuagint (1935), {suffix}" if suffix else "Rahlfs Septuagint (1935)"


def _mapping_targets(mapped: list[dict]) -> tuple[list[tuple[str, str, str]], str]:
    """Return MT targets plus the method label used when there are none."""
    targets: list[tuple[str, str, str]] = []
    for mapping in mapped:
        for mt_ref in mapping.get("mt_refs") or []:
            targets.append((MT_BOOK_CODES[mapping["mt_book"]], mt_ref, mapping["method"]))
    first = mapped[0]
    unpaired_method = first.get("method", "lxx-only")
    if first.get("reason"):
        unpaired_method = f"{unpaired_method} ({first['reason']})"
    return targets, unpaired_method


def iter_lxx_rows(
    morphology_dir: Path,
    pairings: dict[tuple[str, str], list[dict]],
    translations: dict[tuple[str, str], str],
) -> Iterator[tuple]:
    """Yield rows for biblical_version_verses from pinned Rahlfs morphology.

    Paired verses carry the MT/English canonical position from the explicit
    mapping. Verses without an MT counterpart (unpaired pluses, deuterocanonical
    books) get NULL canonical columns, so canonical lookups never return them as
    a counterpart of a different MT verse; they are reachable by their own
    edition (Greek) numbering.
    """
    for path in sorted(morphology_dir.glob("*.json")):
        slug = path.stem
        if slug not in LXX_BOOK_CODES:
            continue
        edition_book = LXX_BOOK_CODES[slug]
        edition = _edition_name(slug)
        translation_code = BRENTON_BOOK_CODES.get(slug)
        seen: set[tuple[str, str | None]] = set()
        for verse in json.loads(path.read_text(encoding="utf-8")):
            raw_ref = verse["ref"].rsplit(" ", 1)[-1]
            edition_chapter, edition_verse, edition_suffix = _split_ref(raw_ref)
            edition_ref = f"{edition_book}.{edition_chapter}.{edition_verse}{edition_suffix}"
            mapped = pairings.get((slug, raw_ref)) or [{"method": "lxx-only"}]
            targets, unpaired_method = _mapping_targets(mapped)
            text = _detokenize(verse["words"])
            morphology = _morphology(verse["words"])
            translation = translations.get((translation_code, raw_ref)) if translation_code else None

            positions: list[tuple[str | None, str | None, int | None, int | None, str, str | None, str]] = []
            for canonical_book, canonical_raw, method in targets:
                chapter, verse_number, suffix = _split_ref(canonical_raw)
                row_translation = translation
                if slug == "2-esdras":
                    brenton_code = "EZR" if canonical_book == "Ezr" else "NEH"
                    row_translation = translations.get((brenton_code, canonical_raw))
                positions.append((
                    f"{canonical_book}.{chapter}.{verse_number}{suffix}", canonical_book,
                    chapter, verse_number, suffix, row_translation, method,
                ))
            if not positions:
                positions.append((None, None, None, None, "", translation, unpaired_method))

            for canonical_ref, book, chapter, verse_number, suffix, row_translation, method in positions:
                if (edition_ref, canonical_ref) in seen:
                    continue
                seen.add((edition_ref, canonical_ref))
                yield (
                    edition, canonical_ref, book, chapter, verse_number, suffix,
                    edition_ref, edition_book, edition_chapter, edition_verse,
                    edition_suffix, text, row_translation, morphology, method,
                )


def missing_lxx_sources(lxx_dir: Path) -> list[str]:
    """List required LXX/Brenton source files that are absent or truncated."""
    missing = [
        f"morphology/{slug}.json"
        for slug in sorted(LXX_SOURCE_BOOKS)
        if not (lxx_dir / "morphology" / f"{slug}.json").is_file()
    ]
    if not (lxx_dir / "verse_pairs.jsonl").is_file():
        missing.append("verse_pairs.jsonl")

    brenton_books: set[str] = set()
    brenton_dir = lxx_dir / "brenton"
    for path in sorted(brenton_dir.glob("*.usfm")) if brenton_dir.is_dir() else []:
        with path.open(encoding="utf-8-sig") as source:
            id_match = re.match(r"^\\id\s+(\S+)", source.readline())
        if id_match:
            brenton_books.add(id_match.group(1))
    missing.extend(
        f"brenton/*{code}*.usfm" for code in sorted(REQUIRED_BRENTON_BOOKS - brenton_books)
    )

    manifest_path = lxx_dir / "source_manifest.json"
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        listed = dict(manifest.get("files", {}))
        listed.update(
            {f"brenton/{name}": info for name, info in manifest.get("brenton", {}).get("files", {}).items()}
        )
        for relative, info in sorted(listed.items()):
            path = lxx_dir / relative
            if not path.is_file():
                missing.append(relative)
            elif "bytes" in info and path.stat().st_size != info["bytes"]:
                missing.append(f"{relative} (size {path.stat().st_size}, expected {info['bytes']})")
    return list(dict.fromkeys(missing))


def import_biblical_versions(conn: sqlite3.Connection, data_dir: Path) -> tuple[int, int]:
    """Replace LXX rows from the downloaded sources; MT is read from ``verses``.

    Returns ``(mt_count, lxx_count)`` where ``mt_count`` is the number of OT
    TAHOT verses available for MT comparison at query time. Raises
    ``FileNotFoundError`` when the LXX download is incomplete.
    """
    lxx_dir = data_dir / "lxx"
    missing = missing_lxx_sources(lxx_dir)
    if missing:
        raise FileNotFoundError(
            f"Incomplete LXX sources in {lxx_dir}: missing {', '.join(missing)}. "
            "Run python scripts/download_biblical_versions.py to complete the download."
        )

    placeholders = ",".join("?" for _ in _OT_CODES)
    mt_count = conn.execute(
        f"SELECT COUNT(*) FROM verses WHERE book IN ({placeholders}) AND text_original IS NOT NULL",
        _OT_CODES,
    ).fetchone()[0]

    pairings = load_lxx_pairings(lxx_dir / "verse_pairs.jsonl")
    translations = parse_brenton_usfm(lxx_dir / "brenton")
    rows = iter_lxx_rows(lxx_dir / "morphology", pairings, translations)
    conn.execute("DELETE FROM biblical_version_verses")
    before = conn.total_changes
    conn.executemany(
        """INSERT INTO biblical_version_verses
           (edition, canonical_reference, canonical_book, canonical_chapter,
            canonical_verse, canonical_verse_suffix, edition_reference,
            edition_book, edition_chapter, edition_verse, edition_verse_suffix,
            text, translation, morphology, mapping_method)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        rows,
    )
    lxx_count = conn.total_changes - before
    conn.commit()
    return mt_count, lxx_count
