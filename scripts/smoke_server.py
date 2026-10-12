#!/usr/bin/env python3
"""Exercise every registered MCP tool against a full local database.

This is an integration smoke runner, not a pytest module. Missing optional data is
reported as a skip; handler errors and registry drift fail the run.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from study_bible_mcp.database import StudyBibleDB
from study_bible_mcp.tools import TOOLS
import study_bible_mcp.server as server_module


TOOL_CASES: dict[str, dict] = {
    "word_study": {"strongs": "G26"},
    "lookup_verse": {"reference": "John 3:16"},
    "search_lexicon": {"query": "love", "language": "greek"},
    "get_cross_references": {"reference": "John 3:16"},
    "lookup_name": {"name": "David", "type": "person"},
    "compare_biblical_versions": {"reference": "Psalm 24:1"},
    "parse_morphology": {"code": "V-AAI-3S", "language": "greek"},
    "search_by_strongs": {"strongs": "G26", "limit": 5},
    "find_similar_passages": {"reference": "John 3:16", "limit": 5},
    "explore_genealogy": {"person": "David", "direction": "ancestors", "generations": 3},
    "people_in_passage": {"reference": "Genesis 22"},
    "explore_person_events": {"person": "Moses"},
    "explore_place": {"place": "Jerusalem"},
    "find_connection": {"person1": "Abraham", "person2": "David"},
    "graph_enriched_search": {"reference": "Genesis 22:1"},
    "get_study_notes": {"reference": "John 3:16"},
    "get_bible_dictionary": {"topic": "covenant"},
    "get_key_terms": {"term": "atonement"},
    "get_ane_context": {"reference": "Genesis 15:1"},
    "get_theology_context": {"reference": "Psalm 82:1", "limit": 3},
    "get_torah_weave": {"reference": "Genesis 1:1"},
    "get_textual_variant": {"reference": "Deuteronomy 32:8"},
}

_OPTIONAL_DATA_MESSAGES = (
    "not available",
    "not been generated",
    "no textual-variant or lxx-quotation data on file",
    "no torah weave unit contains",
    "no biblical edition data found",
)


async def run(db_path: Path) -> bool:
    registered = {tool.name for tool in TOOLS}
    covered = set(TOOL_CASES)
    if registered != covered:
        print(f"Registry mismatch: missing={sorted(registered - covered)}, extra={sorted(covered - registered)}")
        return False
    if not db_path.is_file():
        print(f"Database not found: {db_path}")
        return False

    database = StudyBibleDB(db_path)
    await database.connect()
    server_module.db = database
    failures = 0
    skips = 0
    try:
        for name, arguments in TOOL_CASES.items():
            response = await server_module.call_tool(name, arguments)
            result = response[0].text if response else ""
            lowered = result.lower()
            if not result or lowered.startswith("error:") or lowered.startswith("unknown tool"):
                failures += 1
                print(f"FAIL {name}: {result[:200]}")
            elif any(message in lowered for message in _OPTIONAL_DATA_MESSAGES):
                skips += 1
                print(f"SKIP {name}: optional data unavailable")
            else:
                print(f"OK   {name}: {len(result)} characters")
    finally:
        server_module.db = None
        await database.close()

    print(f"\n{len(TOOL_CASES) - failures - skips} passed, {skips} skipped, {failures} failed")
    return failures == 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "db" / "study_bible.db",
        help="Path to the full study_bible.db",
    )
    args = parser.parse_args()
    raise SystemExit(0 if asyncio.run(run(args.db)) else 1)


if __name__ == "__main__":
    main()
