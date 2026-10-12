import hashlib
import importlib.util
import io
import json
import sqlite3
import zipfile
import zlib
from pathlib import Path

import httpx
import pytest

from study_bible_mcp import server
from study_bible_mcp.database import StudyBibleDB, create_schema, parse_passage_reference
from study_bible_mcp.parsers.biblical_versions import (
    BRENTON_BOOK_CODES,
    LXX_BOOK_CODES,
    LXX_SOURCE_BOOKS,
    REQUIRED_BRENTON_BOOKS,
    expand_lxx_morphology,
    import_biblical_versions,
    iter_lxx_rows,
    parse_brenton_usfm,
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
LXX_ROW_FIELDS = (
    "edition", "canonical_reference", "canonical_book", "canonical_chapter",
    "canonical_verse", "canonical_verse_suffix", "edition_reference", "edition_book",
    "edition_chapter", "edition_verse", "edition_verse_suffix", "text", "translation",
    "morphology", "mapping_method",
)


def _word(surface, lemma="", parsing="", pos=""):
    return {
        "surface": surface, "lemma": lemma, "parsing": parsing, "pos": pos,
        "confidence": "high", "source": "morpheus", "reasoning": "unused",
    }


def _words(text):
    return [_word(token) for token in text.split()]


def _pair(slug, grk_ref, mt_book, mt_ref, method="tvtms"):
    return {"grk_book": slug, "grk_ref": grk_ref, "mt_book": mt_book, "mt_refs": [mt_ref], "method": method}


def _unpaired(slug, grk_ref):
    return {"grk_book": slug, "grk_ref": grk_ref, "method": "unpaired", "reason": "no-hebrew-counterpart"}


LXX_FIXTURE = {
    "psalms-lxx": [
        ("Ps 8:6", "ἠλάττωσας αὐτὸν βραχύ τι παρ᾿ ἀγγέλους"),
        ("Ps 39:7", "θυσίαν καὶ προσφορὰν οὐκ ἠθέλησας"),
        ("Ps 80:7", "ἀπέστησεν ἀπὸ ἄρσεων τὸν νῶτον αὐτοῦ"),
        ("Ps 81:1", "ψαλμὸς τῷ Ασαφ ὁ θεὸς ἔστη ἐν συναγωγῇ θεῶν"),
        ("Ps 81:6", "ἐγὼ εἶπα θεοί ἐστε καὶ υἱοὶ ὑψίστου πάντες"),
    ],
    "daniel": [
        ("Dan 3:25", "στὰς δὲ Αζαριας προσηύξατο"),
        ("Dan 3:92", "ἡ ὅρασις τοῦ τετάρτου ὁμοίωμα ἀγγέλου θεοῦ"),
    ],
    "daniel-theodotion": [
        ("Dan 3:25", "καὶ συστὰς Αζαριας προσηύξατο"),
        ("Dan 3:92", "ἡ ὅρασις τοῦ τετάρτου ὁμοία υἱῷ θεοῦ"),
    ],
    "tobit": [("Tob 1:1", "βίβλος λόγων Τωβιθ")],
}
PAIRS = [
    _pair("psalms-lxx", "8:6", "psalms", "8:5"),
    _pair("psalms-lxx", "39:7", "psalms", "40:6"),
    _pair("psalms-lxx", "80:7", "psalms", "81:6"),
    _pair("psalms-lxx", "81:1", "psalms", "82:1"),
    _pair("psalms-lxx", "81:6", "psalms", "82:6"),
    _unpaired("daniel", "3:25"),
    _pair("daniel", "3:92", "daniel", "3:25"),
    _unpaired("daniel-theodotion", "3:25"),
    _pair("daniel-theodotion", "3:92", "daniel", "3:25"),
]
BRENTON_FIXTURE = {
    "PSA": "\\c 81\n\\v 6 I said, Ye are gods; and all \\add of you\\add* children of the most High.\n",
    "DAG": (
        "\\c 3\n\\v 25 Then Azarias stood up, and prayed on this manner;\n"
        "\\v 92 and the appearance of the fourth is like the Son of God.\n"
    ),
    "TOB": "\\c 1\n\\v 1 The book of the words of Tobit.\n",
}
MT_FIXTURE = [
    ("Psa.8.5(8.6)", "Psa", 8, 5, "and/ you have made lack/ him", "וַתְּחַסְּרֵהוּ"),
    ("Psa.40.6(40.7)", "Psa", 40, 6, "sacrifice and/ offering", "זֶבַח וּמִנְחָה"),
    ("Psa.81.6(81.7)", "Psa", 81, 6, "I removed from burden", "הֲסִירוֹתִי"),
    ("Psa.82.1", "Psa", 82, 1, "God [is] standing", "אֱלֹהִים נִצָּב"),
    ("Psa.82.6", "Psa", 82, 6, "I I said gods you", "אֲנִי אָמַרְתִּי"),
    ("Dan.3.25", "Dan", 3, 25, "he answered and/ said", "עָנֵה וְאָמַר"),
    ("Luk.4.9", "Luk", 4, 9, "And he led him", "Ἤγαγεν δὲ αὐτὸν"),
]


def _write_lxx_sources(data_dir: Path) -> Path:
    lxx_dir = data_dir / "lxx"
    morphology_dir = lxx_dir / "morphology"
    morphology_dir.mkdir(parents=True)
    for slug in LXX_SOURCE_BOOKS:
        verses = [{"ref": ref, "words": _words(text)} for ref, text in LXX_FIXTURE.get(slug, [])]
        (morphology_dir / f"{slug}.json").write_text(json.dumps(verses), encoding="utf-8")
    (lxx_dir / "verse_pairs.jsonl").write_text(
        "".join(json.dumps(pair) + "\n" for pair in PAIRS), encoding="utf-8"
    )
    brenton_dir = lxx_dir / "brenton"
    brenton_dir.mkdir()
    for code in REQUIRED_BRENTON_BOOKS:
        (brenton_dir / f"{code}eng-Brenton.usfm").write_text(
            f"\\id {code}\n{BRENTON_FIXTURE.get(code, '')}", encoding="utf-8"
        )
    return lxx_dir


def _build_db(tmp_path: Path, import_lxx: bool = True) -> Path:
    db_path = tmp_path / "versions.db"
    conn = sqlite3.connect(db_path)
    create_schema(conn)
    conn.executemany(
        """INSERT INTO verses (reference, book, chapter, verse, text_english, text_original, word_data)
           VALUES (?, ?, ?, ?, ?, ?, '[]')""",
        MT_FIXTURE,
    )
    conn.commit()
    if import_lxx:
        _write_lxx_sources(tmp_path / "data")
        import_biblical_versions(conn, tmp_path / "data")
    conn.close()
    return db_path


async def _compare(db_path: Path, reference: str, versions=("MT", "LXX"), numbering="canonical") -> dict:
    database = StudyBibleDB(db_path)
    await database.connect()
    try:
        return await database.compare_biblical_versions(reference, list(versions), numbering)
    finally:
        await database.close()


def test_all_pinned_lxx_books_have_canonical_codes():
    assert len(LXX_SOURCE_BOOKS) == 59
    assert LXX_SOURCE_BOOKS <= LXX_BOOK_CODES.keys()


def test_lxx_psalm_numbering_uses_explicit_mapping(tmp_path):
    morphology_dir = tmp_path / "morphology"
    morphology_dir.mkdir()
    (morphology_dir / "psalms-lxx.json").write_text(
        json.dumps([{
            "ref": "Ps 23:1",
            "words": [_word("τοῦ", "ὁ", "masc gen sg", "article"), _word("κυρίου", "κύριος", "masc gen sg", "noun")],
        }]),
        encoding="utf-8",
    )
    pairings = {("psalms-lxx", "23:1"): [_pair("psalms-lxx", "23:1", "psalms", "24:1")]}

    row = dict(zip(LXX_ROW_FIELDS, next(iter_lxx_rows(
        morphology_dir, pairings, {("PSA", "23:1"): "The earth is the Lord's."}
    ))))

    assert row["canonical_reference"] == "Psa.24.1"
    assert row["edition_reference"] == "Psa.23.1"
    assert row["translation"] == "The earth is the Lord's."
    assert row["mapping_method"] == "tvtms"
    assert json.loads(zlib.decompress(row["morphology"]))[1] == [
        "κυρίου", "κύριος", "masc gen sg", "noun", "high", "morpheus",
    ]
    assert expand_lxx_morphology(row["morphology"])[1] == {
        "surface": "κυρίου", "lemma": "κύριος", "parsing": "masc gen sg", "pos": "noun",
        "confidence": "high", "source": "morpheus",
    }


def test_brenton_markers_keep_word_spacing(tmp_path):
    (tmp_path / "test.usfm").write_text(
        "\\id GEN\n\\c 1\n"
        "\\v 1 In \\add the\\add* beginning \\sc God\\sc* made\\f + \\fr 1:1 \\ft Or, formed.\\f* the heaven.\n"
        "\\v 2 But the earth was \\x - \\xo 1:2 \\xt Jer 4.23\\x*unsightly and \\add unfurnished\\add*, and darkness.\n"
        "\\c 81\n\\v 6 and all \\add of you\\add* children of the most High.\n",
        encoding="utf-8",
    )

    translations = parse_brenton_usfm(tmp_path)

    assert translations[("GEN", "1:1")] == "In the beginning God made the heaven."
    assert translations[("GEN", "1:2")] == "But the earth was unsightly and unfurnished, and darkness."
    assert translations[("GEN", "81:6")] == "and all of you children of the most High."


def test_brenton_is_attached_only_to_its_own_recension(tmp_path):
    morphology_dir = tmp_path / "morphology"
    morphology_dir.mkdir()
    for slug in ("daniel", "daniel-theodotion"):
        verses = [{"ref": ref, "words": _words(text)} for ref, text in LXX_FIXTURE[slug]]
        (morphology_dir / f"{slug}.json").write_text(json.dumps(verses), encoding="utf-8")
    pairings = {}
    for pair in PAIRS:
        pairings.setdefault((pair["grk_book"], pair["grk_ref"]), []).append(pair)

    rows = [dict(zip(LXX_ROW_FIELDS, row)) for row in iter_lxx_rows(
        morphology_dir, pairings, {("DAG", "3:92"): "like the Son of God."}
    )]
    by_edition = {(row["edition"], row["edition_reference"]): row for row in rows}

    assert by_edition[("Rahlfs Septuagint (1935), Theodotion", "Dan.3.92")]["translation"] == "like the Son of God."
    assert by_edition[("Rahlfs Septuagint (1935), Old Greek", "Dan.3.92")]["translation"] is None
    assert BRENTON_BOOK_CODES["daniel-theodotion"] == "DAG"
    assert BRENTON_BOOK_CODES["susanna-theodotion"] == "SUS"
    assert BRENTON_BOOK_CODES["bel-and-the-dragon-theodotion"] == "BEL"
    assert BRENTON_BOOK_CODES["judges-vaticanus-b"] == "JDG"
    assert BRENTON_BOOK_CODES["joshua-vaticanus-b"] == "JOS"
    for slug in ("daniel", "susanna", "bel-and-the-dragon", "judges", "joshua"):
        assert slug not in BRENTON_BOOK_CODES


def test_unpaired_lxx_verses_get_no_canonical_position(tmp_path):
    morphology_dir = tmp_path / "morphology"
    morphology_dir.mkdir()
    (morphology_dir / "daniel.json").write_text(
        json.dumps([{"ref": "Dan 3:25", "words": _words("στὰς δὲ Αζαριας")}]), encoding="utf-8"
    )

    row = dict(zip(LXX_ROW_FIELDS, next(iter_lxx_rows(
        morphology_dir, {("daniel", "3:25"): [_unpaired("daniel", "3:25")]}, {}
    ))))

    assert row["canonical_reference"] is None
    assert row["canonical_book"] is None
    assert row["edition_reference"] == "Dan.3.25"
    assert row["mapping_method"] == "unpaired (no-hebrew-counterpart)"


async def test_import_stores_only_lxx_rows_and_is_idempotent(tmp_path):
    db_path = _build_db(tmp_path)
    conn = sqlite3.connect(db_path)
    try:
        first = import_biblical_versions(conn, tmp_path / "data")
        second = import_biblical_versions(conn, tmp_path / "data")
        stored = conn.execute("SELECT COUNT(*) FROM biblical_version_verses").fetchone()[0]
    finally:
        conn.close()

    assert first == second == (6, 10)
    assert stored == 10


@pytest.mark.parametrize(("reference", "mt_edition", "lxx_edition"), [
    ("Psalm 40:6", "Psa.40.7", "Psa.39.7"),
    ("Psalm 8:5", "Psa.8.6", "Psa.8.6"),
    ("Psalm 82:6", "Psa.82.6", "Psa.81.6"),
])
async def test_mt_and_lxx_share_one_canonical_header(tmp_path, reference, mt_edition, lxx_edition):
    db_path = _build_db(tmp_path)

    result = await _compare(db_path, reference)
    rows = result["rows"]
    output = format_biblical_version_comparison(reference, rows)

    assert result["status"] == "ok"
    assert [row["version"] for row in rows] == ["MT", "LXX"]
    assert all(PROVENANCE_FIELDS <= row.keys() for row in rows)
    assert len({row["canonical_reference"] for row in rows}) == 1
    assert [row["edition_reference"] for row in rows] == [mt_edition, lxx_edition]
    assert output.count("\n### ") == 1
    assert "STEPBible word glosses (interlinear, not a translation)" in output
    shifted = mt_edition != rows[0]["canonical_reference"]
    assert ("TAHOT English/Hebrew versification" in output) == shifted


async def test_comparison_shows_spaced_brenton_text(tmp_path):
    db_path = _build_db(tmp_path)

    result = await _compare(db_path, "Psalm 82:6", ["LXX"])

    assert result["rows"][0]["translation"] == "I said, Ye are gods; and all of you children of the most High."
    output = format_biblical_version_comparison("Psalm 82:6", result["rows"], include_morphology=True)
    assert "- **ἐγὼ**" in output


async def test_unpaired_lxx_verses_are_not_mt_counterparts(tmp_path):
    db_path = _build_db(tmp_path)

    canonical = await _compare(db_path, "Daniel 3:25")
    greek = await _compare(db_path, "Daniel 3:25", numbering="lxx")
    output = format_biblical_version_comparison("Daniel 3:25", canonical["rows"])

    assert [(row["version"], row["edition_reference"]) for row in canonical["rows"]] == [
        ("MT", "Dan.3.25"), ("LXX", "Dan.3.92"), ("LXX", "Dan.3.92"),
    ]
    assert "Αζαριας" not in output
    assert "Azarias" not in output
    assert "Not available for this edition." in output
    assert "like the Son of God." in output
    assert [row["edition_reference"] for row in canonical["lxx_only"]] == ["Dan.3.25", "Dan.3.25"]
    assert [row["canonical_reference"] for row in greek["rows"]] == [None, None]
    assert "LXX-only, no MT counterpart" in format_biblical_version_comparison(
        "Daniel 3:25", greek["rows"], numbering="lxx"
    )


@pytest.mark.parametrize(("reference", "expected"), [
    ("Psalm 81:6-82:1", ["Psa.81.6", "Psa.81.6", "Psa.82.1", "Psa.82.1"]),
    ("Psalm 81:6–82:1", ["Psa.81.6", "Psa.81.6", "Psa.82.1", "Psa.82.1"]),
    ("Psalm 82", ["Psa.82.1", "Psa.82.1", "Psa.82.6", "Psa.82.6"]),
    ("Psalm 82:1-8", ["Psa.82.1", "Psa.82.1", "Psa.82.6", "Psa.82.6"]),
    ("Ps82:6", ["Psa.82.6", "Psa.82.6"]),
])
async def test_comparison_reference_forms(tmp_path, reference, expected):
    db_path = _build_db(tmp_path)

    result = await _compare(db_path, reference)

    assert result["status"] == "ok"
    assert [row["canonical_reference"] for row in result["rows"]] == expected


@pytest.mark.parametrize(("reference", "status"), [
    ("Luke 4:9", "new_testament"),
    ("Psalm eighty-two", "invalid_reference"),
    ("Psalm 82:8-1", "invalid_reference"),
    ("Hezekiah 1:1", "unsupported_book"),
    ("Psalm 24:1a", "no_match"),
    ("Psalm 30:1", "no_match"),
])
async def test_comparison_reports_why_nothing_matched(tmp_path, reference, status):
    db_path = _build_db(tmp_path)

    assert (await _compare(db_path, reference))["status"] == status


async def test_lxx_only_books_use_edition_numbering(tmp_path):
    db_path = _build_db(tmp_path)

    result = await _compare(db_path, "Tobit 1:1")

    assert result["numbering"] == "lxx"
    assert [(row["version"], row["edition_reference"]) for row in result["rows"]] == [("LXX", "Tob.1.1")]
    assert result["rows"][0]["translation"] == "The book of the words of Tobit."


async def test_missing_lxx_table_is_reported_but_mt_still_works(tmp_path):
    db_path = _build_db(tmp_path, import_lxx=False)

    assert (await _compare(db_path, "Psalm 82:6"))["status"] == "missing_data"
    mt_only = await _compare(db_path, "Psalm 82:6", ["MT"])
    assert [row["edition_reference"] for row in mt_only["rows"]] == ["Psa.82.6"]


@pytest.mark.parametrize(("reference", "versions", "expected", "absent"), [
    ("Luke 4:9", ["MT", "LXX"], "Old Testament and Septuagint books only", "download"),
    ("Psalm eighty-two", ["MT", "LXX"], "Could not parse", "download"),
    ("Psalm 30:1", ["MT", "LXX"], "No MT or LXX text found for Psalm 30:1", "download"),
    ("Daniel 3:25", ["LXX"], "numbering='lxx'", "Αζαριας"),
])
async def test_handler_messages(tmp_path, monkeypatch, reference, versions, expected, absent):
    database = StudyBibleDB(_build_db(tmp_path))
    await database.connect()
    monkeypatch.setattr(server, "db", database)
    try:
        response = await server.handle_compare_biblical_versions(
            {"reference": reference, "versions": versions}
        )
    finally:
        await database.close()

    assert expected in response[0].text
    assert absent not in response[0].text


async def test_handler_suggests_upgrade_when_table_is_empty(tmp_path, monkeypatch):
    database = StudyBibleDB(_build_db(tmp_path, import_lxx=False))
    await database.connect()
    monkeypatch.setattr(server, "db", database)
    try:
        response = await server.handle_compare_biblical_versions({"reference": "Psalm 82:6"})
    finally:
        await database.close()

    assert "python scripts/upgrade_database.py" in response[0].text
    assert "download" not in response[0].text


def test_partial_lxx_download_fails_loudly(tmp_path):
    lxx_dir = _write_lxx_sources(tmp_path / "data")
    (lxx_dir / "morphology" / "sirach.json").unlink()
    conn = sqlite3.connect(":memory:")
    create_schema(conn)

    with pytest.raises(FileNotFoundError, match="morphology/sirach.json"):
        import_biblical_versions(conn, tmp_path / "data")


def test_parse_passage_reference_bounds():
    assert parse_passage_reference("Psalm 81:6-82:1")[1:] == ("Psa", (81, 6, ""), (82, 1, "~"))
    assert parse_passage_reference("Psalm 82")[1:] == ("Psa", (82, 0, ""), (82, 9999, "~"))
    assert parse_passage_reference("1 Kings 2:35a")[1:] == ("1Ki", (2, 35, "a"), (2, 35, "a"))


@pytest.mark.parametrize(("reference", "expected"), [
    ("John3:16", "Jhn.3.16"),
    ("1John3:16", "1Jn.3.16"),
    ("Gen1:1", "Gen.1.1"),
    ("Ps23:1", "Psa.23.1"),
    ("John 3:16", "Jhn.3.16"),
    ("Song of Solomon 2:1", "Sng.2.1"),
])
def test_normalize_reference_accepts_compact_forms(tmp_path, reference, expected):
    assert StudyBibleDB(tmp_path / "unused.db")._normalize_reference(reference) == expected


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


def test_formatter_prints_non_identity_mapping_even_when_references_match():
    row = {
        "version": "LXX", "edition": "Rahlfs Septuagint (1935)", "language": "Greek",
        "canonical_reference": "Psa.8.6", "edition_reference": "Psa.8.6", "text": "x",
        "translation": None, "license": "CC BY 4.0", "source_url": "https://example.test",
        "mapping_method": "tvtms",
    }

    assert "**Versification mapping:** tvtms" in format_biblical_version_comparison("Psalm 8:6", [row])


def _load_download_script():
    path = Path(__file__).resolve().parent.parent / "scripts" / "download_biblical_versions.py"
    spec = importlib.util.spec_from_file_location("download_biblical_versions", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _zip_client(files: dict[str, bytes]) -> httpx.Client:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in files.items():
            archive.writestr(f"eng-Brenton_usfm/{name}", content)
    return httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, content=buffer.getvalue())
    ))


def test_brenton_download_verifies_extracted_books_not_archive_bytes(tmp_path, monkeypatch):
    module = _load_download_script()
    good = b"\\id GEN\n\\c 1\n\\v 1 In the beginning.\n"
    monkeypatch.setattr(module, "BRENTON_USFM_SHA256", {"02-GENeng-Brenton.usfm": hashlib.sha256(good).hexdigest()})

    manifest = module._download_brenton(_zip_client({"02-GENeng-Brenton.usfm": good}), tmp_path, force=False)

    assert (tmp_path / "brenton" / "02-GENeng-Brenton.usfm").read_bytes() == good
    assert manifest["files"]["02-GENeng-Brenton.usfm"]["sha256"] == hashlib.sha256(good).hexdigest()


def test_brenton_mismatch_keeps_user_files(tmp_path, monkeypatch):
    module = _load_download_script()
    good = b"\\id GEN\n\\c 1\n\\v 1 In the beginning.\n"
    local = tmp_path / "brenton" / "02-GENeng-Brenton.usfm"
    local.parent.mkdir()
    local.write_bytes(good)
    monkeypatch.setattr(module, "BRENTON_USFM_SHA256", {"02-GENeng-Brenton.usfm": hashlib.sha256(good).hexdigest()})

    with pytest.raises(RuntimeError, match="02-GENeng-Brenton.usfm"):
        module._download_brenton(_zip_client({"02-GENeng-Brenton.usfm": b"changed"}), tmp_path, force=True)
    assert local.read_bytes() == good

    local.write_bytes(b"locally edited")
    with pytest.raises(RuntimeError, match="left in place"):
        module._download_brenton(_zip_client({}), tmp_path, force=False)
    assert local.read_bytes() == b"locally edited"


def test_download_default_data_dir_is_project_relative():
    module = _load_download_script()
    default = next(param.default for param in module.main.params if param.name == "data_dir")

    assert default == Path(module.__file__).resolve().parent.parent / "data" / "lxx"
