"""Tests for connector dispatch (skill/scripts/lore.py:_pick_connector_name)
and the connector plugins themselves (skill/connectors/*.py).

Connector modules are never regular Python-package imports (see
_load_connector's docstring in lore.py): each one is loaded standalone via
importlib.util.spec_from_file_location, exactly like lore.py itself loads
them at runtime. These tests do the same, rather than importing skill.connectors.*
directly.
"""
from __future__ import annotations

import ast
import http.server
import importlib.util
import os
import sys
import threading
import unittest
import unittest.mock
from pathlib import Path
from typing import Any

from tests.helpers import CONNECTORS_DIR, LORE_PY, REPO_ROOT, SKILL_DIR


def _load_module(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_lore_py() -> Any:
    return _load_module(LORE_PY, "_lore_test_connectors")


class DispatchTests(unittest.TestCase):
    """_pick_connector_name: exact URL-pattern -> connector-name mapping."""

    @classmethod
    def setUpClass(cls):
        cls.lore = _load_lore_py()

    def test_pypi(self):
        self.assertEqual(self.lore._pick_connector_name("https://pypi.org/pypi/requests/json"), "pypi")

    def test_npm_all_three_hosts(self):
        for url in (
            "https://www.npmjs.com/package/left-pad",
            "https://registry.npmjs.org/left-pad/latest",
            "https://foo.npmjs.org/left-pad",
        ):
            self.assertEqual(self.lore._pick_connector_name(url), "npm", url)

    def test_github_releases_both_hosts(self):
        self.assertEqual(
            self.lore._pick_connector_name("https://github.com/foo/bar/releases/latest"), "github_releases"
        )
        self.assertEqual(
            self.lore._pick_connector_name("https://api.github.com/repos/foo/bar/releases/latest"),
            "github_releases",
        )

    def test_rss_suffix_forms(self):
        for url in (
            "https://example.com/feed.rss",
            "https://example.com/feed.atom",
            "https://example.com/feed.xml",
            "https://example.com/blog/feed",
        ):
            self.assertEqual(self.lore._pick_connector_name(url), "rss", url)

    def test_pdf_suffix(self):
        self.assertEqual(self.lore._pick_connector_name("https://example.com/whitepaper.pdf"), "pdf")

    def test_git_forms(self):
        for url in (
            "git@github.com:foo/bar.git",
            "git://example.com/foo.git",
            "git+https://example.com/foo.git",
            "https://example.com/plain-repo.git",
        ):
            self.assertEqual(self.lore._pick_connector_name(url), "git_repo", url)

    def test_plain_url_falls_back_to_web(self):
        self.assertEqual(self.lore._pick_connector_name("https://example.com/some/page"), "web")

    def test_github_without_releases_falls_back_to_web(self):
        self.assertEqual(self.lore._pick_connector_name("https://github.com/foo/bar"), "web")


class GitRepoConnectorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.git_repo = _load_module(CONNECTORS_DIR / "git_repo.py", "_lore_test_git_repo")

    def test_validate_url_rejects_leading_dash(self):
        with self.assertRaises(RuntimeError) as ctx:
            self.git_repo._validate_url("-oProxyCommand=evil")
        self.assertIn("refusing git URL starting with", str(ctx.exception))

    def test_validate_url_rejects_unrecognized_scheme(self):
        with self.assertRaises(RuntimeError) as ctx:
            self.git_repo._validate_url("ftp://example.com/repo.git")
        self.assertIn("unrecognized git URL scheme", str(ctx.exception))

    def test_validate_url_accepts_https(self):
        self.git_repo._validate_url("https://github.com/foo/bar.git")  # must not raise

    def test_validate_url_accepts_scp_style(self):
        self.git_repo._validate_url("git@github.com:foo/bar.git")  # must not raise

    def test_fetch_rejects_leading_dash_before_ever_invoking_git(self):
        with self.assertRaises(RuntimeError):
            self.git_repo.fetch("-oProxyCommand=evil")

    def test_hash_content_is_sha256(self):
        import hashlib
        self.assertEqual(
            self.git_repo.hash_content("abc"), hashlib.sha256(b"abc").hexdigest()
        )


class PdfConnectorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pdf = _load_module(CONNECTORS_DIR / "pdf.py", "_lore_test_pdf")

    def test_fetch_without_api_key_raises_before_any_network_call(self):
        prev = os.environ.pop("MISTRAL_API_KEY", None)
        try:
            with self.assertRaises(RuntimeError) as ctx:
                self.pdf.fetch("https://example.com/whitepaper.pdf")
            self.assertIn("MISTRAL_API_KEY not set", str(ctx.exception))
        finally:
            if prev is not None:
                os.environ["MISTRAL_API_KEY"] = prev


class _JsonServer:
    """Loopback HTTP server serving a fixed JSON body, for connectors whose
    fetch() targets a URL directly (bypassing their registry _normalize()
    since a full http://... URL is passed straight through unchanged)."""

    def __init__(self, body: bytes, content_type: str = "application/json"):
        self.body = body
        self.content_type = content_type
        self._server = http.server.HTTPServer(("127.0.0.1", 0), self._make_handler())
        self.port = self._server.server_port
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def _make_handler(self):
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                self.send_response(200)
                self.send_header("Content-Type", outer.content_type)
                self.send_header("Content-Length", str(len(outer.body)))
                self.end_headers()
                self.wfile.write(outer.body)

            def log_message(self, fmt, *args):
                pass

        return Handler

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/data"

    def shutdown(self) -> None:
        self._server.shutdown()
        self._server.server_close()


class RssConnectorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rss = _load_module(CONNECTORS_DIR / "rss.py", "_lore_test_rss")

    def test_fetch_parses_rss2_items(self):
        """Regression test for the fixed elem-truthiness bug
        (skill/connectors/rss.py): `item.find("title") or item.find(atom_title)`
        is wrong because ElementTree.Element.__bool__ counts child elements,
        not text/None-ness, so a plain RSS 2.0 <item><title>Foo</title></item>
        (text, no child elements) made the found title_el falsy and the `or`
        always fell through to the Atom-namespaced lookup (None, since this
        isn't an Atom feed), printing "(no title)" for every well-formed RSS
        2.0 item regardless of content. This is the exact shape that
        triggered the bug; the explicit `is not None` checks fix it."""
        rss_xml = (
            b'<?xml version="1.0"?><rss version="2.0"><channel>'
            b"<item><title>First Post</title><link>https://example.com/1</link></item>"
            b"<item><title>Second Post</title><link>https://example.com/2</link></item>"
            b"</channel></rss>"
        )
        server = _JsonServer(rss_xml, content_type="application/rss+xml")
        try:
            content, _etag = self.rss.fetch(server.url)
            self.assertIn("First Post :: https://example.com/1", content)
            self.assertIn("Second Post :: https://example.com/2", content)
            self.assertNotIn("(no title)", content)
        finally:
            server.shutdown()

    def test_fetch_empty_feed_reports_empty(self):
        rss_xml = b'<?xml version="1.0"?><rss version="2.0"><channel></channel></rss>'
        server = _JsonServer(rss_xml, content_type="application/rss+xml")
        try:
            content, _etag = self.rss.fetch(server.url)
            self.assertEqual(content, "(empty feed)")
        finally:
            server.shutdown()

    def test_fetch_parses_atom_entries(self):
        """Coverage for the Atom branch itself (the actual elem-truthiness
        regression is test_fetch_parses_rss2_items above, on the RSS 2.0
        shape that triggered it): an Atom <entry> has no unqualified
        "title" tag, so item.find("title") was already None (correctly
        falsy) even under the old buggy `or` fallback, and this always
        resolved via the Atom-namespaced lookup. This confirms that path
        still resolves correctly under the explicit `is not None` checks."""
        atom_xml = (
            b'<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">'
            b'<entry><title>Atom Post</title><link href="https://example.com/atom-1"/></entry>'
            b"</feed>"
        )
        server = _JsonServer(atom_xml, content_type="application/atom+xml")
        try:
            content, _etag = self.rss.fetch(server.url)
            self.assertIn("Atom Post :: https://example.com/atom-1", content)
        finally:
            server.shutdown()

    def test_fetch_malformed_xml_raises_runtime_error(self):
        server = _JsonServer(b"<rss><channel><item>not closed", content_type="application/rss+xml")
        try:
            with self.assertRaises(RuntimeError) as ctx:
                self.rss.fetch(server.url)
            self.assertIn("Feed parse error", str(ctx.exception))
        finally:
            server.shutdown()


class GithubReleasesConnectorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.gh = _load_module(CONNECTORS_DIR / "github_releases.py", "_lore_test_gh_releases")

    def test_extract_shape(self):
        payload = {
            "tag_name": "v1.2.3",
            "name": "v1.2.3 release",
            "published_at": "2026-08-01T00:00:00Z",
            "body": "x" * 800,
        }
        import json as _json
        extracted = self.gh.extract(_json.dumps(payload))
        self.assertEqual(extracted["tag_name"], "v1.2.3")
        self.assertEqual(extracted["name"], "v1.2.3 release")
        self.assertEqual(extracted["published_at"], "2026-08-01T00:00:00Z")
        self.assertEqual(len(extracted["body"]), 500)


class NpmConnectorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.npm = _load_module(CONNECTORS_DIR / "npm.py", "_lore_test_npm")

    def test_extract_shape(self):
        payload = {
            "name": "left-pad",
            "version": "1.3.0",
            "description": "pad a string",
            "dependencies": {},
        }
        import json as _json
        extracted = self.npm.extract(_json.dumps(payload))
        self.assertEqual(extracted["name"], "left-pad")
        self.assertEqual(extracted["version"], "1.3.0")


class PypiConnectorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pypi = _load_module(CONNECTORS_DIR / "pypi.py", "_lore_test_pypi")

    def test_extract_shape_and_recent_releases_sorted_desc(self):
        payload = {
            "info": {
                "name": "requests",
                "version": "2.32.0",
                "summary": "HTTP for humans",
                "requires_python": ">=3.8",
            },
            "releases": {
                "2.30.0": [],
                "2.32.0": [],
                "2.31.0": [],
                "2.29.0": [],
                "2.28.0": [],
                "2.27.0": [],
            },
        }
        import json as _json
        extracted = self.pypi.extract(_json.dumps(payload))
        self.assertEqual(extracted["name"], "requests")
        self.assertEqual(extracted["version"], "2.32.0")
        self.assertEqual(
            extracted["recent_releases"], ["2.32.0", "2.31.0", "2.30.0", "2.29.0", "2.28.0"]
        )


class HttpHelperTests(unittest.TestCase):
    """_http.py (C8-C11 shared fetch helpers). Every connector loads it
    standalone the same way lore.py loads connectors (see its own module
    docstring); this loads it the same way rather than importing it as a
    package."""

    @classmethod
    def setUpClass(cls):
        cls.http = _load_module(CONNECTORS_DIR / "_http.py", "_lore_test_http")

    def test_module_loads_standalone_and_exposes_fetch_helpers(self):
        for name in ("fetch", "curl_fetch", "urllib_fetch", "parse_curl_headers", "MAX_FETCH_BYTES"):
            self.assertTrue(hasattr(self.http, name), name)

    def test_max_fetch_bytes_matches_c10_constant(self):
        self.assertEqual(self.http.MAX_FETCH_BYTES, 25 * 1024 * 1024)

    def test_curl_fetch_c9_authorization_header_never_reaches_argv(self):
        """C9 fix: an Authorization header must ride the curl --config file
        fed on stdin (the `input=` kwarg to subprocess.run), never a -H
        argv entry, since argv is visible to any other process on the
        machine via `ps`. Spies on the real subprocess.run call curl_fetch
        makes rather than mocking curl_fetch itself, so the actual
        _curl_config_escape/config-building logic still runs for real."""
        secret_token = "test-secret-token-1234567890"
        captured = {}

        def fake_run(cmd, input=None, capture_output=None, timeout=None):
            captured["cmd"] = list(cmd)
            captured["input"] = input

            class _FakeResult:
                returncode = 0
                stdout = b""
                stderr = b""

            return _FakeResult()

        with unittest.mock.patch.object(self.http.subprocess, "run", side_effect=fake_run):
            # The mocked curl never actually writes the --dump-header file,
            # so parse_curl_headers reads an empty file and curl_fetch
            # raises; only the captured argv/stdin matter to this test.
            with self.assertRaises(RuntimeError):
                self.http.curl_fetch(
                    "https://api.github.com/repos/foo/bar/releases/latest",
                    timeout=5,
                    headers={"Authorization": f"Bearer {secret_token}"},
                )

        self.assertIn("cmd", captured, "subprocess.run was never called")
        argv_text = " ".join(captured["cmd"])
        self.assertNotIn(secret_token, argv_text)
        self.assertNotIn("Authorization", argv_text)
        self.assertIsNotNone(captured["input"])
        config_text = captured["input"].decode("utf-8")
        self.assertIn(f"Authorization: Bearer {secret_token}", config_text)

    def test_urllib_fetch_c10_size_cap_enforced(self):
        """C10 fix: urllib_fetch bounds its read to MAX_FETCH_BYTES + 1 and
        raises instead of buffering an oversized response whole into
        memory. Patches the module-level constant down to a small value
        (looked up at call time from the module's own globals, so this
        takes effect) rather than serving a real 25MB response."""
        server = _JsonServer(b"x" * 5000, content_type="text/plain")
        original = self.http.MAX_FETCH_BYTES
        self.http.MAX_FETCH_BYTES = 1000
        try:
            with self.assertRaises(RuntimeError) as ctx:
                self.http.urllib_fetch(server.url, timeout=5)
            self.assertIn("exceeds 1000-byte cap", str(ctx.exception))
        finally:
            self.http.MAX_FETCH_BYTES = original
            server.shutdown()

    def test_urllib_fetch_under_cap_succeeds(self):
        server = _JsonServer(b"small body", content_type="text/plain")
        try:
            content, _etag = self.http.urllib_fetch(server.url, timeout=5)
            self.assertEqual(content, "small body")
        finally:
            server.shutdown()


class StdlibOnlyImportsTests(unittest.TestCase):
    """I12: skill/requirements.txt documents "standard library only"; every
    .py file shipped under skill/ must import only the stdlib. The one
    documented exception is skill/connectors/pdf.py's _OCR_INVOCATION: a
    string of Python source for a *subprocess* (`python3 -c ...`), never
    imported by pdf.py's own interpreter process (see pdf.py's module
    docstring). A naive line-based `grep '^import|^from'` over skill/ would
    misreport that embedded string's `from mistralai import Mistral` line
    as a real import of the shipped pdf.py module, since the string starts
    at column 0 in the source; parsing each file with ast instead only
    sees genuine top-level import statements, not string contents."""

    def test_requirements_txt_documents_stdlib_only(self):
        text = (SKILL_DIR / "requirements.txt").read_text(encoding="utf-8")
        lowered = text.lower()
        self.assertIn("stdlib only", lowered)
        # Every non-comment, non-blank line would be a real pip requirement
        # spec (I12: "reduced to a comment... or removed" if any package is
        # listed that shipped code doesn't import); there must be none.
        code_lines = [ln for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]
        self.assertEqual(code_lines, [])

    def test_every_shipped_module_imports_only_stdlib(self):
        stdlib_names = set(sys.stdlib_module_names) | {"__future__"}
        offenders = []
        for path in sorted(SKILL_DIR.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [alias.name.split(".")[0] for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    if node.level:  # relative import; none shipped, still not third-party
                        continue
                    names = [node.module.split(".")[0]] if node.module else []
                else:
                    continue
                for name in names:
                    if name not in stdlib_names:
                        offenders.append(f"{path.relative_to(REPO_ROOT)}: {name}")
        self.assertEqual(offenders, [], offenders)

    def test_pdf_ocr_invocation_string_is_not_a_real_import_of_pdf_py(self):
        """Confirms the documented exception itself: mistralai appears as
        text inside pdf.py's _OCR_INVOCATION string (proving the exception
        is real and this isn't a stale comment), but ast sees no such
        import in pdf.py's own top-level code, matching the assertion
        above."""
        pdf_source = (SKILL_DIR / "connectors" / "pdf.py").read_text(encoding="utf-8")
        self.assertIn("from mistralai import Mistral", pdf_source)
        tree = ast.parse(pdf_source, filename="pdf.py")
        top_level_imports = [
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            for alias in node.names
        ]
        self.assertNotIn("Mistral", top_level_imports)


if __name__ == "__main__":
    unittest.main()
