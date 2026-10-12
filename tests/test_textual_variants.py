import sqlite3
from pathlib import Path

import pytest

from scripts.import_heiser_content import import_textual_variants
from scripts.migrate_textual_variants import migrate
from study_bible_mcp.database import StudyBibleDB, create_schema


@pytest.mark.asyncio
async def test_import_keeps_variant_evidence_and_interpretation_separate(tmp_path):
    db_path = tmp_path / "variants.db"
    conn = sqlite3.connect(db_path)
    create_schema(conn)

    variants_dir = Path(__file__).parents[1] / "data" / "heiser" / "variants"
    imported = import_textual_variants(conn, variants_dir)
    legacy_tables = conn.execute(
        """SELECT name FROM sqlite_master
           WHERE type = 'table' AND name IN ('textual_variants', 'manuscript_witnesses')"""
    ).fetchall()
    conn.close()

    database = StudyBibleDB(db_path)
    await database.connect()
    try:
        cases = await database.get_textual_variant_cases("Deuteronomy 32:8")
        interpretation_only = await database.get_textual_variant_cases("Genesis 6:2")
    finally:
        await database.close()

    assert imported == 2
    assert legacy_tables == []
    assert interpretation_only == []
    assert len(cases) == 1

    case = cases[0]
    assert set(case) >= {
        "manuscript_evidence",
        "critical_judgments",
        "theological_interpretations",
        "author_specific_claims",
        "translation_decisions",
    }
    readings = {reading["reading_key"]: reading for reading in case["manuscript_evidence"]}
    assert set(readings) == {"mt", "variant"}
    assert {w["witness"] for w in readings["mt"]["witnesses"]} == {
        "Masoretic Text",
        "Aquila Codex X",
        "Symmachus Codex X",
        "Theodotion",
    }
    assert "4QDeutj" in {
        w["witness"] for w in readings["variant"]["witnesses"]
    }
    assert case["critical_judgments"][0]["attribution"] == "Dataset editorial summary"
    assert case["theological_interpretations"] == []
    assert case["author_specific_claims"][0]["author"] == "Michael S. Heiser"
    assert case["translation_decisions"][0]["translation"] == (
        "Heiser Literal Translation (HLT)"
    )


def test_migration_rebuilds_legacy_schema_and_preserves_hint_links(tmp_path):
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE textual_variants (
            id INTEGER PRIMARY KEY,
            reference TEXT NOT NULL,
            book TEXT NOT NULL,
            chapter INTEGER,
            verse INTEGER,
            mt_reading TEXT NOT NULL,
            mt_hebrew TEXT,
            variant_source TEXT NOT NULL,
            variant_reading TEXT NOT NULL,
            variant_original TEXT,
            variant_significance TEXT,
            heiser_analysis TEXT,
            heiser_content_id INTEGER,
            scholarly_consensus TEXT,
            preferred_for_hlt TEXT,
            hlt_rationale TEXT
        );
        CREATE TABLE manuscript_witnesses (
            id INTEGER PRIMARY KEY,
            variant_id INTEGER NOT NULL,
            manuscript TEXT NOT NULL,
            manuscript_date TEXT,
            reading_support TEXT
        );
        CREATE TABLE nt_ot_lxx_quote_hints (
            id INTEGER PRIMARY KEY,
            nt_reference TEXT NOT NULL,
            nt_display TEXT NOT NULL,
            nt_book TEXT NOT NULL,
            ot_reference TEXT NOT NULL,
            ot_display TEXT NOT NULL,
            ot_book TEXT NOT NULL,
            follows_lxx INTEGER NOT NULL,
            divergence_type TEXT,
            divergence_note TEXT,
            textual_variant_id INTEGER
        );
        """
    )
    conn.execute(
        """INSERT INTO textual_variants VALUES
           (1, 'Deu.32.43', 'Deu', 32, 43, 'short reading', 'הרנינו',
            'DSS_4QDeutq', 'long reading', 'הרנינו שמים', 'major',
            'Named-author argument', NULL, 'Critical judgment',
            'preferred long reading', 'translation rationale')"""
    )
    conn.execute(
        """INSERT INTO textual_variants VALUES
           (2, 'Gen.6.2', 'Gen', 6, 2, 'same reading', NULL,
            'MT_preserved', 'interpretation only', NULL, 'theological',
            'Interpretive claim', NULL, NULL, NULL, NULL)"""
    )
    conn.execute(
        """INSERT INTO manuscript_witnesses VALUES
           (1, 1, '4QDeutq', '~150-100 BCE', 'supports_variant')"""
    )
    conn.execute(
        """INSERT INTO nt_ot_lxx_quote_hints VALUES
           (1, 'Heb.1.6', 'Hebrews 1:6', 'Heb', 'Deu.32.43',
            'Deuteronomy 32:43', 'Deu', 1, 'longer reading',
            'Quotation follows the longer form', 1)"""
    )
    conn.commit()

    result = migrate(conn)

    assert result == (1, 1, 1)
    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert "textual_variants" not in tables
    assert "manuscript_witnesses" not in tables
    assert "textual_variant_cases" in tables
    case_id = conn.execute("SELECT id FROM textual_variant_cases").fetchone()[0]
    linked_case_id = conn.execute(
        "SELECT variant_case_id FROM nt_ot_lxx_quote_hints"
    ).fetchone()[0]
    assert linked_case_id == case_id
    assert conn.execute("SELECT COUNT(*) FROM textual_variant_witnesses").fetchone()[0] == 1
    conn.close()
