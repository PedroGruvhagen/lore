"""Tests for `lore.py lint` (skill/scripts/lore.py:cmd_lint and its
15 numbered checks)."""
from __future__ import annotations

import os
import unittest
from pathlib import Path

from tests.helpers import bootstrap_tmp, run_lore, write_page


class LintTests(unittest.TestCase):
    def setUp(self):
        self.tmp = bootstrap_tmp()
        self.lore_dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_lint_always_exits_zero_even_with_errors(self):
        """cmd_lint contains no sys.exit() call anywhere in its body, so the
        process exit code is 0 regardless of how many ERROR-severity issues
        (including credential leaks) are found."""
        write_page("broken", {}, "This page is missing every required field.\n")
        result = run_lore("lint", "--lore-dir", self.lore_dir)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("[ERROR]", result.stdout)
        self.assertIn("errors,", result.stdout)

    def test_missing_field_is_an_error(self):
        write_page("broken", {"title": "Broken"}, "Missing category/last_verified/sources.\n")
        result = run_lore("lint", "--lore-dir", self.lore_dir)
        self.assertIn("[missing-field] pages/broken.md: missing 'category'", result.stdout)
        self.assertIn("[missing-field] pages/broken.md: missing 'last_verified'", result.stdout)
        self.assertIn("[missing-field] pages/broken.md: missing 'sources'", result.stdout)

    def test_oversized_page_is_a_warning(self):
        big_body = "\n".join(f"Line {i} of filler content." for i in range(250))
        write_page(
            "huge",
            {"title": "Huge", "category": "custom", "last_verified": "2026-08-01", "sources": []},
            big_body + "\n",
        )
        result = run_lore("lint", "--lore-dir", self.lore_dir)
        self.assertIn("[oversized] pages/huge.md:", result.stdout)

    def test_credential_leak_is_an_error(self):
        leaked_key = "sk-" + "a" * 48
        write_page(
            "leaky",
            {"title": "Leaky", "category": "custom", "last_verified": "2026-08-01", "sources": []},
            f"Do not commit this: {leaked_key}\n",
        )
        result = run_lore("lint", "--lore-dir", self.lore_dir)
        self.assertIn("[credential-leak]", result.stdout)
        self.assertNotIn("0 errors,", result.stdout)

    def test_broken_crossref_is_a_warning(self):
        write_page(
            "referrer",
            {"title": "Referrer", "category": "custom", "last_verified": "2026-08-01", "sources": []},
            "See [[does-not-exist]] for detail.\n",
        )
        result = run_lore("lint", "--lore-dir", self.lore_dir)
        self.assertIn("[broken-ref] pages/referrer.md: [[does-not-exist]] points to non-existent page", result.stdout)

    def test_scoped_crossref_is_not_flagged_broken(self):
        write_page(
            "referrer2",
            {"title": "Referrer2", "category": "custom", "last_verified": "2026-08-01", "sources": []},
            "See [[global:something-elsewhere]] for detail.\n",
        )
        result = run_lore("lint", "--lore-dir", self.lore_dir)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("global:something-elsewhere", result.stdout)

    def test_two_cell_rows_never_compared_three_plus_cell_rows_are(self):
        """C3 fix (lore.py:_check_contradictions): a 2-cell table row is a
        label/value pair, not a fact with its own attributes, so it is
        skipped outright even when two pages disagree on the same label.
        A 3+-cell row under an identically-shaped table header is still
        compared and still flags a real contradiction."""
        write_page(
            "label-a",
            {"title": "Label A", "category": "custom", "last_verified": "2026-08-01", "sources": []},
            "| Field | Value |\n|---|---|\n| Status | active |\n",
        )
        write_page(
            "label-b",
            {"title": "Label B", "category": "custom", "last_verified": "2026-08-01", "sources": []},
            "| Field | Value |\n|---|---|\n| Status | inactive |\n",
        )
        write_page(
            "server-a",
            {"title": "Server A", "category": "custom", "last_verified": "2026-08-01", "sources": []},
            "| Host | Port | Role |\n|---|---|---|\n| webserver | 8080 | primary |\n",
        )
        write_page(
            "server-b",
            {"title": "Server B", "category": "custom", "last_verified": "2026-08-01", "sources": []},
            "| Host | Port | Role |\n|---|---|---|\n| webserver | 9090 | primary |\n",
        )
        result = run_lore("lint", "--lore-dir", self.lore_dir)
        self.assertEqual(result.returncode, 0, result.stderr)
        contradiction_lines = [ln for ln in result.stdout.splitlines() if "[contradiction]" in ln]
        self.assertEqual(len(contradiction_lines), 1, result.stdout)
        self.assertIn("webserver", contradiction_lines[0])
        self.assertNotIn("status", contradiction_lines[0].lower())

    def test_stale_failure_skips_seen_records(self):
        """C4 fix: Check 14 only flags failed/*.err records with no adjacent
        {name}.seen marker; a record `lore.py seen` has already reviewed is
        excluded even when it is older than the 24h threshold."""
        failed_dir = Path(self.lore_dir) / "failed"
        failed_dir.mkdir(parents=True, exist_ok=True)
        old_ts = 0  # 1970-01-01, unambiguously older than 24h

        unseen = failed_dir / "widget-refresh.err"
        unseen.write_text("connection reset\n", encoding="utf-8")
        os.utime(unseen, (old_ts, old_ts))

        seen = failed_dir / "gadget-refresh.err"
        seen.write_text("connection reset\n", encoding="utf-8")
        os.utime(seen, (old_ts, old_ts))
        (failed_dir / "gadget-refresh.err.seen").write_text("seen: 2026-08-01T00:00:00Z\n", encoding="utf-8")

        result = run_lore("lint", "--lore-dir", self.lore_dir)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("[stale-failures] failed/widget-refresh.err", result.stdout)
        self.assertNotIn("gadget-refresh.err", result.stdout)

    def test_clean_page_reports_zero_errors(self):
        # required_fields = ["title", "category", "last_verified", "sources"]
        # and Check 1 treats an empty list as "missing" (`not meta[field]`),
        # so sources needs at least one entry to satisfy the check; index
        # the page too so the orphan-page [WARN] doesn't fire either.
        write_page(
            "clean",
            {
                "title": "Clean Page",
                "category": "custom",
                "last_verified": "2026-08-01",
                "sources": ["https://example.com/clean"],
            },
            "Nothing wrong here.\n",
        )
        run_lore("index", "--lore-dir", self.lore_dir)
        result = run_lore("lint", "--lore-dir", self.lore_dir)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("0 errors", result.stdout)

    def test_needs_review_flag_is_a_warning(self):
        write_page(
            "flagged",
            {
                "title": "Flagged",
                "category": "custom",
                "last_verified": "2026-08-01",
                "sources": [],
                "needs_review": True,
            },
            "Body.\n",
        )
        result = run_lore("lint", "--lore-dir", self.lore_dir)
        self.assertIn("[needs-review] pages/flagged.md: needs_review is set", result.stdout)

    def test_summary_line_format(self):
        write_page(
            "clean2",
            {
                "title": "Clean Page 2",
                "category": "custom",
                "last_verified": "2026-08-01",
                "sources": [],
            },
            "Body.\n",
        )
        result = run_lore("lint", "--lore-dir", self.lore_dir)
        self.assertRegex(result.stdout, r"Lint complete\. \d+ errors, \d+ warnings, \d+ info\.")


if __name__ == "__main__":
    unittest.main()
