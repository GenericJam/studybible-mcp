"""Parsers and import helpers for provenance-aware MT/LXX edition text."""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Iterator


LXX_COMMIT = "c91f6b1e8fb3ba37df701e6ae31f675ace71a2b2"
LXX_SOURCE_URL = f"https://github.com/OpenScriptorium/lxx-morph/tree/{LXX_COMMIT}"
BRENTON_SOURCE_URL = "https://ebible.org/details.php?id=eng-Brenton"

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

BRENTON_BOOK_CODES = {
    "genesis": "GEN", "exodus": "EXO", "leviticus": "LEV", "numbers": "NUM",
    "deuteronomy": "DEU", "joshua": "JOS", "judges": "JDG", "ruth": "RUT",
    "1-samuel": "1SA", "2-samuel": "2SA", "1-kings": "1KI", "2-kings": "2KI",
    "1-chronicles": "1CH", "2-chronicles": "2CH", "1-esdras": "1ES",
    "1-maccabees": "1MA", "2-maccabees": "2MA", "3-maccabees": "3MA",
    "4-maccabees": "4MA", "amos": "AMO", "baruch": "BAR",
    "bel-and-the-dragon": "BEL", "daniel": "DAG", "deuteronomy": "DEU",
    "ecclesiastes": "ECC", "esther-greek": "ESG", "ezekiel": "EZK",
    "habakkuk": "HAB", "haggai": "HAG", "hosea": "HOS", "isaiah": "ISA",
    "jeremiah-lxx": "JER", "job-lxx": "JOB", "joel": "JOL", "jonah": "JON",
    "judith": "JDT", "lamentations": "LAM", "letter-of-jeremiah": "LJE",
    "malachi": "MAL", "micah": "MIC", "nahum": "NAM", "obadiah": "OBA",
    "proverbs": "PRO", "psalms-lxx": "PSA", "sirach": "SIR",
    "song-of-solomon": "SNG", "susanna": "SUS", "tobit": "TOB",
    "wisdom": "WIS", "zechariah": "ZEC", "zephaniah": "ZEP",
}

_EDITION_SUFFIXES = {
    "daniel": "Old Greek",
    "daniel-theodotion": "Theodotion",
    "susanna": "Old Greek",
    "susanna-theodotion": "Theodotion",
    "bel-and-the-dragon": "Old Greek",
    "bel-and-the-dragon-theodotion": "Theodotion",
    "joshua-vaticanus-b": "Vaticanus B recension",
    "judges-vaticanus-b": "Vaticanus B recension",
    "tobit-sinaiticus": "Sinaiticus recension",
}

_OT_CODES = tuple(MT_BOOK_CODES.values())
_REF_RE = re.compile(r"^(\d+):(\d+[a-z]?)$", re.IGNORECASE)
_USFM_NOTE_RE = re.compile(r"\\(?:f|x)\s.*?\\(?:f|x)\*", re.DOTALL)
_USFM_MARKER_RE = re.compile(r"\\[a-z0-9-]+\*?\s?", re.IGNORECASE)


def _split_ref(reference: str) -> tuple[int, int, str]:
    match = _REF_RE.match(reference)
    if not match:
        raise ValueError(f"Unsupported edition reference: {reference}")
    chapter = int(match.group(1))
    verse_token = match.group(2)
    verse_match = re.match(r"^(\d+)([a-z]?)$", verse_token, re.IGNORECASE)
    return chapter, int(verse_match.group(1)), verse_match.group(2).lower()


def _canonical_reference(book: str, reference: str) -> tuple[str, int, int, str]:
    chapter, verse, suffix = _split_ref(reference)
    return f"{book}.{chapter}.{verse}{suffix}", chapter, verse, suffix


def _edition_reference(book: str, reference: str) -> str:
    chapter, verse, suffix = _split_ref(reference)
    return f"{book}.{chapter}.{verse}{suffix}"


def _clean_usfm(text: str) -> str:
    text = _USFM_NOTE_RE.sub("", text)
    text = _USFM_MARKER_RE.sub("", text)
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


def _morphology(words: list[dict]) -> str:
    fields = ("surface", "lemma", "parsing", "pos", "confidence", "source")
    compact = [{key: word.get(key) for key in fields if word.get(key) not in (None, "")} for word in words]
    return json.dumps(compact, ensure_ascii=False, separators=(",", ":"))


def _edition_name(slug: str) -> str:
    suffix = _EDITION_SUFFIXES.get(slug)
    return f"Rahlfs Septuagint (1935), {suffix}" if suffix else "Rahlfs Septuagint (1935)"


def iter_lxx_rows(
    morphology_dir: Path,
    pairings: dict[tuple[str, str], list[dict]],
    translations: dict[tuple[str, str], str],
) -> Iterator[tuple]:
    """Yield rows for biblical_version_verses from pinned Rahlfs morphology."""
    for path in sorted(morphology_dir.glob("*.json")):
        slug = path.stem
        if slug not in LXX_BOOK_CODES:
            continue
        edition_book = LXX_BOOK_CODES[slug]
        verses = json.loads(path.read_text(encoding="utf-8"))
        for verse in verses:
            raw_ref = verse["ref"].rsplit(" ", 1)[-1]
            edition_ref = _edition_reference(edition_book, raw_ref)
            mapped = pairings.get((slug, raw_ref)) or [{
                "mt_book": "",
                "mt_refs": [],
                "method": "lxx-only",
            }]
            targets: list[tuple[str, str, str]] = []
            for mapping in mapped:
                mt_book = mapping.get("mt_book")
                for mt_ref in mapping.get("mt_refs") or []:
                    targets.append((MT_BOOK_CODES[mt_book], mt_ref, mapping["method"]))
            if not targets:
                targets.append((edition_book, raw_ref, mapped[0].get("method", "lxx-only")))

            translation_code = BRENTON_BOOK_CODES.get(slug)
            translation = translations.get((translation_code, raw_ref)) if translation_code else None
            for canonical_book, canonical_raw, method in targets:
                canonical_ref, chapter, verse_number, suffix = _canonical_reference(
                    canonical_book, canonical_raw
                )
                row_translation = translation
                if slug == "2-esdras":
                    brenton_code = "EZR" if canonical_book == "Ezr" else "NEH"
                    row_translation = translations.get((brenton_code, canonical_raw))
                yield (
                    "LXX", _edition_name(slug), "Greek", canonical_ref,
                    canonical_book, chapter, verse_number, suffix, edition_ref,
                    _detokenize(verse["words"]), row_translation,
                    "CC BY 4.0 (morphology); Rahlfs Greek text public domain; Brenton translation public domain",
                    LXX_SOURCE_URL, _morphology(verse["words"]), method,
                )


def import_biblical_versions(conn: sqlite3.Connection, data_dir: Path) -> tuple[int, int]:
    """Replace MT/LXX rows using existing TAHOT verses and downloaded LXX files."""
    conn.execute("DELETE FROM biblical_version_verses")
    mt_sql = f"""
        INSERT INTO biblical_version_verses
            (version, edition, language, canonical_reference, canonical_book,
             canonical_chapter, canonical_verse, canonical_verse_suffix,
             edition_reference, text, translation, license, source_url,
             morphology, mapping_method)
        SELECT 'MT', 'STEPBible TAHOT / Westminster Leningrad Codex', 'Hebrew',
               reference, book, chapter, verse, '', reference, text_original,
               text_english, 'CC BY 4.0',
               'https://github.com/STEPBible/STEPBible-Data', word_data, 'identity'
        FROM verses
        WHERE book IN ({','.join('?' for _ in _OT_CODES)})
          AND text_original IS NOT NULL
    """
    cursor = conn.execute(mt_sql, _OT_CODES)
    mt_count = cursor.rowcount

    lxx_dir = data_dir / "lxx"
    pairings = load_lxx_pairings(lxx_dir / "verse_pairs.jsonl")
    translations = parse_brenton_usfm(lxx_dir / "brenton")
    rows = iter_lxx_rows(lxx_dir / "morphology", pairings, translations)
    before = conn.total_changes
    conn.executemany(
        """INSERT OR REPLACE INTO biblical_version_verses
           (version, edition, language, canonical_reference, canonical_book,
            canonical_chapter, canonical_verse, canonical_verse_suffix,
            edition_reference, text, translation, license, source_url,
            morphology, mapping_method)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        rows,
    )
    lxx_count = conn.total_changes - before
    conn.commit()
    return mt_count, lxx_count
