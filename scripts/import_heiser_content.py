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


def import_textual_variants(conn: sqlite3.Connection, variants_dir: Path) -> int:
    """Import textual variants from JSON files in the variants directory."""
    count = 0
    for filepath in sorted(variants_dir.glob("*.json")):
        with open(filepath) as f:
            data = json.load(f)

        for variant in data.get("variants", []):
            ref = variant["reference"]
            parts = ref.split(".")
            book = parts[0] if parts else ""
            chapter = int(parts[1]) if len(parts) > 1 else None
            verse = int(parts[2]) if len(parts) > 2 else None

            try:
                cursor = conn.execute(
                    """INSERT OR IGNORE INTO textual_variants
                       (reference, book, chapter, verse, mt_reading, mt_hebrew,
                        variant_source, variant_reading, variant_original,
                        variant_significance, heiser_analysis, scholarly_consensus,
                        preferred_for_hlt, hlt_rationale)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        ref, book, chapter, verse,
                        variant["mt_reading"], variant.get("mt_hebrew"),
                        variant["variant_source"], variant["variant_reading"],
                        variant.get("variant_original"),
                        variant.get("variant_significance", "major_theological"),
                        variant.get("heiser_analysis"),
                        variant.get("scholarly_consensus"),
                        variant.get("preferred_for_hlt"),
                        variant.get("hlt_rationale"),
                    ),
                )
                if cursor.rowcount > 0:
                    count += 1

                    # Import manuscript witnesses
                    variant_id = cursor.lastrowid
                    for witness in variant.get("witnesses", []):
                        conn.execute(
                            """INSERT OR IGNORE INTO manuscript_witnesses
                               (variant_id, manuscript, manuscript_date, reading_support)
                               VALUES (?, ?, ?, ?)""",
                            (variant_id, witness["manuscript"],
                             witness.get("manuscript_date"), witness.get("reading_support")),
                        )
            except sqlite3.Error as e:
                print(f"  Warning: Failed to insert variant for {ref}: {e}")

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

    # 3. Import textual variants
    variants_dir = data_dir / "variants"
    if variants_dir.exists() and list(variants_dir.glob("*.json")):
        count = import_textual_variants(conn, variants_dir)
        print(f"\n[Variants] Imported {count} textual variants")
    else:
        print(f"\n[Variants] No variant files found in {variants_dir}")

    # Summary
    print("\n" + "=" * 60)
    for table in ["theology_themes", "theology_content", "theology_verse_index", "theology_theme_index",
                   "textual_variants", "manuscript_witnesses"]:
        row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
        print(f"  {table}: {row[0]} rows")
    print("=" * 60)

    conn.close()
    print("\nDone!")


if __name__ == "__main__":
    main()
