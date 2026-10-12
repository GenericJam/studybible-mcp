"""Rabbinic corpus: locators, text cleaning, parsing, import, citations, tools, download."""

import hashlib
import json
import sqlite3
from pathlib import Path

import httpx
import pytest

from study_bible_mcp import server
from study_bible_mcp.corpora import CORPORA, combined_license, rabbinic, resolve_citation
from study_bible_mcp.corpora.download import git_blob_sha1
from study_bible_mcp.corpora.rabbinic import (
    DAF_AMUD_SEGMENT,
    TOSEFTA_MENAHOT,
    align_segments,
    chunk_beads,
    clean_html,
    render_wikitext,
)
from study_bible_mcp.database import StudyBibleDB, create_schema
from study_bible_mcp.parsers.ancient_sources import import_corpus

CORPUS = CORPORA["rabbinic"]

SHEKALIM = "Mishnah/Seder Moed/Mishnah Shekalim"
MENACHOT = "Tosefta/Vilna Edition/Seder Kodashim/Tosefta Menachot"
BAVA_BATRA = "Talmud/Bavli/Seder Nezikin/Bava Batra"
VAYIKRA = "Midrash/Aggadah/Midrash Rabbah/Vayikra Rabbah"
SHEKALIM_HEBREW = "באחד באדר משמיעין על השקלים"
LONG = "Herod built the Temple with stones of white marble. " * 20  # ~1,000 characters
MID = "Bava ben Buta gave counsel to Herod. " * 26  # ~960 characters


def _version(title, license_, text, language="en"):
    return {"versionTitle": title, "license": license_, "language": language, "text": text}


def _bava_batra_amudim(english=True):
    """Amudim 1a-4a. 3b has ten long segments; 4a has six short ones. 2b (eight
    segments, 7,700 characters) and 3a (ten long segments) have one more
    Aramaic segment than English."""
    if english:
        return [
            [], [], ["2a one"], [f"{MID}{n}" for n in range(1, 9)],
            [f"{LONG} 3a segment {n}" for n in range(1, 11)],
            [f"<b>{LONG}</b> segment {n}" for n in range(1, 11)],
            [f"4a segment {n}: Herod’s building, white and green marble" for n in range(1, 7)],
        ]
    return [
        [], [], ["ארמית ne"], [f"ארמית ב {n}" for n in range(1, 10)],
        [f"ארמית א {n}" for n in range(1, 12)], [f"ארמית {n}" for n in range(1, 11)],
        [f"ארמית {n}" for n in range(1, 7)],
    ]


FILES = {
    f"{SHEKALIM}/Hebrew/Mishnah, ed. Romm, Vilna 1913.json": _version(
        "Mishnah, ed. Romm, Vilna 1913", "Public Domain",
        [[f"<b>{SHEKALIM_HEBREW}</b>", "ב", "שולחנות היו יושבין במדינה"]], "he"),
    f"{SHEKALIM}/English/Mishnah Yomit by Dr. Joshua Kulp.json": _version(
        "Mishnah Yomit by Dr. Joshua Kulp", "CC-BY",
        [["On the first of Adar they announce the shekels.<br>Second line.", "Two.",
          "They set up tables of money changers."], ["An English-only mishnah."]]),
    f"{MENACHOT}/Hebrew/Tosefta Menahot - Machon Mamre.json": _version(
        "Tosefta Menahot - Machon Mamre", "Public Domain",
        [["הלכה א"], [], [], [], [], [], [], [], [], [], [], [],
         ["א", "ב", "ג", "אוי לי מבית ביתוס &lt;כשר&gt;"]], "he"),
    f"{MENACHOT}/English/Sefaria Community Translation.json": _version(
        "Sefaria Community Translation", "CC0",
        [["Halakhah one<sup>*</sup><i class=\"footnote\">a <i>note</i></i> continues."]]),
    f"{BAVA_BATRA}/Hebrew/William Davidson Edition - Aramaic.json": _version(
        "William Davidson Edition - Aramaic", "CC-BY-NC", _bava_batra_amudim(english=False), "he"),
    f"{BAVA_BATRA}/English/William Davidson Edition - English.json": _version(
        "William Davidson Edition - English", "CC-BY-NC", _bava_batra_amudim()),
    f"{VAYIKRA}/English/The Sefaria Midrash Rabbah, 2022.json": _version(
        "The Sefaria Midrash Rabbah, 2022", "CC-BY",
        [[], [], ["", "", "", "",
                  "King Agrippa<sup class=\"footnote-marker\">1</sup><i class=\"footnote\">Note.</i> "
                  "sought to sacrifice. <Behold> the poor man."]]),
}
WIKITEXT = (
    "{{סרגל ניווט|ויקרא רבה|ג|ד|ה|ו|}}{{פמ|ויקרא רבה|ג|ה}}\n"
    "{{צמד|ויקרא א יז|ושסע אותו בכנפיו}} {{הה\"ד}} {{מצ|משלי|ג|ט|כבד את ה'}}\n"
    "* אגריפס המלך<ref>הערה {{קטן|(מתנות כהונה)}}</ref> ביקש להקריב '''אלף''' עולות "
    "[[קטגוריה:אגריפס]][[ויקרא א|ויקרא]] {{ב|קומפון|אסיפה}}{{ש}}שורה\n== הערות ==\n<references/>\n"
)
REVISION = 2845408


@pytest.fixture
def fixture_dir(tmp_path, monkeypatch):
    directory = tmp_path / "rabbinic"
    blobs = []
    for path, version in FILES.items():
        content = json.dumps(version, ensure_ascii=False).encode()
        (directory / "sefaria" / path).parent.mkdir(parents=True, exist_ok=True)
        (directory / "sefaria" / path).write_bytes(content)
        blobs.append(f"{git_blob_sha1(content)}  {path}")
    wiki = WIKITEXT.encode()
    (directory / "wikisource").mkdir(parents=True)
    (directory / "wikisource" / "vayikra_rabbah_03_05.wiki").write_bytes(wiki)
    monkeypatch.setattr(rabbinic, "_SEFARIA_BLOBS", "\n".join(blobs))
    monkeypatch.setattr(
        rabbinic, "_WIKISOURCE_REVISIONS", f"3:5 {REVISION} {hashlib.sha1(wiki).hexdigest()}"
    )
    keys = {"mishnah_shekalim", "tosefta_menachot", "bavli_bava_batra"}
    monkeypatch.setattr(
        rabbinic, "SOURCES", tuple(source for source in rabbinic.SOURCES if source.work.key in keys)
    )
    monkeypatch.setattr(rabbinic, "_TEXT_CORRECTIONS", ())
    monkeypatch.setattr(rabbinic, "_WIKITEXT_CORRECTIONS", {})
    monkeypatch.setattr(rabbinic, "_BIBLICAL_LINKS", (
        ("mishnah_shekalim", "1:1", "Mat", 17, 24, 17, 27, "Related account: half-shekel"),
        ("bavli_bava_batra", "4a:4", "Jhn", 2, 20, 2, 20, "Narrative overlap: Herod rebuilds the temple"),
    ))
    return directory


def _rows(directory):
    return {row["reference"]: row for row in rabbinic.parse(directory)}


# --- Locators


@pytest.mark.parametrize("text, expected", [
    ("3b", (700, 799)),
    ("3b-4a", (700, 899)),
    ("3a-b", (600, 799)),
    ("3", (600, 799)),
    ("3b:5", (705, 705)),
    ("3b:5-9", (705, 709)),
    ("3b:5–4a:2", (705, 802)),
    ("3b:5-4a", (705, 899)),
    ("57a:8", (11408, 11408)),
])
def test_amud_segment_locator(text, expected):
    assert DAF_AMUD_SEGMENT.parse(text) == expected


@pytest.mark.parametrize("text", ["3:5", "4a-3b", "3b:9-5", "3c", "1:1", "3b:100", "0a"])
def test_amud_segment_locator_rejects(text):
    assert DAF_AMUD_SEGMENT.parse(text) is None


def test_amud_segment_locator_formats_amudim_and_segments():
    assert DAF_AMUD_SEGMENT.format_range(700, 899) == "3b–4a"
    assert DAF_AMUD_SEGMENT.format_range(600, 799) == "3"
    assert DAF_AMUD_SEGMENT.format_range(705, 709) == "3b:5–9"
    assert DAF_AMUD_SEGMENT.format_range(705, 802) == "3b:5–4a:2"
    assert DAF_AMUD_SEGMENT.key(57, "a", 8) == 11408


def test_tosefta_menahot_reads_zuckermandel_13_18_to_23_as_vilna_13_4():
    assert TOSEFTA_MENAHOT.parse("13:22") == (13004, 13004)
    assert TOSEFTA_MENAHOT.parse("13:21-23") == (13004, 13004)
    assert TOSEFTA_MENAHOT.parse("13:4") == (13004, 13004)
    assert TOSEFTA_MENAHOT.parse("13") == (13000, 13999)
    assert TOSEFTA_MENAHOT.parse("13:17") == (13017, 13017)
    assert TOSEFTA_MENAHOT.name == "chapter:halakhah"


# --- Text cleaning


def test_clean_html_drops_tags_and_footnotes_but_keeps_editorial_brackets():
    raw = 'A <b>bold</b><sup class="footnote-marker">1</sup><i class="footnote">x <i>y</i></i> b.<br><Behold> &amp; c'
    assert clean_html(raw, "t") == "A bold b.\n<Behold> & c"
    assert clean_html("word<sup>*</sup><i class=\"footnote\">gloss</i> end", "t") == "word end"


@pytest.mark.parametrize("raw, message", [
    ('a<i class="footnote">open <i>x</i> b', "unbalanced"),
    ('a<sup class="footnote-marker">1</sup><i class="footnote">note<i>x</i>', "unbalanced"),
])
def test_clean_html_rejects_footnotes_that_would_swallow_text(raw, message):
    with pytest.raises(ValueError, match=message):
        clean_html(raw, "t")


def test_render_wikitext_keeps_text_and_quotations_and_drops_apparatus():
    text = render_wikitext(WIKITEXT, "3:5")

    assert text == (
        '"ושסע אותו בכנפיו" הה"ד "כבד את ה\'"\n'
        "אגריפס המלך ביקש להקריב אלף עולות ויקרא קומפון\nשורה"
    )


@pytest.mark.parametrize("wikitext, message", [
    ("{{תבנית לא מוכרת|x}}", "unsupported Wikisource template"),
    ("{{מצ|בראשית כב|יג|וירא {{צמד|מקרא}}", "unparsed wikitext"),
])
def test_render_wikitext_rejects_unknown_or_unclosed_templates(wikitext, message):
    with pytest.raises(ValueError, match=message):
        render_wikitext(wikitext, "x")


def test_align_segments_groups_an_extra_aramaic_segment_by_length():
    english = ["a" * 400, "b" * 800, "c" * 400]
    aramaic = ["a" * 100, "b" * 100, "b" * 100, "c" * 100]

    assert align_segments(english, aramaic) == [((0,), (0,)), ((1,), (1, 2)), ((2,), (3,))]
    assert align_segments(["x", "y"], ["x", "y"]) == [((0,), (0,)), ((1,), (1,))]


def test_chunk_beads_balances_long_amudim_under_the_limit():
    english = ["e" * 1000] * 10
    beads = align_segments(english, ["a"] * 10)
    chunks = chunk_beads(beads, english, ["a"] * 10, limit=7500)

    assert [len(chunk) for chunk in chunks] == [5, 5]
    assert chunk_beads(beads[:3], english[:3], ["a"] * 3, limit=7500) == [beads[:3]]


# --- Parsing


def test_parse_pairs_versions_with_licences(fixture_dir):
    rows = _rows(fixture_dir)
    shekalim = rows["Mishnah Shekalim 1:1"]

    assert shekalim["text"] == "On the first of Adar they announce the shekels.\nSecond line."
    assert shekalim["original_text"] == SHEKALIM_HEBREW
    assert (shekalim["sort_start"], shekalim["sort_end"]) == (1001, 1001)
    assert shekalim["edition"] == "Mishnah, ed. Romm, Vilna 1913"
    assert shekalim["translation"] == "Mishnah Yomit by Dr. Joshua Kulp"
    assert shekalim["original_license"] == "Public domain"
    assert shekalim["translation_license"] == "CC BY 3.0"
    assert shekalim["license"] == combined_license("Public domain", "CC BY 3.0", "Hebrew")
    assert shekalim["period_tags"] == "tannaitic" and shekalim["author"] == "Mishnah"

    english_only = rows["Mishnah Shekalim 2:1"]
    assert "original_text" not in english_only and english_only["license"] == "CC BY 3.0"
    assert english_only["edition"] == "Mishnah, ed. Romm, Vilna 1913 (lacks this passage)"


def test_parse_tosefta_original_only_passages_and_zuckermandel_alias(fixture_dir):
    rows = _rows(fixture_dir)
    with_english = rows["Tosefta Menachot 1:1"]
    vilna_13_4 = rows["Tosefta Menachot 13:4"]

    assert with_english["text"] == "Halakhah one continues."
    assert with_english["license"] == "Hebrew text: Public domain; English translation: CC0"
    assert not {"text", "translation", "translation_license"} & set(vilna_13_4)
    assert vilna_13_4["original_text"] == "אוי לי מבית ביתוס <כשר>"
    assert vilna_13_4["license"] == "Public domain"
    assert vilna_13_4["alternate_reference"] == "Zuckermandel 13:18–23"


def test_parse_talmud_cuts_long_amudim_into_segment_ranges(fixture_dir):
    rows = _rows(fixture_dir)

    assert "Bava Batra 2a" in rows and "Bava Batra 1a" not in rows
    first, second = rows["Bava Batra 3b:1–5"], rows["Bava Batra 3b:6–10"]
    assert (first["sort_start"], first["sort_end"]) == (700, 705)
    assert (second["sort_start"], second["sort_end"]) == (706, 799)
    assert first["text"].startswith("Herod built") and "<b>" not in first["text"]
    assert first["original_text"] == "\n\n".join(f"ארמית {n}" for n in range(1, 6))
    whole = rows["Bava Batra 4a"]
    assert (whole["sort_start"], whole["sort_end"]) == (800, 899)
    assert whole["license"] == "CC BY-NC 4.0" and whole["original_language"] == "Aramaic"
    assert whole["author"] == "Babylonian Talmud" and whole["period_tags"] == "amoraic"


def test_parse_talmud_marks_or_avoids_length_based_segment_pairing(fixture_dir):
    rows = _rows(fixture_dir)
    cut = [row for reference, row in rows.items() if reference.startswith("Bava Batra 3a:")]

    kept_whole = rows["Bava Batra 2b"]
    assert kept_whole["original_text"].count("ארמית") == 9 and len(kept_whole["text"]) > 7500
    assert kept_whole["edition"] == "William Davidson Edition - Aramaic"
    assert len(cut) == 2
    assert all(rabbinic.LENGTH_PAIRED_NOTE in row["edition"] for row in cut)
    assert rows["Bava Batra 3b:1–5"]["edition"] == "William Davidson Edition - Aramaic"


def test_parse_leviticus_rabbah_pairs_wikisource_hebrew_with_sefaria_english(fixture_dir):
    row = _rows(fixture_dir)["Leviticus Rabbah 3:5"]

    assert row["text"] == "King Agrippa sought to sacrifice. <Behold> the poor man."
    assert row["original_text"].startswith('"ושסע אותו בכנפיו"')
    assert row["license"] == "Hebrew text: CC BY-SA 4.0; English translation: CC BY 3.0"
    assert row["source_url"] == f"https://he.wikisource.org/w/index.php?oldid={REVISION}"
    assert row["edition"] == f"Hebrew Wikisource, ויקרא רבה 3:5 (revision {REVISION})"


def test_parse_rejects_versions_without_an_open_licence(fixture_dir):
    path = fixture_dir / "sefaria" / f"{SHEKALIM}/English/Mishnah Yomit by Dr. Joshua Kulp.json"
    version = json.loads(path.read_text())
    path.write_text(json.dumps({**version, "license": "unknown"}))

    with pytest.raises(ValueError, match="not an open licence"):
        list(rabbinic.parse(fixture_dir))


def test_parse_rejects_a_correction_that_no_longer_applies(fixture_dir, monkeypatch):
    monkeypatch.setattr(rabbinic, "_TEXT_CORRECTIONS", (
        (f"{VAYIKRA}/English/The Sefaria Midrash Rabbah, 2022.json", 3, 5, "absent", "x"),
    ))
    with pytest.raises(ValueError, match="correction no longer applies"):
        list(rabbinic.parse(fixture_dir))


def test_require_files_names_every_missing_file(fixture_dir):
    CORPUS.require_files(fixture_dir)
    (fixture_dir / "wikisource" / "vayikra_rabbah_03_05.wiki").unlink()
    (fixture_dir / "sefaria" / f"{MENACHOT}/Hebrew/Tosefta Menahot - Machon Mamre.json").unlink()

    with pytest.raises(FileNotFoundError) as error:
        CORPUS.require_files(fixture_dir)
    assert "vayikra_rabbah_03_05.wiki" in str(error.value)
    assert "Tosefta Menahot - Machon Mamre.json" in str(error.value)


def test_registered_pins_cover_every_declared_work():
    pins = rabbinic._sefaria_pins()
    for source in rabbinic.SOURCES:
        assert rabbinic._pinned_version(pins, source.hebrew_dir, "Hebrew"), source.work.key
        if source.collection != "T":
            assert rabbinic._pinned_version(pins, source.english_dir, "English"), source.work.key
    assert len(rabbinic._wikisource_pins()) == 330
    assert {(work, chapter, number) for work, chapter, number in rabbinic._ALTERNATE_REFERENCES} <= {
        (source.work.key, 13, 4) for source in rabbinic.SOURCES
    }


# --- Import


def _import(fixture_dir, tmp_path):
    conn = sqlite3.connect(tmp_path / "rabbinic.db")
    create_schema(conn)
    count = import_corpus(conn, "rabbinic", fixture_dir)
    return conn, count


def test_import_is_idempotent_and_resolves_links(fixture_dir, tmp_path):
    conn, count = _import(fixture_dir, tmp_path)
    again = import_corpus(conn, "rabbinic", fixture_dir)
    links = conn.execute(
        """SELECT p.reference, l.biblical_book, l.sort_start FROM ancient_source_biblical_links l
           JOIN ancient_source_passages p ON p.id = l.passage_id ORDER BY p.reference"""
    ).fetchall()

    assert count == again == len(_rows(fixture_dir))
    assert conn.execute("SELECT COUNT(*) FROM ancient_source_passages").fetchone() == (count,)
    assert links == [("Bava Batra 4a", "Jhn", 2020), ("Mishnah Shekalim 1:1", "Mat", 17024)]


def test_import_fails_on_an_unresolved_link(fixture_dir, tmp_path, monkeypatch):
    monkeypatch.setattr(rabbinic, "_BIBLICAL_LINKS", (
        ("bavli_bava_batra", "9a", "Jhn", 2, 20, 2, 20, "x"),
    ))
    with pytest.raises(ValueError, match="does not resolve"):
        _import(fixture_dir, tmp_path)


# --- Citations


@pytest.mark.parametrize("citation, work, sort_range", [
    ("Mishnah Shekalim 1:1", "mishnah_shekalim", (1001, 1001)),
    ("m. Shek. 1:1", "mishnah_shekalim", (1001, 1001)),
    ("Bava Batra 3b-4a", "bavli_bava_batra", (700, 899)),
    ("b. B. Bat. 4a:6", "bavli_bava_batra", (806, 806)),
    ("Baba Bathra 3b", "bavli_bava_batra", (700, 799)),
    ("Pesachim 57a", "bavli_pesachim", (11400, 11499)),
    ("Pes. 57a:8", "bavli_pesachim", (11408, 11408)),
    ("Avodah Zarah 5a", "bavli_avodah_zarah", (1000, 1099)),
    ("Abod. Zar. 5a", "bavli_avodah_zarah", (1000, 1099)),
    ("Tosefta Menahot 13:22", "tosefta_menachot", (13004, 13004)),
    ("t. Men. 13:4", "tosefta_menachot", (13004, 13004)),
    ("Leviticus Rabbah 3:5", "leviticus_rabbah", (3005, 3005)),
    ("Lev. Rab. 3:5", "leviticus_rabbah", (3005, 3005)),
    ("Vayikra Rabbah 3", "leviticus_rabbah", (3000, 3999)),
    ("Mishnah Pirkei Avot 1:1", "mishnah_pirkei_avot", (1001, 1001)),
    ("Tosefta Kelim Kamma 1:1", "tosefta_kelim_kamma", (1001, 1001)),
    ("Kelim 1:1", "mishnah_kelim", (1001, 1001)),
    ("Sanhedrin 43a", "bavli_sanhedrin", (8600, 8699)),
])
def test_resolves_rabbinic_citations(citation, work, sort_range):
    resolution = resolve_citation(citation, [CORPUS])

    assert resolution.status == "ok", citation
    assert (resolution.match.work.key, resolution.match.sort_range) == (work, sort_range)


def test_shared_tractate_titles_need_a_collection_for_chapter_citations():
    ambiguous = resolve_citation("Shekalim 1:1", [CORPUS])

    assert ambiguous.status == "ambiguous"
    assert {match.work.key for match in ambiguous.candidates} == {"mishnah_shekalim", "tosefta_shekalim"}
    assert resolve_citation("Mishnah Menachot 13:22", [CORPUS]).match.sort_range == (13022, 13022)
    assert resolve_citation("Bava Kamma 3b", [CORPUS]).status == "bad_locator"


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
async def test_lookup_and_search_tools(fixture_dir, tmp_path, monkeypatch):
    conn, _ = _import(fixture_dir, tmp_path)
    conn.close()
    path = tmp_path / "rabbinic.db"
    lookup, search = server.handle_lookup_ancient_source, server.handle_search_ancient_sources

    amud = await _call(path, monkeypatch, lookup, citation="Bava Batra 3b-4a")
    menahot = await _call(path, monkeypatch, lookup, citation="Tosefta Menahot 13:22")
    shekalim = await _call(path, monkeypatch, lookup, citation="Mishnah Shekalim 1:1")
    folded = await _call(path, monkeypatch, search, query="בֵּיתוֹס", corpus="rabbinic")
    english = await _call(path, monkeypatch, search, query="Herod marble", corpus="rabbinic")

    assert "3 overlapping passages" in amud
    assert amud.index("Bava Batra 3b:1–5") < amud.index("Bava Batra 3b:6–10") < amud.index("Bava Batra 4a")
    assert "**License:** CC BY-NC 4.0" in amud and "Jhn 2:20 — Narrative overlap" in amud
    assert menahot.startswith("## Tosefta Menachot 13:22")
    assert "Tosefta Menachot 13:4 (Zuckermandel 13:18–23)" in menahot
    assert "chapter:halakhah 13:4" in menahot
    assert "**English translation:** none openly licensed" in menahot
    assert "**License:** Public domain" in menahot
    assert "**License:** Hebrew text: Public domain; English translation: CC BY 3.0" in shekalim
    assert "Mat 17:24–27 — Related account" in shekalim
    assert "Tosefta Menachot 13:4" in folded
    assert "Bava Batra" in english


# --- Download


def test_download_verifies_pins_licences_and_sends_a_wikimedia_user_agent(tmp_path, monkeypatch):
    version = json.dumps(_version("Mishnah Yomit by Dr. Joshua Kulp", "CC-BY", [["x"]])).encode()
    closed = json.dumps(_version("Closed", "unknown", [["x"]])).encode()
    wiki = WIKITEXT.encode()
    path = f"{SHEKALIM}/English/Mishnah Yomit by Dr. Joshua Kulp.json"
    monkeypatch.setattr(rabbinic, "_SEFARIA_BLOBS", f"{git_blob_sha1(version)}  {path}")
    monkeypatch.setattr(rabbinic, "_WIKISOURCE_REVISIONS", f"3:5 {REVISION} {hashlib.sha1(wiki).hexdigest()}")
    agents = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "he.wikisource.org":
            agents.append(request.headers["User-Agent"])
            assert request.url.params["oldid"] == str(REVISION)
            return httpx.Response(200, content=wiki)
        assert str(request.url).startswith(rabbinic.ARCHIVE_RAW_URL)
        return httpx.Response(200, content=served)

    served = version
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        manifest = CORPUS.download(client, tmp_path, False)

    assert manifest["commit"] == rabbinic.ARCHIVE_COMMIT
    assert manifest["files"][f"sefaria/{path}"]["license"] == "CC BY 3.0"
    assert manifest["files"]["wikisource/vayikra_rabbah_03_05.wiki"]["oldid"] == REVISION
    assert "https://github.com/djayatillake/studybible-mcp" in agents[0]

    served = closed
    monkeypatch.setattr(rabbinic, "_SEFARIA_BLOBS", f"{git_blob_sha1(closed)}  {path}")
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RuntimeError, match="not an open licence"):
            CORPUS.download(client, tmp_path / "closed", False)
        with pytest.raises(RuntimeError, match="Checksum mismatch"):
            monkeypatch.setattr(rabbinic, "_SEFARIA_BLOBS", f"{'0' * 40}  {path}")
            CORPUS.download(client, tmp_path / "tampered", True)
