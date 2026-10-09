"""Tests for `lore.py bootstrap` (skill/scripts/lore.py:cmd_bootstrap)."""
from __future__ import annotations

import json
import os
import unittest
from pathlib import Path

from tests.helpers import CONNECTORS_DIR, run_lore


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self._prev_lore_dir = os.environ.get("LORE_DIR")

    def tearDown(self):
        if self._prev_lore_dir is None:
            os.environ.pop("LORE_DIR", None)
        else:
            os.environ["LORE_DIR"] = self._prev_lore_dir

    def test_bootstrap_creates_expected_layout(self):
        import tempfile
        with tempfile.TemporaryDirectory(prefix="lore-test-") as tmp:
            result = run_lore("bootstrap", "--lore-dir", tmp, "--scope", "global")
            self.assertEqual(result.returncode, 0, result.stderr)
            # cmd_bootstrap resolves --lore-dir (Path.resolve()) before
            # printing, so on macOS (/tmp -> /private/tmp symlink) the
            # printed path differs textually from the unresolved tmp
            # string; compare against the resolved form instead.
            self.assertIn(f"OK: Lore initialized at {Path(tmp).resolve()}", result.stdout)

            base = Path(tmp)
            for sub in ("pages", "sources", "extracts", "connectors", "failed", "queue", "inbox"):
                self.assertTrue((base / sub).is_dir(), f"missing subdir: {sub}")

            for fname in ("schema.md", "log.md", "index.md", ".gitignore"):
                self.assertTrue((base / fname).is_file(), f"missing file: {fname}")

            hashes = json.loads((base / ".hashes.json").read_text())
            self.assertEqual(hashes, {})

            graph = json.loads((base / ".graph.json").read_text())
            self.assertEqual(graph["nodes"], [])
            self.assertEqual(graph["edges"], [])
            self.assertIn("built", graph)

            lint_config = json.loads((base / "lint-config.json").read_text())
            self.assertIn("contradiction_generic_keys", lint_config)
            self.assertIn("customer", lint_config["contradiction_generic_keys"])

    def test_bootstrap_seeds_connectors_from_shipped_copies(self):
        import tempfile
        with tempfile.TemporaryDirectory(prefix="lore-test-") as tmp:
            result = run_lore("bootstrap", "--lore-dir", tmp)
            self.assertEqual(result.returncode, 0, result.stderr)

            seeded = sorted(p.name for p in (Path(tmp) / "connectors").glob("*.py"))
            shipped = sorted(p.name for p in CONNECTORS_DIR.glob("*.py"))
            self.assertTrue(seeded, "no connectors were seeded into the new lore dir")
            self.assertEqual(seeded, shipped)

    def test_bootstrap_does_not_overwrite_customized_connector(self):
        """A second bootstrap run must not clobber a connector the operator
        has already customized in place (connectors/ is only seeded when
        empty, skill/scripts/lore.py:cmd_bootstrap)."""
        import tempfile
        with tempfile.TemporaryDirectory(prefix="lore-test-") as tmp:
            result = run_lore("bootstrap", "--lore-dir", tmp)
            self.assertEqual(result.returncode, 0, result.stderr)

            web_connector = Path(tmp) / "connectors" / "web.py"
            self.assertTrue(web_connector.is_file())
            marker = "# TEST-CUSTOMIZATION-MARKER\n"
            web_connector.write_text(marker + web_connector.read_text(), encoding="utf-8")

            result2 = run_lore("bootstrap", "--lore-dir", tmp)
            self.assertEqual(result2.returncode, 0, result2.stderr)
            self.assertTrue(web_connector.read_text().startswith(marker))

    def test_bootstrap_is_idempotent_on_schema_and_index(self):
        """A second bootstrap must not overwrite schema.md/index.md/log.md
        once they exist (cmd_bootstrap only writes them "if not already
        present")."""
        import tempfile
        with tempfile.TemporaryDirectory(prefix="lore-test-") as tmp:
            result = run_lore("bootstrap", "--lore-dir", tmp)
            self.assertEqual(result.returncode, 0, result.stderr)

            index_path = Path(tmp) / "index.md"
            custom_text = "# custom index content preserved across re-bootstrap\n"
            index_path.write_text(custom_text, encoding="utf-8")

            result2 = run_lore("bootstrap", "--lore-dir", tmp)
            self.assertEqual(result2.returncode, 0, result2.stderr)
            self.assertEqual(index_path.read_text(), custom_text)


if __name__ == "__main__":
    unittest.main()
