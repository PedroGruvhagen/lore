"""Regression tests for the body separator dropped by set_frontmatter_field
(skill/scripts/lore.py:set_frontmatter_field, FRONTMATTER_RE at line 131).

FRONTMATTER_RE's trailing `\\s*\\n` also consumes any blank line(s) between
the frontmatter's closing '---' and the body, so every write through
set_frontmatter_field silently dropped them. These tests drive the fix
through the shipped CLI's `review clear <slug>` (skill/scripts/lore.py:2310),
the simplest caller, per the tests/helpers.py convention of exercising the
CLI as a subprocess rather than importing lore.py's functions directly.
"""
from __future__ import annotations

import unittest
from pathlib import Path

from tests.helpers import bootstrap_tmp, run_lore, write_page


def _body_after_frontmatter(text: str) -> str:
    """Test-only helper: return every byte after the closing '---' line.

    Locates the closing delimiter by a literal '\\n---\\n' substring search
    (not FRONTMATTER_RE's greedy `\\s*` pattern), so this helper does not
    share the bug under test and can be trusted as an independent oracle.
    """
    assert text.startswith("---\n"), text[:40]
    close_at = text.index("\n---\n", 4)
    return text[close_at + len("\n---\n"):]


class SetFrontmatterFieldBodySeparatorTests(unittest.TestCase):
    """Cases (a)-(d), (f): all driven through `review clear <slug>`, which
    calls set_frontmatter_field(page_path, "needs_review", False)."""

    def setUp(self):
        self.tmp = bootstrap_tmp()
        self.lore_dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def _page_text(self, slug: str) -> str:
        return (Path(self.lore_dir) / "pages" / f"{slug}.md").read_text(encoding="utf-8")

    def test_one_blank_line_after_frontmatter_is_preserved(self):
        """Case (a): the common case, one blank line separating frontmatter
        from body."""
        body = "\nActual body text.\nSecond line.\n"
        write_page(
            "blank-one",
            {
                "title": "Blank One",
                "category": "custom",
                "last_verified": "2026-08-01",
                "sources": [],
                "needs_review": True,
            },
            body,
        )
        before = self._page_text("blank-one")
        self.assertEqual(_body_after_frontmatter(before), body)

        result = run_lore("review", "clear", "blank-one", "--lore-dir", self.lore_dir)
        self.assertEqual(result.returncode, 0, result.stderr)

        after = self._page_text("blank-one")
        self.assertEqual(_body_after_frontmatter(after), body)
        self.assertIn("needs_review: false", after)

    def test_two_blank_lines_after_frontmatter_are_preserved(self):
        """Case (b): two blank lines."""
        body = "\n\nActual body text.\n"
        write_page(
            "blank-two",
            {
                "title": "Blank Two",
                "category": "custom",
                "last_verified": "2026-08-01",
                "sources": [],
                "needs_review": True,
            },
            body,
        )
        result = run_lore("review", "clear", "blank-two", "--lore-dir", self.lore_dir)
        self.assertEqual(result.returncode, 0, result.stderr)

        after = self._page_text("blank-two")
        self.assertEqual(_body_after_frontmatter(after), body)
        self.assertIn("needs_review: false", after)

    def test_no_blank_line_after_frontmatter_is_preserved(self):
        """Case (c): body starts immediately after the closing '---' line,
        nothing to preserve; must stay exactly as before the fix."""
        body = "Actual body text.\n"
        write_page(
            "blank-zero",
            {
                "title": "Blank Zero",
                "category": "custom",
                "last_verified": "2026-08-01",
                "sources": [],
                "needs_review": True,
            },
            body,
        )
        result = run_lore("review", "clear", "blank-zero", "--lore-dir", self.lore_dir)
        self.assertEqual(result.returncode, 0, result.stderr)

        after = self._page_text("blank-zero")
        self.assertEqual(_body_after_frontmatter(after), body)
        self.assertIn("needs_review: false", after)

    def test_adding_a_field_that_did_not_exist_preserves_body(self):
        """Case (d): the page has no needs_review field at all, so
        `review clear` appends it (set_frontmatter_field's else branch,
        skill/scripts/lore.py:1875-1876) instead of substituting an
        existing line."""
        body = "\nActual body text.\n"
        write_page(
            "no-needs-review-field",
            {
                "title": "No Needs Review Field",
                "category": "custom",
                "last_verified": "2026-08-01",
                "sources": [],
            },
            body,
        )
        before = self._page_text("no-needs-review-field")
        self.assertNotIn("needs_review", before)

        result = run_lore("review", "clear", "no-needs-review-field", "--lore-dir", self.lore_dir)
        self.assertEqual(result.returncode, 0, result.stderr)

        after = self._page_text("no-needs-review-field")
        self.assertEqual(_body_after_frontmatter(after), body)
        self.assertIn("needs_review: false", after)

    def test_end_to_end_cli_preserves_bytes_after_frontmatter(self):
        """Case (f): explicit end-to-end assertion that `review clear`
        through the CLI leaves every byte after the frontmatter untouched,
        not just a 'contains' check."""
        body = "\nSome unrelated body content.\nWith multiple lines.\n"
        write_page(
            "end-to-end",
            {
                "title": "End To End",
                "category": "custom",
                "last_verified": "2026-08-01",
                "sources": [],
                "needs_review": True,
            },
            body,
        )
        result = run_lore("review", "clear", "end-to-end", "--lore-dir", self.lore_dir)
        self.assertEqual(result.returncode, 0, result.stderr)

        after = self._page_text("end-to-end")
        self.assertEqual(_body_after_frontmatter(after), body)


class SetFrontmatterFieldNoFrontmatterTests(unittest.TestCase):
    """Case (e): a page with no frontmatter block at all. The no-frontmatter
    branch of set_frontmatter_field (skill/scripts/lore.py:1849-1857)
    prepends a minimal block and must leave the original text untouched;
    this behaviour is unaffected by the fix and stays covered here."""

    def setUp(self):
        self.tmp = bootstrap_tmp()
        self.lore_dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_no_frontmatter_page_body_untouched(self):
        original = "Just a plain page.\nNo frontmatter block at all.\n"
        page_path = Path(self.lore_dir) / "pages" / "plain-page.md"
        page_path.write_text(original, encoding="utf-8")

        result = run_lore("review", "clear", "plain-page", "--lore-dir", self.lore_dir)
        self.assertEqual(result.returncode, 0, result.stderr)

        after = page_path.read_text(encoding="utf-8")
        self.assertTrue(after.endswith(original))
        self.assertTrue(after.startswith("---\nneeds_review: false\n---\n"))


if __name__ == "__main__":
    unittest.main()
