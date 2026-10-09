"""Tests for `lore.py index` and `lore.py search` (skill/scripts/lore.py:
cmd_index, cmd_search, _search_fts5, _search_substring)."""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from tests.helpers import bootstrap_tmp, run_lore, write_page


class IndexAndSearchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = bootstrap_tmp()
        self.lore_dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_index_rebuilds_index_md_and_search_db(self):
        write_page(
            "widget-api",
            {
                "title": "Widget API",
                "category": "api-reference",
                "confidence": "high",
                "last_verified": "2026-08-01",
                "sources": ["https://example.com/widget"],
            },
            "The Widget API accepts a `widget_id` and returns JSON.\n",
        )
        result = run_lore("index", "--lore-dir", self.lore_dir)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("1 pages indexed", result.stdout)
        self.assertIn("Marked 0 pages as needs_review", result.stdout)

        index_text = (Path(self.lore_dir) / "index.md").read_text()
        self.assertIn("widget-api", index_text)
        self.assertIn("api-reference", index_text)

        self.assertTrue((Path(self.lore_dir) / ".search.db").is_file())

        graph = json.loads((Path(self.lore_dir) / ".graph.json").read_text())
        self.assertEqual(graph["nodes"], ["widget-api"])

    def test_index_never_flags_needs_review(self):
        """cmd_index must never set needs_review on its own; only
        cmd_refresh does, gated on the current run's own diffs
        (skill/scripts/lore.py: `Marked 0 pages as needs_review` is the
        fixed literal cmd_index always prints)."""
        write_page(
            "dependent-page",
            {
                "title": "Dependent Page",
                "category": "tooling",
                "last_verified": "2020-01-01",
                "sources": [],
            },
            "References [[widget-api]] for details.\n",
        )
        write_page(
            "widget-api",
            {
                "title": "Widget API",
                "category": "api-reference",
                "last_verified": "2020-01-01",
                "sources": [],
            },
            "Base page.\n",
        )
        result = run_lore("index", "--lore-dir", self.lore_dir)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Marked 0 pages as needs_review", result.stdout)

        dependent_text = (Path(self.lore_dir) / "pages" / "dependent-page.md").read_text()
        self.assertNotIn("needs_review: true", dependent_text)

    def test_search_finds_indexed_page_by_keyword(self):
        write_page(
            "acme-cli",
            {
                "title": "Acme CLI",
                "category": "tooling",
                "last_verified": "2026-08-01",
                "sources": [],
            },
            "The acme-cli tool ships a `deploy` subcommand for production rollouts.\n",
        )
        result = run_lore("index", "--lore-dir", self.lore_dir)
        self.assertEqual(result.returncode, 0, result.stderr)

        search_result = run_lore("search", "deploy", "--lore-dir", self.lore_dir)
        self.assertEqual(search_result.returncode, 0, search_result.stderr)
        self.assertIn("acme-cli", search_result.stdout)

    def test_search_with_hyphenated_query_does_not_crash(self):
        """FTS5 query syntax treats bare hyphens specially; _quote_fts5_tokens
        must keep a hyphenated search term from raising (C1 fix)."""
        write_page(
            "acme-cli",
            {
                "title": "Acme CLI",
                "category": "tooling",
                "last_verified": "2026-08-01",
                "sources": [],
            },
            "acme-cli documentation.\n",
        )
        run_lore("index", "--lore-dir", self.lore_dir)
        result = run_lore("search", "acme-cli", "--lore-dir", self.lore_dir)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("acme-cli", result.stdout)

    def test_search_with_c1_specific_failing_inputs_does_not_crash(self):
        """Three query shapes the audit found raising OperationalError /
        fts5 syntax errors before the C1 fix (task file: lore.py:531-548
        _search_fts5 passed the raw query straight into MATCH ?): a
        hyphenated two-word slug (e.g. 'widget-bot' -> "no such column: bot"),
        a hyphenated three-word slug ('claude-code-headless' -> "no such
        column: code"), and a bare symbol query ('C++' -> fts5 syntax error
        near '+'). None of these may raise, crash the process, or fall
        through to the ranking-degraded substring fallback silently changing
        the top result (S4). Reverting the C1 fix (lore.py:711, passing raw
        `query` instead of `_quote_fts5_tokens(query)`) makes _search_fts5
        raise sqlite3.OperationalError, caught internally and returned as
        None (lore.py:717-718), so cmd_search falls straight to
        _search_substring (lore.py:668-674) without ever raising. Asserting
        only "the slug string appears somewhere in stdout" would still pass
        in that reverted state for two different reasons, so each case here
        needs a stronger, per-case check (verified by actually reverting
        lore.py:711 locally and re-running this test, which fails as
        intended in all three subTests):
          - 'widget-bot' / 'claude-code-headless': neither hyphenated query
            string appears literally in the page text (title/body use a
            space, not a hyphen), so the substring fallback finds nothing
            and prints "No pages matching '<query>'" -- which itself
            contains the query text, and the query here equals
            expected_slug, so a bare `assertIn(expected_slug, stdout)`
            would wrongly pass on that failure message. Ruled out below by
            asserting "No pages matching" is absent.
          - 'C++': the page body literally contains "C++" (case-
            insensitively), so the substring fallback finds a real hit
            here even with the fix reverted, printing the same
            '(pages/cpp-notes.md)' line the real fix produces -- the
            "No pages matching" check above is not enough to distinguish
            them for this one case. _search_fts5's snippet() call always
            wraps a genuine FTS5 match in '>>>'/'<<<' (lore.py:702, column
            5 = body/content); _search_substring's plain '...'-only
            snippet (lore.py:721-732) never produces those markers, so
            requiring '>>>' in stdout for this case specifically proves
            the FTS5 path actually ran. The same '>>>' check is required
            below for 'widget-bot' too, since its body ("Widget bot
            assistant runtime notes.") also contains the matched phrase
            and FTS5 highlights it there; it is waived only for
            'claude-code-headless', whose body has no literal "code" for
            the body-column snippet() call to highlight even though the
            title column is what actually matched (confirmed by running
            `lore.py search` directly against this fixture: 'widget-bot'
            and 'C++' both print '>>>...<<<' in their snippet line,
            'claude-code-headless' does not)."""
        write_page(
            "widget-bot",
            {"title": "Widget Bot", "category": "tooling", "last_verified": "2026-08-01", "sources": []},
            "Widget bot assistant runtime notes.\n",
        )
        write_page(
            "claude-code-headless",
            {"title": "Claude Code Headless", "category": "tooling", "last_verified": "2026-08-01", "sources": []},
            "Running claude -p in headless mode for autonomous workers.\n",
        )
        write_page(
            "cpp-notes",
            {"title": "C++ Notes", "category": "tooling", "last_verified": "2026-08-01", "sources": []},
            "Notes on C++ build tooling.\n",
        )
        run_lore("index", "--lore-dir", self.lore_dir)

        for query, expected_slug, require_fts5_snippet_marker in (
            ("widget-bot", "widget-bot", True),
            ("claude-code-headless", "claude-code-headless", False),
            ("C++", "cpp-notes", True),
        ):
            with self.subTest(query=query):
                result = run_lore("search", query, "--lore-dir", self.lore_dir)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotIn("No pages matching", result.stdout, result.stdout)
                self.assertIn(f"(pages/{expected_slug}.md)", result.stdout)
                if require_fts5_snippet_marker:
                    self.assertIn(">>>", result.stdout, result.stdout)

    def test_search_respects_limit(self):
        for i in range(5):
            write_page(
                f"page-{i}",
                {
                    "title": f"Page {i}",
                    "category": "custom",
                    "last_verified": "2026-08-01",
                    "sources": [],
                },
                "shared-keyword appears in every page for this test.\n",
            )
        run_lore("index", "--lore-dir", self.lore_dir)
        result = run_lore("search", "shared-keyword", "--lore-dir", self.lore_dir, "--limit", "2")
        self.assertEqual(result.returncode, 0, result.stderr)
        found = sum(1 for i in range(5) if f"page-{i}" in result.stdout)
        self.assertEqual(found, 2, result.stdout)


if __name__ == "__main__":
    unittest.main()
