"""Tests for `lore.py fact` (skill/scripts/lore.py:cmd_fact,
_resolve_dotted_path)."""
from __future__ import annotations

import json
import shutil
import unittest
from pathlib import Path

from tests.helpers import REPO_ROOT, bootstrap_tmp, run_lore

SAMPLE_EXTRACT = REPO_ROOT / "tests" / "fixtures" / "sample-extract.json"


class FactTests(unittest.TestCase):
    def setUp(self):
        self.tmp = bootstrap_tmp()
        self.lore_dir = self.tmp.name
        extracts_dir = Path(self.lore_dir) / "extracts"
        extracts_dir.mkdir(exist_ok=True)
        shutil.copy2(SAMPLE_EXTRACT, extracts_dir / "sample-extract.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_dict_path_resolves(self):
        result = run_lore("fact", "sample-extract", "scalar", "--lore-dir", self.lore_dir)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "hello world")

    def test_nested_dict_path_resolves(self):
        result = run_lore("fact", "sample-extract", "nested.x.y", "--lore-dir", self.lore_dir)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "42")

    def test_list_index_path_resolves(self):
        """The C17 parity case: a.b.1 must resolve to the second element of
        the list at a.b (value 2), matching lore-mcp-server.py's lore_fact."""
        result = run_lore("fact", "sample-extract", "a.b.1", "--lore-dir", self.lore_dir)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "2")

    def test_list_of_dicts_path_resolves(self):
        result = run_lore("fact", "sample-extract", "items.1.name", "--lore-dir", self.lore_dir)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "second")

    def test_bool_value_prints_lowercase_json(self):
        result = run_lore("fact", "sample-extract", "flag", "--lore-dir", self.lore_dir)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "true")

    def test_invalid_slug_exits_2(self):
        """C7 fix: valid_slug() rejects a slug containing '/' (path
        traversal) before any extract path is ever built from it."""
        result = run_lore("fact", "../etc-passwd", "a.b", "--lore-dir", self.lore_dir)
        self.assertEqual(result.returncode, 2)
        self.assertIn("ERROR: invalid slug '../etc-passwd'", result.stdout + result.stderr)

    def test_missing_extract_exits_2(self):
        result = run_lore("fact", "no-such-slug", "a.b", "--lore-dir", self.lore_dir)
        self.assertEqual(result.returncode, 2)
        self.assertIn("ERROR: no extract for slug 'no-such-slug'", result.stdout + result.stderr)

    def test_malformed_json_extract_exits_2(self):
        (Path(self.lore_dir) / "extracts" / "broken.json").write_text("{not valid json", encoding="utf-8")
        result = run_lore("fact", "broken", "a.b", "--lore-dir", self.lore_dir)
        self.assertEqual(result.returncode, 2)
        self.assertIn("ERROR: failed to parse extracts/broken.json:", result.stdout + result.stderr)

    def test_unresolvable_dotted_path_exits_1(self):
        result = run_lore("fact", "sample-extract", "a.b.z", "--lore-dir", self.lore_dir)
        self.assertEqual(result.returncode, 1)
        self.assertIn("ERROR: Path 'a.b.z' not found in extracts/sample-extract.json", result.stdout + result.stderr)

    def test_out_of_range_list_index_exits_1(self):
        result = run_lore("fact", "sample-extract", "a.b.99", "--lore-dir", self.lore_dir)
        self.assertEqual(result.returncode, 1)
        self.assertIn("ERROR: Path 'a.b.99' not found in extracts/sample-extract.json", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
