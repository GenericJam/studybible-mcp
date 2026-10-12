"""Theology content import: entry-level source links and re-import of an existing DB."""

import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

from study_bible_mcp.database import create_schema
from study_bible_mcp.parsers.heiser import parse_heiser_content_file

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def _load_script(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_content(path: Path, file_url: str | None, entries: list[dict]) -> Path:
    path.write_text(json.dumps({
        "source_key": "Owen — Communion with God (1657)",
        "title": "Of Communion with God",
        "author": "owen",
        "type": "treatise",
        "url": file_url,
        "entries": entries,
    }))
    return path


def _entry(chapter: str, summary: str = "Summary.", **extra) -> dict:
    return {
        "chapter_or_episode": chapter,
        "title": f"Title for {chapter}",
        "content_summary": summary,
        "references": [{"reference": "1Jn.1.3", "relevance": "primary"}],
        "themes": ["communion_with_god"],
        **extra,
    }


WORK_URL = "https://ccel.org/ccel/owen/communion/communion"
CHAPTER_URL = "https://ccel.org/ccel/owen/communion/communion.i.vi.i.html"


def test_entry_url_overrides_file_url(tmp_path):
    path = _write_content(tmp_path / "c.json", WORK_URL, [
        _entry("Part I, ch. 1", url=CHAPTER_URL),
        _entry("Part III"),
    ])

    urls = {entry["chapter_or_episode"]: entry["url"]
            for entry, _, _ in parse_heiser_content_file(path)}

    assert urls == {"Part I, ch. 1": CHAPTER_URL, "Part III": WORK_URL}


@pytest.fixture
def db(tmp_path):
    conn = sqlite3.connect(tmp_path / "study_bible.db")
    create_schema(conn)
    yield conn
    conn.close()


def _rows(conn):
    return conn.execute(
        "SELECT chapter_or_episode, content_summary, url FROM theology_content ORDER BY id"
    ).fetchall()


def _index_counts(conn):
    return (
        conn.execute("SELECT COUNT(*) FROM theology_verse_index").fetchone()[0],
        conn.execute("SELECT COUNT(*) FROM theology_theme_index").fetchone()[0],
    )


def test_reimport_updates_url_without_duplicating_rows(db, tmp_path):
    importer = _load_script("import_heiser_content")
    path = tmp_path / "c.json"

    _write_content(path, None, [_entry("Part I", "Original summary.")])
    assert importer.import_content_file(db, path)[:2] == (1, 0)
    assert _rows(db) == [("Part I", "Original summary.", None)]

    # The data file now carries a link (and an edited summary, which the
    # upgrade must not apply to the existing row).
    _write_content(path, WORK_URL, [_entry("Part I", "Edited summary.", url=CHAPTER_URL)])
    assert importer.import_content_file(db, path)[:2] == (0, 1)
    assert importer.import_content_file(db, path)[:2] == (0, 0)

    assert _rows(db) == [("Part I", "Original summary.", CHAPTER_URL)]
    assert _index_counts(db) == (1, 1)


def test_reformed_evangelical_reimport_does_not_duplicate_theme_links(db, tmp_path, monkeypatch):
    importer = _load_script("ingest_reformed_evangelical")
    content_dir = tmp_path / "owen"
    content_dir.mkdir()
    _write_content(content_dir / "c.json", WORK_URL, [_entry("Part I"), _entry("Part II")])
    monkeypatch.setattr(importer, "CONTENT_DIRS", [content_dir])

    importer.import_content(db)
    importer.import_content(db)

    assert [r[2] for r in _rows(db)] == [WORK_URL, WORK_URL]
    assert _index_counts(db) == (2, 2)
