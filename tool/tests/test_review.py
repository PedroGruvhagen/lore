"""Tests for `lore.py review list` / `lore.py review clear`
(skill/scripts/lore.py:cmd_review, _clear_propagated_needs_review)."""
from __future__ import annotations

import unittest
from pathlib import Path

from tests.helpers import bootstrap_tmp, run_lore, write_page


class ReviewListTests(unittest.TestCase):
    def setUp(self):
        self.tmp = bootstrap_tmp()
        self.lore_dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_list_with_no_flagged_pages(self):
        result = run_lore("review", "list", "--lore-dir", self.lore_dir)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("No pages flagged needs_review.", result.stdout)

    def test_list_with_flagged_page(self):
        write_page(
            "flagged-one",
            {
                "title": "Flagged One",
                "category": "custom",
                "last_verified": "2026-08-01",
                "sources": [],
                "needs_review": True,
            },
            "Body.\n",
        )
        result = run_lore("review", "list", "--lore-dir", self.lore_dir)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("flagged-one", result.stdout)
        self.assertIn("Flagged One", result.stdout)
        self.assertIn("1 page(s) need review.", result.stdout)


class ReviewClearSlugTests(unittest.TestCase):
    def setUp(self):
        self.tmp = bootstrap_tmp()
        self.lore_dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_clear_slug_removes_flag(self):
        write_page(
            "flagged-two",
            {
                "title": "Flagged Two",
                "category": "custom",
                "last_verified": "2026-08-01",
                "sources": [],
                "needs_review": True,
            },
            "Body.\n",
        )
        result = run_lore("review", "clear", "flagged-two", "--lore-dir", self.lore_dir)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Cleared needs_review on 'flagged-two'.", result.stdout)

        page_text = (Path(self.lore_dir) / "pages" / "flagged-two.md").read_text()
        self.assertIn("needs_review: false", page_text)

    def test_clear_invalid_slug_exits_2(self):
        """C7 fix: valid_slug() rejects a slug containing '/' before any
        page path is ever built from it."""
        result = run_lore("review", "clear", "../etc-passwd", "--lore-dir", self.lore_dir)
        self.assertEqual(result.returncode, 2)
        self.assertIn("ERROR: Invalid slug '../etc-passwd'", result.stderr)

    def test_clear_no_slug_no_propagated_errors(self):
        result = run_lore("review", "clear", "--lore-dir", self.lore_dir)
        self.assertEqual(result.returncode, 1)
        self.assertIn("review clear requires a slug argument, or use --propagated", result.stderr)

    def test_clear_nonexistent_slug_errors(self):
        result = run_lore("review", "clear", "no-such-page", "--lore-dir", self.lore_dir)
        self.assertEqual(result.returncode, 1)
        self.assertIn("ERROR: No page found with slug 'no-such-page'", result.stderr)


class ReviewClearPropagatedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = bootstrap_tmp()
        self.lore_dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_propagated_clear_eligible_page(self):
        """A page with needs_review true, auto_update not set, no queue diff,
        and no unresolved failed/ record is eligible to have the flag
        cleared by --propagated."""
        write_page(
            "propagated-clean",
            {
                "title": "Propagated Clean",
                "category": "custom",
                "last_verified": "2026-08-01",
                "sources": [],
                "needs_review": True,
            },
            "Body.\n",
        )
        result = run_lore("review", "clear", "--propagated", "--lore-dir", self.lore_dir)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Cleared needs_review on 1 propagated-only page(s).", result.stdout)

        page_text = (Path(self.lore_dir) / "pages" / "propagated-clean.md").read_text()
        self.assertIn("needs_review: false", page_text)

    def test_propagated_clear_skips_auto_update_page(self):
        write_page(
            "propagated-auto",
            {
                "title": "Propagated Auto",
                "category": "custom",
                "last_verified": "2026-08-01",
                "sources": [],
                "needs_review": True,
                "auto_update": True,
            },
            "Body.\n",
        )
        result = run_lore("review", "clear", "--propagated", "--lore-dir", self.lore_dir)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Cleared needs_review on 0 propagated-only page(s).", result.stdout)

        page_text = (Path(self.lore_dir) / "pages" / "propagated-auto.md").read_text()
        self.assertIn("needs_review: true", page_text)

    def test_propagated_clear_skips_page_with_pending_queue_diff(self):
        write_page(
            "propagated-queued",
            {
                "title": "Propagated Queued",
                "category": "custom",
                "last_verified": "2026-08-01",
                "sources": [],
                "needs_review": True,
            },
            "Body.\n",
        )
        queue_dir = Path(self.lore_dir) / "queue"
        queue_dir.mkdir(exist_ok=True)
        (queue_dir / "propagated-queued-20260801.diff.md").write_text("pending diff\n", encoding="utf-8")

        result = run_lore("review", "clear", "--propagated", "--lore-dir", self.lore_dir)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Cleared needs_review on 0 propagated-only page(s).", result.stdout)

        page_text = (Path(self.lore_dir) / "pages" / "propagated-queued.md").read_text()
        self.assertIn("needs_review: true", page_text)

    def test_propagated_clear_dry_run_does_not_write(self):
        write_page(
            "propagated-dryrun",
            {
                "title": "Propagated Dryrun",
                "category": "custom",
                "last_verified": "2026-08-01",
                "sources": [],
                "needs_review": True,
            },
            "Body.\n",
        )
        result = run_lore("review", "clear", "--propagated", "--dry-run", "--lore-dir", self.lore_dir)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Would clear needs_review on 1 propagated-only page(s).", result.stdout)

        page_text = (Path(self.lore_dir) / "pages" / "propagated-dryrun.md").read_text()
        self.assertIn("needs_review: true", page_text)


if __name__ == "__main__":
    unittest.main()
