"""Tests for `lore.py prune` (skill/scripts/lore.py:cmd_prune)."""
from __future__ import annotations

import hashlib
import json
import os
import time
import unittest
from pathlib import Path

from tests.helpers import bootstrap_tmp, run_lore, write_page


def _touch_with_mtime(path: Path, age_days: float) -> None:
    path.write_text("stale content\n", encoding="utf-8")
    old = time.time() - age_days * 86400
    os.utime(path, (old, old))


class PruneTests(unittest.TestCase):
    def setUp(self):
        self.tmp = bootstrap_tmp()
        self.lore_dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_no_flags_errors(self):
        result = run_lore("prune", "--lore-dir", self.lore_dir)
        self.assertEqual(result.returncode, 1)
        self.assertIn(
            "Provide at least one of --failed-older-than, --orphan-hashes, --run-logs-older-than.",
            result.stderr,
        )

    def test_failed_older_than_deletes_old_records_only(self):
        failed_dir = Path(self.lore_dir) / "failed"
        failed_dir.mkdir(exist_ok=True)
        old_err = failed_dir / "old-source-20260101.err"
        new_err = failed_dir / "new-source-20260801.err"
        _touch_with_mtime(old_err, age_days=40)
        _touch_with_mtime(new_err, age_days=1)
        (failed_dir / "old-source-20260101.err.seen").write_text("seen: x\n", encoding="utf-8")

        result = run_lore("prune", "--failed-older-than", "30", "--lore-dir", self.lore_dir)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Pruned 1 failed/ record(s) older than 30d.", result.stdout)
        self.assertFalse(old_err.exists())
        self.assertFalse((failed_dir / "old-source-20260101.err.seen").exists())
        self.assertTrue(new_err.exists())

    def test_failed_older_than_dry_run_does_not_delete(self):
        failed_dir = Path(self.lore_dir) / "failed"
        failed_dir.mkdir(exist_ok=True)
        old_err = failed_dir / "old-source-20260101.err"
        _touch_with_mtime(old_err, age_days=40)

        result = run_lore("prune", "--failed-older-than", "30", "--dry-run", "--lore-dir", self.lore_dir)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("[DRY-RUN] Pruned 1 failed/ record(s) older than 30d.", result.stdout)
        self.assertTrue(old_err.exists())

    def test_orphan_hashes_drops_unreferenced_urls(self):
        write_page(
            "referencing-page",
            {
                "title": "Referencing Page",
                "category": "custom",
                "last_verified": "2026-08-01",
                "sources": ["https://example.com/kept"],
            },
            "Body.\n",
        )
        hashes_path = Path(self.lore_dir) / ".hashes.json"
        hashes_path.write_text(
            json.dumps(
                {
                    "https://example.com/kept": {"sha256": "a" * 64, "last_fetched": "2026-08-01T00:00:00Z"},
                    "https://example.com/orphaned": {"sha256": "b" * 64, "last_fetched": "2026-01-01T00:00:00Z"},
                }
            ),
            encoding="utf-8",
        )

        result = run_lore("prune", "--orphan-hashes", "--lore-dir", self.lore_dir)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Orphan hash entry: https://example.com/orphaned", result.stdout)
        self.assertIn("Pruned 1 orphan .hashes.json entry.", result.stdout)

        remaining = json.loads(hashes_path.read_text())
        self.assertIn("https://example.com/kept", remaining)
        self.assertNotIn("https://example.com/orphaned", remaining)

    def test_orphan_hashes_dry_run_does_not_write(self):
        hashes_path = Path(self.lore_dir) / ".hashes.json"
        hashes_path.write_text(
            json.dumps({"https://example.com/orphaned": {"sha256": "c" * 64}}), encoding="utf-8"
        )
        result = run_lore("prune", "--orphan-hashes", "--dry-run", "--lore-dir", self.lore_dir)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("[DRY-RUN] Pruned 1 orphan .hashes.json entry.", result.stdout)
        remaining = json.loads(hashes_path.read_text())
        self.assertIn("https://example.com/orphaned", remaining)

    def test_g_orphan_hashes_also_drops_unreferenced_refresh_cache_files(self):
        # C19: .refresh-cache/ files are keyed by sha256(url), independent of
        # .hashes.json membership (skill/scripts/lore.py:cmd_prune's
        # --orphan-hashes branch, second sweep).
        write_page(
            "referencing-page",
            {
                "title": "Referencing Page",
                "category": "custom",
                "last_verified": "2026-08-01",
                "sources": ["https://example.com/kept"],
            },
            "Body.\n",
        )
        cache_dir = Path(self.lore_dir) / ".refresh-cache"
        cache_dir.mkdir(exist_ok=True)
        kept_name = hashlib.sha256(b"https://example.com/kept").hexdigest() + ".txt"
        orphaned_name = hashlib.sha256(b"https://example.com/orphaned").hexdigest() + ".txt"
        (cache_dir / kept_name).write_text("kept payload\n", encoding="utf-8")
        (cache_dir / orphaned_name).write_text("orphaned payload\n", encoding="utf-8")

        result = run_lore("prune", "--orphan-hashes", "--lore-dir", self.lore_dir)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"Orphan cache file: {orphaned_name}", result.stdout)
        self.assertNotIn(kept_name, result.stdout)
        self.assertIn("Pruned 1 orphan .refresh-cache/ file.", result.stdout)

        self.assertTrue((cache_dir / kept_name).is_file())
        self.assertFalse((cache_dir / orphaned_name).is_file())

    def test_run_logs_older_than_deletes_old_logs_only(self):
        gardener_dir = Path(self.lore_dir) / ".gardener"
        gardener_dir.mkdir(exist_ok=True)
        old_log = gardener_dir / "run-2026-01-01.jsonl"
        new_log = gardener_dir / "run-2026-08-01.jsonl"
        _touch_with_mtime(old_log, age_days=40)
        _touch_with_mtime(new_log, age_days=1)

        result = run_lore("prune", "--run-logs-older-than", "30", "--lore-dir", self.lore_dir)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Pruned 1 gardener run log(s) older than 30d.", result.stdout)
        self.assertFalse(old_log.exists())
        self.assertTrue(new_log.exists())

    def test_multiple_flags_combine_independently(self):
        failed_dir = Path(self.lore_dir) / "failed"
        failed_dir.mkdir(exist_ok=True)
        _touch_with_mtime(failed_dir / "old-20260101.err", age_days=40)

        gardener_dir = Path(self.lore_dir) / ".gardener"
        gardener_dir.mkdir(exist_ok=True)
        _touch_with_mtime(gardener_dir / "run-2026-01-01.jsonl", age_days=40)

        result = run_lore(
            "prune", "--failed-older-than", "30", "--run-logs-older-than", "30", "--lore-dir", self.lore_dir
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Pruned 1 failed/ record(s) older than 30d.", result.stdout)
        self.assertIn("Pruned 1 gardener run log(s) older than 30d.", result.stdout)


if __name__ == "__main__":
    unittest.main()
