import sqlite3

import pytest

from study_bible_mcp.database import StudyBibleDB, create_schema
from study_bible_mcp.parsers.ancient_sources import parse_josephus_work
from study_bible_mcp.tools import format_ancient_source_results


TEI_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<TEI xmlns="http://www.tei-c.org/ns/1.0">
  <teiHeader><fileDesc><publicationStmt><availability>
    <licence target="https://creativecommons.org/licenses/by-sa/4.0/">CC BY-SA 4.0</licence>
  </availability></publicationStmt></fileDesc></teiHeader>
  <text><body><div type="{kind}">
    <div type="textpart" subtype="book" n="1">
      <div type="textpart" subtype="section" n="27">
        <p>{text}<note>editorial note must not enter source text</note></p>
      </div>
    </div>
  </div></body></text>
</TEI>
"""


def test_josephus_parser_pairs_languages_and_excludes_editorial_notes(tmp_path):
    (tmp_path / "tlg0526.tlg001.perseus-eng2.xml").write_text(
        TEI_TEMPLATE.format(kind="translation", text="In the beginning God created."),
        encoding="utf-8",
    )
    (tmp_path / "tlg0526.tlg001.perseus-grc2.xml").write_text(
        TEI_TEMPLATE.format(kind="edition", text="Ἐν ἀρχῇ ὁ θεὸς ἔκτισεν."),
        encoding="utf-8",
    )

    rows = parse_josephus_work(tmp_path, "tlg001")

    assert len(rows) == 1
    assert rows[0]["reference"] == "Antiquities 1.27"
    assert rows[0]["text"] == "In the beginning God created."
    assert rows[0]["original_text"] == "Ἐν ἀρχῇ ὁ θεὸς ἔκτισεν."
    assert "editorial note" not in rows[0]["text"]
    assert rows[0]["license"] == "CC BY-SA 4.0"


@pytest.mark.asyncio
async def test_ancient_source_search_combines_text_period_and_biblical_filters(tmp_path):
    db_path = tmp_path / "ancient.db"
    conn = sqlite3.connect(db_path)
    create_schema(conn)
    cursor = conn.execute(
        """INSERT INTO ancient_source_passages
           (corpus, author, work_key, work_title, reference, book_number,
            section_number, text, original_text, period_tags, edition,
            translation, license, source_url)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            "josephus", "Flavius Josephus", "antiquities", "Jewish Antiquities",
            "Antiquities 18.116", 18, "116",
            "John, that was called the Baptist, was a good man.",
            "Ἰωάννου τοῦ ἐπικαλουμένου βαπτιστοῦ", "second_temple,herodian,roman",
            "Benedikt Niese", "William Whiston", "CC BY-SA 4.0",
            "https://example.test/josephus",
        ),
    )
    passage_id = cursor.lastrowid
    conn.execute(
        """INSERT INTO ancient_source_passages_fts
           (passage_id, work_title, reference, text, original_text)
           VALUES (?, ?, ?, ?, ?)""",
        (passage_id, "Jewish Antiquities", "Antiquities 18.116",
         "John, that was called the Baptist, was a good man.",
         "Ἰωάννου τοῦ ἐπικαλουμένου βαπτιστοῦ"),
    )
    conn.execute(
        """INSERT INTO ancient_source_biblical_links
           (passage_id, biblical_book, sort_start, sort_end, relationship_note)
           VALUES (?, 'Mrk', 6017, 6029, 'Related account: death of John the Baptist')""",
        (passage_id,),
    )
    conn.commit()
    conn.close()

    database = StudyBibleDB(db_path)
    await database.connect()
    try:
        rows = await database.search_ancient_sources(
            "John Baptist",
            corpus="josephus",
            biblical_reference="Mark 6:20",
            period="second_temple",
        )
        excluded = await database.search_ancient_sources(
            "John Baptist",
            corpus="josephus",
            period="hasmonean",
        )
    finally:
        await database.close()

    assert [row["reference"] for row in rows] == ["Antiquities 18.116"]
    assert rows[0]["biblical_links"][0]["relationship_note"].startswith("Related account")
    assert excluded == []


def test_ancient_source_formatter_labels_primary_text_and_provenance():
    row = {
        "author": "Flavius Josephus",
        "reference": "Antiquities 18.116",
        "work_title": "Jewish Antiquities",
        "period_tags": "second_temple,herodian,roman",
        "edition": "Benedikt Niese",
        "translation": "William Whiston",
        "license": "CC BY-SA 4.0",
        "source_url": "https://example.test/josephus",
        "text": "John was called the Baptist.",
        "original_text": "Ἰωάννου τοῦ βαπτιστοῦ",
        "biblical_links": [],
    }

    output = format_ancient_source_results("John Baptist", [row], "josephus")

    assert "Flavius Josephus, Antiquities 18.116" in output
    assert "**Greek edition:** Benedikt Niese" in output
    assert "**English translation:** William Whiston" in output
    assert "**License:** CC BY-SA 4.0" in output
    assert "**Greek text excerpt:**" in output
