"""Import licensed ancient Jewish source texts into searchable passages."""

from __future__ import annotations

import re
import sqlite3
import xml.etree.ElementTree as ET
from pathlib import Path


PERSEUS_COMMIT = "01b725d835e6e733062ffd79e0efdbae1ba06e5c"
PERSEUS_BASE_URL = (
    "https://github.com/PerseusDL/canonical-greekLit/blob/"
    f"{PERSEUS_COMMIT}/data/tlg0526"
)
LICENSE = "CC BY-SA 4.0"

WORKS = {
    "tlg001": {
        "key": "antiquities",
        "title": "Jewish Antiquities",
        "reference_title": "Antiquities",
    },
    "tlg002": {
        "key": "life",
        "title": "Life of Josephus",
        "reference_title": "Life",
    },
    "tlg003": {
        "key": "against_apion",
        "title": "Against Apion",
        "reference_title": "Against Apion",
    },
    "tlg004": {
        "key": "jewish_war",
        "title": "The Jewish War",
        "reference_title": "War",
    },
}

# Editorial links, intentionally separate from Josephus's text. These identify
# direct narrative overlap, not literary dependence or historical agreement.
_BIBLICAL_LINKS = (
    ("antiquities", "Antiquities 1.27", "Gen", 1, 1, 1, 31, "Narrative overlap: creation"),
    ("antiquities", "Antiquities 1.34", "Gen", 2, 4, 2, 25, "Narrative overlap: creation of humanity"),
    ("antiquities", "Antiquities 1.40", "Gen", 3, 1, 3, 24, "Narrative overlap: Eden and expulsion"),
    ("antiquities", "Antiquities 1.72", "Gen", 6, 1, 9, 29, "Narrative overlap: Noah and the flood"),
    ("antiquities", "Antiquities 2.205", "Exo", 2, 1, 2, 25, "Narrative overlap: birth and flight of Moses"),
    ("antiquities", "Antiquities 2.311", "Exo", 12, 1, 14, 31, "Narrative overlap: exodus from Egypt"),
    ("antiquities", "Antiquities 3.89", "Exo", 19, 1, 20, 26, "Narrative overlap: Sinai and the law"),
    ("antiquities", "Antiquities 18.116", "Mrk", 6, 17, 6, 29, "Related account: death of John the Baptist"),
)

_NS = {"tei": "http://www.tei-c.org/ns/1.0"}
_SKIP_TEXT_ELEMENTS = {"note", "pb"}


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _source_text(element: ET.Element) -> str:
    parts: list[str] = []

    def visit(node: ET.Element) -> None:
        if _local_name(node.tag) in _SKIP_TEXT_ELEMENTS:
            return
        if node.text:
            parts.append(node.text)
        for child in node:
            visit(child)
            if child.tail:
                parts.append(child.tail)

    visit(element)
    return re.sub(r"\s+", " ", " ".join(parts)).strip()


def _section_elements(root: ET.Element) -> list[tuple[int, ET.Element]]:
    books = root.findall(".//tei:div[@subtype='book']", _NS)
    if books:
        sections: list[tuple[int, ET.Element]] = []
        for book in books:
            book_number = int(book.attrib["n"])
            for section in book.findall(".//tei:div[@subtype='section']", _NS):
                sections.append((book_number, section))
        return sections
    return [
        (1, section)
        for section in root.findall(".//tei:div[@subtype='section']", _NS)
    ]


def _period_tags(work_key: str, book_number: int) -> str:
    if work_key == "antiquities":
        if book_number <= 10:
            return "biblical"
        if book_number == 11:
            return "biblical,second_temple,persian"
        if book_number == 12:
            return "second_temple,hellenistic,hasmonean"
        if book_number == 13:
            return "second_temple,hasmonean"
        if book_number <= 17:
            return "second_temple,herodian"
        return "second_temple,herodian,roman"
    if work_key == "jewish_war":
        if book_number == 1:
            return "second_temple,hasmonean,herodian"
        return "second_temple,roman,first_jewish_revolt"
    if work_key == "life":
        return "second_temple,roman,first_jewish_revolt"
    return "second_temple,roman"


def _validate_license(root: ET.Element, path: Path) -> None:
    targets = [node.attrib.get("target", "") for node in root.findall(".//tei:licence", _NS)]
    if not any("creativecommons.org/licenses/by-sa/4.0" in target for target in targets):
        raise ValueError(f"Expected CC BY-SA 4.0 license metadata in {path}")


def parse_josephus_work(directory: Path, work_id: str) -> list[dict]:
    """Pair Whiston English and Niese Greek sections for one Josephus work."""
    metadata = WORKS[work_id]
    english_path = directory / f"tlg0526.{work_id}.perseus-eng2.xml"
    greek_path = directory / f"tlg0526.{work_id}.perseus-grc2.xml"
    english_root = ET.parse(english_path).getroot()
    greek_root = ET.parse(greek_path).getroot()
    _validate_license(english_root, english_path)
    _validate_license(greek_root, greek_path)

    greek = {
        (book, section.attrib["n"]): _source_text(section)
        for book, section in _section_elements(greek_root)
    }
    rows: list[dict] = []
    for book, section in _section_elements(english_root):
        section_number = section.attrib["n"]
        if metadata["key"] == "life":
            reference = f"{metadata['reference_title']} {section_number}"
        else:
            reference = f"{metadata['reference_title']} {book}.{section_number}"
        rows.append({
            "corpus": "josephus",
            "author": "Flavius Josephus",
            "work_key": metadata["key"],
            "work_title": metadata["title"],
            "reference": reference,
            "book_number": book,
            "section_number": section_number,
            "text": _source_text(section),
            "original_text": greek.get((book, section_number)),
            "period_tags": _period_tags(metadata["key"], book),
            "edition": "Benedikt Niese, Flavii Iosephi Opera (1885–1895)",
            "translation": "William Whiston, The Works of Flavius Josephus (1856)",
            "license": LICENSE,
            "source_url": f"{PERSEUS_BASE_URL}/{work_id}",
        })
    return rows


def import_josephus(conn: sqlite3.Connection, directory: Path) -> int:
    """Replace the Josephus corpus and rebuild its FTS and biblical links."""
    conn.execute("DELETE FROM ancient_source_biblical_links WHERE passage_id IN (SELECT id FROM ancient_source_passages WHERE corpus = 'josephus')")
    conn.execute("DELETE FROM ancient_source_passages_fts WHERE passage_id IN (SELECT id FROM ancient_source_passages WHERE corpus = 'josephus')")
    conn.execute("DELETE FROM ancient_source_passages WHERE corpus = 'josephus'")

    rows = [row for work_id in WORKS for row in parse_josephus_work(directory, work_id)]
    conn.executemany(
        """INSERT INTO ancient_source_passages
           (corpus, author, work_key, work_title, reference, book_number,
            section_number, text, original_text, period_tags, edition,
            translation, license, source_url)
           VALUES (:corpus, :author, :work_key, :work_title, :reference,
                   :book_number, :section_number, :text, :original_text,
                   :period_tags, :edition, :translation, :license, :source_url)""",
        rows,
    )
    passages = conn.execute(
        "SELECT id, work_key, reference, work_title, text, original_text FROM ancient_source_passages WHERE corpus = 'josephus'"
    ).fetchall()
    conn.executemany(
        """INSERT INTO ancient_source_passages_fts
           (passage_id, work_title, reference, text, original_text)
           VALUES (?, ?, ?, ?, ?)""",
        ((row[0], row[3], row[2], row[4], row[5] or "") for row in passages),
    )
    passage_ids = {(row[1], row[2]): row[0] for row in passages}
    link_rows = []
    for work_key, reference, book, c1, v1, c2, v2, note in _BIBLICAL_LINKS:
        passage_id = passage_ids.get((work_key, reference))
        if passage_id is not None:
            link_rows.append((passage_id, book, c1 * 1000 + v1, c2 * 1000 + v2, note))
    conn.executemany(
        """INSERT INTO ancient_source_biblical_links
           (passage_id, biblical_book, sort_start, sort_end, relationship_note)
           VALUES (?, ?, ?, ?, ?)""",
        link_rows,
    )
    conn.commit()
    return len(rows)
