"""Targum corpus: Targum Onkelos and Targum Pseudo-Jonathan on the Torah, one passage per verse.

Aramaic: the Torat Emet text of each targum as exported by Sefaria. English:
J. W. Etheridge's translation (1862–65, public domain) for Pseudo-Jonathan and
for Onkelos Genesis 6:1–14:24, the only Onkelos chapters Sefaria has from him;
every other Onkelos verse carries the Metsudah Chumash English (2009, CC BY-NC),
which renders the Hebrew text with Onkelos's departures in brackets and is
labelled as such on every passage. All files come from Sefaria's archived export
repository at one commit, verified by Git blob hash.
"""

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import quote

from .base import BiblicalLink, Collection, Corpus, Work, combined_license
from .download import fetch_verified, git_blob_sha1
from .locators import CHAPTER_VERSE

if TYPE_CHECKING:
    import httpx


SEFARIA_REPOSITORY = "Sefaria/Sefaria-Export-Archive"
SEFARIA_COMMIT = "3f1013631fdfe452e953a93a2c5f921319e394ed"
_SOURCE_ROOT = "json/Tanakh/Targum"
_RAW_BASE = f"https://raw.githubusercontent.com/{SEFARIA_REPOSITORY}/{SEFARIA_COMMIT}"
_TREE_BASE = f"https://github.com/{SEFARIA_REPOSITORY}/tree/{SEFARIA_COMMIT}"
_BLOB_BASE = f"https://github.com/{SEFARIA_REPOSITORY}/blob/{SEFARIA_COMMIT}"

ORIGINAL_LANGUAGE = "Aramaic"
PUBLIC_DOMAIN = "Public domain"
CC_BY_NC = "CC BY-NC 4.0"

ONKELOS = Collection(
    "Targum Onkelos", ("Onkelos", "Onqelos", "Targum Onqelos", "Tg. Onq.", "Tg. Onk.", "Onk."),
)
PSEUDO_JONATHAN = Collection(
    "Targum Pseudo-Jonathan",
    (
        "Pseudo-Jonathan", "Tg. Ps.-J.", "Ps.-J.", "TPsJ", "Targum Jonathan",
        "Targum Jonathan ben Uzziel", "Jonathan ben Uzziel", "Targum Yerushalmi I",
    ),
)

# Torah book: (citation code for link notes, database book code, aliases).
BOOKS = {
    "Genesis": ("Gen", "Gen", ("Gen.", "Gn.", "Bereshit", "Bereshith", "Bereishit")),
    "Exodus": ("Exod", "Exo", ("Exod.", "Exo.", "Ex.", "Shemot", "Shemoth")),
    "Leviticus": ("Lev", "Lev", ("Lev.", "Lv.", "Vayikra", "Vaiyikra")),
    "Numbers": ("Num", "Num", ("Num.", "Nm.", "Bamidbar", "Bemidbar")),
    "Deuteronomy": ("Deut", "Deu", ("Deut.", "Dt.", "Devarim", "Debarim")),
}


@dataclass(frozen=True)
class _Targum:
    key: str
    collection: Collection
    author: str
    period_tags: str
    sefaria_directory: str  # Directory of one book's versions, with {book}.
    aramaic_version: str  # Sefaria versionTitle and file name, with {book}.


TARGUMS = (
    _Targum(
        key="onkelos",
        collection=ONKELOS,
        author="Targum Onkelos",
        period_tags="amoraic",
        sefaria_directory="Onkelos/Torah/Onkelos {book}",
        aramaic_version="Onkelos {book}",
    ),
    _Targum(
        key="pseudo_jonathan",
        collection=PSEUDO_JONATHAN,
        author="Targum Pseudo-Jonathan",
        period_tags="amoraic,early_islamic",
        sefaria_directory="Targum Jonathan/Torah/Targum Jonathan on {book}",
        aramaic_version="Targum Jonathan on {book}",
    ),
)
_TARGUMS = {targum.key: targum for targum in TARGUMS}

# Redaction-era tags (editorial): Onkelos took its final form in the amoraic
# period; Pseudo-Jonathan was redacted later and names the wives of Muhammad
# (Gen 21:21), so its redaction reaches the early Islamic period.
PERIODS = ("amoraic", "early_islamic")

ETHERIDGE_ONKELOS = (
    "J. W. Etheridge, The Targums of Onkelos and Jonathan ben Uzziel on the "
    "Pentateuch (London, 1862)"
)
ETHERIDGE_PSEUDO_JONATHAN = (
    "J. W. Etheridge, The Targums of Onkelos and Jonathan ben Uzziel on the "
    "Pentateuch (London, 1862–65), keyed to Masoretic verses by Sefaria"
)
METSUDAH = (
    "Metsudah Chumash, Metsudah Publications, 2009 [with Onkelos translation] — "
    "English of the Hebrew text with Onkelos's renderings in [brackets]; not a "
    "direct translation of the Aramaic"
)


def _work(targum: _Targum, book: str) -> Work:
    return Work(
        key=f"{targum.key}_{book.lower()}",
        title=book,
        reference_title=book,
        locator=CHAPTER_VERSE,
        aliases=BOOKS[book][2],
        collection=targum.collection,
    )


WORKS = {(targum.key, book): _work(targum, book) for targum in TARGUMS for book in BOOKS}


@dataclass(frozen=True)
class SourceFile:
    """One pinned Sefaria version file and the licence and title it must declare."""

    filename: str
    path: str  # Repository path under json/Tanakh/Targum.
    git_blob_sha1: str
    license: str  # Sefaria's exact 'license' value.
    version_title: str
    language: str  # 'he' (Aramaic) or 'en'.
    translation: str | None = None  # Display name of an English version.
    translation_license: str | None = None

    @property
    def raw_url(self) -> str:
        return f"{_RAW_BASE}/{quote(f'{_SOURCE_ROOT}/{self.path}')}"

    @property
    def source_url(self) -> str:
        return f"{_BLOB_BASE}/{quote(f'{_SOURCE_ROOT}/{self.path}')}"


_ETHERIDGE_ONKELOS_VERSION = (
    "J.W. Etheridge. The Targums of Onkelos and Jonathan Ben Uzziel on the Pentateuch. "
    "London: Longmans, Green, 1862"
)
_ETHERIDGE_ONKELOS_FILE = (
    "J.W. Etheridge. The Targums of Onkelos and Jonathan Ben Uzziel on the Pentateuch. "
    "London Longmans, Green, 1862"
)
_ETHERIDGE_PSEUDO_JONATHAN_VERSION = (
    "The Targum of Jonathan ben Uzziel, trans. J. W. Etheridge, London, 1862"
)
_METSUDAH_VERSION = "Metsudah Chumash, Metsudah Publications, 2009 [with Onkelos translation]"

# Git blob SHA-1s at SEFARIA_COMMIT, checked against the GitHub contents API.
_BLOBS = {
    ("onkelos", "Genesis", "aramaic"): "7f4483b9df5aeb1c107a5cc6c1dd9a5d3c350e19",
    ("onkelos", "Exodus", "aramaic"): "2307cb16124f62cd1cd97d24d967d411b760fd35",
    ("onkelos", "Leviticus", "aramaic"): "721423bc857c0416513cd0e21f6b9f8b916b9914",
    ("onkelos", "Numbers", "aramaic"): "d3f56eca6015be24478fc35bf7cb6c5f951de763",
    ("onkelos", "Deuteronomy", "aramaic"): "13e81dc5535fa343b07f591ad7aeae5ed11edcd0",
    ("onkelos", "Genesis", "metsudah"): "2ac288a342b028ebf6e23c28cf136d735c8ce132",
    ("onkelos", "Exodus", "metsudah"): "031df525a8e69b998a02982f79e38e08f495e166",
    ("onkelos", "Leviticus", "metsudah"): "63a385dae702fd1c7bb6c854ead61df360877b48",
    ("onkelos", "Numbers", "metsudah"): "44328083d9f5cc91cb5f4b21305e7ff312849f15",
    ("onkelos", "Deuteronomy", "metsudah"): "76d8d6c9f6f98070a8e08c13e45791dde2d9fc02",
    ("onkelos", "Genesis", "etheridge"): "b69ee04640925baf0b9cb23c1df5642cd49220fb",
    ("pseudo_jonathan", "Genesis", "aramaic"): "cabc73b1883a954e24a9580d16bb3fc3c428abdc",
    ("pseudo_jonathan", "Exodus", "aramaic"): "2594bb6557d166c5e96dbe114bcdc163b85d75ff",
    ("pseudo_jonathan", "Leviticus", "aramaic"): "e68c079fad44f826097e79c1d86cce4b0ba4505a",
    ("pseudo_jonathan", "Numbers", "aramaic"): "9ee5bfa2249f873023753ea0299547f03cd1ad00",
    ("pseudo_jonathan", "Deuteronomy", "aramaic"): "d631fa94e3eaaf95c37729762307aab23a81cfde",
    ("pseudo_jonathan", "Genesis", "etheridge"): "aa850056a4a0beb0434ced165c84f97b11bc844a",
    ("pseudo_jonathan", "Exodus", "etheridge"): "0559950fb86146b20d84a738acddcae4b2c4681b",
    ("pseudo_jonathan", "Leviticus", "etheridge"): "9231b07e51c3519908de442e4f0ac68b2a32f5d0",
    ("pseudo_jonathan", "Numbers", "etheridge"): "e4a2023be46a6ba625b3c3b8d37965cdf56c659b",
    ("pseudo_jonathan", "Deuteronomy", "etheridge"): "46353405cac18b1de4294f0d098e73758eaa1d30",
}


def _source(targum_key: str, book: str, kind: str) -> SourceFile:
    targum = _TARGUMS[targum_key]
    directory = targum.sefaria_directory.format(book=book)
    filename = f"{targum_key}_{book.lower()}.{kind}.json"
    blob = _BLOBS[(targum_key, book, kind)]
    if kind == "aramaic":
        version = targum.aramaic_version.format(book=book)
        return SourceFile(filename, f"{directory}/Hebrew/{version}.json", blob, "Public Domain", version, "he")
    if kind == "metsudah":
        return SourceFile(
            filename, f"{directory}/English/{_METSUDAH_VERSION}.json", blob, "CC-BY-NC",
            _METSUDAH_VERSION, "en", METSUDAH, CC_BY_NC,
        )
    if targum_key == "onkelos":
        return SourceFile(
            filename, f"{directory}/English/{_ETHERIDGE_ONKELOS_FILE}.json", blob, "Public Domain",
            _ETHERIDGE_ONKELOS_VERSION, "en", ETHERIDGE_ONKELOS, PUBLIC_DOMAIN,
        )
    return SourceFile(
        filename, f"{directory}/English/{_ETHERIDGE_PSEUDO_JONATHAN_VERSION}.json", blob,
        "Public Domain", _ETHERIDGE_PSEUDO_JONATHAN_VERSION, "en", ETHERIDGE_PSEUDO_JONATHAN,
        PUBLIC_DOMAIN,
    )


def _english_kinds(targum_key: str, book: str) -> tuple[str, ...]:
    """English versions in order of preference (public domain first)."""
    if targum_key == "pseudo_jonathan":
        return ("etheridge",)
    return ("etheridge", "metsudah") if book == "Genesis" else ("metsudah",)


SOURCE_FILES = tuple(
    _source(targum.key, book, kind)
    for targum in TARGUMS
    for book in BOOKS
    for kind in ("aramaic", *_english_kinds(targum.key, book))
)

# Sefaria numbers the Pseudo-Jonathan Aramaic of Genesis 20 by position: its
# 20:15 is the Sarah verse (MT 20:16, which the Etheridge English and Onkelos
# key at 20:16), its 20:16 is "my land is before you" (MT 20:15), and its
# 20:17 runs on through MT 20:18 ("closed every womb ... because of Sarah").
# Each entry maps (book, chapter, Sefaria verse) to the Masoretic verse span.
_ARAMAIC_VERSE_CORRECTIONS = {
    "pseudo_jonathan": {
        ("Genesis", 20, 15): (16, 16),
        ("Genesis", 20, 16): (15, 15),
        ("Genesis", 20, 17): (17, 18),
    },
}

# Digitization artefacts in the pinned Aramaic: (targum, book, chapter, verse)
# -> (text that must be present, replacement). Sefaria's later export drops
# the "??? " before Onkelos Genesis 49:12.
_ARAMAIC_TEXT_CORRECTIONS = {
    ("onkelos", "Genesis", 49, 12): ("??? ", ""),
}

_WHITESPACE = re.compile(r"\s+")
# Braced text in the Etheridge Pseudo-Jonathan file is not Etheridge's
# translation: it is either a clause the digitizer supplied (eight words or
# more, e.g. Deut 9:12 "{Onq., Nf. MT; they have soon gone aside ...}", Gen
# 20:15 "{And Abimelech said, Behold, my land ...}", absent from Etheridge's
# text at targum.info) or a gloss of an Aramaic term (five words or fewer,
# "the Oracle {Word; Dibbera}", "Rabboni {our lord}"). Supplied clauses become
# an ellipsis, glosses are dropped, and a verse left with only ellipses has no
# Etheridge English. Angle brackets hold transliterations (<mymr'>) or span
# marks (<< >>); asterisks are footnote marks, and where a verse has two, the
# text after the second is Etheridge's footnote ("*Or, lame", Exod 4:10).
_BRACED = re.compile(r"\{([^{}]*)\}")
_SUPPLIED_CLAUSE_WORDS = 8
_ANGLED = re.compile(r"<<|>>|<[^<>]*>")
_EMPHASIS_TAG = re.compile(r"</?(?:b|i)>")


def _normalize(text: str) -> str:
    return _WHITESPACE.sub(" ", text).strip()


def _replace_braced(match: re.Match[str]) -> str:
    return " … " if len(match.group(1).split()) >= _SUPPLIED_CLAUSE_WORDS else " "


def clean_etheridge(text: str) -> str:
    """Etheridge's English without the digitizer's insertions, glosses and marks."""
    text = html.unescape(_BRACED.sub(_replace_braced, text))
    if text.count("*") >= 2:
        text = text[: text.rindex("*")]
    text = _ANGLED.sub(" ", text).replace("*", " ")
    text = _normalize(re.sub(r"\s+([,.;:])", r"\1", text))
    if re.search(r"[{}<>]", text):
        raise ValueError(f"Unexpected markup left in Etheridge text: {text[:80]!r}")
    return "" if not re.search(r"[^\W_]", text) else text


def clean_metsudah(text: str) -> str:
    """Metsudah English without its bold/italic tags; bracketed Onkelos renderings stay."""
    text = _normalize(html.unescape(_EMPHASIS_TAG.sub("", text)))
    if re.search(r"[<>{}]", text):
        raise ValueError(f"Unexpected markup left in Metsudah text: {text[:80]!r}")
    return text


def _load(directory: Path, source: SourceFile) -> list[list[str]]:
    path = directory / source.filename
    data = json.loads(path.read_text(encoding="utf-8"))
    _check_declaration(source, data)
    chapters = data.get("text")
    if not isinstance(chapters, list) or not all(
        isinstance(chapter, list) and all(isinstance(verse, str) for verse in chapter)
        for chapter in chapters
    ):
        raise ValueError(f"{source.filename}: expected a chapter list of verse strings")
    return chapters


def _check_declaration(source: SourceFile, data: dict) -> None:
    if data.get("license") != source.license:
        raise ValueError(
            f"{source.filename}: expected Sefaria license {source.license!r}, "
            f"found {data.get('license')!r}"
        )
    if data.get("versionTitle") != source.version_title or data.get("language") != source.language:
        raise ValueError(f"{source.filename}: unexpected version {data.get('versionTitle')!r}")


def _aramaic_units(targum_key: str, book: str, chapters: list[list[str]]) -> dict[tuple[int, int, int], str]:
    """Aramaic text keyed by (chapter, first verse, last verse) in Masoretic numbering."""
    corrections = _ARAMAIC_VERSE_CORRECTIONS.get(targum_key, {})
    units: dict[tuple[int, int, int], str] = {}
    for chapter_number, verses in enumerate(chapters, 1):
        for verse_number, raw in enumerate(verses, 1):
            text = raw
            fix = _ARAMAIC_TEXT_CORRECTIONS.get((targum_key, book, chapter_number, verse_number))
            if fix is not None:
                if fix[0] not in text:
                    raise ValueError(
                        f"{targum_key} {book} {chapter_number}:{verse_number}: expected {fix[0]!r}"
                    )
                text = text.replace(fix[0], fix[1])
            text = _normalize(text)
            if not text:
                continue
            if "??" in text:
                raise ValueError(f"{targum_key} {book} {chapter_number}:{verse_number}: '??' in Aramaic")
            start, end = corrections.get((book, chapter_number, verse_number), (verse_number, verse_number))
            key = (chapter_number, start, end)
            if key in units:
                raise ValueError(f"{targum_key} {book} {chapter_number}:{start}: duplicate Aramaic verse")
            units[key] = text
    return units


def _english_verses(
    targum_key: str, book: str, directory: Path
) -> dict[tuple[int, int], tuple[str, SourceFile]]:
    """The preferred non-empty English per (chapter, verse) with its source."""
    verses: dict[tuple[int, int], tuple[str, SourceFile]] = {}
    for kind in _english_kinds(targum_key, book):
        source = _source(targum_key, book, kind)
        clean = clean_metsudah if kind == "metsudah" else clean_etheridge
        for chapter_number, chapter in enumerate(_load(directory, source), 1):
            for verse_number, raw in enumerate(chapter, 1):
                text = clean(raw)
                if text and (chapter_number, verse_number) not in verses:
                    verses[(chapter_number, verse_number)] = (text, source)
    return verses


def _reference(work: Work, chapter: int, start: int, end: int) -> str:
    verses = str(start) if start == end else f"{start}–{end}"
    return f"{work.display_title} {chapter}:{verses}"


def parse_targum_book(directory: Path, targum_key: str, book: str) -> list[dict]:
    """One passage per Masoretic verse (a span where the Aramaic joins verses).

    The English of each verse is its preferred licensed version; a verse
    without one (Etheridge omits some Pseudo-Jonathan verses or groups them
    into a neighbour) is an original-only passage.
    """
    targum = _TARGUMS[targum_key]
    work = WORKS[(targum_key, book)]
    aramaic_source = _source(targum_key, book, "aramaic")
    units = _aramaic_units(targum_key, book, _load(directory, aramaic_source))
    english = _english_verses(targum_key, book, directory)

    covered = {(chapter, verse) for chapter, start, end in units for verse in range(start, end + 1)}
    for chapter, verse in english:
        if (chapter, verse) not in covered:
            units[(chapter, verse, verse)] = ""
    rows: list[dict] = []
    for (chapter, start, end), aramaic in sorted(units.items()):
        texts = [english[(chapter, verse)] for verse in range(start, end + 1) if (chapter, verse) in english]
        row = {
            "author": targum.author,
            "work_key": work.key,
            "reference": _reference(work, chapter, start, end),
            "sort_start": work.locator.key(chapter, start),
            "sort_end": work.locator.key(chapter, end),
            "edition": (
                f"Torat Emet Aramaic text (toratemetfreeware.com), Sefaria version "
                f"'{aramaic_source.version_title}'"
            ),
            "period_tags": targum.period_tags,
            "source_url": f"{_TREE_BASE}/{quote(f'{_SOURCE_ROOT}/{targum.sefaria_directory.format(book=book)}')}",
        }
        original_license = PUBLIC_DOMAIN if aramaic else None
        if aramaic:
            row["original_text"] = aramaic
            row["original_language"] = ORIGINAL_LANGUAGE
            row["original_license"] = original_license
        translation_license = None
        if texts:
            sources = {source.filename: source for _, source in texts}
            if len(sources) != 1:
                raise ValueError(f"{row['reference']}: English from more than one version")
            source = next(iter(sources.values()))
            translation_license = source.translation_license
            row["text"] = " ".join(text for text, _ in texts)
            row["translation"] = source.translation
            row["translation_license"] = translation_license
        row["license"] = combined_license(original_license, translation_license, ORIGINAL_LANGUAGE)
        rows.append(row)
    return rows


def parse(directory: Path) -> list[dict]:
    return [
        row
        for targum in TARGUMS
        for book in BOOKS
        for row in parse_targum_book(directory, targum.key, book)
    ]


_BOOK_BY_WORK = {work.key: book for (_, book), work in WORKS.items()}


def biblical_links(passages: list[dict]) -> list[BiblicalLink]:
    """One link per passage to the Torah verse(s) it translates, from its sort keys."""
    links = []
    for passage in passages:
        book = _BOOK_BY_WORK[passage["work_key"]]
        citation_code, book_code, _ = BOOKS[book]
        chapter, start = divmod(passage["sort_start"], 1000)
        end_chapter, end = divmod(passage["sort_end"], 1000)
        verses = f"{chapter}:{start}" if passage["sort_start"] == passage["sort_end"] else (
            f"{chapter}:{start}–{end}" if chapter == end_chapter else f"{chapter}:{start}–{end_chapter}:{end}"
        )
        links.append(BiblicalLink(
            work_key=passage["work_key"],
            locator=f"{chapter}:{start}",
            book=book_code,
            chapter=chapter,
            verse=start,
            end_chapter=end_chapter,
            end_verse=end,
            note=f"Translation of: {citation_code} {verses}",
        ))
    return links


def require_targum_files(directory: Path) -> None:
    """Raise unless every pinned Aramaic and English version file is present."""
    missing = [source.filename for source in SOURCE_FILES if not (directory / source.filename).is_file()]
    if missing:
        raise FileNotFoundError(
            f"Targum sources in {directory} are incomplete; missing {', '.join(missing)}. "
            "Run python scripts/download_ancient_sources.py."
        )


def download(client: httpx.Client, target: Path, force: bool) -> dict:
    """Fetch each pinned Sefaria version file, verify its blob hash and declared licence."""
    files: dict[str, dict] = {}
    for source in SOURCE_FILES:
        content = fetch_verified(
            client, source.raw_url, target / source.filename, source.git_blob_sha1, force, git_blob_sha1
        )
        try:
            _check_declaration(source, json.loads(content))
        except ValueError as error:
            raise RuntimeError(str(error)) from error
        files[source.filename] = {
            "git_blob_sha1": source.git_blob_sha1,
            "bytes": len(content),
            "source_url": source.source_url,
            "version_title": source.version_title,
            "license": source.license,
        }
    return {
        "repository": SEFARIA_REPOSITORY,
        "commit": SEFARIA_COMMIT,
        "license": (
            "Aramaic (Torat Emet): Public domain; Etheridge English: Public domain; "
            "Metsudah English: CC BY-NC 4.0 (as declared per version by Sefaria)"
        ),
        "files": files,
    }


CORPUS = Corpus(
    key="targum",
    label="Targum",
    summary=(
        "Targum Onkelos and Targum Pseudo-Jonathan on the five books of the Torah "
        "(Torat Emet Aramaic via Sefaria), one passage per Masoretic verse, cited "
        "by targum, book and chapter:verse ('Onkelos Genesis 1:1', 'Tg. Ps.-J. "
        "Deut 32:8'), each linked to the verse it translates. English: Etheridge "
        "(1862–65, public domain) for Pseudo-Jonathan and Onkelos Genesis 6–14; "
        "other Onkelos verses carry the Metsudah Chumash (2009, CC BY-NC), an "
        "English of the Hebrew text with Onkelos's renderings in brackets."
    ),
    data_dir="targum",
    original_language=ORIGINAL_LANGUAGE,
    works=tuple(WORKS.values()),
    periods=PERIODS,
    download=download,
    require_files=require_targum_files,
    parse=parse,
    biblical_links=biblical_links,
    example_query="seventy angels",
    example_citation="Pseudo-Jonathan Deuteronomy 32:8",
    aliases=("Targum", "Targums", "Tg."),
)
