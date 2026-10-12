#!/usr/bin/env python3
"""Import theological scholarship content, themes, and textual variants into the study_bible database.

Re-runnable: rows already present (same source_work, chapter_or_episode, title) are
not duplicated; only their url is refreshed from the data files.

Usage: python scripts/import_heiser_content.py [db_path]
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from study_bible_mcp.database import link_theology_theme, upsert_theology_content
from study_bible_mcp.parsers.heiser import parse_themes_file, parse_heiser_content_file


def get_db_path() -> Path:
    """Get the database path: the first CLI argument, else db/study_bible.db."""
    if len(sys.argv) > 1:
        return Path(sys.argv[1])
    return Path(__file__).resolve().parent.parent / "db" / "study_bible.db"


def import_themes(conn: sqlite3.Connection, themes_path: Path) -> int:
    """Import theological themes from themes.json."""
    themes = parse_themes_file(themes_path)
    count = 0
    for theme in themes:
        try:
            conn.execute(
                """INSERT OR REPLACE INTO theology_themes
                   (theme_key, theme_label, description, parent_theme, key_works)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    theme["theme_key"],
                    theme["theme_label"],
                    theme["description"],
                    theme["parent_theme"],
                    theme.get("heiser_key_works") or theme.get("key_works"),
                ),
            )
            count += 1
        except sqlite3.Error as e:
            print(f"  Warning: Failed to insert theme {theme['theme_key']}: {e}")
    conn.commit()
    return count


def import_content_file(conn: sqlite3.Connection, filepath: Path) -> tuple[int, int, int, int]:
    """Import a single Heiser content JSON file.

    Existing rows keep their content but have their url refreshed.

    Returns (entries_imported, urls_updated, verse_refs_imported, theme_links_imported).
    """
    entries = 0
    urls_updated = 0
    refs = 0
    themes = 0

    for content_entry, verse_refs, theme_keys in parse_heiser_content_file(filepath):
        try:
            content_id, status = upsert_theology_content(conn, content_entry)
            if status == "inserted":
                entries += 1
            elif status == "url_updated":
                urls_updated += 1

            # Insert verse references
            for vref in verse_refs:
                try:
                    conn.execute(
                        """INSERT OR IGNORE INTO theology_verse_index
                           (content_id, reference, book, chapter, verse, relevance)
                           VALUES (?, ?, ?, ?, ?, ?)""",
                        (content_id, vref["reference"], vref["book"],
                         vref["chapter"], vref["verse"], vref["relevance"]),
                    )
                    refs += 1
                except sqlite3.Error:
                    pass

            # Insert theme links
            for theme_key in theme_keys:
                try:
                    link_theology_theme(conn, theme_key, content_id)
                    themes += 1
                except sqlite3.Error:
                    pass

        except sqlite3.Error as e:
            print(f"  Warning: Failed to insert entry '{content_entry.get('title', '?')}': {e}")

    conn.commit()
    return entries, urls_updated, refs, themes


NON_VARIANT_CATEGORIES = frozenset({"MT_preserved", "translation_choice"})


def import_textual_variant_record(
    conn: sqlite3.Connection,
    variant: dict,
) -> int | None:
    """Replace one genuine manuscript-variant case and return its case ID.

    The legacy dataset also contains interpretation-only records labelled
    ``MT_preserved`` or ``translation_choice``. Those are not manuscript
    variants and are deliberately excluded from this tool's data model.
    """
    case_key = variant["variant_source"]
    if case_key in NON_VARIANT_CATEGORIES:
        return None

    reference = variant["reference"]
    parts = reference.split(".")
    book = parts[0] if parts else ""
    chapter = int(parts[1]) if len(parts) > 1 else None
    verse = int(parts[2]) if len(parts) > 2 else None

    conn.execute(
        """INSERT INTO textual_variant_cases
           (reference, book, chapter, verse, case_key, significance)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(reference, case_key) DO UPDATE SET
               book = excluded.book,
               chapter = excluded.chapter,
               verse = excluded.verse,
               significance = excluded.significance""",
        (
            reference,
            book,
            chapter,
            verse,
            case_key,
            variant.get("variant_significance"),
        ),
    )
    case_id = conn.execute(
        """SELECT id FROM textual_variant_cases
           WHERE reference = ? AND case_key = ?""",
        (reference, case_key),
    ).fetchone()[0]

    conn.execute(
        """DELETE FROM textual_variant_witnesses
           WHERE reading_id IN (
               SELECT id FROM textual_variant_readings WHERE case_id = ?
           )""",
        (case_id,),
    )
    for table in (
        "textual_variant_readings",
        "textual_critical_judgments",
        "textual_theological_interpretations",
        "textual_author_claims",
        "textual_translation_decisions",
    ):
        conn.execute(f"DELETE FROM {table} WHERE case_id = ?", (case_id,))

    reading_ids: dict[str, int] = {}
    readings = (
        (
            "mt",
            "Masoretic Text",
            variant["mt_reading"],
            variant.get("mt_hebrew"),
        ),
        (
            "variant",
            case_key,
            variant["variant_reading"],
            variant.get("variant_original"),
        ),
    )
    for reading_key, tradition, reading_text, original_text in readings:
        cursor = conn.execute(
            """INSERT INTO textual_variant_readings
               (case_id, reading_key, tradition, text, original_text)
               VALUES (?, ?, ?, ?, ?)""",
            (case_id, reading_key, tradition, reading_text, original_text),
        )
        reading_ids[reading_key] = cursor.lastrowid

    for witness in variant.get("witnesses", []):
        reading_key = (
            "mt" if witness.get("reading_support") == "supports_mt" else "variant"
        )
        conn.execute(
            """INSERT INTO textual_variant_witnesses
               (reading_id, witness, approximate_date)
               VALUES (?, ?, ?)""",
            (
                reading_ids[reading_key],
                witness["manuscript"],
                witness.get("manuscript_date"),
            ),
        )

    if variant.get("scholarly_consensus"):
        conn.execute(
            """INSERT INTO textual_critical_judgments
               (case_id, judgment, attribution)
               VALUES (?, ?, ?)""",
            (
                case_id,
                variant["scholarly_consensus"],
                "Dataset editorial summary",
            ),
        )

    if variant.get("heiser_analysis"):
        conn.execute(
            """INSERT INTO textual_author_claims
               (case_id, author, claim)
               VALUES (?, ?, ?)""",
            (case_id, "Michael S. Heiser", variant["heiser_analysis"]),
        )

    if variant.get("preferred_for_hlt"):
        conn.execute(
            """INSERT INTO textual_translation_decisions
               (case_id, translation, preferred_reading, rationale)
               VALUES (?, ?, ?, ?)""",
            (
                case_id,
                "Heiser Literal Translation (HLT)",
                variant["preferred_for_hlt"],
                variant.get("hlt_rationale"),
            ),
        )

    return case_id


def import_textual_variants(conn: sqlite3.Connection, variants_dir: Path) -> int:
    """Import genuine manuscript variants from the legacy JSON dataset."""
    count = 0
    for filepath in sorted(variants_dir.glob("*.json")):
        with open(filepath, encoding="utf-8") as file:
            data = json.load(file)
        for variant in data.get("variants", []):
            try:
                if import_textual_variant_record(conn, variant) is not None:
                    count += 1
            except (KeyError, TypeError, ValueError, sqlite3.Error) as error:
                print(
                    f"  Warning: Failed to insert variant for "
                    f"{variant.get('reference', '?')}: {error}"
                )

    conn.commit()
    return count


def main():
    db_path = get_db_path()
    if not db_path.exists():
        print(f"Database not found at {db_path}")
        print("Run build_database.py first to create the base database.")
        sys.exit(1)

    data_dir = Path(__file__).resolve().parent.parent / "data" / "heiser"

    conn = sqlite3.connect(str(db_path))

    # Ensure HLT tables exist
    from study_bible_mcp.database import create_schema
    create_schema(conn)

    print("=" * 60)
    print("Importing Heiser Content for HLT")
    print("=" * 60)

    # 1. Import themes
    themes_path = data_dir / "themes.json"
    if themes_path.exists():
        count = import_themes(conn, themes_path)
        print(f"\n[Themes] Imported {count} themes")
    else:
        print(f"\nWarning: {themes_path} not found")

    # 2. Import content files
    content_dir = data_dir / "content"
    total_entries = 0
    total_urls = 0
    total_refs = 0
    total_themes = 0
    json_files = sorted(content_dir.glob("*.json"))
    print(f"\n[Content] Found {len(json_files)} content files")
    for filepath in json_files:
        entries, urls, refs, themes = import_content_file(conn, filepath)
        total_entries += entries
        total_urls += urls
        total_refs += refs
        total_themes += themes
        print(f"  {filepath.name}: {entries} entries, {urls} urls updated, "
              f"{refs} verse refs, {themes} theme links")

    print(f"\n[Content Totals] {total_entries} entries, {total_urls} urls updated, "
          f"{total_refs} verse refs, {total_themes} theme links")

    # 3. Import manuscript variants. Interpretation-only legacy records are
    # intentionally excluded from the textual-variant tool.
    variants_dir = data_dir / "variants"
    if variants_dir.exists() and list(variants_dir.glob("*.json")):
        count = import_textual_variants(conn, variants_dir)
        print(f"\n[Variants] Imported {count} manuscript variants")
    else:
        print(f"\n[Variants] No variant files found in {variants_dir}")

    # Summary
    print("\n" + "=" * 60)
    for table in [
        "theology_themes",
        "theology_content",
        "theology_verse_index",
        "theology_theme_index",
        "textual_variant_cases",
        "textual_variant_readings",
        "textual_variant_witnesses",
        "textual_critical_judgments",
        "textual_theological_interpretations",
        "textual_author_claims",
        "textual_translation_decisions",
    ]:
        row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
        print(f"  {table}: {row[0]} rows")
    print("=" * 60)

    conn.close()
    print("\nDone!")


if __name__ == "__main__":
    main()
