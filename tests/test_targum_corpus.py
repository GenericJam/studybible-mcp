"""Targum corpus: Sefaria version files, verse keying, licences, links, citations, tools."""

import json
import sqlite3
from pathlib import Path

import httpx
import pytest

from study_bible_mcp import server
from study_bible_mcp.corpora import CORPORA, combined_license, resolve_citation, targum
from study_bible_mcp.corpora.download import git_blob_sha1
from study_bible_mcp.database import StudyBibleDB, create_schema
from study_bible_mcp.parsers.ancient_sources import import_corpus

ONKELOS_GEN_1_1 = "בְּקַדְמִין בְּרָא יְיָ יָת שְׁמַיָּא וְיָת אַרְעָא"
ONKELOS_GEN_6_1 = "וַהֲוָה אֲרֵי שָׁרִיאוּ בְּנֵי אֲנָשָׁא לְמִסְגֵּי"
ONKELOS_EXOD_12_40 = "וּמוֹתַב בְּנֵי יִשְׂרָאֵל דִי יְתִיבוּ בְּמִצְרָיִם אַרְבַּע מְאָה וּתְלָתִין שְׁנִין:"
PSJ_DEUT_32_8 = "בְּאַחְסָנוּת עִלָאָה עַלְמָא לְעַמְמַיָא רָמָא פִיצְתָא עִם שׁוּבְעִין מַלְאָכַיָא"
PSJ_SARAH = "וּלְוַת שָרָה אָמַר הָא יְהָבִית אֶלֶף סִלְעִין דִכְסַף לְאָחוּךְ"
PSJ_LAND = "וַאֲמַר אֲבִימֶלְךְ הָא אַרְעִי קַדְמָךְ וּבִדַתָקֵן בְּעֵינָךְ תִּיב"
PSJ_PRAYER = "וְצַלִי אַבְרָהָם קֳדָם יְיָ וְאָסֵי יְיָ יַת אֲבִימֶלֶךְ אֲרוּם מֵיחַד אָחִיד"


def _chapters(**verses_by_chapter) -> list[list[str]]:
    """Chapter lists from {'c12': [...]}; missing chapters are empty."""
    last = max(int(key[1:]) for key in verses_by_chapter)
    return [verses_by_chapter.get(f"c{number}", []) for number in range(1, last + 1)]


def _texts() -> dict[str, list[list[str]]]:
    texts = {source.filename: [[f"{source.filename} 1:1"]] for source in targum.SOURCE_FILES}
    texts["onkelos_genesis.aramaic.json"] = _chapters(
        c1=[ONKELOS_GEN_1_1], c6=[ONKELOS_GEN_6_1], c49=[""] * 11 + ["??? יְסַמְקוּן טּוּרוֹהִי"],
    )
    texts["onkelos_genesis.metsudah.json"] = _chapters(
        c1=["In the beginning [<b>In the first times</b>] Elohim <i>created</i>  the heavens."],
        c6=["Metsudah 6:1, not used where Etheridge exists."],
        c49=[""] * 11 + ["His eyes are red from wine."],
    )
    texts["onkelos_genesis.etheridge.json"] = _chapters(
        c6=["And it was when the sons of men had begun to multiply upon the earth,"],
    )
    texts["onkelos_exodus.aramaic.json"] = _chapters(c12=[""] * 39 + [ONKELOS_EXOD_12_40])
    texts["onkelos_exodus.metsudah.json"] = _chapters(
        c12=[""] * 39 + ["The habitation of the B&#8217;nei Yisrael in Egypt [<b>lasted</b>] 430 years."],
    )
    texts["pseudo_jonathan_genesis.aramaic.json"] = _chapters(
        c1=["בְּקַדְמִין בְּרָא יְיָ"], c20=["א"] * 14 + [PSJ_SARAH, PSJ_LAND, PSJ_PRAYER],
    )
    texts["pseudo_jonathan_genesis.etheridge.json"] = _chapters(
        c1=["In the beginning the Lord created."],
        c20=["E"] * 14 + [
            " {And Abimelech said, Behold, my land is before you; and wherever is right in your eyes, dwell.}  ",
            " And to Sarah he said, Behold, I have given a thousand sileen of silver to thy brother. ",
        ],
    )
    texts["pseudo_jonathan_deuteronomy.aramaic.json"] = _chapters(c32=[""] * 7 + [PSJ_DEUT_32_8, "ט"])
    texts["pseudo_jonathan_deuteronomy.etheridge.json"] = _chapters(c32=[""] * 7 + [
        " When the Most High made allotment of the world, He cast the lot among the seventy "
        "angels; the Word &lt;mymr'&gt; {Dibbura} of the Lord * was there ; *Or, the decree\n",
        " _  ",
    ])
    return texts


def _write(directory: Path, texts=None, **declaration) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    for source in targum.SOURCE_FILES:
        data = {
            "language": source.language,
            "versionTitle": source.version_title,
            "license": source.license,
            "text": (texts or _texts())[source.filename],
            **declaration.get(source.filename, {}),
        }
        (directory / source.filename).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return directory


def _rows(directory: Path) -> dict[str, dict]:
    return {row["reference"]: row for row in targum.parse(directory)}


# --- Parser


def test_onkelos_pairs_aramaic_with_etheridge_where_he_exists_and_labelled_metsudah_elsewhere(tmp_path):
    rows = _rows(_write(tmp_path))

    genesis = rows["Targum Onkelos Genesis 1:1"]
    etheridge = rows["Targum Onkelos Genesis 6:1"]
    exodus = rows["Targum Onkelos Exodus 12:40"]

    assert genesis["original_text"] == ONKELOS_GEN_1_1
    assert genesis["text"] == "In the beginning [In the first times] Elohim created the heavens."
    assert genesis["translation"] == targum.METSUDAH
    assert "not a direct translation of the Aramaic" in genesis["translation"]
    assert genesis["translation_license"] == "CC BY-NC 4.0"
    assert genesis["license"] == "Aramaic text: Public domain; English translation: CC BY-NC 4.0"
    assert (genesis["sort_start"], genesis["sort_end"], genesis["work_key"]) == (1001, 1001, "onkelos_genesis")
    assert genesis["author"] == "Targum Onkelos" and genesis["period_tags"] == "amoraic"
    assert etheridge["text"].startswith("And it was when the sons of men")
    assert etheridge["translation"] == targum.ETHERIDGE_ONKELOS
    assert etheridge["license"] == "Public domain"
    assert exodus["text"] == "The habitation of the B’nei Yisrael in Egypt [lasted] 430 years."
    assert exodus["original_text"] == ONKELOS_EXOD_12_40
    assert rows["Targum Onkelos Genesis 49:12"]["original_text"] == "יְסַמְקוּן טּוּרוֹהִי"
    assert "Sefaria-Export-Archive/tree/" in genesis["source_url"]
    assert targum.SEFARIA_COMMIT in genesis["source_url"]


def test_pseudo_jonathan_keys_genesis_20_by_masoretic_verse_and_cleans_etheridge(tmp_path):
    rows = _rows(_write(tmp_path))

    land = rows["Targum Pseudo-Jonathan Genesis 20:15"]
    sarah = rows["Targum Pseudo-Jonathan Genesis 20:16"]
    prayer = rows["Targum Pseudo-Jonathan Genesis 20:17–18"]
    deut = rows["Targum Pseudo-Jonathan Deuteronomy 32:8"]

    assert land["original_text"] == PSJ_LAND
    assert "text" not in land and "translation" not in land and "translation_license" not in land
    assert land["license"] == "Public domain"
    assert sarah["original_text"] == PSJ_SARAH
    assert sarah["text"].startswith("And to Sarah he said")
    assert (prayer["sort_start"], prayer["sort_end"]) == (20017, 20018)
    assert deut["text"] == (
        "When the Most High made allotment of the world, He cast the lot among the seventy "
        "angels; the Word of the Lord was there;"
    )
    assert deut["translation"] == targum.ETHERIDGE_PSEUDO_JONATHAN
    assert deut["period_tags"] == "amoraic,early_islamic"
    assert "text" not in rows["Targum Pseudo-Jonathan Deuteronomy 32:9"]
    for row in rows.values():
        assert row["license"] == combined_license(
            row.get("original_license"), row.get("translation_license"), "Aramaic"
        )


def test_supplied_clauses_become_ellipses_and_glosses_are_dropped():
    assert targum.clean_etheridge(
        "{for we are about to destroy this place,} for the cry of it is great."
    ) == "… for the cry of it is great."
    assert targum.clean_etheridge("the Oracle {Word; Dibbera} speaking") == "the Oracle speaking"
    assert targum.clean_etheridge(" . . .\n") == ""
    with pytest.raises(ValueError, match="markup"):
        targum.clean_etheridge("a stray { brace")
    with pytest.raises(ValueError, match="markup"):
        targum.clean_metsudah("an <sup>1</sup> footnote")


def test_parser_rejects_unexpected_licence_and_missing_correction(tmp_path):
    _write(tmp_path / "licence", **{"onkelos_exodus.metsudah.json": {"license": "unknown"}})
    texts = _texts()
    texts["onkelos_genesis.aramaic.json"][48][11] = "יְסַמְקוּן"
    _write(tmp_path / "artefact", texts)

    with pytest.raises(ValueError, match="expected Sefaria license 'CC-BY-NC'"):
        targum.parse(tmp_path / "licence")
    with pytest.raises(ValueError, match="expected '\\?\\?\\? '"):
        targum.parse(tmp_path / "artefact")


def test_require_files_names_every_missing_file(tmp_path):
    directory = _write(tmp_path)
    (directory / "onkelos_genesis.etheridge.json").unlink()
    (directory / "pseudo_jonathan_numbers.aramaic.json").unlink()

    with pytest.raises(FileNotFoundError) as error:
        targum.require_targum_files(directory)

    assert "onkelos_genesis.etheridge.json, pseudo_jonathan_numbers.aramaic.json" in str(error.value)
    assert len(targum.SOURCE_FILES) == 21


# --- Import and links


def _import(tmp_path: Path) -> Path:
    db_path = tmp_path / "targum.db"
    conn = sqlite3.connect(db_path)
    create_schema(conn)
    import_corpus(conn, "targum", _write(tmp_path / "targum"))
    conn.close()
    return db_path


def test_import_links_every_passage_to_the_verse_it_translates_and_is_idempotent(tmp_path):
    db_path = _import(tmp_path)
    conn = sqlite3.connect(db_path)
    count = conn.execute("SELECT COUNT(*) FROM ancient_source_passages").fetchone()[0]

    assert import_corpus(conn, "targum", tmp_path / "targum") == count
    assert conn.execute("SELECT COUNT(*) FROM ancient_source_biblical_links").fetchone()[0] == count
    assert conn.execute(
        "SELECT l.biblical_book, l.sort_start, l.sort_end, l.relationship_note "
        "FROM ancient_source_biblical_links l JOIN ancient_source_passages p ON p.id = l.passage_id "
        "WHERE p.reference = ?",
        ("Targum Pseudo-Jonathan Genesis 20:17–18",),
    ).fetchone() == ("Gen", 20017, 20018, "Translation of: Gen 20:17–18")
    assert conn.execute(
        "SELECT l.biblical_book, l.relationship_note FROM ancient_source_biblical_links l "
        "JOIN ancient_source_passages p ON p.id = l.passage_id WHERE p.reference = ?",
        ("Targum Pseudo-Jonathan Deuteronomy 32:8",),
    ).fetchone() == ("Deu", "Translation of: Deut 32:8")
    assert conn.execute(
        "SELECT collection, work_title FROM ancient_source_passages WHERE reference = ?",
        ("Targum Onkelos Exodus 12:40",),
    ).fetchone() == ("Targum Onkelos", "Exodus")


# --- Citations


@pytest.mark.parametrize("citation, work_key, sort_range", [
    ("Onkelos Genesis 1:1", "onkelos_genesis", (1001, 1001)),
    ("Targum Onkelos Gen 1:1", "onkelos_genesis", (1001, 1001)),
    ("Tg. Onq. Gen. 1:1-3", "onkelos_genesis", (1001, 1003)),
    ("Onqelos Exodus 12", "onkelos_exodus", (12000, 12999)),
    ("Onkelos Num 24:17", "onkelos_numbers", (24017, 24017)),
    ("Pseudo-Jonathan Exodus 12:40", "pseudo_jonathan_exodus", (12040, 12040)),
    ("Tg. Ps.-J. Deut 32:8", "pseudo_jonathan_deuteronomy", (32008, 32008)),
    ("Pseudo-Jonathan Deuteronomy 32:8", "pseudo_jonathan_deuteronomy", (32008, 32008)),
    ("Targum Pseudo-Jonathan Lev 16:1–2", "pseudo_jonathan_leviticus", (16001, 16002)),
    ("Targum Jonathan Genesis 20:17", "pseudo_jonathan_genesis", (20017, 20017)),
    ("TPsJ Gen 20:15", "pseudo_jonathan_genesis", (20015, 20015)),
])
def test_resolves_targum_citations(citation, work_key, sort_range):
    resolution = resolve_citation(citation, list(CORPORA.values()))

    assert resolution.status == "ok", (citation, resolution)
    assert (resolution.match.corpus.key, resolution.match.work.key, resolution.match.sort_range) == (
        "targum", work_key, sort_range,
    )


def test_bare_book_citation_is_ambiguous_between_targums():
    resolution = resolve_citation("Genesis 1:1", list(CORPORA.values()))

    assert resolution.status == "ambiguous"
    assert {match.work.key for match in resolution.candidates} == {
        "onkelos_genesis", "pseudo_jonathan_genesis",
    }


# --- Tools


async def _call(path: Path, monkeypatch, handler, **args) -> str:
    database = StudyBibleDB(path)
    await database.connect()
    monkeypatch.setattr(server, "db", database)
    try:
        return (await handler(args))[0].text
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_lookup_shows_aramaic_english_and_licence_labels(tmp_path, monkeypatch):
    db_path = _import(tmp_path)
    lookup = server.handle_lookup_ancient_source

    deut = await _call(db_path, monkeypatch, lookup, citation="Pseudo-Jonathan Deuteronomy 32:8")
    onkelos = await _call(db_path, monkeypatch, lookup, citation="Onkelos Genesis 1:1")
    original_only = await _call(db_path, monkeypatch, lookup, citation="Tg. Ps.-J. Gen 20:15")
    span = await _call(db_path, monkeypatch, lookup, citation="Pseudo-Jonathan Genesis 20:18")

    assert deut.startswith("## Targum Pseudo-Jonathan Deuteronomy 32:8")
    assert "seventy angels" in deut and PSJ_DEUT_32_8 in deut
    assert "**License:** Public domain" in deut
    assert "Translation of: Deut 32:8" in deut
    assert ONKELOS_GEN_1_1 in onkelos and "In the beginning [In the first times]" in onkelos
    assert "not a direct translation of the Aramaic" in onkelos
    assert "**License:** Aramaic text: Public domain; English translation: CC BY-NC 4.0" in onkelos
    assert "**English translation:** none openly licensed" in original_only
    assert PSJ_LAND in original_only
    assert "Targum Pseudo-Jonathan Genesis 20:17–18" in span


@pytest.mark.asyncio
async def test_search_filters_by_biblical_reference_and_folds_aramaic(tmp_path, monkeypatch):
    db_path = _import(tmp_path)
    search = server.handle_search_ancient_sources

    linked = await _call(
        db_path, monkeypatch, search, query="angels", corpus="targum",
        biblical_reference="Deuteronomy 32:8",
    )
    elsewhere = await _call(
        db_path, monkeypatch, search, query="angels", corpus="targum",
        biblical_reference="Genesis 1:1",
    )
    aramaic = await _call(db_path, monkeypatch, search, query="מלאכיא", corpus="targum")

    assert "Targum Pseudo-Jonathan Deuteronomy 32:8" in linked
    assert "No Targum passage contains" in elsewhere
    assert "Targum Pseudo-Jonathan Deuteronomy 32:8" in aramaic


# --- Download


def test_download_verifies_blob_hashes_and_declared_licences(tmp_path):
    texts = _texts()
    contents = {}
    for source in targum.SOURCE_FILES:
        data = {"language": source.language, "versionTitle": source.version_title,
                "license": source.license, "text": texts[source.filename]}
        contents[source.raw_url] = json.dumps(data).encode()
    pins = {source.filename: git_blob_sha1(contents[source.raw_url]) for source in targum.SOURCE_FILES}
    sources = tuple(
        targum.SourceFile(**{**source.__dict__, "git_blob_sha1": pins[source.filename]})
        for source in targum.SOURCE_FILES
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=contents[str(request.url)])

    with pytest.MonkeyPatch.context() as patch, httpx.Client(transport=httpx.MockTransport(handler)) as client:
        patch.setattr(targum, "SOURCE_FILES", sources)
        manifest = targum.download(client, tmp_path / "ok", force=False)
        targum.require_targum_files(tmp_path / "ok")

        bad = sources[0]
        contents[bad.raw_url] = json.dumps({
            "language": bad.language, "versionTitle": bad.version_title, "license": "unknown", "text": [],
        }).encode()
        patch.setattr(targum, "SOURCE_FILES", (
            targum.SourceFile(**{**bad.__dict__, "git_blob_sha1": git_blob_sha1(contents[bad.raw_url])}),
        ))
        with pytest.raises(RuntimeError, match="expected Sefaria license"):
            targum.download(client, tmp_path / "bad", force=False)
        with pytest.raises(RuntimeError, match="Checksum mismatch"):
            patch.setattr(targum, "SOURCE_FILES", sources[:1])
            targum.download(client, tmp_path / "tampered", force=False)

    assert manifest["repository"] == "Sefaria/Sefaria-Export-Archive"
    assert manifest["commit"] == targum.SEFARIA_COMMIT
    assert len(manifest["files"]) == 21
    assert manifest["files"]["onkelos_genesis.metsudah.json"]["license"] == "CC-BY-NC"
