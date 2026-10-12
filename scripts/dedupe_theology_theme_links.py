#!/usr/bin/env python3
"""Remove duplicate theology_theme_index rows left by earlier importer runs.

Content-level theme links carry reference NULL. SQLite treats NULL != NULL, so the
UNIQUE(theme_key, content_id, reference) constraint never matched them and every
INSERT OR IGNORE re-run of the Heiser importer added another copy of each link.
The duplicates inflate the entry_count that get_theology_context(theme=...) and
the theme listing report (e.g. divine_council: 105 links for 23 entries).

The importers now add each link once (database.link_theology_theme), so this is a
one-time cleanup for a DB that already has the duplicates. It keeps the lowest id
of each (theme_key, content_id, reference) group and deletes the rest; GROUP BY
treats NULLs as equal, so the NULL-reference copies collapse together.

Idempotent: re-running after a successful pass is a no-op.

Usage: python scripts/dedupe_theology_theme_links.py [db_path]
"""

import sqlite3
import sys

COUNTS_BY_THEME = """
    SELECT theme_key, COUNT(*), COUNT(DISTINCT content_id)
    FROM theology_theme_index GROUP BY theme_key
"""


def main(db_path: str = "db/study_bible.db") -> int:
    conn = sqlite3.connect(db_path)
    before = {key: (links, entries) for key, links, entries in conn.execute(COUNTS_BY_THEME)}
    total_before = sum(links for links, _ in before.values())

    deleted = conn.execute(
        """DELETE FROM theology_theme_index
           WHERE id NOT IN (
               SELECT MIN(id) FROM theology_theme_index
               GROUP BY theme_key, content_id, reference
           )"""
    ).rowcount
    if not deleted:
        print(f"{db_path}: no duplicate theme links ({total_before} rows) — nothing to do")
        conn.close()
        return 0

    after = {key: (links, entries) for key, links, entries in conn.execute(COUNTS_BY_THEME)}
    lost = sorted(key for key, (_, entries) in before.items() if after.get(key, (0, 0))[1] != entries)
    if lost:
        conn.rollback()
        print(f"ERROR: dedupe would change entries for themes {lost}; rolled back", file=sys.stderr)
        conn.close()
        return 1
    conn.commit()

    print(f"{db_path}: deleted {deleted} duplicate theme links ({total_before} -> {total_before - deleted} rows)")
    for key, (links, entries) in sorted(before.items(), key=lambda kv: -kv[1][0]):
        if links != after[key][0]:
            print(f"   {key}: {links} -> {after[key][0]} links ({entries} entries)")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
