"""Rabbinic corpus: Mishnah, Tosefta, Babylonian Talmud and Leviticus Rabbah.

Sefaria texts come from the last full JSON export committed to
Sefaria/Sefaria-Export-Archive, pinned by commit and Git blob SHA-1 (the live
export is an unversioned GCS bucket overwritten monthly, so a checksum pin on
it cannot be re-fetched once Sefaria edits a text). Every version carries its
own licence, recorded on each passage. The Hebrew of Leviticus Rabbah comes
from Hebrew Wikisource, pinned per page by revision id and SHA-1.
"""

from __future__ import annotations

import hashlib
import html
import json
import math
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from .base import BiblicalLink, Collection, Corpus, Work, combined_license
from .download import fetch_verified, git_blob_sha1
from .locators import DAF_AMUD, NumberedLocator, SortRange

if TYPE_CHECKING:
    import httpx


ARCHIVE_REPOSITORY = "Sefaria/Sefaria-Export-Archive"
# "upload sources", 2026-03-23: the last commit holding the full JSON export
# before the data moved to GCS (a1b6997, "migrate data to GCS").
ARCHIVE_COMMIT = "3f1013631fdfe452e953a93a2c5f921319e394ed"
ARCHIVE_RAW_URL = f"https://raw.githubusercontent.com/{ARCHIVE_REPOSITORY}/{ARCHIVE_COMMIT}/json/"
ARCHIVE_TREE_URL = f"https://github.com/{ARCHIVE_REPOSITORY}/tree/{ARCHIVE_COMMIT}/json/"
WIKISOURCE_RAW_URL = "https://he.wikisource.org/w/index.php?oldid={revid}&action=raw"
WIKISOURCE_PAGE_URL = "https://he.wikisource.org/w/index.php?oldid={revid}"
# Hebrew Wikisource site licence (API siteinfo rightsinfo, 2026-10-12).
WIKISOURCE_LICENSE = "CC BY-SA 4.0"
SEFARIA_DIR = "sefaria"
WIKISOURCE_DIR = "wikisource"
ORIGINAL_LANGUAGE = "Hebrew"
TALMUD_LANGUAGE = "Aramaic"

# Sefaria's per-version ``license`` values, spelled as the licence Sefaria's
# reader links them to (Sefaria-Project static/js/sefaria/sefaria.js
# getLicenseMap: CC-BY -> licenses/by/3.0, CC-BY-NC -> licenses/by-nc/4.0).
# Any other value (e.g. "unknown") is rejected at download and parse time.
SEFARIA_LICENSES = {
    "Public Domain": "Public domain",
    "CC0": "CC0",
    "CC-BY": "CC BY 3.0",
    "CC-BY-NC": "CC BY-NC 4.0",
}

MISHNAH_HEBREW = "Mishnah, ed. Romm, Vilna 1913"
MISHNAH_ENGLISH = "Mishnah Yomit by Dr. Joshua Kulp"
TALMUD_ARAMAIC = "William Davidson Edition - Aramaic"
TALMUD_ENGLISH = "William Davidson Edition - English"
LEVITICUS_RABBAH_ENGLISH = "The Sefaria Midrash Rabbah, 2022"

MISHNAH = Collection("Mishnah", ("m.",))
TOSEFTA = Collection("Tosefta", ("t.", "Tos."))
BAVLI = Collection("Babylonian Talmud", ("Bavli", "Talmud", "b.", "BT"))
PERIODS = ("tannaitic", "amoraic")

_SEGMENT_SCALE = 100
_DASH = re.compile(r"\s*[-‐‑‒–—]\s*")
_DAF_POINT = re.compile(r"(\d+)\s*([ab])?(?:\s*[:.]\s*(\d+))?", re.IGNORECASE)


@dataclass(frozen=True)
class AmudSegmentLocator:
    """Talmud daf+amud citations with optional Sefaria segment numbers.

    Key = ``DAF_AMUD.key(daf, amud) * 100 + segment``. Amud-level citations
    ('3b', '3b-4a', '3a-b', '3', '2-3') are read by ``DAF_AMUD`` and cover
    every segment of the amudim; '3b:5', '3b:5-9' and '3b:5-4a:2' name
    segments, numbered as in the English version.
    """

    name: str = "daf+amud[:segment]"
    example: str = "3b"

    @staticmethod
    def key(daf: int, amud: str, segment: int) -> int:
        if not 0 <= segment < _SEGMENT_SCALE:
            raise ValueError(f"daf+amud[:segment]: invalid segment {segment}")
        return DAF_AMUD.key(daf, amud) * _SEGMENT_SCALE + segment

    def parse(self, text: str) -> SortRange | None:
        amudim = DAF_AMUD.parse(text)
        if amudim is not None:
            return amudim[0] * _SEGMENT_SCALE, amudim[1] * _SEGMENT_SCALE + _SEGMENT_SCALE - 1
        pieces = _DASH.split(re.sub(r"[\s.;,]+$", "", text.strip()))
        if len(pieces) > 2 or not all(pieces):
            return None
        start = _DAF_POINT.fullmatch(pieces[0])
        if start is None or int(start[1]) < 1 or (start[3] and not start[2]):
            return None
        daf, amud = int(start[1]), start[2] or "a"
        try:
            low = (
                self.key(daf, amud, int(start[3])) if start[3]
                else DAF_AMUD.key(daf, amud) * _SEGMENT_SCALE
            )
            high = low if start[3] else low + _SEGMENT_SCALE - 1
            if len(pieces) == 2:
                high = self._end(pieces[1], daf, amud, bool(start[3]))
        except ValueError:
            return None
        return (low, high) if high is not None and low <= high else None

    def _end(self, text: str, daf: int, amud: str, has_segment: bool) -> int | None:
        if has_segment and text.isdigit():
            return self.key(daf, amud, int(text))
        if re.fullmatch(r"[ab]", text, re.IGNORECASE):
            return DAF_AMUD.key(daf, text) * _SEGMENT_SCALE + _SEGMENT_SCALE - 1
        end = _DAF_POINT.fullmatch(text)
        if end is None or int(end[1]) < 1 or (end[3] and not end[2]):
            return None
        if end[3]:
            return self.key(int(end[1]), end[2], int(end[3]))
        return DAF_AMUD.key(int(end[1]), end[2] or "b") * _SEGMENT_SCALE + _SEGMENT_SCALE - 1

    def format_range(self, start: int, end: int) -> str:
        first, first_segment = divmod(start, _SEGMENT_SCALE)
        last, last_segment = divmod(end, _SEGMENT_SCALE)
        if first_segment == 0 and last_segment == _SEGMENT_SCALE - 1:
            return DAF_AMUD.format_range(first, last)
        point = lambda amud, segment: f"{DAF_AMUD.format(amud)}:{segment}"  # noqa: E731
        if start == end:
            return point(first, first_segment)
        if first == last:
            return f"{point(first, first_segment)}–{last_segment}"
        return f"{point(first, first_segment)}–{point(last, last_segment)}"


@dataclass(frozen=True)
class ConcordanceLocator:
    """A chapter:halakhah locator that also reads documented points of another numbering.

    ``concordance`` maps an inclusive range of the other numbering (in this
    locator's key space) to the sort range of the passage that contains it.
    It applies only to citations wholly inside such a range; every other
    citation is read by ``base``.
    """

    base: NumberedLocator
    concordance: tuple[tuple[SortRange, SortRange], ...]

    @property
    def name(self) -> str:
        return self.base.name

    @property
    def example(self) -> str:
        return self.base.example

    def key(self, *parts: int) -> int:
        return self.base.key(*parts)

    def parse(self, text: str) -> SortRange | None:
        found = self.base.parse(text)
        if found is None:
            return None
        for (low, high), target in self.concordance:
            if low <= found[0] and found[1] <= high:
                return target
        return found

    def format_range(self, start: int, end: int) -> str:
        return self.base.format_range(start, end)


CHAPTER_MISHNAH = NumberedLocator("chapter:mishnah", "1:1", separator=":")
CHAPTER_HALAKHAH = NumberedLocator("chapter:halakhah", "1:1", separator=":")
CHAPTER_SECTION = NumberedLocator("chapter:section", "3:5", separator=":")
DAF_AMUD_SEGMENT = AmudSegmentLocator()

# Sefaria's Tosefta follows the Vilna numbering, in which Menahot 13 has four
# halakhot. Zuckermandel's 13:18-23 (the priests seizing hides and beams, the
# woes over the high-priestly houses, R. Yohanan ben Torta on why Shiloh and
# both Temples fell, and the rebuilt Temple) all fall in Vilna 13:4. Source:
# Judaism and Rome (ERC), "Tosefta Menahot 13:21-23"
# (https://www.judaism-and-rome.org/tosefta-menahot-1321-23), which cites
# 13:21 for the woes, 13:22-23 for R. Yohanan ben Torta and the rebuilding,
# and 13:18-20 for the preceding accounts of priestly violence; each unit was
# checked in the pinned Machon Mamre text of Vilna 13:4, and Vilna 13:3 ends
# with the preceding halakhah on a priest sacrificing in another's watch.
ZUCKERMANDEL_MENAHOT = "Zuckermandel 13:18–23"
TOSEFTA_MENAHOT = ConcordanceLocator(
    CHAPTER_HALAKHAH,
    (((CHAPTER_HALAKHAH.key(13, 18), CHAPTER_HALAKHAH.key(13, 23)),
      (CHAPTER_HALAKHAH.key(13, 4), CHAPTER_HALAKHAH.key(13, 4))),),
)
_ALTERNATE_REFERENCES = {("tosefta_menachot", 13, 4): ZUCKERMANDEL_MENAHOT}


# Tractates in the order of the Mishnah: (title, seder, collections, aliases).
# Collections: M = Mishnah, T = Tosefta, B = Babylonian Talmud (William
# Davidson edition). The whole Bavli (37 tractates) adds 168.6 MB to the
# vacuumed database (204.2 MB with the rest of the corpus), over the deploy
# budget, so B marks the tractates most cited for Second Temple and New
# Testament background, 71.3 MB (106.9 MB for the corpus): Berakhot, Shabbat,
# Pesachim, Yoma, Sukkah, Rosh Hashanah, Taanit, Megillah, Chagigah, Sotah,
# Gittin, Bava Batra, Sanhedrin, Makkot, Avodah Zarah and Tamid. Another
# tractate is imported by adding B and pinning its two William Davidson files.
# Titles follow Sefaria's spelling; aliases add SBL abbreviations (without
# diacritics or apostrophes, which citation matching does not skip) and
# common spellings, shortest first.
_TRACTATES = (
    ("Berakhot", "Zeraim", "MTB", ("Ber.", "Berachot", "Brachot")),
    ("Peah", "Zeraim", "MT", ("Pea",)),
    ("Demai", "Zeraim", "MT", ("Dem.",)),
    ("Kilayim", "Zeraim", "MT", ("Kil.", "Kilaim")),
    ("Sheviit", "Zeraim", "MT", ("Sheb.", "Shebiit")),
    ("Terumot", "Zeraim", "MT", ("Ter.", "Terumoth")),
    ("Maasrot", "Zeraim", "MT", ("Maas.", "Maaserot")),
    ("Maaser Sheni", "Zeraim", "MT", ("Maas. Sh.", "Maaser Sheini")),
    ("Challah", "Zeraim", "MT", ("Hal.", "Hallah")),
    ("Orlah", "Zeraim", "MT", ("Orl.",)),
    ("Bikkurim", "Zeraim", "MT", ("Bik.", "Bikk.")),
    ("Shabbat", "Moed", "MTB", ("Shabb.", "Shab.", "Sabbath")),
    ("Eruvin", "Moed", "MT", ("Erub.", "Eruv.", "Erubin")),
    ("Pesachim", "Moed", "MTB", ("Pes.", "Pesah.", "Pesahim")),
    ("Shekalim", "Moed", "MT", ("Shek.", "Sheqal.", "Sheqalim")),
    ("Yoma", "Moed", "MTB", ("Kippurim",)),
    ("Sukkah", "Moed", "MTB", ("Suk.", "Sukk.")),
    ("Beitzah", "Moed", "MT", ("Besah", "Betzah", "Yom Tov")),
    ("Rosh Hashanah", "Moed", "MTB", ("RH", "Rosh Hash.")),
    ("Taanit", "Moed", "MTB", ("Taan.", "Taanith")),
    ("Megillah", "Moed", "MTB", ("Meg.",)),
    ("Moed Katan", "Moed", "MT", ("MK", "Moed Qat.", "Moed Qatan")),
    ("Chagigah", "Moed", "MTB", ("Hag.", "Hagigah")),
    ("Yevamot", "Nashim", "MT", ("Yev.", "Yebam.", "Yebamot")),
    ("Ketubot", "Nashim", "MT", ("Ket.", "Ketub.", "Ketuboth")),
    ("Nedarim", "Nashim", "MT", ("Ned.",)),
    ("Nazir", "Nashim", "MT", ("Naz.",)),
    ("Sotah", "Nashim", "MTB", ("Sot.", "Sota")),
    ("Gittin", "Nashim", "MTB", ("Git.",)),
    ("Kiddushin", "Nashim", "MT", ("Kid.", "Qidd.", "Qiddushin")),
    ("Bava Kamma", "Nezikin", "MT", ("BK", "B. Qam.", "Baba Kamma", "Bava Qamma")),
    ("Bava Metzia", "Nezikin", "MT", ("BM", "B. Mes.", "Baba Metzia", "Baba Mezia")),
    ("Bava Batra", "Nezikin", "MTB", ("BB", "B. Bat.", "Baba Bathra", "Baba Batra")),
    ("Sanhedrin", "Nezikin", "MTB", ("Sanh.",)),
    ("Makkot", "Nezikin", "MTB", ("Mak.", "Makkoth")),
    ("Shevuot", "Nezikin", "MT", ("Shebu.", "Shevu.", "Shebuot")),
    ("Eduyot", "Nezikin", "MT", ("Ed.", "Eduy.", "Eduyyot")),
    ("Avodah Zarah", "Nezikin", "MTB", ("AZ", "Abod. Zar.", "Avod. Zar.", "Avoda Zara")),
    ("Pirkei Avot", "Nezikin", "M", ("Avot", "Abot", "Aboth", "Pirke Avot")),
    ("Horayot", "Nezikin", "MT", ("Hor.",)),
    ("Zevachim", "Kodashim", "MT", ("Zev.", "Zebah.", "Zebahim", "Zevahim")),
    ("Menachot", "Kodashim", "MT", ("Men.", "Menah.", "Menahot")),
    ("Chullin", "Kodashim", "MT", ("Hul.", "Hullin")),
    ("Bekhorot", "Kodashim", "MT", ("Bek.", "Bekh.", "Bekorot")),
    ("Arakhin", "Kodashim", "MT", ("Arak.", "Arakin")),
    ("Temurah", "Kodashim", "MT", ("Tem.",)),
    ("Keritot", "Kodashim", "MT", ("Ker.", "Kerithot")),
    ("Meilah", "Kodashim", "MT", ("Meil.",)),
    ("Tamid", "Kodashim", "MB", ("Tam.",)),
    ("Middot", "Kodashim", "M", ("Mid.", "Midd.")),
    ("Kinnim", "Kodashim", "M", ("Qin.", "Qinnim")),
    ("Kelim", "Tahorot", "M", ("Kel.",)),
    ("Kelim Kamma", "Tahorot", "T", ("Kel. B. Qam.", "Kelim Bava Kamma")),
    ("Kelim Metzia", "Tahorot", "T", ("Kel. B. Mes.", "Kelim Bava Metzia")),
    ("Kelim Batra", "Tahorot", "T", ("Kel. B. Bat.", "Kelim Bava Batra")),
    ("Oholot", "Tahorot", "MT", ("Ohal.", "Ohol.", "Ohalot", "Ahilot")),
    ("Negaim", "Tahorot", "MT", ("Neg.",)),
    ("Parah", "Tahorot", "MT", ("Par.",)),
    ("Tahorot", "Tahorot", "MT", ("Tehar.", "Toh.", "Tohorot", "Teharot")),
    ("Mikvaot", "Tahorot", "MT", ("Mikv.", "Miqw.", "Miqvaot")),
    ("Niddah", "Tahorot", "MT", ("Nid.", "Nidd.")),
    ("Makhshirin", "Tahorot", "MT", ("Maksh.", "Makhsh.", "Makshirin")),
    ("Zavim", "Tahorot", "MT", ("Zab.", "Zabim")),
    ("Tevul Yom", "Tahorot", "MT", ("T. Yom", "Tebul Yom")),
    ("Yadayim", "Tahorot", "MT", ("Yad.",)),
    ("Oktzin", "Tahorot", "MT", ("Uq.", "Uktzin", "Uqsin", "Oktsin")),
)

# Sefaria directory names that differ from the title: (collection, title).
_SEFARIA_NAMES = {
    ("M", "Taanit"): "Ta'anit",
    ("T", "Taanit"): "Ta'anit",
    ("T", "Oktzin"): "Oktsin",
}
# The Tosefta Tahorot Hebrew Sefaria labels "Public Domain" is filed as its own
# index, "Tosefta Tohorot"; the copy under "Tosefta Tahorot" is "unknown". Both
# follow the same Vilna chapter/halakhah structure as the English.
_TOSEFTA_HEBREW_NAMES = {"Tahorot": "Tohorot"}


def _slug(title: str) -> str:
    return re.sub(r"\W+", "_", title.lower()).strip("_")


@dataclass(frozen=True)
class _Source:
    """One work's Sefaria files, relative to the archive's ``json/`` directory."""

    work: Work
    collection: str
    hebrew_dir: str
    english_dir: str


def _sources() -> tuple[_Source, ...]:
    sources: list[_Source] = []
    for code, collection in (("M", MISHNAH), ("T", TOSEFTA), ("B", BAVLI)):
        for title, seder, collections, aliases in _TRACTATES:
            if code not in collections:
                continue
            name = _SEFARIA_NAMES.get((code, title), title)
            if code == "M":
                directory = name if title == "Pirkei Avot" else f"Mishnah {name}"
                english = hebrew = f"Mishnah/Seder {seder}/{directory}"
                locator: Any = CHAPTER_MISHNAH
            elif code == "T":
                english = f"Tosefta/Vilna Edition/Seder {seder}/Tosefta {name}"
                hebrew_name = _TOSEFTA_HEBREW_NAMES.get(title, name)
                hebrew = f"Tosefta/Vilna Edition/Seder {seder}/Tosefta {hebrew_name}"
                locator = TOSEFTA_MENAHOT if title == "Menachot" else CHAPTER_HALAKHAH
            else:
                english = hebrew = f"Talmud/Bavli/Seder {seder}/{name}"
                locator = DAF_AMUD_SEGMENT
            prefix = {"M": "mishnah", "T": "tosefta", "B": "bavli"}[code]
            work = Work(f"{prefix}_{_slug(title)}", title, title, locator, aliases, collection)
            sources.append(_Source(work, code, hebrew, english))
    return tuple(sources)


SOURCES = _sources()
LEVITICUS_RABBAH = Work(
    "leviticus_rabbah", "Leviticus Rabbah", "Leviticus Rabbah", CHAPTER_SECTION,
    ("Lev. Rab.", "Vayikra Rabbah", "Wayyiqra Rabbah", "Leviticus Rabba"),
)
LEVITICUS_RABBAH_DIR = "Midrash/Aggadah/Midrash Rabbah/Vayikra Rabbah"
WORKS = (*(source.work for source in SOURCES), LEVITICUS_RABBAH)
_AUTHORS = {"M": "Mishnah", "T": "Tosefta", "B": "Babylonian Talmud"}
_PERIOD = {"M": "tannaitic", "T": "tannaitic", "B": "amoraic"}

# Editorial links, separate from the texts: narrative overlap or related
# accounts, not literary dependence or historical agreement. Fields: work
# key, locator in the work's grammar, biblical book, chapter, verse, end
# chapter, end verse, note. The comment on each entry names what was verified
# in the pinned text of the passage containing the locator.
_BIBLICAL_LINKS = (
    # Kulp 1:1: "they make a public announcement about the shekels"
    ("mishnah_shekalim", "1:1", "Mat", 17, 24, 17, 27,
     "Related account: the annual half-shekel contribution to the temple"),
    # Kulp 1:3: "they would set up tables [of money changers] in the provinces.
    # On the twenty-fifth they set them up in the Temple"; pledges exacted
    ("mishnah_shekalim", "1:3", "Mat", 17, 24, 17, 27,
     "Related account: collection of the half-shekel temple contribution"),
    # WD 4a:4 (Bava Batra 4a segment 4): Bava ben Buta's counsel; "you can
    # demolish the Temple and rebuild it. He did so."
    ("bavli_bava_batra", "4a:4", "Jhn", 2, 20, 2, 20,
     "Narrative overlap: Herod rebuilds the temple"),
    # WD 4a:6: "One who has not seen Herod's building has never seen a
    # beautiful building ... With stones of white and green marble"
    ("bavli_bava_batra", "4a:6", "Mrk", 13, 1, 13, 1,
     "Narrative overlap: the stones and beauty of Herod's temple"),
    ("bavli_bava_batra", "4a:6", "Luk", 21, 5, 21, 5,
     "Narrative overlap: the stones and beauty of Herod's temple"),
    # WD 57a:8: "Woe is me due to the High Priests of the house of Baitos ...
    # of the house of Ḥanin ... of the house of Katros ... Yishmael ben
    # Piakhi"; Acts 4:6 names Annas, Caiaphas and the high-priestly family
    ("bavli_pesachim", "57a:8", "Act", 4, 6, 4, 6,
     "Narrative overlap: the high-priestly families of the late Second Temple "
     "period (Acts names Annas and the high priest's family; the lament names "
     "the houses of Baitos, Ḥanin, Katros and Yishmael ben Piakhi)"),
)


# --- Text cleaning

_HTML_TAG = re.compile(r"<(/?)([a-zA-Z][a-zA-Z0-9]*)\b([^<>]*)>")
# Only these are markup; anything else in angle brackets ('<Behold>', the
# Tosefta's '<כשר>') is editorial text of the version and is kept.
_HTML_TAGS = frozenset(
    {"a", "b", "big", "br", "div", "em", "i", "p", "small", "span", "strong", "sub", "sup", "u"}
)

# Corrections to the pinned files, applied before cleaning: (file, chapter,
# section, wrong, right). Each closes a footnote left open in Sefaria's
# markup, which would otherwise hide the rest of the section as a footnote.
_LEVITICUS_RABBAH_ENGLISH_FILE = f"{LEVITICUS_RABBAH_DIR}/English/{LEVITICUS_RABBAH_ENGLISH}.json"
_TEXT_CORRECTIONS = (
    # Sefaria fixed this after the archive commit; the 2026-10 GCS export reads
    # "Kohelet Rabba 5:5.</i> The Rabbis".
    (_LEVITICUS_RABBAH_ENGLISH_FILE, 16, 5,
     "<i>Kohelet Rabba </i>5:5.<i>   <br>", "<i>Kohelet Rabba </i>5:5.</i> <br>"),
    # Footnote 16 ends at 'literally means "he."'; the following words are the
    # translation ('Hu is written, [indicating that] it was he who sanctified').
    (_LEVITICUS_RABBAH_ENGLISH_FILE, 11, 7,
     '<i>Hu<sup class="footnote-marker">16</sup>', '<i>Hu</i><sup class="footnote-marker">16</sup>'),
    (_LEVITICUS_RABBAH_ENGLISH_FILE, 11, 7,
     "“he.” is written, [indicating", "“he.”</i> is written, [indicating"),
)


def _tidy(text: str) -> str:
    lines = (re.sub(r"[ \t\u00a0]+", " ", line).strip() for line in text.split("\n"))
    return "\n".join(line for line in lines if line)


def clean_html(raw: str, where: str) -> str:
    """Plain text of a Sefaria segment: tags dropped, footnotes and their markers removed.

    A footnote is a ``<sup>`` marker or an element whose attributes name a
    footnote class; its content is skipped up to the matching close tag. In a
    segment with footnotes, unbalanced ``<i>``/``<sup>`` tags (which would
    misplace a footnote's end) raise ``ValueError``.
    """
    if "<sup" in raw or "footnote" in raw:
        for name in ("i", "sup"):
            opened = len(re.findall(rf"<{name}\b[^<>]*>", raw))
            if opened != raw.count(f"</{name}>"):
                raise ValueError(f"{where}: unbalanced <{name}> tags around a footnote")
    parts: list[str] = []
    skipping: str | None = None
    depth = 0
    position = 0
    for match in _HTML_TAG.finditer(raw):
        closing, name, attributes = match[1] == "/", match[2].lower(), match[3]
        if name not in _HTML_TAGS:
            continue
        if skipping is None:
            parts.append(raw[position:match.start()])
        position = match.end()
        if name == "br":
            if skipping is None:
                parts.append("\n")
            continue
        if attributes.rstrip().endswith("/"):
            continue
        if skipping is not None:
            if name == skipping:
                depth += -1 if closing else 1
                if depth == 0:
                    skipping = None
        elif not closing and (name == "sup" or "footnote" in attributes):
            skipping, depth = name, 1
    if skipping is not None:
        raise ValueError(f"{where}: unclosed footnote <{skipping}>")
    parts.append(raw[position:])
    return _tidy(html.unescape("".join(parts)))


# --- Hebrew Wikisource wikitext (Leviticus Rabbah)

_QUOTE = '"'
# Templates whose output is a quoted biblical verse; the value is a function
# of the positional and named parameters returning the quoted text. Source
# citations these templates also print are editorial apparatus and dropped.
_VERSE_TEMPLATES = {
    "מצ": lambda p, n: p[3] if len(p) > 3 else p[2] if len(p) > 2 else "",
    "צמפ": lambda p, n: (p[3] if len(p) > 3 else p[2]) if len(p) > 1 else p[0],
    "מצפ": lambda p, n: (p[3] if len(p) > 3 else p[2]) if len(p) > 1 else p[0],
    "צמנ": lambda p, n: (p[3] if len(p) > 3 else p[2]) if len(p) > 1 else p[0],
    "צמד": lambda p, n: p[1] if len(p) > 1 and p[1] else p[0],
    "שם": lambda p, n: p[0] if "שם" in n else p[3] if len(p) > 3 else "",
    "צ": lambda p, n: n.get("תוכן", p[0] if p else ""),
    "צמ": lambda p, n: n.get("תוכן", p[0] if p else ""),
    "צפ": lambda p, n: n.get("תוכן", p[0] if p else ""),
    "פסוק": lambda p, n: " ".join(x for x in (p[0] if p else "", n.get("פסוק", "")) if x),
}
# Templates that print their first parameter (a glossed word, a reading, a
# category-tagged name, a gematria) or, for abbreviations, their own name.
_FIRST_PARAMETER = frozenset({"ב", "נוסח", "גמט", "קט", "גדול"})
_ABBREVIATIONS = frozenset({'הה"ד', 'ד"א', 'זש"ה', 'א"ל', 'ח"ו', 'י"א', 'או"ה', 'א"ר'})
_SPACES = {"ש": "\n", "ססס": " ", "ררר": " ", "רווח קשיח": " "}
# Navigation, section headings, commentary boxes, source references, small
# editorial notes, story frames and maintenance banners.
_DROPPED = frozenset({
    "סרגל ניווט", "פמ", "פיסקת מדרש", "במ", 'מ"מ', "ממ", "מקור", "הפניה לפסוקים", "קטן",
    'התחלת סיפור חז"ל', "סוף סיפור", "סוף", "מידה טובה", "מסורת חכמים", "טקסט מנוקד",
    "להשלים", "שם הדף", "נק", "*",
})
_NAMED_PARAMETERS = frozenset({
    "ק", "גיבור", "נושא", "מקור", "תוכן", "שם", "פסוק", "בציטוט", "מרכאות", "קטגוריה",
    "סוג", "גודל", "קטנה", "הק",
})
_INNER_TEMPLATE = re.compile(r"\{\{([^{}]*)\}\}")


def _render_template(body: str, where: str) -> str:
    name, *arguments = body.split("|")
    # 'ס:' (subst:) occurs inside an unsubstituted category key; render the template it names.
    name = name.strip().removeprefix("ס:").strip()
    positional: list[str] = []
    named: dict[str, str] = {}
    for argument in arguments:
        key, separator, value = argument.partition("=")
        if separator and key.strip() in _NAMED_PARAMETERS:
            named[key.strip()] = value.strip()
        else:
            positional.append(argument.strip())
    if name in _VERSE_TEMPLATES:
        verse = _VERSE_TEMPLATES[name](positional, named)
        return f"{_QUOTE}{verse}{_QUOTE}" if verse else ""
    if name in _FIRST_PARAMETER:
        return positional[0] if positional else ""
    if name in _ABBREVIATIONS:
        return name
    if name in _SPACES:
        return _SPACES[name]
    if name in _DROPPED:
        return ""
    raise ValueError(f"{where}: unsupported Wikisource template {{{{{name}}}}}")


def render_wikitext(source: str, where: str) -> str:
    """Plain Hebrew text of a Wikisource midrash page.

    Notes (``<ref>``), headings, categories, navigation and source citations
    are dropped; quotation templates keep the quoted verse; link labels are
    kept. An unknown template or leftover markup raises ``ValueError``.
    """
    text = re.sub(r"<!--.*?-->", "", source, flags=re.DOTALL)
    text = re.sub(r"<(noinclude|includeonly)>.*?</\1>", "", text, flags=re.DOTALL)
    text = re.sub(r"<ref\b[^>]*/>|<ref\b[^>]*>.*?</ref>", "", text, flags=re.DOTALL)
    text = re.sub(r"<references\s*/>", "", text)
    text = re.sub(r"^=+[^=\n]*=+[ \t]*$", "", text, flags=re.MULTILINE)
    text = re.sub(r"\[\[(?:קטגוריה|Category):[^\]]*\]\]", "", text)
    text = re.sub(r"\[\[(?:[^\]|]*\|)?([^\]|]*)\]\]", r"\1", text)
    while (rendered := _INNER_TEMPLATE.sub(lambda m: _render_template(m[1], where), text)) != text:
        text = rendered
    text = re.sub(r"<br\s*/?>", "\n", text).replace("'''", "").replace("''", "")
    text = re.sub(r"^[*:#;]+", "", text, flags=re.MULTILINE)
    leftover = re.search(r"\{\{|\}\}|\[\[|\]\]|<\w", text)
    if leftover:
        raise ValueError(f"{where}: unparsed wikitext near {text[leftover.start():leftover.start() + 40]!r}")
    return _tidy(text.replace("\t", " "))


# --- Pins

def _sefaria_pins() -> dict[str, str]:
    pins = {}
    for line in _SEFARIA_BLOBS.strip().splitlines():
        blob, path = line.split("  ", 1)
        pins[path] = blob
    return pins


def _wikisource_pins() -> dict[tuple[int, int], tuple[int, str]]:
    pins = {}
    for line in _WIKISOURCE_REVISIONS.strip().splitlines():
        locator, revid, sha1 = line.split()
        chapter, section = (int(part) for part in locator.split(":"))
        pins[chapter, section] = (int(revid), sha1)
    return pins


def _wikisource_file(chapter: int, section: int) -> str:
    return f"{WIKISOURCE_DIR}/vayikra_rabbah_{chapter:02d}_{section:02d}.wiki"


def _pinned_version(pins: dict[str, str], directory: str, language: str) -> str | None:
    """The pinned file of ``directory``/``language``, if any; more than one is an error."""
    found = [path for path in pins if path.startswith(f"{directory}/{language}/")]
    if len(found) > 1:
        raise ValueError(f"Several pinned {language} versions in {directory}: {found}")
    return found[0] if found else None


def required_files() -> list[str]:
    return [
        *(f"{SEFARIA_DIR}/{path}" for path in _sefaria_pins()),
        *(_wikisource_file(*key) for key in _wikisource_pins()),
    ]


def require_rabbinic_files(directory: Path) -> None:
    """Raise unless every pinned Sefaria and Wikisource file is present."""
    missing = [name for name in required_files() if not (directory / name).is_file()]
    if missing:
        raise FileNotFoundError(
            f"Rabbinic sources in {directory} are incomplete; missing {len(missing)} files: "
            f"{', '.join(missing)}. Run python scripts/download_ancient_sources.py."
        )


# --- Parsing

def _load_version(directory: Path, path: str) -> dict[str, Any]:
    version = json.loads((directory / SEFARIA_DIR / path).read_text(encoding="utf-8"))
    license_ = version.get("license")
    if license_ not in SEFARIA_LICENSES:
        raise ValueError(f"{path}: Sefaria licence {license_!r} is not an open licence")
    version["license"] = SEFARIA_LICENSES[license_]
    for correction_path, chapter, section, wrong, right in _TEXT_CORRECTIONS:
        if correction_path == path:
            segment = version["text"][chapter - 1][section - 1]
            if wrong not in segment:
                raise ValueError(f"{path} {chapter}:{section}: correction no longer applies")
            version["text"][chapter - 1][section - 1] = segment.replace(wrong, right)
    return version


def _source_url(directory: str) -> str:
    return ARCHIVE_TREE_URL + quote(directory)


def _passage(work: Work, author: str, period: str, reference: str, sort_range: SortRange,
             english: tuple[dict, str] | None, original: tuple[dict, str] | None,
             original_version: dict, original_language: str, source_url: str,
             **extra: Any) -> dict[str, Any]:
    """A passage dict; ``original_version`` names the edition even where it lacks this passage."""
    row: dict[str, Any] = {
        "work_key": work.key,
        "reference": reference,
        "sort_start": sort_range[0],
        "sort_end": sort_range[1],
        "author": author,
        "period_tags": period,
        "source_url": source_url,
        "edition": original_version["versionTitle"] if original else
        f"{original_version['versionTitle']} (lacks this passage)",
        **extra,
    }
    original_license = translation_license = None
    if original is not None:
        version, text = original
        original_license = version["license"]
        row.update(original_text=text, original_language=original_language,
                   original_license=original_license)
    if english is not None:
        version, text = english
        translation_license = version["license"]
        row.update(text=text, translation=version["versionTitle"],
                   translation_license=translation_license)
    row["license"] = combined_license(original_license, translation_license, original_language)
    return row


def _numbered_passages(directory: Path, source: _Source, pins: dict[str, str]) -> Iterator[dict]:
    """One passage per mishnah (Mishnah) or halakhah (Tosefta)."""
    hebrew_path = _pinned_version(pins, source.hebrew_dir, "Hebrew")
    english_path = _pinned_version(pins, source.english_dir, "English")
    if hebrew_path is None:
        raise ValueError(f"No pinned Hebrew version for {source.work.display_title}")
    if source.collection == "M" and english_path is None:
        raise ValueError(f"No pinned English version for {source.work.display_title}")
    hebrew = _load_version(directory, hebrew_path)
    english = _load_version(directory, english_path) if english_path else None
    english_text = english["text"] if english else []
    work = source.work
    chapters = max(len(hebrew["text"]), len(english_text))
    for chapter in range(1, chapters + 1):
        hebrew_chapter = hebrew["text"][chapter - 1] if chapter <= len(hebrew["text"]) else []
        english_chapter = english_text[chapter - 1] if chapter <= len(english_text) else []
        for number in range(1, max(len(hebrew_chapter), len(english_chapter)) + 1):
            where = f"{work.display_title} {chapter}:{number}"
            original = english_segment = None
            if number <= len(hebrew_chapter) and (text := clean_html(hebrew_chapter[number - 1], where)):
                original = (hebrew, text)
            if number <= len(english_chapter) and (text := clean_html(english_chapter[number - 1], where)):
                english_segment = (english, text)
            if original is None and english_segment is None:
                continue
            key = work.locator.key(chapter, number)
            alternate = _ALTERNATE_REFERENCES.get((work.key, chapter, number))
            yield _passage(
                work, _AUTHORS[source.collection], _PERIOD[source.collection],
                f"{work.display_title} {chapter}:{number}", (key, key), english_segment, original,
                hebrew, ORIGINAL_LANGUAGE, _source_url(source.hebrew_dir),
                **({"alternate_reference": alternate} if alternate else {}),
            )


# Longest English or Aramaic text per Talmud passage; lookup shows 8,000
# characters of each, so longer amudim are cut into segment ranges.
PASSAGE_CHARACTER_LIMIT = 7500

Bead = tuple[tuple[int, ...], tuple[int, ...]]


def _length(texts: list[str], indexes: tuple[int, ...]) -> int:
    return sum(len(texts[index]) + 2 for index in indexes)


def align_segments(english: list[str], original: list[str]) -> list[Bead]:
    """Pair English and Aramaic segments of one amud.

    Equal counts pair by position. Otherwise (96 amudim of the edition differ
    by a segment) a length-based dynamic programme groups 1-1, 1-2, 2-1, 1-0
    and 0-1 segments, comparing each group's length ratio to the amud's.
    """
    if len(english) == len(original):
        return [((index,), (index,)) for index in range(len(english))]
    ratio = (sum(map(len, english)) + 1) / (sum(map(len, original)) + 1)
    moves = ((1, 1, 0.0), (1, 2, 0.5), (2, 1, 0.5), (1, 0, 3.0), (0, 1, 3.0))
    rows, columns = len(english), len(original)
    cost = [[math.inf] * (columns + 1) for _ in range(rows + 1)]
    back: list[list[tuple[int, int] | None]] = [[None] * (columns + 1) for _ in range(rows + 1)]
    cost[0][0] = 0.0
    for i in range(rows + 1):
        for j in range(columns + 1):
            if cost[i][j] == math.inf:
                continue
            for di, dj, penalty in moves:
                ni, nj = i + di, j + dj
                if ni > rows or nj > columns:
                    continue
                english_length = sum(len(text) for text in english[i:ni])
                original_length = sum(len(text) for text in original[j:nj])
                step = math.log((english_length + 20) / (ratio * original_length + 20)) ** 2
                if cost[i][j] + step + penalty < cost[ni][nj]:
                    cost[ni][nj] = cost[i][j] + step + penalty
                    back[ni][nj] = (i, j)
    beads: list[Bead] = []
    i, j = rows, columns
    while (i, j) != (0, 0):
        pi, pj = back[i][j]
        beads.append((tuple(range(pi, i)), tuple(range(pj, j))))
        i, j = pi, pj
    return beads[::-1]


def chunk_beads(beads: list[Bead], english: list[str], original: list[str],
                limit: int = PASSAGE_CHARACTER_LIMIT) -> list[list[Bead]]:
    """Group consecutive beads into passages of at most ``limit`` characters per language.

    It aims for the fewest passages the limit allows, cutting before the bead
    whose midpoint passes the next equal share of the English text.
    """
    english_total = sum(_length(english, bead[0]) for bead in beads)
    original_total = sum(_length(original, bead[1]) for bead in beads)
    parts = max(1, math.ceil(english_total / limit), math.ceil(original_total / limit))
    share = english_total / parts
    chunks: list[list[Bead]] = [[]]
    consumed = english_length = original_length = 0
    for bead in beads:
        bead_english, bead_original = _length(english, bead[0]), _length(original, bead[1])
        over_limit = english_length + bead_english > limit or original_length + bead_original > limit
        past_share = len(chunks) < parts and consumed + bead_english / 2 > len(chunks) * share
        if chunks[-1] and (over_limit or past_share):
            chunks.append([])
            english_length = original_length = 0
        chunks[-1].append(bead)
        consumed += bead_english
        english_length += bead_english
        original_length += bead_original
    return chunks


def _talmud_passages(directory: Path, source: _Source, pins: dict[str, str]) -> Iterator[dict]:
    """One passage per amud, or per segment range when the amud is long."""
    work = source.work
    aramaic_path = f"{source.hebrew_dir}/Hebrew/{TALMUD_ARAMAIC}.json"
    english_path = f"{source.english_dir}/English/{TALMUD_ENGLISH}.json"
    for path in (aramaic_path, english_path):
        if path not in pins:
            raise ValueError(f"{work.display_title}: {path} is not pinned")
    aramaic, english = _load_version(directory, aramaic_path), _load_version(directory, english_path)
    if len(aramaic["text"]) != len(english["text"]):
        raise ValueError(f"{work.display_title}: Aramaic and English have different amud counts")
    for index, (aramaic_amud, english_amud) in enumerate(zip(aramaic["text"], english["text"])):
        daf, side = index // 2 + 1, "ab"[index % 2]
        amud = f"{daf}{side}"
        where = f"{work.display_title} {amud}"
        original_segments = [clean_html(text, where) for text in aramaic_amud]
        english_segments = [clean_html(text, where) for text in english_amud]
        if not any(original_segments) and not any(english_segments):
            continue
        beads = align_segments(english_segments, original_segments)
        chunks = chunk_beads(beads, english_segments, original_segments)
        for number, chunk in enumerate(chunks):
            english_indexes = [i for bead in chunk for i in bead[0]]
            original_indexes = [j for bead in chunk for j in bead[1]]
            if len(chunks) == 1:
                reference = f"{work.reference_title} {amud}"
                first, last = 0, _SEGMENT_SCALE - 1
            else:
                if not english_indexes:
                    raise ValueError(f"{where}: a segment range without English segments")
                first, last = english_indexes[0] + 1, english_indexes[-1] + 1
                span = str(first) if first == last else f"{first}–{last}"
                reference = f"{work.reference_title} {amud}:{span}"
                first = 0 if number == 0 else first
                last = _SEGMENT_SCALE - 1 if number + 1 == len(chunks) else last
            english_text = "\n\n".join(english_segments[i] for i in english_indexes if english_segments[i])
            original_text = "\n\n".join(original_segments[j] for j in original_indexes if original_segments[j])
            yield _passage(
                work, _AUTHORS["B"], _PERIOD["B"], reference,
                (work.locator.key(daf, side, first), work.locator.key(daf, side, last)),
                (english, english_text) if english_text else None,
                (aramaic, original_text) if original_text else None,
                aramaic, TALMUD_LANGUAGE, _source_url(source.hebrew_dir),
            )


# Unclosed quotation templates in the pinned revisions, closed after the
# quoted verse (Gen 22:13 'וירא והנה איל'; Joel 2:10 '... וכוכבים אספו נגהם').
_WIKITEXT_CORRECTIONS = {
    (29, 7): (("וירא והנה איל {{צמד|מקרא קודש}}", "וירא והנה איל}} {{צמד|מקרא קודש}}"),),
    (31, 9): (("וכוכבים אספו נגהם ר'", "וכוכבים אספו נגהם}} ר'"),),
}


def _leviticus_rabbah_passages(directory: Path, pins: dict[str, str]) -> Iterator[dict]:
    """One passage per chapter:section, Wikisource Hebrew with Sefaria's English."""
    work = LEVITICUS_RABBAH
    english_path = f"{LEVITICUS_RABBAH_DIR}/English/{LEVITICUS_RABBAH_ENGLISH}.json"
    if english_path not in pins:
        raise ValueError(f"{english_path} is not pinned")
    english = _load_version(directory, english_path)
    revisions = _wikisource_pins()
    english_keys = {
        (chapter, section)
        for chapter, sections in enumerate(english["text"], 1)
        for section, text in enumerate(sections, 1) if text.strip()
    }
    if english_keys != set(revisions):
        raise ValueError(
            "Leviticus Rabbah: Wikisource sections and Sefaria English differ at "
            f"{sorted(english_keys ^ set(revisions))}"
        )
    for (chapter, section), (revid, _sha1) in sorted(revisions.items()):
        where = f"Leviticus Rabbah {chapter}:{section}"
        wikitext = (directory / _wikisource_file(chapter, section)).read_text(encoding="utf-8")
        for wrong, right in _WIKITEXT_CORRECTIONS.get((chapter, section), ()):
            if wrong not in wikitext:
                raise ValueError(f"{where}: Wikisource correction no longer applies")
            wikitext = wikitext.replace(wrong, right)
        hebrew = {
            "versionTitle": f"Hebrew Wikisource, ויקרא רבה {chapter}:{section} (revision {revid})",
            "license": WIKISOURCE_LICENSE,
        }
        key = work.locator.key(chapter, section)
        yield _passage(
            work, "Midrash Rabbah", "amoraic", f"Leviticus Rabbah {chapter}:{section}", (key, key),
            (english, clean_html(english["text"][chapter - 1][section - 1], where)),
            (hebrew, render_wikitext(wikitext, where)), hebrew, ORIGINAL_LANGUAGE,
            WIKISOURCE_PAGE_URL.format(revid=revid),
        )


def parse(directory: Path) -> Iterator[dict]:
    pins = _sefaria_pins()
    for source in SOURCES:
        if source.collection == "B":
            yield from _talmud_passages(directory, source, pins)
        else:
            yield from _numbered_passages(directory, source, pins)
    yield from _leviticus_rabbah_passages(directory, pins)


def biblical_links(passages: list[dict]) -> list[BiblicalLink]:
    return [BiblicalLink(*link) for link in _BIBLICAL_LINKS]


# --- Download

def _require_open_license(path: str, content: bytes) -> str:
    license_ = json.loads(content).get("license")
    if license_ not in SEFARIA_LICENSES:
        raise RuntimeError(f"{path}: Sefaria licence {license_!r} is not an open licence")
    return SEFARIA_LICENSES[license_]


# Wikimedia's robot policy (https://w.wiki/4wJS) rejects user agents without
# contact information with 403.
WIKIMEDIA_USER_AGENT = (
    "studybible-mcp/1.0 (https://github.com/djayatillake/studybible-mcp; ancient-source download)"
)


@dataclass(frozen=True)
class _WikimediaClient:
    """``client.get`` with the Wikimedia user agent, for ``fetch_verified``."""

    client: httpx.Client

    def get(self, url: str) -> httpx.Response:
        return self.client.get(url, headers={"User-Agent": WIKIMEDIA_USER_AGENT})


def download(client: httpx.Client, target: Path, force: bool) -> dict:
    """Fetch the pinned Sefaria archive files and Wikisource revisions, verifying each."""
    files: dict[str, dict] = {}
    for path, blob in _sefaria_pins().items():
        local = f"{SEFARIA_DIR}/{path}"
        content = fetch_verified(
            client, ARCHIVE_RAW_URL + quote(path), target / local, blob, force, git_blob_sha1
        )
        files[local] = {
            "git_blob_sha1": blob,
            "bytes": len(content),
            "license": _require_open_license(path, content),
            "source_url": f"https://github.com/{ARCHIVE_REPOSITORY}/blob/{ARCHIVE_COMMIT}/json/{quote(path)}",
        }
    wikisource = _WikimediaClient(client)
    for (chapter, section), (revid, sha1) in _wikisource_pins().items():
        local = _wikisource_file(chapter, section)
        content = fetch_verified(
            wikisource, WIKISOURCE_RAW_URL.format(revid=revid), target / local, sha1, force,
            lambda data: hashlib.sha1(data).hexdigest(),
        )
        files[local] = {
            "oldid": revid,
            "sha1": sha1,
            "bytes": len(content),
            "license": WIKISOURCE_LICENSE,
            "source_url": WIKISOURCE_PAGE_URL.format(revid=revid),
        }
    return {
        "repository": ARCHIVE_REPOSITORY,
        "commit": ARCHIVE_COMMIT,
        "source": "he.wikisource.org (Leviticus Rabbah Hebrew, pinned revisions)",
        "license": "Per version: see each file's licence",
        "files": files,
    }


CORPUS = Corpus(
    key="rabbinic",
    label="Rabbinic",
    summary=(
        "Rabbinic literature from Sefaria: the Mishnah (Vilna 1913 Hebrew with Joshua "
        "Kulp's English, by chapter:mishnah), the Tosefta (Vilna numbering, Hebrew with "
        "Sefaria Community English where it exists, by chapter:halakhah), the Babylonian "
        "Talmud (William Davidson Aramaic and English, CC BY-NC, by daf and amud, long "
        "amudim cut into segment ranges such as 'Bava Batra 3b:1–9'), and Leviticus "
        "Rabbah (Hebrew Wikisource with The Sefaria Midrash Rabbah English, by "
        "chapter:section). Each passage carries its versions' licences."
    ),
    data_dir="rabbinic",
    original_language=ORIGINAL_LANGUAGE,
    works=WORKS,
    periods=PERIODS,
    download=download,
    require_files=require_rabbinic_files,
    parse=parse,
    biblical_links=biblical_links,
    example_query="shekels Adar",
    example_citation="Mishnah Shekalim 1:1",
    aliases=("Rabbinic",),
)


# --- Pins. Sefaria files: Git blob SHA-1 and path under json/ in
# Sefaria/Sefaria-Export-Archive at ARCHIVE_COMMIT. Each version's licence is
# its JSON ``license`` field, checked at download and parse time.
_SEFARIA_BLOBS = """
b9f1ac998bdd19aca4228f796bbc8a170a2e9f99  Midrash/Aggadah/Midrash Rabbah/Vayikra Rabbah/English/The Sefaria Midrash Rabbah, 2022.json
e9df1fefec2f44cb1ebefb478446f248b19b2913  Mishnah/Seder Kodashim/Mishnah Arakhin/English/Mishnah Yomit by Dr. Joshua Kulp.json
17a8c1817ed7e3c3aa5fd6eaf83e162e12c61824  Mishnah/Seder Kodashim/Mishnah Arakhin/Hebrew/Mishnah, ed. Romm, Vilna 1913.json
6b6927ad7c2041c623680a5fc184260f62d175c4  Mishnah/Seder Kodashim/Mishnah Bekhorot/English/Mishnah Yomit by Dr. Joshua Kulp.json
eadcf73f0fb6cd44eef81c5fae0b9f8a4e060ade  Mishnah/Seder Kodashim/Mishnah Bekhorot/Hebrew/Mishnah, ed. Romm, Vilna 1913.json
8034a657bcf9ce7c6ecca077c32f335a51ca9498  Mishnah/Seder Kodashim/Mishnah Chullin/English/Mishnah Yomit by Dr. Joshua Kulp.json
3b9f31756a97b73106284ba9c76442fc7f184806  Mishnah/Seder Kodashim/Mishnah Chullin/Hebrew/Mishnah, ed. Romm, Vilna 1913.json
4781100b6bba4a97e927881ced24c29d87961195  Mishnah/Seder Kodashim/Mishnah Keritot/English/Mishnah Yomit by Dr. Joshua Kulp.json
a5e8c034872714f61a594287dcfb3a3ed9bc703b  Mishnah/Seder Kodashim/Mishnah Keritot/Hebrew/Mishnah, ed. Romm, Vilna 1913.json
f0b4604f0a77b6ef9f638fa395000c3de1465310  Mishnah/Seder Kodashim/Mishnah Kinnim/English/Mishnah Yomit by Dr. Joshua Kulp.json
37994f782a7f0820a97b9585da4b0242e6cdee9e  Mishnah/Seder Kodashim/Mishnah Kinnim/Hebrew/Mishnah, ed. Romm, Vilna 1913.json
34966d324e74b6a317d090a5308fb2f2a5ae68d9  Mishnah/Seder Kodashim/Mishnah Meilah/English/Mishnah Yomit by Dr. Joshua Kulp.json
466477b20664e1e020c605a4f275b82fe86a4513  Mishnah/Seder Kodashim/Mishnah Meilah/Hebrew/Mishnah, ed. Romm, Vilna 1913.json
835f8b99acf8857c808da1b66022724c2f9544f9  Mishnah/Seder Kodashim/Mishnah Menachot/English/Mishnah Yomit by Dr. Joshua Kulp.json
26f5ec7f472512a7227bf7801bd1b3f98261c482  Mishnah/Seder Kodashim/Mishnah Menachot/Hebrew/Mishnah, ed. Romm, Vilna 1913.json
420a5b3aa848291bff003ccd67d146f15ac2c998  Mishnah/Seder Kodashim/Mishnah Middot/English/Mishnah Yomit by Dr. Joshua Kulp.json
46079cf7215fddfa794c42a005af29ded2c99d76  Mishnah/Seder Kodashim/Mishnah Middot/Hebrew/Mishnah, ed. Romm, Vilna 1913.json
307df490bb991cf7ed7d456bfbac387a9adbeb4f  Mishnah/Seder Kodashim/Mishnah Tamid/English/Mishnah Yomit by Dr. Joshua Kulp.json
356e989fc85683ee6c6b0e7449f54c68eb429c79  Mishnah/Seder Kodashim/Mishnah Tamid/Hebrew/Mishnah, ed. Romm, Vilna 1913.json
2adb83327d0307842a9e5f569ce80df601bb839a  Mishnah/Seder Kodashim/Mishnah Temurah/English/Mishnah Yomit by Dr. Joshua Kulp.json
faae65c4ae16f2a73ab9517d7f5b7de74c71d5fb  Mishnah/Seder Kodashim/Mishnah Temurah/Hebrew/Mishnah, ed. Romm, Vilna 1913.json
f52909550a034262140bfa2765bfb6e732c39f9a  Mishnah/Seder Kodashim/Mishnah Zevachim/English/Mishnah Yomit by Dr. Joshua Kulp.json
412162f32289155d9addd1b57cd67c7ab0fef6a8  Mishnah/Seder Kodashim/Mishnah Zevachim/Hebrew/Mishnah, ed. Romm, Vilna 1913.json
a927c6bc65517aab13bd4d52c2d1140e71191b1e  Mishnah/Seder Moed/Mishnah Beitzah/English/Mishnah Yomit by Dr. Joshua Kulp.json
3571d4cf76da1e7ec81d30567d0308633d13ec6b  Mishnah/Seder Moed/Mishnah Beitzah/Hebrew/Mishnah, ed. Romm, Vilna 1913.json
e37ad0c4feb65d7226ba012d27d5b5a0ea9247c8  Mishnah/Seder Moed/Mishnah Chagigah/English/Mishnah Yomit by Dr. Joshua Kulp.json
11c5866ee7b894eab2f02cdd1e77e8d51df50f83  Mishnah/Seder Moed/Mishnah Chagigah/Hebrew/Mishnah, ed. Romm, Vilna 1913.json
1fed90b487d3b0d392fb4918c09a91fb1ca8fc7d  Mishnah/Seder Moed/Mishnah Eruvin/English/Mishnah Yomit by Dr. Joshua Kulp.json
5e18a12500006b33b0482f531cf1d9bc7a2fece8  Mishnah/Seder Moed/Mishnah Eruvin/Hebrew/Mishnah, ed. Romm, Vilna 1913.json
012eaf978da2bb69bcafe0c9d37676210c0bf2e9  Mishnah/Seder Moed/Mishnah Megillah/English/Mishnah Yomit by Dr. Joshua Kulp.json
6c6c34fdd066970ecef9ab0dbd99c802a07809fb  Mishnah/Seder Moed/Mishnah Megillah/Hebrew/Mishnah, ed. Romm, Vilna 1913.json
5891c6adc2e7ce68bdc08f049e700431a2a230f7  Mishnah/Seder Moed/Mishnah Moed Katan/English/Mishnah Yomit by Dr. Joshua Kulp.json
8615732d86b7205a489b90f5da43d843d0d34602  Mishnah/Seder Moed/Mishnah Moed Katan/Hebrew/Mishnah, ed. Romm, Vilna 1913.json
6b67686cbc27df3129a43723a12a70a7f1d038b8  Mishnah/Seder Moed/Mishnah Pesachim/English/Mishnah Yomit by Dr. Joshua Kulp.json
97da310d4198d7da9c1d757b2b03187b93e7cc73  Mishnah/Seder Moed/Mishnah Pesachim/Hebrew/Mishnah, ed. Romm, Vilna 1913.json
74a7f5f62b6a59ded9fa3863095c13cdaa80ee9b  Mishnah/Seder Moed/Mishnah Rosh Hashanah/English/Mishnah Yomit by Dr. Joshua Kulp.json
979f83510f497c2892de6a5b6d1e9b2a0f10a9e3  Mishnah/Seder Moed/Mishnah Rosh Hashanah/Hebrew/Mishnah, ed. Romm, Vilna 1913.json
01ccf3ebf15a90e89ccce46720b4fa6ac1e84f71  Mishnah/Seder Moed/Mishnah Shabbat/English/Mishnah Yomit by Dr. Joshua Kulp.json
33e5c57f1a085d6ea8447352528edc8521606957  Mishnah/Seder Moed/Mishnah Shabbat/Hebrew/Mishnah, ed. Romm, Vilna 1913.json
e0224bdc53708f29358f602129c1683750618ee0  Mishnah/Seder Moed/Mishnah Shekalim/English/Mishnah Yomit by Dr. Joshua Kulp.json
4c570c7e07e5d0f01ff6a48cf7f7b55f443642c3  Mishnah/Seder Moed/Mishnah Shekalim/Hebrew/Mishnah, ed. Romm, Vilna 1913.json
538c6f35433efc533a8e87975c4526083cbe2dc5  Mishnah/Seder Moed/Mishnah Sukkah/English/Mishnah Yomit by Dr. Joshua Kulp.json
b87665dc9cd4b33224ce45788cc84bef31fc34bb  Mishnah/Seder Moed/Mishnah Sukkah/Hebrew/Mishnah, ed. Romm, Vilna 1913.json
f845e1fcb572d93f5cb9fb1a9f89177a095bfb8f  Mishnah/Seder Moed/Mishnah Ta'anit/English/Mishnah Yomit by Dr. Joshua Kulp.json
667750fab4e48cf28ac54fa5587dfc058ac87436  Mishnah/Seder Moed/Mishnah Ta'anit/Hebrew/Mishnah, ed. Romm, Vilna 1913.json
9f8e93e135b1f1d8edf1202197d8727fcd3ce49a  Mishnah/Seder Moed/Mishnah Yoma/English/Mishnah Yomit by Dr. Joshua Kulp.json
6c5115ee11632c68f3e37591d812760c44953b6d  Mishnah/Seder Moed/Mishnah Yoma/Hebrew/Mishnah, ed. Romm, Vilna 1913.json
52a04e74912922c7256c3afa603505216bdfa45f  Mishnah/Seder Nashim/Mishnah Gittin/English/Mishnah Yomit by Dr. Joshua Kulp.json
42bf5ff6289994fa22191029f41560ebac9be8ec  Mishnah/Seder Nashim/Mishnah Gittin/Hebrew/Mishnah, ed. Romm, Vilna 1913.json
7d861424d5b6750dd84f0b41bb17e6e028c9415f  Mishnah/Seder Nashim/Mishnah Ketubot/English/Mishnah Yomit by Dr. Joshua Kulp.json
19739b737b3062dec7faaa8171e549398af7172f  Mishnah/Seder Nashim/Mishnah Ketubot/Hebrew/Mishnah, ed. Romm, Vilna 1913.json
ce1e4cf57aa047c9dbb197b99ef06855368395d3  Mishnah/Seder Nashim/Mishnah Kiddushin/English/Mishnah Yomit by Dr. Joshua Kulp.json
fc8703f318bf2870f064e6eb0bdfc6887c5a79c1  Mishnah/Seder Nashim/Mishnah Kiddushin/Hebrew/Mishnah, ed. Romm, Vilna 1913.json
e007f1e54a47a8e998c7dc4f66f61a6035c9bd79  Mishnah/Seder Nashim/Mishnah Nazir/English/Mishnah Yomit by Dr. Joshua Kulp.json
a4d4e1e8a0a744a99be729477040757668120850  Mishnah/Seder Nashim/Mishnah Nazir/Hebrew/Mishnah, ed. Romm, Vilna 1913.json
49e976fd68662776a5d75b1f5a6cfb7ce1462ae3  Mishnah/Seder Nashim/Mishnah Nedarim/English/Mishnah Yomit by Dr. Joshua Kulp.json
d3074ff4350eaffb030c66b4fd3b89065fc9db19  Mishnah/Seder Nashim/Mishnah Nedarim/Hebrew/Mishnah, ed. Romm, Vilna 1913.json
f951e4be123fe455ecbe510687b092bc7dd408a3  Mishnah/Seder Nashim/Mishnah Sotah/English/Mishnah Yomit by Dr. Joshua Kulp.json
4693577314500c22915b00c6331b8078fea0726b  Mishnah/Seder Nashim/Mishnah Sotah/Hebrew/Mishnah, ed. Romm, Vilna 1913.json
6b548c789e381790d44123faa27a8961d609436c  Mishnah/Seder Nashim/Mishnah Yevamot/English/Mishnah Yomit by Dr. Joshua Kulp.json
83c25c2007f657c8fafb824649576eea4f1d43e4  Mishnah/Seder Nashim/Mishnah Yevamot/Hebrew/Mishnah, ed. Romm, Vilna 1913.json
f1c1e48df745d4eef4411e0034c42f2ec11d43a8  Mishnah/Seder Nezikin/Mishnah Avodah Zarah/English/Mishnah Yomit by Dr. Joshua Kulp.json
6f573aea678e24b44fa81745a6e397371aa096a9  Mishnah/Seder Nezikin/Mishnah Avodah Zarah/Hebrew/Mishnah, ed. Romm, Vilna 1913.json
4d8b2068ba28f9b8ba9db49237025f95acd7bb93  Mishnah/Seder Nezikin/Mishnah Bava Batra/English/Mishnah Yomit by Dr. Joshua Kulp.json
88d534256b04ca0673ba4bcb83b76d06857ef7a8  Mishnah/Seder Nezikin/Mishnah Bava Batra/Hebrew/Mishnah, ed. Romm, Vilna 1913.json
b2e6762c3cd78f2b021b38ac9d50a9f16d893fb3  Mishnah/Seder Nezikin/Mishnah Bava Kamma/English/Mishnah Yomit by Dr. Joshua Kulp.json
a918c9c9c8b739995724c7571f17380f3ddee5a6  Mishnah/Seder Nezikin/Mishnah Bava Kamma/Hebrew/Mishnah, ed. Romm, Vilna 1913.json
158dbbd33f0385f2c336f27a76e06eac9420d722  Mishnah/Seder Nezikin/Mishnah Bava Metzia/English/Mishnah Yomit by Dr. Joshua Kulp.json
575af823968ed44e16d18724be6ffdef86cf0f51  Mishnah/Seder Nezikin/Mishnah Bava Metzia/Hebrew/Mishnah, ed. Romm, Vilna 1913.json
b730b0bcdeafc34501711123a5fce94685baceac  Mishnah/Seder Nezikin/Mishnah Eduyot/English/Mishnah Yomit by Dr. Joshua Kulp.json
dab24dc65bdc0261c911e67af34d4496c825150b  Mishnah/Seder Nezikin/Mishnah Eduyot/Hebrew/Mishnah, ed. Romm, Vilna 1913.json
4a7f79421b1f77fd961e6f6b72c1e3f6f145e7e5  Mishnah/Seder Nezikin/Mishnah Horayot/English/Mishnah Yomit by Dr. Joshua Kulp.json
e00ca809e72d683c4fb5208886e72141d2c76ff4  Mishnah/Seder Nezikin/Mishnah Horayot/Hebrew/Mishnah, ed. Romm, Vilna 1913.json
fbb4b1b76e5357007990a7751b1afc4dc941c788  Mishnah/Seder Nezikin/Mishnah Makkot/English/Mishnah Yomit by Dr. Joshua Kulp.json
6294e0aab0b3f1f1dddf88db67cffc255988bd05  Mishnah/Seder Nezikin/Mishnah Makkot/Hebrew/Mishnah, ed. Romm, Vilna 1913.json
783e2592296ce02dce874d82b595956e33dca998  Mishnah/Seder Nezikin/Mishnah Sanhedrin/English/Mishnah Yomit by Dr. Joshua Kulp.json
990b83ab7a96f57c9ca02936ed431cd2d940e818  Mishnah/Seder Nezikin/Mishnah Sanhedrin/Hebrew/Mishnah, ed. Romm, Vilna 1913.json
4ec9d9efa7daf65ae76c9de9b3d51ab245c059a0  Mishnah/Seder Nezikin/Mishnah Shevuot/English/Mishnah Yomit by Dr. Joshua Kulp.json
0f2c739e7d081e155e94bedafd833bf3d80036be  Mishnah/Seder Nezikin/Mishnah Shevuot/Hebrew/Mishnah, ed. Romm, Vilna 1913.json
c3164223aa4ddeecf23c76bfce29a91138b0cb0f  Mishnah/Seder Nezikin/Pirkei Avot/English/Mishnah Yomit by Dr. Joshua Kulp.json
76b6484cb972f56fc31565e8be3a44101eaf042d  Mishnah/Seder Nezikin/Pirkei Avot/Hebrew/Mishnah, ed. Romm, Vilna 1913.json
16dc313781d1d4fee57fcdc3772157b0784dbfae  Mishnah/Seder Tahorot/Mishnah Kelim/English/Mishnah Yomit by Dr. Joshua Kulp.json
2067eeb89c69903c708e9b608fbc3128782a36a3  Mishnah/Seder Tahorot/Mishnah Kelim/Hebrew/Mishnah, ed. Romm, Vilna 1913.json
8e9fc091ad80271bc160c4d019aaf61f1ce309ea  Mishnah/Seder Tahorot/Mishnah Makhshirin/English/Mishnah Yomit by Dr. Joshua Kulp.json
50b68a14b9077faa8d60035cbe97b928b6cc5388  Mishnah/Seder Tahorot/Mishnah Makhshirin/Hebrew/Mishnah, ed. Romm, Vilna 1913.json
026220ce56eccf748f5097056294f265118cb6ff  Mishnah/Seder Tahorot/Mishnah Mikvaot/English/Mishnah Yomit by Dr. Joshua Kulp.json
a9cb603ecf24056047bfe4ec3b877ba926886310  Mishnah/Seder Tahorot/Mishnah Mikvaot/Hebrew/Mishnah, ed. Romm, Vilna 1913.json
8406bd7e9654860b4b816abf5a6279b560d6cc7b  Mishnah/Seder Tahorot/Mishnah Negaim/English/Mishnah Yomit by Dr. Joshua Kulp.json
2d867ba1b2dac4992a67110c447ba18d7234f3ad  Mishnah/Seder Tahorot/Mishnah Negaim/Hebrew/Mishnah, ed. Romm, Vilna 1913.json
e14bc4b6b5893f984d06f73611cdd93af473b530  Mishnah/Seder Tahorot/Mishnah Niddah/English/Mishnah Yomit by Dr. Joshua Kulp.json
9320fc5ed22d32437c664b4b195466e0e2056720  Mishnah/Seder Tahorot/Mishnah Niddah/Hebrew/Mishnah, ed. Romm, Vilna 1913.json
548db13bbb75630faea3b4263ce8a669dea0e8de  Mishnah/Seder Tahorot/Mishnah Oholot/English/Mishnah Yomit by Dr. Joshua Kulp.json
b245546dbda4e7e8255f1eda2e8ecbdee6b43400  Mishnah/Seder Tahorot/Mishnah Oholot/Hebrew/Mishnah, ed. Romm, Vilna 1913.json
d27323c895e1889bf69db90beedb8f349f8915c6  Mishnah/Seder Tahorot/Mishnah Oktzin/English/Mishnah Yomit by Dr. Joshua Kulp.json
60ed4fb4da5730474d4bf5b23b5fc0b1ce2cf980  Mishnah/Seder Tahorot/Mishnah Oktzin/Hebrew/Mishnah, ed. Romm, Vilna 1913.json
94a66f013cba08dca41061d8ef7b92abc8849f37  Mishnah/Seder Tahorot/Mishnah Parah/English/Mishnah Yomit by Dr. Joshua Kulp.json
b818233a9f91617a08f3aa529376296561632e73  Mishnah/Seder Tahorot/Mishnah Parah/Hebrew/Mishnah, ed. Romm, Vilna 1913.json
626decf7f796b73f70eef7059c082c43e293739a  Mishnah/Seder Tahorot/Mishnah Tahorot/English/Mishnah Yomit by Dr. Joshua Kulp.json
dd579f3d45090369912a07bdfa28577fcd72c41c  Mishnah/Seder Tahorot/Mishnah Tahorot/Hebrew/Mishnah, ed. Romm, Vilna 1913.json
bf60c487672e92727d5645913e833632c299c309  Mishnah/Seder Tahorot/Mishnah Tevul Yom/English/Mishnah Yomit by Dr. Joshua Kulp.json
4fbaad9e767462176aebe0baa80b09884c583873  Mishnah/Seder Tahorot/Mishnah Tevul Yom/Hebrew/Mishnah, ed. Romm, Vilna 1913.json
d4af3abdbeb9ab2ad717b517177e51ae95e9a2bb  Mishnah/Seder Tahorot/Mishnah Yadayim/English/Mishnah Yomit by Dr. Joshua Kulp.json
373515656974d5a7100137b5a0e97eb4024d1ec8  Mishnah/Seder Tahorot/Mishnah Yadayim/Hebrew/Mishnah, ed. Romm, Vilna 1913.json
accfd83d560d35c570892101b54ac98c37ee5824  Mishnah/Seder Tahorot/Mishnah Zavim/English/Mishnah Yomit by Dr. Joshua Kulp.json
9acf0f1e9ef0943fc78a388e02d28a9ac6f3411f  Mishnah/Seder Tahorot/Mishnah Zavim/Hebrew/Mishnah, ed. Romm, Vilna 1913.json
d00c7bba9c12ad8a8be249619da9a600db2fbb36  Mishnah/Seder Zeraim/Mishnah Berakhot/English/Mishnah Yomit by Dr. Joshua Kulp.json
62b357931b00366f5c4d0f250ba1fbd3fd6d79de  Mishnah/Seder Zeraim/Mishnah Berakhot/Hebrew/Mishnah, ed. Romm, Vilna 1913.json
f0f006cce15cc7d35752f0b8d25e130faff4a537  Mishnah/Seder Zeraim/Mishnah Bikkurim/English/Mishnah Yomit by Dr. Joshua Kulp.json
a288833f36b81cfa0c386a350255f279ee555962  Mishnah/Seder Zeraim/Mishnah Bikkurim/Hebrew/Mishnah, ed. Romm, Vilna 1913.json
582512085e5bba9561a1a365376755823c5e723d  Mishnah/Seder Zeraim/Mishnah Challah/English/Mishnah Yomit by Dr. Joshua Kulp.json
36be8be4d29953fe58455633f868909b7b29a2a5  Mishnah/Seder Zeraim/Mishnah Challah/Hebrew/Mishnah, ed. Romm, Vilna 1913.json
2bc9b8428d812f94f4afe89e29ec74cce71725e6  Mishnah/Seder Zeraim/Mishnah Demai/English/Mishnah Yomit by Dr. Joshua Kulp.json
35957b1be5d3de8c5b7fab9d717d37c3388139fe  Mishnah/Seder Zeraim/Mishnah Demai/Hebrew/Mishnah, ed. Romm, Vilna 1913.json
68a4fc89e794f1ecfe790d1eece8e29c6627c72c  Mishnah/Seder Zeraim/Mishnah Kilayim/English/Mishnah Yomit by Dr. Joshua Kulp.json
38b5a465112b841f353533c9ab0d87503548513e  Mishnah/Seder Zeraim/Mishnah Kilayim/Hebrew/Mishnah, ed. Romm, Vilna 1913.json
e0cf029434fe874ec7e9f68946b4915795fc077f  Mishnah/Seder Zeraim/Mishnah Maaser Sheni/English/Mishnah Yomit by Dr. Joshua Kulp.json
b46ec33f927b9819f6b79dc2713dc9296d41b819  Mishnah/Seder Zeraim/Mishnah Maaser Sheni/Hebrew/Mishnah, ed. Romm, Vilna 1913.json
e7e98d282e4f50f0ed912f81cf8b75cfffc709ed  Mishnah/Seder Zeraim/Mishnah Maasrot/English/Mishnah Yomit by Dr. Joshua Kulp.json
1fd93cc64accc688656306563714039d9f075c3e  Mishnah/Seder Zeraim/Mishnah Maasrot/Hebrew/Mishnah, ed. Romm, Vilna 1913.json
f738cef67eb7fb16dd388496ec09a36ea9deca0b  Mishnah/Seder Zeraim/Mishnah Orlah/English/Mishnah Yomit by Dr. Joshua Kulp.json
547ff8f64965ee3677de160278fafb9a8a31ca23  Mishnah/Seder Zeraim/Mishnah Orlah/Hebrew/Mishnah, ed. Romm, Vilna 1913.json
ee7da8bf70dcc5b12f4689e4ffcbf1668e19dddf  Mishnah/Seder Zeraim/Mishnah Peah/English/Mishnah Yomit by Dr. Joshua Kulp.json
26c99385e70359265cee2c24c3390b53d1a82ceb  Mishnah/Seder Zeraim/Mishnah Peah/Hebrew/Mishnah, ed. Romm, Vilna 1913.json
416e7c2df5efa18175d85df95f2f605709c02c41  Mishnah/Seder Zeraim/Mishnah Sheviit/English/Mishnah Yomit by Dr. Joshua Kulp.json
9b576cc4c4fa56028cd918973b18be113f4537b0  Mishnah/Seder Zeraim/Mishnah Sheviit/Hebrew/Mishnah, ed. Romm, Vilna 1913.json
be084d1b572ea5f189cfb2d39e07149056b957d4  Mishnah/Seder Zeraim/Mishnah Terumot/English/Mishnah Yomit by Dr. Joshua Kulp.json
f64b01ca0c3d7e6e7a6a08973a7ec175195e3ace  Mishnah/Seder Zeraim/Mishnah Terumot/Hebrew/Mishnah, ed. Romm, Vilna 1913.json
3b7bb0e80fc1822fbe250179f2da7b0e5c4a8e03  Talmud/Bavli/Seder Kodashim/Tamid/English/William Davidson Edition - English.json
2489783832d34e06c2572e3b8d91ab3dd1d3fae2  Talmud/Bavli/Seder Kodashim/Tamid/Hebrew/William Davidson Edition - Aramaic.json
58ec7394b7c9d6a5732abb4474595f0401c8e6d2  Talmud/Bavli/Seder Moed/Chagigah/English/William Davidson Edition - English.json
4f241e7404a3a019b5bb066694f91d5b843a39fa  Talmud/Bavli/Seder Moed/Chagigah/Hebrew/William Davidson Edition - Aramaic.json
13e62b0b57caec50dca81c4b121edf33f527ca51  Talmud/Bavli/Seder Moed/Megillah/English/William Davidson Edition - English.json
46657c5a609d71103bb23a7b089db9c8aa8d1afe  Talmud/Bavli/Seder Moed/Megillah/Hebrew/William Davidson Edition - Aramaic.json
3f53360b31fce1e0d7985221732eaf2fb13b4060  Talmud/Bavli/Seder Moed/Pesachim/English/William Davidson Edition - English.json
3de8a30ebb6dd1b772c3100f95417ba48e880818  Talmud/Bavli/Seder Moed/Pesachim/Hebrew/William Davidson Edition - Aramaic.json
28a0aada352ab6da655c3d1acc15a343ce549b40  Talmud/Bavli/Seder Moed/Rosh Hashanah/English/William Davidson Edition - English.json
7bff69be4aff41789b54c547203f7426fe9a48c0  Talmud/Bavli/Seder Moed/Rosh Hashanah/Hebrew/William Davidson Edition - Aramaic.json
57d9614ae67212075e22d94d2386a54cfc996a9e  Talmud/Bavli/Seder Moed/Shabbat/English/William Davidson Edition - English.json
d191440035ce856f15935be435c45df35380daf8  Talmud/Bavli/Seder Moed/Shabbat/Hebrew/William Davidson Edition - Aramaic.json
95377112d1b9e29ae449e72db2cf3d056c42be3e  Talmud/Bavli/Seder Moed/Sukkah/English/William Davidson Edition - English.json
3c9249362144bb1b2e521a70f7197387f74eafe7  Talmud/Bavli/Seder Moed/Sukkah/Hebrew/William Davidson Edition - Aramaic.json
579fb9490b4be3b184259d7db49b9696bd22b728  Talmud/Bavli/Seder Moed/Taanit/English/William Davidson Edition - English.json
416cdcda102b50545e31cc56ed9abba0178acd5d  Talmud/Bavli/Seder Moed/Taanit/Hebrew/William Davidson Edition - Aramaic.json
c74299f92cbc789b8258ba7dc2ec0ed638bddbab  Talmud/Bavli/Seder Moed/Yoma/English/William Davidson Edition - English.json
eed9268f7f79097d7754a5e054ff274f644440d7  Talmud/Bavli/Seder Moed/Yoma/Hebrew/William Davidson Edition - Aramaic.json
9efa8982ed908423e25cd9d57881bd5f0f76b17d  Talmud/Bavli/Seder Nashim/Gittin/English/William Davidson Edition - English.json
be934329d9c607c6f475ef99ffbab0f7ca8c733e  Talmud/Bavli/Seder Nashim/Gittin/Hebrew/William Davidson Edition - Aramaic.json
2ddb88c22c97f6d6a4cb3a731cd4bb71b6006a57  Talmud/Bavli/Seder Nashim/Sotah/English/William Davidson Edition - English.json
a757aa54d0834ea6d5b4c33e9549436e8d969536  Talmud/Bavli/Seder Nashim/Sotah/Hebrew/William Davidson Edition - Aramaic.json
142045af2a4e0061339680dd5f7250c6809c43f1  Talmud/Bavli/Seder Nezikin/Avodah Zarah/English/William Davidson Edition - English.json
bfd73452f3e7b139fedb87e808c08015a99b6f7f  Talmud/Bavli/Seder Nezikin/Avodah Zarah/Hebrew/William Davidson Edition - Aramaic.json
471bdf35b745afe83c336ff41f4ea699333c52ac  Talmud/Bavli/Seder Nezikin/Bava Batra/English/William Davidson Edition - English.json
348feb2f712efa4596954d60e240bd0346196cf3  Talmud/Bavli/Seder Nezikin/Bava Batra/Hebrew/William Davidson Edition - Aramaic.json
9a35e406149ffa28473eb9d238ab8bca3351a103  Talmud/Bavli/Seder Nezikin/Makkot/English/William Davidson Edition - English.json
3f0e04325dfbd89bde68190f72314ef7fd5b1f48  Talmud/Bavli/Seder Nezikin/Makkot/Hebrew/William Davidson Edition - Aramaic.json
9ca90b3f7aaf5c0cbfc7f94d248f9b17adf20af2  Talmud/Bavli/Seder Nezikin/Sanhedrin/English/William Davidson Edition - English.json
1b8e034b31c2ef8c305782b7cd26488d7f614c32  Talmud/Bavli/Seder Nezikin/Sanhedrin/Hebrew/William Davidson Edition - Aramaic.json
133ea9dce3d110705f1fcc98c38ba921370d92a5  Talmud/Bavli/Seder Zeraim/Berakhot/English/William Davidson Edition - English.json
497c5fe34af6ae5e9f5fee3fdda054d2143edc7d  Talmud/Bavli/Seder Zeraim/Berakhot/Hebrew/William Davidson Edition - Aramaic.json
56df0be99d4e14a797f3e8cc7bbbeaa4366b927e  Tosefta/Vilna Edition/Seder Kodashim/Tosefta Arakhin/English/Sefaria Community Translation.json
32a9aacdc3f97c4e9670f6dbfb5d960a8a4b08be  Tosefta/Vilna Edition/Seder Kodashim/Tosefta Arakhin/Hebrew/Tosefta Arakhin - Machon Mamre.json
5b27062a79c51440095a19822c7dbafa9cce08df  Tosefta/Vilna Edition/Seder Kodashim/Tosefta Bekhorot/English/Sefaria Community Translation.json
d2d33fc67ebb7f650c89896d0baba304058a2617  Tosefta/Vilna Edition/Seder Kodashim/Tosefta Bekhorot/Hebrew/Tosefta Bekhorot - Machon Mamre.json
5e4101122a6f493449486018eb3aca71c36f906c  Tosefta/Vilna Edition/Seder Kodashim/Tosefta Chullin/English/Sefaria Community Translation.json
bc6c3cd869e46fd3ea1cf92e9cb7db580123187a  Tosefta/Vilna Edition/Seder Kodashim/Tosefta Chullin/Hebrew/Tosefta Chullin - Machon Mamre.json
51a35201e5b236f56e6ae765ddf877fb3ba3b94d  Tosefta/Vilna Edition/Seder Kodashim/Tosefta Keritot/English/Sefaria Community Translation.json
90fd58c00d4d39dedba9dd8b152afaf5cf1577f0  Tosefta/Vilna Edition/Seder Kodashim/Tosefta Keritot/Hebrew/Tosefta Keritot - Machon Mamre.json
e7d3c97f5b0d0ae3e19f7a5414c07bb27b584b95  Tosefta/Vilna Edition/Seder Kodashim/Tosefta Meilah/Hebrew/Tosefta Meilah.json
bbdc9e584bd8b8d747a00fc80f8b776850ceca5d  Tosefta/Vilna Edition/Seder Kodashim/Tosefta Menachot/English/Sefaria Community Translation.json
c256ba7fd1ba9d5b3a4f474b8025a3120060a9cb  Tosefta/Vilna Edition/Seder Kodashim/Tosefta Menachot/Hebrew/Tosefta Menahot - Machon Mamre.json
3e39b8e789851b46f4cb181036fceed776059003  Tosefta/Vilna Edition/Seder Kodashim/Tosefta Temurah/Hebrew/Tosefta Temurah - Machon Mamre.json
688de516e8f75ec4be796ad0ed696f5edc89d5fb  Tosefta/Vilna Edition/Seder Kodashim/Tosefta Zevachim/English/Sefaria Community Translation.json
9d5b1db122c0559c6413ded92a0e06cdf8307076  Tosefta/Vilna Edition/Seder Kodashim/Tosefta Zevachim/Hebrew/Tosefta Zevachim - Machon Mamre.json
af92ca335ad4f0b77d8ece14dfcb5aff08252dea  Tosefta/Vilna Edition/Seder Moed/Tosefta Beitzah/English/Sefaria Community Translation.json
5e7bd7b4156ee1249dfa266a77603ef1755b785a  Tosefta/Vilna Edition/Seder Moed/Tosefta Beitzah/Hebrew/Tosefta - Machon Mamre.json
c5971de501ecfa4104533d49fcd8275f1e36f43a  Tosefta/Vilna Edition/Seder Moed/Tosefta Chagigah/English/Sefaria Community Translation.json
fa9f6ee62bca63ba0686912f674ae150b8fa8f73  Tosefta/Vilna Edition/Seder Moed/Tosefta Chagigah/Hebrew/Tosefta Chagigah - Machon Mamre.json
866f286052fb6c9083350b10096ade09b863f30c  Tosefta/Vilna Edition/Seder Moed/Tosefta Eruvin/English/Sefaria Community Translation.json
c3852c627e3c742eba0199b8e660396301db55fc  Tosefta/Vilna Edition/Seder Moed/Tosefta Eruvin/Hebrew/Tosefta Eruvin.json
0c6c1260241e7106be4624ee6b9f6d7d492df230  Tosefta/Vilna Edition/Seder Moed/Tosefta Megillah/English/Sefaria Community Translation.json
504e2317fa208107957d28f461a0580a9d217a2e  Tosefta/Vilna Edition/Seder Moed/Tosefta Megillah/Hebrew/Tosefta Megillah - Machon Mamre.json
936b71a49b0407adcd89bd0ad13753032fb056b7  Tosefta/Vilna Edition/Seder Moed/Tosefta Moed Katan/English/Sefaria Community Translation.json
6687892d7c2765aa29d841346391deeb2a5048ca  Tosefta/Vilna Edition/Seder Moed/Tosefta Moed Katan/Hebrew/Tosefta Moed Katan - Machon Mamre.json
ac75d45d290f48d96f9781e8eeaea0983baa1f32  Tosefta/Vilna Edition/Seder Moed/Tosefta Pesachim/English/Sefaria Community Translation.json
0898efcd4408e560b042527d622211d6c9f0bfcb  Tosefta/Vilna Edition/Seder Moed/Tosefta Pesachim/Hebrew/Tosefta Pesachim.json
32983117ba0c26b42ff52d5377946d246ebd72de  Tosefta/Vilna Edition/Seder Moed/Tosefta Rosh Hashanah/English/Sefaria Community Translation.json
baf00522d8af8cd2424ae6e39f41f218d1a13de1  Tosefta/Vilna Edition/Seder Moed/Tosefta Rosh Hashanah/Hebrew/Tosefta Rosh Hashanah  -- Machon Mamre.json
44ae3d33ff2b3300ce4dcc5707e3967820232ef6  Tosefta/Vilna Edition/Seder Moed/Tosefta Shabbat/English/Sefaria Community Translation.json
2ecf772883f8d9c6899152d02a54cdb4fb256bda  Tosefta/Vilna Edition/Seder Moed/Tosefta Shabbat/Hebrew/Tosefta Shabbat.json
74178c2b9b83561cf24b988a27bdd5daed8b5bbb  Tosefta/Vilna Edition/Seder Moed/Tosefta Shekalim/Hebrew/Tosefta Shekalim.json
29282d8d87265dce35760eb8aeb5a631bacab03d  Tosefta/Vilna Edition/Seder Moed/Tosefta Sukkah/English/Sefaria Community Translation.json
a84260d01059ee5e2fa3e941a6c84c2e30f46c36  Tosefta/Vilna Edition/Seder Moed/Tosefta Sukkah/Hebrew/Tosefta Sukkah.json
2b63b3a2a04d2ba616fb48cd83e9128fcc8266cf  Tosefta/Vilna Edition/Seder Moed/Tosefta Ta'anit/English/Sefaria Community Translation.json
6e705c73eca436c8f45e19e8ac241c303411e5b3  Tosefta/Vilna Edition/Seder Moed/Tosefta Ta'anit/Hebrew/Tosefta Ta'anit -- Machon Mamre.json
cd5177cba5cf4c9f0e158ec205d94fe7c71e2c3f  Tosefta/Vilna Edition/Seder Moed/Tosefta Yoma/English/Sefaria Community Translation.json
2d404589952a1c5518f53917e95ba623f73eae70  Tosefta/Vilna Edition/Seder Moed/Tosefta Yoma/Hebrew/Tosefta Yoma.json
78b692ad8c81f60faae9438cdde61c657e869ec2  Tosefta/Vilna Edition/Seder Nashim/Tosefta Gittin/English/Sefaria Community Translation.json
bc392a48c1e30d3f997f12c5c3cdfe4c9b9cda87  Tosefta/Vilna Edition/Seder Nashim/Tosefta Gittin/Hebrew/Tosefta Gittin - Machon Mamre.json
ec068bd49735e0f3a62bfe7550faaabe0d705896  Tosefta/Vilna Edition/Seder Nashim/Tosefta Ketubot/English/Sefaria Community Translation.json
3a38a6354331de1a96299ebae43ad8e8ec5824d6  Tosefta/Vilna Edition/Seder Nashim/Tosefta Ketubot/Hebrew/Tosefta Ketuvot -- Machon Mamre.json
777e48e127cdd6cd412c7ad2133f343ae0b5c91e  Tosefta/Vilna Edition/Seder Nashim/Tosefta Kiddushin/English/Sefaria Community Translation.json
bccfc8bf915d60c9b3e831437c6d57139aed6072  Tosefta/Vilna Edition/Seder Nashim/Tosefta Kiddushin/Hebrew/Tosefta Kidushin - Machon Mamre.json
31a826ac8894aec322ffaa69b05b46f0ee6ca2af  Tosefta/Vilna Edition/Seder Nashim/Tosefta Nazir/English/Sefaria Community Translation.json
ac53361c907e4ebc444a25e960d2c14a0c7b09d8  Tosefta/Vilna Edition/Seder Nashim/Tosefta Nazir/Hebrew/Tosefta Nazir - Machon Mamre.json
1ce442408998790321705ea96d6249def232139a  Tosefta/Vilna Edition/Seder Nashim/Tosefta Nedarim/English/Sefaria Community Translation.json
dedbca4cdd3db9ded9a3d71046d2540e97019ea2  Tosefta/Vilna Edition/Seder Nashim/Tosefta Nedarim/Hebrew/Tosefta Nedarim - Machon Mamre.json
a0a4686386a470e8d6a831f810753d322549d233  Tosefta/Vilna Edition/Seder Nashim/Tosefta Sotah/English/Sefaria Community Translation.json
1bcfa5bfd524f1fadb372f7e52ad6174dbe6c24f  Tosefta/Vilna Edition/Seder Nashim/Tosefta Sotah/Hebrew/Tosefta Sota - Machon Mamre.json
1d2564bf588aa4fcd594a75f968613f2fb266017  Tosefta/Vilna Edition/Seder Nashim/Tosefta Yevamot/English/Sefaria Community Translation.json
f89e81af6ecdfca78be7029bdf2b77b4c16e6406  Tosefta/Vilna Edition/Seder Nashim/Tosefta Yevamot/Hebrew/Tosefta Yevamot -- Machon Mamre.json
8fb41362afd22ce0631075444e0e0021bed32e0c  Tosefta/Vilna Edition/Seder Nezikin/Tosefta Avodah Zarah/English/Sefaria Community Translation.json
2a28af8993f23476cb7ad3101b70fc8da4a2fc08  Tosefta/Vilna Edition/Seder Nezikin/Tosefta Avodah Zarah/Hebrew/Tosefta Avodah Zarah - Machon Mamre.json
e8c7d50e577a0c8327b4ef4dce5a0b673eadf3d9  Tosefta/Vilna Edition/Seder Nezikin/Tosefta Bava Batra/English/Sefaria Community Translation.json
c103559c85be88420114bce5a0406d871c359985  Tosefta/Vilna Edition/Seder Nezikin/Tosefta Bava Batra/Hebrew/Tosefta Bava Batra - Machon Mamre.json
6dd0a07372e70cc3ce47334e5559b047339639df  Tosefta/Vilna Edition/Seder Nezikin/Tosefta Bava Kamma/English/Sefaria Community Translation.json
a3aeec963a9d69648fe53a2d96477ee1bc8931b3  Tosefta/Vilna Edition/Seder Nezikin/Tosefta Bava Kamma/Hebrew/Tosefta Bava Kamma - Machon Mamre.json
a5a330c647bca00eae19f0868588942718d5b044  Tosefta/Vilna Edition/Seder Nezikin/Tosefta Bava Metzia/Hebrew/Tosefta Bava Metziah - Machon Mamre.json
b17e30e3b1aec275406276219e911dff9efceb66  Tosefta/Vilna Edition/Seder Nezikin/Tosefta Eduyot/English/Sefaria Community Translation.json
df591529301a330142780628b1afdccc6aedc966  Tosefta/Vilna Edition/Seder Nezikin/Tosefta Eduyot/Hebrew/Tosefta Eduyot - Machon Mamre.json
6dd8045eb79884ec844b29fb0efc81cd10bd68a3  Tosefta/Vilna Edition/Seder Nezikin/Tosefta Horayot/English/Sefaria Community Translation.json
f589d276556e36714514e49938e8eb9bbc34a57c  Tosefta/Vilna Edition/Seder Nezikin/Tosefta Horayot/Hebrew/Tosefta Horayot - Machon Mamre.json
a9245db08c0a5ae93cdb1036b7ed530c2e0da722  Tosefta/Vilna Edition/Seder Nezikin/Tosefta Makkot/English/Sefaria Community Translation.json
6a0a42bac67839dd81d93e1459775049e6020552  Tosefta/Vilna Edition/Seder Nezikin/Tosefta Makkot/Hebrew/Tosefta Makkot - Machon Mamre.json
08c7074791634c52af8ef0cecde068642fc75b8e  Tosefta/Vilna Edition/Seder Nezikin/Tosefta Sanhedrin/English/Sefaria Community Translation.json
99f5bdb68cf95a1b544bc6754e5b78d31f498989  Tosefta/Vilna Edition/Seder Nezikin/Tosefta Sanhedrin/Hebrew/Tosefta Sanhedrin - Machon Mamre.json
123fc34841370a19df8331496ab8a1d56af86008  Tosefta/Vilna Edition/Seder Nezikin/Tosefta Shevuot/English/Sefaria Community Translation.json
225fdff531c4a0feac7b361c992c6dbc2ff7f2dc  Tosefta/Vilna Edition/Seder Nezikin/Tosefta Shevuot/Hebrew/Tosefta Shevuot - Machon Mamre.json
bd07107718f0eed87ec05dbf997730b425e3c42c  Tosefta/Vilna Edition/Seder Tahorot/Tosefta Kelim Batra/English/Sefaria Community Translation.json
cd25ad40663af96f52aa3b30ebbe6f51424937d0  Tosefta/Vilna Edition/Seder Tahorot/Tosefta Kelim Batra/Hebrew/Tosefta Keilim Batra - Machon Mamre.json
ccdea1a6ba015175663b9e749cc3451b8a6f84f4  Tosefta/Vilna Edition/Seder Tahorot/Tosefta Kelim Kamma/English/Sefaria Community Translation.json
73b132807f0ec6e074e16e295be53886c3e7bada  Tosefta/Vilna Edition/Seder Tahorot/Tosefta Kelim Kamma/Hebrew/Tosefta Keilim Kamma - Machon Mamre.json
0f791ced3cdfebd3f60ca4ac8bc4a85bea00f125  Tosefta/Vilna Edition/Seder Tahorot/Tosefta Kelim Metzia/English/Sefaria Community Translation.json
6ab9cd11b35c0d702c5783f2420ef8d9f9362bcd  Tosefta/Vilna Edition/Seder Tahorot/Tosefta Kelim Metzia/Hebrew/Tosefta Keilim Metsia - Machon Mamre.json
89555d52861c21c948d3300562b1ca775a72a2fd  Tosefta/Vilna Edition/Seder Tahorot/Tosefta Makhshirin/Hebrew/Tosefta Makhshirin - Machon Mamre.json
babe39576bb1e46ca37d7a9992e7b7c026f03d63  Tosefta/Vilna Edition/Seder Tahorot/Tosefta Mikvaot/English/Sefaria Community Translation.json
237cb0b95f3352ecad4b76b11ba0a51a5ca59af2  Tosefta/Vilna Edition/Seder Tahorot/Tosefta Mikvaot/Hebrew/Tosefta Mikvaot - Machon Mamre.json
b6b70531df13fa929f33013761dda90ebc4a1c47  Tosefta/Vilna Edition/Seder Tahorot/Tosefta Negaim/English/Sefaria Community Translation.json
63164112e671d9e22c27f27da718c53f9d7931c7  Tosefta/Vilna Edition/Seder Tahorot/Tosefta Negaim/Hebrew/Tosefta Negaim - Machon Mamre.json
23070837a45fd814cb3344e4b23481f064227b8e  Tosefta/Vilna Edition/Seder Tahorot/Tosefta Niddah/English/Sefaria Community Translation.json
652e96644840bb5e4fd9b8e8dc0139249e16cb6a  Tosefta/Vilna Edition/Seder Tahorot/Tosefta Niddah/Hebrew/Tosefta Niddah - Machon Mamre.json
b28d7129af8c7fc564bc6982f9f4f12fb57b25b1  Tosefta/Vilna Edition/Seder Tahorot/Tosefta Oholot/English/Sefaria Community Translation.json
1fa0a9c7721da89f99b50014bcd5fc211c0fe6e0  Tosefta/Vilna Edition/Seder Tahorot/Tosefta Oholot/Hebrew/Tosefta Ohalot - Machon Mamre.json
d80d56555af3c4f5e06df89eed871919357ec013  Tosefta/Vilna Edition/Seder Tahorot/Tosefta Oktsin/Hebrew/Tosefta Uktsin - Machon Mamre.json
0f06f63f21590fdcae60f05169251c38c6991f75  Tosefta/Vilna Edition/Seder Tahorot/Tosefta Parah/English/Sefaria Community Translation.json
bab413c5d25aa90d1f095fb6beb147efd4d9f2fb  Tosefta/Vilna Edition/Seder Tahorot/Tosefta Parah/Hebrew/Tosefta Parah - Machon Mamre.json
ee52ccc58b913b87b7a0d72b5f6704208f57f586  Tosefta/Vilna Edition/Seder Tahorot/Tosefta Tahorot/English/Sefaria Community Translation.json
dd791d96e2e5f1a17e43f7493bc3d039c6184151  Tosefta/Vilna Edition/Seder Tahorot/Tosefta Tevul Yom/Hebrew/Tosefta Tevul Yom - Machon Mamre.json
f4340b2caad2027547b100002ba6d97f676311b2  Tosefta/Vilna Edition/Seder Tahorot/Tosefta Tohorot/Hebrew/Tosefta Tohorot - Machon Mamre.json
90075ebdf2c2a4bd9f952651c433f8fd0686011b  Tosefta/Vilna Edition/Seder Tahorot/Tosefta Yadayim/English/Sefaria Community Translation.json
4b1d05855ec5846b0fca838d65308e84dbfbcf74  Tosefta/Vilna Edition/Seder Tahorot/Tosefta Yadayim/Hebrew/Tosefta Yadayim -  Machon Mamre.json
0a33ea5182cbe11c9a885115081f43a8eafdb2ad  Tosefta/Vilna Edition/Seder Tahorot/Tosefta Zavim/English/Sefaria Community Translation.json
4dac407d2f346df6b7d18bf02914b5e74dcfc1e8  Tosefta/Vilna Edition/Seder Tahorot/Tosefta Zavim/Hebrew/Tosefta Zavim - Machon Mamre.json
a2050ecae43668755c60784fde246006832c7cf1  Tosefta/Vilna Edition/Seder Zeraim/Tosefta Berakhot/English/Sefaria Community Translation.json
99b3bb202dff46011b6f5f65e616f123a1c34408  Tosefta/Vilna Edition/Seder Zeraim/Tosefta Berakhot/Hebrew/Tosefta B'rachot.json
3efd697cdec53b3ccb9a4c9d7f541e0449a5e4c9  Tosefta/Vilna Edition/Seder Zeraim/Tosefta Bikkurim/English/Sefaria Community Translation.json
1d496d931fba1150bf19b0ff7cfd23f100c42397  Tosefta/Vilna Edition/Seder Zeraim/Tosefta Bikkurim/Hebrew/Tosefta Bikkurim.json
bd1efd69a8c0678f7ba68162676025ec6a054e1d  Tosefta/Vilna Edition/Seder Zeraim/Tosefta Challah/English/Sefaria Community Translation.json
311d9f721482e03bd7feb21b2e049fa228a13ff9  Tosefta/Vilna Edition/Seder Zeraim/Tosefta Challah/Hebrew/Tosefta Challah.json
d6cbaf51397fd367f706f0abbfd2b00a09c7cc3f  Tosefta/Vilna Edition/Seder Zeraim/Tosefta Demai/English/Sefaria Community Translation.json
e4b8f5264b39d4b89b8b725bbfb3bed5a02ddeab  Tosefta/Vilna Edition/Seder Zeraim/Tosefta Demai/Hebrew/Tosefta Dem'ai.json
b64a4b1b3d5a7b94a5fa5e9ba0d95d1ab4acca29  Tosefta/Vilna Edition/Seder Zeraim/Tosefta Kilayim/English/Sefaria Community Translation.json
756aff30b405c27ab60b0bda0b5e628f5ce2873d  Tosefta/Vilna Edition/Seder Zeraim/Tosefta Kilayim/Hebrew/Tosefta Kilayim.json
1aff41dc650615fed0ea88d13fc99f6928df3b14  Tosefta/Vilna Edition/Seder Zeraim/Tosefta Maaser Sheni/English/Sefaria Community Translation.json
a00c37205f533a26a87a33d0a14b4e00a84b08a9  Tosefta/Vilna Edition/Seder Zeraim/Tosefta Maaser Sheni/Hebrew/Tosefta Maaser Sheini.json
6acec714ffb8f9976c4aec45ddc56173707af645  Tosefta/Vilna Edition/Seder Zeraim/Tosefta Maasrot/English/Sefaria Community Translation.json
3e3fd1e56c99a110f647cd122cd18a5fe4e6876e  Tosefta/Vilna Edition/Seder Zeraim/Tosefta Maasrot/Hebrew/Tosefta Maasrot.json
d553f33e24264d3dcf9f291b676a1e00741c27d8  Tosefta/Vilna Edition/Seder Zeraim/Tosefta Orlah/English/Sefaria Community Translation.json
a7994896f9c0ad3cee024667594407638486f099  Tosefta/Vilna Edition/Seder Zeraim/Tosefta Orlah/Hebrew/Tosefta Orlah.json
e59226f082fe4c538529a1c17b7c7323f9bfe745  Tosefta/Vilna Edition/Seder Zeraim/Tosefta Peah/English/Sefaria Community Translation.json
7611c04742d04d597cc7dc6203e6c1afc6003bb8  Tosefta/Vilna Edition/Seder Zeraim/Tosefta Peah/Hebrew/Tosefta Pei'ah - Machon Mamre.json
5df7cbe732554ad594e351db619fa31b9ca42ff6  Tosefta/Vilna Edition/Seder Zeraim/Tosefta Sheviit/English/Sefaria Community Translation.json
425f5cc7d94b1011e55bac14bf5df84c31917c58  Tosefta/Vilna Edition/Seder Zeraim/Tosefta Sheviit/Hebrew/Tosefta Shvi'it.json
4c0faa78a8eaabbc1dd39a027e1ca8a290b451fa  Tosefta/Vilna Edition/Seder Zeraim/Tosefta Terumot/English/Sefaria Community Translation.json
fd0c8af47bbf98a046abf1045600fa7ce1e81c5b  Tosefta/Vilna Edition/Seder Zeraim/Tosefta Terumot/Hebrew/Tosefta Terumot.json
"""

# Hebrew Wikisource "ויקרא רבה <chapter> <section>" pages: chapter:section,
# revision id, and the revision's SHA-1 (MediaWiki rev sha1 of the wikitext).
_WIKISOURCE_REVISIONS = """
1:1 337448 f26c890848d7973e61a190e10b0174baab2f1309
1:2 392822 9325721cb2145905543c924020e03560ac0b964d
1:3 392823 a43d8b940631938a4572a73329ea2a6abac5aaf1
1:4 392824 10d8a775fe26cc40db29f2e912553c8f4a56eaee
1:5 591549 bfd33dc7686df9eb41340c0914a894b4ca333933
1:6 504579 d42750aa5c478ca4033a761e0f61cc81534231a7
1:7 504581 2197692a0455fcbe89712b183373499474b63721
1:8 504582 569392247ce9591683bade6c4588c86f4f00909c
1:9 504583 48d97896d0831295663f1aa1af55c0ca2fbef8c2
1:10 504585 3f5c967cd750776ea502f28dba96a7e7f1b86b44
1:11 504586 4fd60a004fb9f2b67239731af9ff8cda16de5135
1:12 594824 aa5ccbaef71f36598f7cd4db30619cf81a056f3d
1:13 421925 97cfaa3e7108856cfad4f54b1cc3c277ba915688
1:14 591639 06b75a46bccb8b68af847b495beb5361f9cc8353
1:15 454146 b3af6847beb35a17895a0c39f6f68d353a5cb295
2:1 392398 9ff5c79e7cb2d5cd0682eb90022f8cb7c3d4e7e3
2:2 505071 6e96059546863b7bd60351345735ddc2781e3f3a
2:3 454137 019031b402566c28568e023f2cf942cbb2220907
2:4 269963 95fc0e4258d99ae730721e5e8b4adafce34c1929
2:5 269967 4b8b7cf8d6b2f15101954207f794d99f26e0b117
2:6 269968 f4542f0613ef18a1b07e676fc4d97cad9ed26961
2:7 269969 5459aa796c83738fe565673094a3d844562536ed
2:8 269971 48d820aa3171047203fcedc8556663b8e13f574e
2:9 504587 750aaf129e7ae40a73156ebd43675286e7a5e521
2:10 270400 aceda9de720065a7d8985f8c9068351e07115580
2:11 580193 a733a7825a6e157b3cf4d3dc60004847cea6744c
2:12 270402 53b6769bbc99e831b014d038418cbcbdacc9676b
3:1 454127 c0a4f292e4df6aec304dc0926c9a53ee5eb846d2
3:2 454136 16c758fb333510446bf919e16d561187163d2220
3:3 454139 bff3b22e6a14d0637382d19c54bda1942428a1d0
3:4 454140 ee5a0849de1cd43a3ad02717361ce9a3e4a4accd
3:5 2845408 6c7bad81ee8110e243f92091473275003d0ea1b6
3:6 454149 3655151fb7ad55dd2365cad6d9dcd3e91f3f955e
3:7 858448 6f4bcfe684ec44077f15bb98525f144e9ade2fce
4:1 1098545 5fa53e69e899023025e5d13bd8701616814a710b
4:2 1098546 ae7a3b79903d0e81c467056f6d1d26dffd881ce8
4:3 1098547 480b5e3b6a99b1b45e9c638ff89123308d4721f6
4:4 1098548 6ed49b7a495469a6aaba67979062d034ea3533bf
4:5 1098549 c7a2348f213e56aa725db81a4be5c79cda8bb59a
4:6 1382784 cb43adf92feb4c1282b4b16a18fe01e8637044e0
4:7 506611 942b4345d621ab095f547719772ec87d10ea0fbb
4:8 506612 ed5be99ece89353e7270fd813293ca639f773219
5:1 1098555 6d3a53d2b5bf993e64d9bf126161639728ec84de
5:2 1098556 8870185221f6b872095ce0f64115a7fbaa22cec7
5:3 1098559 0fc6cb82f120b38f5fef86b9b9805c7f7178f4ee
5:4 1098558 44008dd76f1d814e9487b0efca0f273fe3e4ca5e
5:5 1098561 3ae35fa3a3c6228888bddd22f9c4d1beaab840b5
5:6 1098562 7f926dc80d4c51a053316c898270f2e556e1a880
5:7 1098563 6127fd131308fcb2249c7874ad5f0a4f8fb5f4fc
5:8 1098564 189742758218902354cff1180ff96a5d7c8009fe
6:1 520062 779a727d61d769ac7b219e9047a4524f3f7beef7
6:2 520065 23f26088ce142650f6208522f0b1a5b7abba9731
6:3 520066 ebdffa50d5ccdd496a1dbd297cc761280d4bc811
6:4 520067 82073d7b143d2dab06503f72d996ba4a2787f647
6:5 520069 ddc55377058531702de8cd5088a42ded212ec490
6:6 520071 8a0401c9795abad2aa3e1e90d0532f41497efaf1
7:1 303247 90b859710416d5a4f3b46429c205c7c732e33817
7:2 342892 3097b9be49d2faabf89dbd05e71d62438f67e5bb
7:3 270545 5b81cdbcc76378c2e53ddb17f0b815ffcfaca580
7:4 504599 6662c13e233a758981a34025831d2b7bea7de11b
7:5 504600 b574865fcc6bb58a301e6c59c8d5d988ded971a1
7:6 504601 a7a2d098535693b3943492e39ab4339b7347b3c7
8:1 354048 d06e8ff58fd41c7e95fded2f1b3a502b242a687d
8:2 504602 858227d8e6976a3eb3ebb97a41fe22f8e8276d84
8:3 280494 02d4998fd8a52b959229e7031af4a9fdf48ae4d4
8:4 504603 4e6baef6c8ed2db05bf2548ab1392c295b756329
9:1 2900044 34942be4b1034b850ff1de13276776211aab0c6c
9:2 368677 7486910edeed8b5f4b55a6f16818d71e8133ebb8
9:3 596529 8484ab39ee8184e5c9bd8c507daf24f6566f15f2
9:4 270962 488176c75e22c27f34a281b64c1a5467d5401cce
9:5 504604 6b5dace308bd17be24cb51a6f701b93aa69bea21
9:6 270964 87b296cc87fb7a80f35140ca4b0dd4476fd213a9
9:7 270965 277b2b9929bd3273211e652e5fadaba663408288
9:8 270966 77ad41d4c55dfac0d090f80260b38090187667c1
9:9 270967 137df0186ba4657b231ae2f6b25ffe86a6ae429f
10:1 271017 a842e70f3011d5db8ba080c4dd8368c5d6e2c0ec
10:2 271018 b65e227d1ed7e1956ed857915b3f68ab028b4a77
10:3 334089 af01eeea3ab8158f11902d93e91bd7bb32a332a8
10:4 504671 00c1dd34eac28f164a48a7bf61c5a3788e254dc2
10:5 504701 184aedbe5a1631d4cb9a2885fba32d5d97663edf
10:6 504861 c6c810b45e38917c27fb0c583bc690802d4b8c7d
10:7 368033 30191faf9726965d5b1a1d272351d62b5bd6f81e
10:8 504864 7429920cd11a2edc1fbbdeb12ed6a8df9a85b52a
10:9 961915 977f354d4de8306c939f122c899832856489d151
11:1 271256 b6ff18fc342159d2f0cac18c2d9bd4edc585aba0
11:2 505069 b629d0cb81cba744e148cbe3524d16d879699ff9
11:3 271679 123557fb367675f4c7a5e0a932f00ae723e7b4aa
11:4 505090 cf90ffa805494d446ff0b459f0f974e839f3b7fd
11:5 271681 809a885041d52c55e1db74d40920eaaea6202472
11:6 369204 619bf8da4be9b8ccb4cc03a733ceb2d994e8fe78
11:7 335198 aa884f8ea485b0b71e8792f4183cf134d5bf1936
11:8 505092 9509bc2af60c035586f58d58ce10018065dfb2e0
11:9 271732 d9a295397ba05a468654307ce201f5a6cdbc68db
12:1 505074 3254315da8f63798d7e7e0ae37bd0a809696e5c2
12:2 505077 2dea2df022e47a67751ed7501ec8a298be9b95a4
12:3 1278027 5951669adc1775046085de1434f55508e5ed3e0a
12:4 1278031 e7c13286669509a484c9195f5b7c7511ab027182
12:5 400093 03de501180fe2ef24a719dc5f1ebca63b65a023d
13:1 505086 55a82ba0fd10da445a14fa8a39458e26c6960dec
13:2 1104076 f88929ddec34e7f7f67f9b1d48d3d8a66b2445b7
13:3 1116701 4b2f7c19aa578b7d1b93577a561b3905f2b83de7
13:4 505089 e733db59b841f48e2108cf85d8d9a632475a51d9
13:5 2896467 87be8813bdd9ef9e5d0878f96a3d615f6532d3b6
14:1 534378 135396ea21d8b9e22fbbc8e348c4cf0d567ef936
14:2 534380 dc172ef6aba459a96ad68346947bc059cd15f49a
14:3 915199 edec594c21a5eb9263132590e2d7b64154581bc6
14:4 274177 3c9b9e42e1da62f7432a85f61fd3851ecd46fade
14:5 274178 eec10c2390fc4ca3df47ecbe616811346d0df2cb
14:6 274179 06d3f0383878a766a24420562ecf4601a9b01535
14:7 274189 ceb60a4e515167f4590c6775c1c4361c1a8903c9
14:8 274182 82594e6de742259a1043d3f0670ae2e4bb06921a
14:9 274183 313e16e2dae9cc1be705a6b06ee0dbbf7c8ef3b0
15:1 274192 4d49540e0ab6b661ad0fbb12eccdcb1fbceb0d3c
15:2 906974 74574f182b81df7449d9b6569d973114ee27ca7b
15:3 274283 15d23192d8fc2a04a1c1728975808a824d4f289f
15:4 274284 fd70032f50e81e14723e385831d32908d5630871
15:5 274285 803de5007c18c8b2846baf8a7777c7a87e9d17d1
15:6 274286 76cb80ea019839ea7def5aceb32ec1b7d4409db3
15:7 274287 1906735e2df8c37a4927385c35f6834993217c1a
15:8 274288 6926c11649cfe995622fd49611a466e9accbfa10
15:9 1082051 18e928fbfce32c0735879a7e7c24a0b406baf287
16:1 451663 1933f22e04fd08211f5afccf8d717803122d652b
16:2 479977 e0e97e339e44d0812c3a1fdbccb6309ef1cfd65c
16:3 274312 cc36afc937566ecfec9fd60821df4a382b28c5c0
16:4 369245 ca402a3cb472b84b0dbcb2f0338e34b9faee8e83
16:5 274427 ad132f466c47fdbe475981f2b6cc43db4771b9ee
16:6 274429 1398684a96cc3b90b7841083aade86964760df45
16:7 274430 d5221e8d2e28a148149992b911095ec3ec2e63f4
16:8 274431 f78a9a88f11a2aa7ba97f1be7b2f3b47720201b4
16:9 274433 66adca821f3955f8e8967a93116aa5efc8b8792d
17:1 721022 b7973d5d76d42f250289e1b90dae13ba0d779235
17:2 721023 7269114d80a83130f6aea69dde4a7a669f0ebbea
17:3 688528 ea27d3f32c89034a13a10a4b6af03271417b1716
17:4 721024 19da69772189481529651061025f18d87b07eb90
17:5 721025 5db39aa21b4f496f19f71a3cfe90658dbe4c8f1c
17:6 721026 9f32eae7316b67bd0c637e113cbf5508a5b853ba
17:7 721027 bde03042c5562e3644822de81f17b0e1ae96f5c0
18:1 1289461 f7a0b832c44ad05ad10c0717173b7ee924c86a0b
18:2 368144 d91939c3c8b4d8dbf3c91c20fcc72af180044cc6
18:3 274646 4302ace22327e5aa993e1a9ef346be966be0a2b0
18:4 274748 7c5dcb58154c944fb6b3b4a15de0dd4fb8d1db64
18:5 274750 d976495ecfde69207554e8eacfe63e165f7dd562
19:1 274754 c10f6d6201c7c596e32c7bb872a9f129b58ecb70
19:2 746778 ad068fae9485fff6b66ff4a87ec4b3a4fa88695c
19:3 401316 e0c27df1a0d349ae0bae5c8f3b7697440d8f4368
19:4 2901505 686fb5594d95751ba3d8ab1b79cc77bf11b2bcf0
19:5 2901504 4e15d8d6bcc807c22144f3c57273b379be175ef2
19:6 2901503 036f51a2841c9545a15d5a873bd88eb04a5719fd
20:1 2872932 445675d8f3c5c5282d7406b628d0e1c7f55ef396
20:2 369316 49d44c9d0ac7b4d6743dece6d752fd28fef97ce2
20:3 275326 8f813f39eeb823ce9efb25b6a24ec3cc0397416b
20:4 275327 5bf335763bb92766b784c4c20017dbb5f3cba8bf
20:5 1405749 cb35e987358a9fb8dc3c2fa8bb12564eded9731c
20:6 278863 7b4254a971b1f2036c94caca233f5e253064e3c4
20:7 275330 1caa62b09b7bdb0979a0655d423fb4116eeecaa6
20:8 275331 9523bf7e43e8117709a29b29c7c6a0cd40548acf
20:9 275332 33ebdb722f6f42b06cf46cb6c5b08677d15bd90f
20:10 1459009 9005a4df24ba78ad5fe0cf6a9e63f45929236e36
20:11 275334 992b51534568bf8b5b900584c0afbd686461ecfd
20:12 275335 c93b5562005df7dad7afec1fc57f4342a990894f
21:1 275397 faae588e9992d988a7b809841ca0bd217238475e
21:2 275396 cfc7ce3549fc03be3a8cbb2713e7d4a1c2cb9819
21:3 275395 f3d5c9c16347881cfad9faf191caff26b85b3ca5
21:4 275439 049a48af6135f827cdab22da06e7fdc213cb056d
21:5 451643 75fa07ff04f15b826956c07e3185edf0966b9666
21:6 505097 095b4d5452994c8874e34665024545bc253efd9c
21:7 950032 c47e630610d34a4c9b2e40f95784e5efdabf2d03
21:8 275418 340158c4f16c21f9df1faa2c919ca7df4923a527
21:9 418249 2cd869b6baf906541da6cb8e2ed1929741d479a5
21:10 275420 3c4f9a512d7f5c88a5ea09ecab004342bcd7f3ef
21:11 345060 7d925f3284d0042f2de13d96a7c28aa4a37d3e6f
21:12 275423 1dae879aa8a918ea7c979b680de43de2254859b3
22:1 275438 3b2bfd6db7106a2c6a9de2ef8817820c7c9adbad
22:2 275440 5c5628b7eeb9c795ad28d4d0eab8a1812f7f112d
22:3 275441 77171bad561b9215ff3ca7e5ac4c879d36be77b0
22:4 392339 143a1f1241763718e4e51bc015da036befda96fa
22:5 505098 e32ab1c2448ca1335811a34bf747acb93bfe1bef
22:6 335376 53f58f6ace841e72624dec5efb1240d70349fa48
22:7 275541 19378e659e230380afd30f233eeaee5fe1f0edb7
22:8 275542 fdb550df1272bb0b632f8ba89c358f2d3fb33318
22:9 275543 c3593b4582777e0c40a5608a3e4c83cc8f753697
22:10 275544 33c5e6a1c1244ee34972c39354e588878fc05e27
23:1 275560 50c6abfad15b4ee2906ddb380166d2f6c4ab5991
23:2 1076198 518f892927fee052dbff81b6d3c07736b18100bd
23:3 275563 17186feb71b9f7f094bc7b4fa2c4485bea06bfb9
23:4 275567 1356c0fc891a5b77b28b8553073af8cd98fc7af9
23:5 1076199 94e2e8d2bd8881a8721afc1ee9d08593672c2a68
23:6 1076200 d3b06dc14416e206f37d08194943c9b5f73597a7
23:7 1076201 98a5f65657b048a8106ec26d7fb36fd91da17c18
23:8 275584 7acc97715e81a8f5ea25a4335e6106c4c7635261
23:9 275579 2680ea11fa7e576957569749cbb737fddf64d168
23:10 275585 4dab2ebdab8bfce9326b5f2cc3edf80ca959ac6c
23:11 275598 811034512d4bfe8811f06d5d3623ef67ebf1c9c9
23:12 415880 1bea9b93ba6ce12155962868ee40ea66538c6daf
23:13 275600 9b3eeefe71553b5386b4405a481ad014cbf7b1b1
24:1 275548 7c0a6f2e2b57df1d34fb85b98ef736b360ba7c98
24:2 1007335 003ea25a29369649fbd9766ea8ce4a3d0acb1ea8
24:3 2840594 4f5658033516eacdc0639d0ccd5e042a73948b29
24:4 275388 a65304f58a2f603c90a56731692396590d234585
24:5 801585 2775cc9f8a76d4b4c3695c9fad7bd21776ae08ae
24:6 363669 5292f1789124af80c604128333fd710fe8da36f6
24:7 275858 5edd1ab4f143b3cb2b944f1e27fadee8ffb6c219
24:8 1389167 3fbbd54651be0a049db4cd041bf1e67441726f0a
24:9 275863 1e8e6e7b045c883bd0d3629559275d5a355959ca
25:1 434797 cb0854cf46f5b9a86b4d7f1dd922b4f084835f0c
25:2 992636 3fa9f9c916a83eb3de3ceb4686dee7ffadcbfe18
25:3 389238 f69e19e1ff3dfece6cb61932b9fe25bd1d538e91
25:4 992637 fadd7f5a0de3b3d587aef3cee9550efb0e2bf1d4
25:5 992638 49175b2945c1a63078c6139d83b86d7ef07604be
25:6 440483 9e4117dcdc2c03d0cf4460a8b5e5b5f7169816a9
25:7 275937 68759ae0b61027fa55320f70776b983842c22642
25:8 275938 264eb73b985ce4e1f28e67630b0b942b8a820811
26:1 276873 4208c2c9ccd1b7e12803f5d5ab3c36666841201a
26:2 346646 61bfa08e43293412fe4d9c240dc0668de508fecd
26:3 276611 b9155dbea56a8a31dd1a04e0145b0163775a0a08
26:4 276614 54468d32c02d58d9113e6effca09ad482e1b58c0
26:5 276620 1060186dddd0ef0c380ba394641779923ab51fe9
26:6 278728 ba65a3735e770eaecfaec0a116e4344eade74b00
26:7 465770 a2bf27e8e3ce9dd6b574121419a0b62bd651eb46
26:8 276632 ad0b59ef8f101997222d82a5bbe16b69c047bd73
26:9 276634 2201382db1a1d76c890346b05b67ea8970e683b8
27:1 357598 cdad3c5da552802119301f4764581d946c836e80
27:2 384087 66107860520df046ee919d21e450a0162251c95e
27:3 350795 17dfdbc351b0ad2e97e361ce739a6f23eb90ceaa
27:4 3080230 68c1924ad231ebba4b68c9c004d0379723f1abcc
27:5 276903 b78dde9a383cb26afeacac861c35fa9781eec7db
27:6 277853 d9dda5072028373d831134e4211237ea481abcfb
27:7 707481 2a422f21a6fda6911f0104f4d9a5adc21779e6ef
27:8 2901764 6201877f38c1e4d54ce436a756a8a21d399e23e1
27:9 282039 b6a325e5837ea0f4755a026f8e68d49edba448dd
27:10 277843 ceb820330c3dbd27361f80c3af59bad1d799258c
27:11 337965 1962f5b25ec4a3bbb9cffa438e2c6e01d97081cf
27:12 277835 15e8dbd012d0e429248becd95b8fb8384cde4564
28:1 572704 b1ff5f2790e348506ac876e882cb1317e8044a6e
28:2 278062 7f23b7260ce1d11151ab1f8fdf5e0661d4df4681
28:3 539429 3d01d6e716122100f260121cf33aca908f238e66
28:4 278042 6442d01b3b5ca894a82a88cc5c7e359f00aa9d0a
28:5 278043 58314c49d5a5f822392a002387f2a591ba7cf54c
28:6 2904915 a144cc3fabe3bc2310e16fa175d7d23f01befc13
29:1 344688 8bf3b35d20535db5a744f6835f0d3112195d41b8
29:2 278176 30288d5898acfe03c95bd0adc896069f35fc2838
29:3 278175 f39ca22a42cccf7d767c25e026254106cbd9d230
29:4 278174 c0e040e529b26110fcd9a600ce21ef2b1c9eb1dc
29:5 278170 cbb304f804cde14856aac8d7971e9621e5787ace
29:6 278169 c362bd44c32df859a52e22741f704f74e4c4ac72
29:7 278545 d00c5f52b48f76572db976fec4836bd1d30a9ece
29:8 278544 534f7026b123046bf4f45bac4235b1b7dde29229
29:9 728926 2c3d60f09345bd3a5603134dc679c0e52724ddac
29:10 278542 603634e0e1d4f8f038c82333ec1f9eb85669c48a
29:11 298319 8bffc1f6541429f4853cce61cfacefc765ee4f7f
29:12 278756 9b237c9eb018d90dfe1205d242505143be6639f8
30:1 2830317 4530a451519c959719d4dbcde5086b35f177b5e9
30:2 278766 1ab0fcc734ebe22e003e47e30b9ff597a4b54f72
30:3 782821 875de5c0eee8d7f0dc4ce2019b2283f9bbdbfe7b
30:4 278810 600e03271eb620539bd1a6f1f6fe5b6a2c78bcf1
30:5 333662 d207feed8929f69f57644ca1c2052a2e308a2f85
30:6 2878333 b24c1b67ace15c318af26143bbf96323db1d67cc
30:7 278836 4e886749e730191d7a391ea9f7be95c4cdf8ab5c
30:8 278837 5b71b307d00a1e50e8aaaa1128bdc492245b0a55
30:9 923401 04ae23c51534dc5da57e032c48451ca4cd763ecd
30:10 923402 c0428cc9c83c5b4cc0003c00d3c4f36a68fbb4de
30:11 278849 2f183c00d7e6aa0ef5944d88fb764df5d8f5a0b6
30:12 278851 d7fcc5b60bd18871483d7e8ff28d563bed0814dc
30:13 278852 f7c13789a6babe2b4e0a1ed5ef300605ae599251
30:14 278853 d85fa94a172f1f6ae378b19127f3206e72c685e7
30:15 298327 dcea647853b156633304c589c7e7c6172d5f0f95
30:16 335426 ca0054aa22d7c11bebba8d2729738f0770e250d3
31:1 344706 d0cf64160ec91d1568b7333a13451b2b03717b7f
31:2 278897 0fa842bfa90cd664185f62913b8c6584505edc48
31:3 278901 6f02ed6f8e608a7e4e438903e9a725256a50e6e3
31:4 278904 81c67e4a8542690c939ec421924b6561c1a710d9
31:5 278928 0959ba80a6380f0524772d4c2ebf06249344993a
31:6 278929 1de3ca099bf810083b2524e06ba4b34cb95924e5
31:7 439103 5f46308a7109f7e536c2c9dd1ace5fb7ad30562f
31:8 278932 4ce871152b63c93fb4e966e0168a286e8d9f64b0
31:9 278933 ac708548edfac7d5382c9c155963d590f9dcabf8
31:10 278934 8cd9dc787e37a6e787983a7c5be76ebc0584361c
31:11 278935 11e6d36d8962c7af9679e1021d2584afdb8570bf
32:1 711418 8c35b5afa756903742edf491299905c1840f56b2
32:2 278941 bfa272aa4068095a4d7dd384e7ec786d51c6796f
32:3 278942 f54e2b7939242f1d53a9c222aa48b971632c9502
32:4 278943 c2e9f46f02ca4849619484c12edd911c8c0c3b16
32:5 368718 52c6afe7e6526305b78ff37d7f33f2b171f8a7d2
32:6 278945 b447302a487868e8d6874fa43a8bee3bc2bad1a7
32:7 278946 7626234187352439cfe9806ae5a5cf0199ee52a9
32:8 278947 7e2a06ed9f4ac28ffc895133c6c414c9fcccc8cf
33:1 2756746 25532b63d48a899b315482b83f375a170a29d6da
33:2 279247 afd2ea2a6a27bdaca62559fdce89c84bffd95f77
33:3 349178 ce0d20b89599d9f2bfed6bb57c268914c2666712
33:4 279292 209a652bad336df1d78426175f41847595e0ad86
33:5 279293 492a87a64ae3eb19de1884f6915dd088eae9c1f6
33:6 279294 f9d9830f104d9399d068ec5879cb9c4fdef0d91f
34:1 279296 31fbe56a364ca51f8b43ab335c10edff051c86bf
34:2 387362 96b4daeb796f3389fe615d2863015699e1652af6
34:3 279326 92d80dc78660e61173f59d634f506e830517453b
34:4 279328 c7c002a3470b09bc977c84c4f77cf1bac8c2cb91
34:5 279329 fec24eade36d1d4371532ac7880b08797da0a59e
34:6 365976 8c59349644e381034c551943015092ee4a16e660
34:7 279331 8560ec9b37be80ce1e33fd8c1e72feeb2d75eb49
34:8 279342 0decb1307aafc6cb047ecef1c2c9e4cff796fd9d
34:9 279507 3f45eacd8766aad90ea1adb75a0054455b5fbfb7
34:10 909451 5b4acca7b907e12fd408d42e3b975fbbffb1f0ea
34:11 280180 37ab73f1f0dbb1630c2fd07ca9a4be5485c6bd78
34:12 370766 387b4a995b48f53d987f502d46fa2c9378740ca6
34:13 280175 4bd57f6428175c5aa6758fe3f4d5e19d1dc29ed0
34:14 280387 7d6f7995f513ac50a83e016610bf29e7192166a9
34:15 280396 69104052fc6d9be7a04679008ff1ad17112ee53f
34:16 370769 3c92cf8e72f69fc74e8d82e3f95077017e1476a8
35:1 658608 d3460aa37cd9502d469383d64cdee4fc12fe7e74
35:2 280408 f8fb8889d408db4dc0703947c22786bcb2084226
35:3 280409 7f6fc3b672fc8c5acedb6eb22f0ab80981a2ed16
35:4 280410 52e5ae9b509d27b82179c46d6861fb5ef1757ff5
35:5 280411 00f2c0d07e2fcd7a50f76e5640a5d215444a3126
35:6 280416 1311d47c14a03d7c56d92d1044be94bbf3bc051a
35:7 280417 bd67fd59190f418ba55ec8f3bc2375c607938024
35:8 280423 4dda1a5086f12264eb023560b7c4bbb5229a2e1f
35:9 280425 1940b4d5f1ba76443efb3efa9605f7ed81354a33
35:10 280426 06f071c005e3352361b4a297ee465ffb659823d8
35:11 280427 2d764b5d30438fdcc5eb9887e3d4d30c52e7f55b
35:12 280429 9297b4c1d99c32ccc3beafbc3aa283c858bb88c7
36:1 282038 0db5c8a51ffe509e2750a3d4d53196eb31c17cfc
36:2 280439 ff4e73bc1ca548d4dc91dfc3e9064158e3d028a2
36:3 1420494 b82fd7aa22275cb2eb2014c1f3cbfa2fc43ded6e
36:4 399968 e4f228f63494430af5276ce54f392f0bf9abcd38
36:5 282042 a4f67026b156a3ff22d6834c9e5e7b8ed868a475
36:6 282030 2c530974a5de17eda9a74652e2a4774864d32bc1
37:1 281729 37962fe3ed61e96378defa19a8ff9c66d367e18d
37:2 280449 db98198670e2657d888a46c52969fc4b204e70e8
37:3 280451 9f1c0ca8194d46888cd363cae03f1dbe650520ae
37:4 353155 53ac7725fe33dcf8dfd2caa98df50e53e68262cc
"""
