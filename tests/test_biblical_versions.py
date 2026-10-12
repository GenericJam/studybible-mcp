import json
import sqlite3

import pytest

from study_bible_mcp.database import StudyBibleDB, create_schema
from study_bible_mcp.parsers.biblical_versions import (
    LXX_BOOK_CODES,
    LXX_SOURCE_BOOKS,
    iter_lxx_rows,
)
from study_bible_mcp.tools import format_biblical_version_comparison


PROVENANCE_FIELDS = {
    "version",
    "edition",
    "language",
    "canonical_reference",
    "edition_reference",
    "text",
    "translation",
    "license",
    "source_url",
}


def test_all_pinned_lxx_books_have_canonical_codes():
    assert len(LXX_SOURCE_BOOKS) == 59
    assert LXX_SOURCE_BOOKS <= LXX_BOOK_CODES.keys()


def test_lxx_psalm_numbering_uses_explicit_mapping(tmp_path):
    morphology_dir = tmp_path / "morphology"
    morphology_dir.mkdir()
    (morphology_dir / "psalms-lxx.json").write_text(
        json.dumps([
            {
                "ref": "Ps 23:1",
                "words": [
                    {
                        "surface": "τοῦ",
                        "lemma": "ὁ",
                        "parsing": "masc gen sg",
                        "pos": "article",
                        "confidence": "high",
                        "source": "context_resolved",
                    },
                    {
                        "surface": "κυρίου",
                        "lemma": "κύριος",
                        "parsing": "masc gen sg",
                        "pos": "noun",
                        "confidence": "high",
                        "source": "context_resolved",
                    },
                ],
            }
        ]),
        encoding="utf-8",
    )
    pairings = {
        ("psalms-lxx", "23:1"): [
            {
                "grk_book": "psalms-lxx",
                "grk_ref": "23:1",
                "mt_book": "psalms",
                "mt_refs": ["24:1"],
                "method": "tvtms",
            }
        ]
    }

    row = next(iter_lxx_rows(morphology_dir, pairings, {("PSA", "23:1"): "The earth is the Lord's."}))

    assert row[0] == "LXX"
    assert row[3] == "Psa.24.1"
    assert row[8] == "Psa.23.1"
    assert row[10] == "The earth is the Lord's."
    assert row[14] == "tvtms"


@pytest.mark.asyncio
async def test_comparison_returns_provenance_in_requested_version_order(tmp_path):
    db_path = tmp_path / "versions.db"
    conn = sqlite3.connect(db_path)
    create_schema(conn)
    rows = [
        (
            "LXX", "Rahlfs Septuagint (1935)", "Greek", "Psa.24.1", "Psa", 24, 1, "",
            "Psa.23.1", "τοῦ κυρίου", "The earth is the Lord's.",
            "CC BY 4.0", "https://example.test/lxx", "[]", "tvtms",
        ),
        (
            "MT", "Westminster Leningrad Codex", "Hebrew", "Psa.24.1", "Psa", 24, 1, "",
            "Psa.24.1", "לַיהוָה הָאָרֶץ", "The earth is the LORD's.",
            "CC BY 4.0", "https://example.test/mt", "[]", "identity",
        ),
    ]
    conn.executemany(
        """INSERT INTO biblical_version_verses
           (version, edition, language, canonical_reference, canonical_book,
            canonical_chapter, canonical_verse, canonical_verse_suffix,
            edition_reference, text, translation, license, source_url,
            morphology, mapping_method)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        rows,
    )
    conn.commit()
    conn.close()

    database = StudyBibleDB(db_path)
    await database.connect()
    try:
        result = await database.compare_biblical_versions("Psalm 24:1", ["MT", "LXX"])
    finally:
        await database.close()

    assert [row["version"] for row in result] == ["MT", "LXX"]
    assert all(PROVENANCE_FIELDS <= row.keys() for row in result)
    assert result[1]["canonical_reference"] == "Psa.24.1"
    assert result[1]["edition_reference"] == "Psa.23.1"


def test_comparison_formatter_honors_original_and_morphology_flags():
    row = {
        "version": "LXX",
        "edition": "Rahlfs Septuagint (1935)",
        "language": "Greek",
        "canonical_reference": "Psa.24.1",
        "edition_reference": "Psa.23.1",
        "text": "τοῦ κυρίου",
        "translation": "The earth is the Lord's.",
        "license": "CC BY 4.0",
        "source_url": "https://example.test/lxx",
        "mapping_method": "tvtms",
        "morphology": json.dumps([
            {"surface": "κυρίου", "lemma": "κύριος", "parsing": "masc gen sg", "pos": "noun"}
        ]),
    }

    compact = format_biblical_version_comparison("Psalm 24:1", [row], False, False)
    detailed = format_biblical_version_comparison("Psalm 24:1", [row], True, True)

    assert "τοῦ κυρίου" not in compact
    assert "Morphology" not in compact
    assert "**Canonical reference:** Psa.24.1" in compact
    assert "**Edition reference:** Psa.23.1" in compact
    assert "τοῦ κυρίου" in detailed
    assert "κύριος; noun; masc gen sg" in detailed
