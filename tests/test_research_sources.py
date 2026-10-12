"""Tests for the curated research-source catalogue, its MCP tool, and the link checker."""

import importlib.util
import re
import sqlite3
import sys
from datetime import date
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

import study_bible_mcp.server as server_module
from study_bible_mcp.tools import (
    find_research_sources,
    load_research_sources,
    research_search_url,
)

_SMOKE_PATH = Path(__file__).resolve().parent.parent / "scripts" / "smoke_server.py"


@pytest.fixture(scope="module")
def smoke():
    spec = importlib.util.spec_from_file_location("smoke_server", _SMOKE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["smoke_server"] = module
    spec.loader.exec_module(module)
    return module


def _source(key: str) -> dict:
    return next(s for s in load_research_sources() if s["key"] == key)


# ---------------------------------------------------------------------------
# Catalogue integrity — a malformed entry ships a broken link or crashes the tool
# ---------------------------------------------------------------------------

class TestCatalogue:
    def test_entry_count_in_curated_range(self):
        assert 20 <= len(load_research_sources()) <= 40

    def test_keys_are_unique(self):
        keys = [s["key"] for s in load_research_sources()]
        assert len(keys) == len(set(keys))

    @pytest.mark.parametrize("source", load_research_sources(), ids=lambda s: s["key"])
    def test_entry_has_required_fields(self, source):
        for field in ("key", "name", "url", "description", "perspective", "access"):
            assert isinstance(source.get(field), str) and source[field].strip(), field
        assert re.match(r"https?://", source["url"])
        assert isinstance(source["coverage"], list) and source["coverage"]
        assert all(isinstance(tag, str) and tag == tag.lower() for tag in source["coverage"])
        date.fromisoformat(source["last_verified"])

    @pytest.mark.parametrize("source", load_research_sources(), ids=lambda s: s["key"])
    def test_search_template_has_one_query_placeholder(self, source):
        assert "search_url_template" in source
        template = source["search_url_template"]
        if template is not None:
            assert re.match(r"https?://", template)
            assert template.count("{query}") == 1

    @pytest.mark.parametrize("source", load_research_sources(), ids=lambda s: s["key"])
    def test_link_check_skip_carries_reason(self, source):
        if "link_check" in source:
            assert source["link_check"] == "skip"
            assert source.get("link_check_reason", "").strip()


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------

class TestFiltering:
    def test_topic_matches_coverage_tag(self):
        keys = {s["key"] for s in find_research_sources(topic="rabbinic")}
        assert {"sefaria", "alhatorah", "mechon_mamre"} <= keys
        assert all("rabbinic" in _source(k)["coverage"] for k in keys)

    def test_topic_phrase_containing_a_tag_still_matches(self):
        keys = {s["key"] for s in find_research_sources(topic="rabbinic commentary")}
        assert "sefaria" in keys
        assert "tyndale_bulletin" not in keys

    def test_topic_alias(self):
        keys = {s["key"] for s in find_research_sources(topic="DSS")}
        assert {"leon_levy_dss", "orion_bibliography"} <= keys

    def test_topic_matches_source_name_first(self):
        assert find_research_sources(topic="Sefaria")[0]["key"] == "sefaria"

    def test_unknown_topic_returns_nothing(self):
        assert find_research_sources(topic="quantum chromodynamics") == []

    def test_perspective_is_case_insensitive_and_partial(self):
        reformed = find_research_sources(perspective="reformed")
        assert reformed and all(s["perspective"] == "Reformed" for s in reformed)
        academic = find_research_sources(perspective="Academic")
        assert academic and all(s["perspective"] == "academic/critical" for s in academic)

    def test_topic_and_perspective_combine(self):
        results = find_research_sources(topic="church fathers", perspective="catholic")
        assert [s["key"] for s in results] == ["new_advent_fathers"]


# ---------------------------------------------------------------------------
# Search links
# ---------------------------------------------------------------------------

class TestSearchLinks:
    def test_query_is_url_encoded(self):
        url = research_search_url(_source("sefaria"), "sons of God & Psalm 82")
        assert url == "https://www.sefaria.org/search?q=sons+of+God+%26+Psalm+82"

    def test_query_cannot_break_later_parameters(self):
        url = research_search_url(_source("jstor_early_journal_content"), "a&and[]=x #frag")
        params = parse_qs(urlsplit(url).query)
        assert params["query"] == ["a&and[]=x #frag"]
        assert params["and[]"] == ['collection:"jstor_ejc"']

    def test_source_without_template_has_no_link(self):
        assert research_search_url(_source("mechon_mamre"), "Psalm 82") is None


# ---------------------------------------------------------------------------
# Tool through server.call_tool
# ---------------------------------------------------------------------------

class TestTool:
    @pytest.fixture(autouse=True)
    def _db_present(self, monkeypatch):
        # The handler needs no database, but call_tool opens one first.
        monkeypatch.setattr(server_module, "db", object())

    async def test_returns_filtered_sources_with_search_links(self):
        response = await server_module.call_tool(
            "list_research_sources", {"topic": "rabbinic", "query": "Psalm 82"}
        )
        output = response[0].text
        assert "not endorsements" in output
        assert "### Sefaria" in output
        assert "https://www.sefaria.org/search?q=Psalm+82" in output
        assert "- **Search**: no linkable search" in output  # Mechon Mamre
        assert "Tyndale Bulletin" not in output

    async def test_limit_caps_output_and_reports_total(self):
        response = await server_module.call_tool("list_research_sources", {"limit": 3})
        output = response[0].text
        assert output.count("\n### ") == 3
        assert f"Showing 3 of {len(load_research_sources())}" in output

    async def test_no_match_lists_available_tags(self):
        response = await server_module.call_tool(
            "list_research_sources", {"topic": "quantum chromodynamics"}
        )
        output = response[0].text
        assert "No sources match" in output
        assert "dead sea scrolls" in output


# ---------------------------------------------------------------------------
# Link checker (scripts/smoke_server.py --check-links)
# ---------------------------------------------------------------------------

class TestLinkChecker:
    async def test_status_classification(self, smoke):
        statuses = {"https://ok": 200, "https://gone": 404, "https://down": 503, "https://blocked": 403}

        async def fetch(url):
            if url == "https://dns":
                raise httpx.ConnectError("Name or service not known")
            return statuses[url]

        targets = {url: [f"origin of {url}"] for url in [*statuses, "https://dns"]}
        results = {r.url: r for r in await smoke.check_links(targets, fetch, concurrency=2)}

        assert not results["https://ok"].broken
        assert all(results[u].broken for u in ("https://gone", "https://down", "https://blocked"))
        assert results["https://dns"].broken
        assert results["https://dns"].status is None
        assert "ConnectError" in results["https://dns"].error
        assert results["https://gone"].origins == ["origin of https://gone"]

    async def test_timeout_is_retried_and_persistent_timeouts_are_labelled(self, smoke):
        async def fetch(url):
            raise httpx.ReadTimeout("slow")

        async def retry(url):
            if url == "https://slow":
                return 200
            raise httpx.ReadTimeout("still slow")

        targets = {"https://slow": ["a"], "https://dead": ["b"]}
        results = {r.url: r for r in await smoke.check_links(targets, fetch, retry=retry)}

        assert not results["https://slow"].broken
        assert results["https://dead"].broken and results["https://dead"].timed_out

    async def test_get_fallback_when_head_refused(self, smoke):
        def handler(request):
            if request.url.path == "/no-head":
                return httpx.Response(405 if request.method == "HEAD" else 200)
            return httpx.Response(404)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            fetch = smoke.make_http_fetcher(client)
            assert await fetch("https://example.org/no-head") == 200
            assert await fetch("https://example.org/missing") == 404

    def test_targets_include_catalogue_templates_and_db_urls(self, smoke, tmp_path):
        db_path = tmp_path / "links.db"
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "CREATE TABLE theology_content (source_work TEXT, source_author TEXT, url TEXT)"
            )
            conn.executemany(
                "INSERT INTO theology_content VALUES (?, ?, ?)",
                [
                    ("Work A", "heiser", "https://example.org/a.pdf"),
                    ("Work B", "heiser", " https://example.org/a.pdf "),
                    ("Work C", "owen", None),
                    ("Work D", "owen", ""),
                ],
            )

        targets, skipped = smoke.collect_link_targets(db_path)

        assert targets["https://example.org/a.pdf"] == [
            "theology_content:heiser (Work A,Work B; 2 rows)"
        ]
        assert "https://www.sefaria.org/search?q=Psalm+82" in targets
        skipped_keys = {s["key"] for s in load_research_sources() if s.get("link_check") == "skip"}
        assert len(skipped) == len(skipped_keys)
        for key in skipped_keys:
            assert not any(f"research_sources:{key} " in o for os in targets.values() for o in os)
