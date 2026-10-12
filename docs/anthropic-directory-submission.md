# Anthropic Connectors Directory — Submission Draft

## Server Name
Study Bible

## Tagline (max 55 characters)
Greek/Hebrew lexicons & scholarly tools for Bible study

## Description (50-100 words)
Study Bible gives Claude access to read-only scholarly Bible study tools: full Greek (LSJ, Abbott-Smith) and Hebrew (BDB) lexicons, morphologically-tagged biblical texts, cross-references, Theographic genealogy graphs, Aquifer study notes, a Tyndale Bible Dictionary, key theological terms, Ancient Near East cultural context, theological scholarship, Torah structural analysis, textual variants, and vector-based passage similarity search. Queries run against a pre-built SQLite database. No API keys, signup, or data collection.

## Categories
- Education
- Research

## Use Case Examples

### 1. Original Language Word Study
**Prompt**: "What does the Greek word for 'love' (agape) mean? How is it different from other Greek words for love?"
**Tools used**: `word_study`, `search_lexicon`, `search_by_strongs`
**Value**: Returns verified lexical data from LSJ and Abbott-Smith with Strong's numbers, NT occurrence counts, and example passages — not hallucinated definitions.

### 2. Passage Study with Commentary
**Prompt**: "Help me understand Romans 8:28 — what does it mean in the original Greek and what do the scholars say?"
**Tools used**: `lookup_verse`, `get_study_notes`, `word_study`, `get_cross_references`
**Value**: Combines the Greek text with word-by-word analysis, three sources of peer-reviewed commentary, and related passages.

### 3. Genealogy and Historical Connections
**Prompt**: "How are Ruth and Jesus related? Show me the family tree."
**Tools used**: `find_connection`, `explore_genealogy`, `lookup_name`
**Value**: Traces the multi-generational path from Ruth through Obed, Jesse, and David to Jesus using Theographic graph data, with a Mermaid diagram.

### 4. Cultural Background for Interpretation
**Prompt**: "What's the Ancient Near East background to the covenant ceremony in Genesis 15?"
**Tools used**: `get_ane_context`, `lookup_verse`, `get_bible_dictionary`
**Value**: Surfaces ANE suzerainty treaty forms, self-maledictory oath customs, and parallels from Hittite texts that illuminate the passage's meaning.

## Technical Details

- **Transport**: Streamable HTTP (`/mcp`) + SSE (`/sse`) + stdio
- **Authentication**: None required (public, read-only)
- **Rate limiting**: 100 requests/minute per IP
- **Tools**: All tools are annotated with `readOnlyHint: true`
- **Sponsored content**: None
- **Data collection**: None — see [Privacy Policy](https://studybible-mcp.fly.dev/privacy)

## URLs

- **Server endpoint**: `https://studybible-mcp.fly.dev/mcp`
- **SSE endpoint**: `https://studybible-mcp.fly.dev/sse`
- **GitHub**: `https://github.com/djayatillake/studybible-mcp`
- **PyPI**: `https://pypi.org/project/studybible-mcp/`
- **Privacy policy**: `https://studybible-mcp.fly.dev/privacy`
- **Support**: `https://github.com/djayatillake/studybible-mcp/issues`
- **Icon**: `https://studybible-mcp.fly.dev/static/icon.png`
