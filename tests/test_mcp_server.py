"""Tests for the MCP stdio server (skill/scripts/lore-mcp-server.py).

Spawns the real server as a subprocess and drives it over stdin/stdout with
newline-delimited JSON-RPC, exactly as a real MCP client (Claude Desktop or
otherwise) would. Includes the C17 parity check: lore_fact's dotted-path
resolution must match `lore.py fact`'s _resolve_dotted_path() exactly,
including list-index segments.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from typing import Any, Dict, Optional

from tests.helpers import MCP_SERVER_PY, REPO_ROOT, bootstrap_tmp, run_lore

SAMPLE_EXTRACT = REPO_ROOT / "tests" / "fixtures" / "sample-extract.json"


def _load_mcp_module():
    spec = importlib.util.spec_from_file_location("_lore_test_mcp_server", MCP_SERVER_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class McpServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Read PROTOCOL_VERSION from the real source, never a hardcoded
        # literal in the test, so this catches any future drift.
        cls.server_module = _load_mcp_module()

    def setUp(self):
        self.tmp = bootstrap_tmp()
        self.lore_dir = self.tmp.name
        extracts_dir = Path(self.lore_dir) / "extracts"
        extracts_dir.mkdir(exist_ok=True)
        shutil.copy2(SAMPLE_EXTRACT, extracts_dir / "sample-extract.json")

        pages_dir = Path(self.lore_dir) / "pages"
        pages_dir.mkdir(exist_ok=True)
        (pages_dir / "widget-api.md").write_text(
            "---\n"
            "title: Widget API\n"
            "category: api-reference\n"
            "last_verified: 2026-08-01\n"
            "sources:\n"
            "  - https://example.com/widget\n"
            "---\n"
            "The Widget API accepts a widget_id and returns JSON.\n",
            encoding="utf-8",
        )
        index_result = run_lore("index", "--lore-dir", self.lore_dir)
        self.assertEqual(index_result.returncode, 0, index_result.stderr)

        env = dict(os.environ)
        env["LORE_DIR"] = self.lore_dir
        self.proc = subprocess.Popen(
            [sys.executable, str(MCP_SERVER_PY)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=env,
        )
        self._next_id = 1

    def tearDown(self):
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=10)
        for stream in (self.proc.stdout, self.proc.stderr):
            try:
                stream.close()
            except Exception:
                pass
        self.tmp.cleanup()

    def _rpc(self, method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        req_id = self._next_id
        self._next_id += 1
        request = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}}
        assert self.proc.stdin is not None and self.proc.stdout is not None
        self.proc.stdin.write(json.dumps(request) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        if not line:
            # No response: the server likely exited already (stdout hit
            # EOF), so its stderr will also reach EOF quickly rather than
            # block; self.proc.stderr is a text-mode stream (text=True),
            # which has no read1(), so a plain read() is used to surface
            # whatever it printed instead of silently reporting "".
            stderr_so_far = self.proc.stderr.read()
            self.fail(f"no response from server for method={method!r}; stderr so far: {stderr_so_far!r}")
        response = json.loads(line)
        self.assertEqual(response.get("id"), req_id)
        return response

    def _call_tool(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        response = self._rpc("tools/call", {"name": name, "arguments": arguments})
        self.assertIn("result", response, response)
        text = response["result"]["content"][0]["text"]
        return json.loads(text)

    def test_initialize_protocol_version_matches_source_constant(self):
        response = self._rpc("initialize")
        self.assertEqual(response["result"]["protocolVersion"], self.server_module.PROTOCOL_VERSION)
        self.assertIn("capabilities", response["result"])
        self.assertEqual(response["result"]["serverInfo"]["name"], "lore")

    def test_tools_list_exposes_all_four_tools(self):
        self._rpc("initialize")
        response = self._rpc("tools/list")
        names = {t["name"] for t in response["result"]["tools"]}
        self.assertEqual(names, {"lore_search", "lore_get_page", "lore_list_pages", "lore_fact"})

    def test_lore_search_finds_page(self):
        self._rpc("initialize")
        result = self._call_tool("lore_search", {"query": "widget_id", "limit": 5})
        self.assertIn("results", result)
        slugs = [r.get("slug") for r in result["results"]]
        self.assertIn("widget-api", slugs)

    def test_lore_get_page_returns_content(self):
        self._rpc("initialize")
        result = self._call_tool("lore_get_page", {"slug": "widget-api"})
        self.assertNotIn("error", result)
        self.assertIn("widget_id", result.get("body", "") + json.dumps(result))

    def test_lore_list_pages_lists_slug(self):
        self._rpc("initialize")
        result = self._call_tool("lore_list_pages", {})
        slugs = [p.get("slug") for p in result["pages"]]
        self.assertIn("widget-api", slugs)

    def test_lore_fact_invalid_slug_returns_error(self):
        self._rpc("initialize")
        result = self._call_tool("lore_fact", {"slug": "../etc-passwd", "path": "a.b"})
        self.assertIn("error", result)

    def test_lore_fact_dict_path(self):
        self._rpc("initialize")
        result = self._call_tool("lore_fact", {"slug": "sample-extract", "path": "scalar"})
        self.assertEqual(result.get("value"), "hello world")

    def test_lore_fact_list_index_matches_cli_fact_c17_parity(self):
        """The core C17 regression test: lore_fact("sample-extract", "a.b.1")
        must resolve the list index (value 2) exactly like `lore.py fact
        sample-extract a.b.1` does via _resolve_dotted_path()."""
        self._rpc("initialize")
        mcp_result = self._call_tool("lore_fact", {"slug": "sample-extract", "path": "a.b.1"})
        self.assertNotIn("error", mcp_result, mcp_result)
        self.assertEqual(mcp_result["value"], 2)

        cli_result = run_lore("fact", "sample-extract", "a.b.1", "--lore-dir", self.lore_dir)
        self.assertEqual(cli_result.returncode, 0, cli_result.stderr)
        self.assertEqual(cli_result.stdout.strip(), "2")
        self.assertEqual(str(mcp_result["value"]), cli_result.stdout.strip())

    def test_lore_fact_out_of_range_list_index_is_error(self):
        self._rpc("initialize")
        result = self._call_tool("lore_fact", {"slug": "sample-extract", "path": "a.b.99"})
        self.assertIn("error", result)

    def test_lore_fact_missing_slug_returns_error(self):
        """C17's third required case (dict path, list-index path already
        covered above; missing path via out-of-range above): a slug with no
        extracts/{slug}.json at all."""
        self._rpc("initialize")
        result = self._call_tool("lore_fact", {"slug": "no-such-slug", "path": "a.b"})
        self.assertIn("error", result)
        self.assertIn("no-such-slug", result["error"])

    def test_lore_fact_missing_slug_error_class_matches_cli(self):
        """C17 parity, error-class half: lore_fact and `lore.py fact` must
        classify a missing extract the same way, not just agree on values
        for a present one. lore_fact wraps the message in JSON
        ({"error": "no extract for slug: X"}, tool_lore_fact in
        lore-mcp-server.py) while the CLI prints text to stderr and exits 2
        (cmd_fact in lore.py); the wrapping differs by design (JSON-RPC tool
        result vs. a CLI exit code) but the classification -- "no extract
        for this slug" specifically, not a generic or path-not-found error
        -- and the slug named in it must be identical."""
        self._rpc("initialize")
        mcp_result = self._call_tool("lore_fact", {"slug": "no-such-slug", "path": "a.b"})
        self.assertIn("error", mcp_result, mcp_result)
        self.assertEqual(mcp_result["error"], "no extract for slug: no-such-slug")

        cli_result = run_lore("fact", "no-such-slug", "a.b", "--lore-dir", self.lore_dir)
        self.assertEqual(cli_result.returncode, 2, cli_result.stderr)
        self.assertIn("ERROR: no extract for slug 'no-such-slug'", cli_result.stderr)

        # Same error class ("no extract for slug") and same slug on both sides.
        self.assertIn("no extract for slug", mcp_result["error"])
        self.assertIn("no extract for slug", cli_result.stderr)
        self.assertIn("no-such-slug", mcp_result["error"])
        self.assertIn("no-such-slug", cli_result.stderr)

    def test_lore_fact_invalid_slug_error_class_matches_cli(self):
        """Same error-class parity check as above, for the invalid-slug
        (path traversal, C7) case rather than the missing-slug case."""
        self._rpc("initialize")
        mcp_result = self._call_tool("lore_fact", {"slug": "../etc-passwd", "path": "a.b"})
        self.assertIn("error", mcp_result, mcp_result)
        self.assertEqual(mcp_result["error"], "invalid slug: ../etc-passwd")

        cli_result = run_lore("fact", "../etc-passwd", "a.b", "--lore-dir", self.lore_dir)
        self.assertEqual(cli_result.returncode, 2, cli_result.stderr)
        self.assertIn("ERROR: invalid slug '../etc-passwd'", cli_result.stderr)

        self.assertIn("invalid slug", mcp_result["error"])
        self.assertIn("invalid slug", cli_result.stderr)


if __name__ == "__main__":
    unittest.main()
