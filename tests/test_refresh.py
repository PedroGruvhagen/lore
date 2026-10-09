"""Tests for `lore.py refresh` (skill/scripts/lore.py:cmd_refresh).

Connector dispatch: a plain http://127.0.0.1:<port>/... URL matches none of
_pick_connector_name's special-cased patterns (pypi.org, npmjs, github
releases, .rss/.atom/feed, .pdf, git@/git://), so it always falls through to
the generic "web" connector -- exactly the connector a real https:// source
would use. That lets these tests exercise the full refresh pipeline (hash
diffing, queue/ diff records, needs_review propagation, quarantine) against
a real local HTTP server, without needing internet access.

Note on file:// sources (recorded per the task's explicit authorization to
document this decision rather than expand shipped-code scope): both
lore.py's own inline _parse_curl_headers() (skill/scripts/lore.py:1577-1597)
and connectors/_http.py's curl_fetch() require an "HTTP/x.x NNN" status
line, which a local-file curl fetch never produces, and cmd_refresh's own
source-URL filter (`url.startswith(("http://", "https://", "git@",
"git://", "git+"))`, skill/scripts/lore.py:1986) silently skips file://
URLs outright regardless. No file:// support exists anywhere in the fetch
pipeline today. Rather than add it (out of Section 1's item scope for this
phase), this suite uses a real loopback HTTP server, which exercises the
identical code path a live https:// source would.
"""
from __future__ import annotations

import hashlib
import http.server
import importlib.util
import json
import socket
import threading
import unittest
from pathlib import Path
from typing import Any

from tests.helpers import LORE_PY, bootstrap_tmp, run_lore, write_page


class _LocalContentServer:
    """Minimal loopback HTTP server serving mutable bytes at /source.txt."""

    def __init__(self, content: bytes = b"initial content"):
        self.content = content
        self._server = http.server.HTTPServer(("127.0.0.1", 0), self._make_handler())
        self.port = self._server.server_port
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def _make_handler(self):
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 (stdlib method name)
                body = outer.content
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, fmt, *args):  # silence stderr request logging
                pass

        return Handler

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/source.txt"

    def set_content(self, content: bytes) -> None:
        self.content = content

    def shutdown(self) -> None:
        self._server.shutdown()
        self._server.server_close()


def _unreachable_url() -> str:
    """A loopback URL with nothing listening: bind then immediately close, so
    the port is real but connecting to it deterministically refuses."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return f"http://127.0.0.1:{port}/nope"


class RefreshFirstFetchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = bootstrap_tmp()
        self.lore_dir = self.tmp.name
        self.server = _LocalContentServer(b"version one")

    def tearDown(self):
        self.server.shutdown()
        self.tmp.cleanup()

    def test_first_fetch_stores_hash_without_writing_diff(self):
        write_page(
            "source-page",
            {
                "title": "Source Page",
                "category": "tooling",
                "last_verified": "2026-01-01",
                "sources": [self.server.url],
                "auto_update": True,
            },
            "Tracks an upstream source.\n",
        )
        result = run_lore("refresh", "--lore-dir", self.lore_dir)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("[NEW]", result.stdout)
        self.assertIn("1 sources checked: 0 unchanged, 1 changed/new, 0 failed, 0 quarantine-skipped.", result.stdout)

        hashes = json.loads((Path(self.lore_dir) / ".hashes.json").read_text())
        self.assertIn(self.server.url, hashes)
        self.assertIn("sha256", hashes[self.server.url])

        queue_dir = Path(self.lore_dir) / "queue"
        self.assertEqual(list(queue_dir.glob("*.diff.md")), [], "first fetch must not write a diff record")


class RefreshChangeDetectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = bootstrap_tmp()
        self.lore_dir = self.tmp.name
        self.server = _LocalContentServer(b"version one")
        write_page(
            "source-page",
            {
                "title": "Source Page",
                "category": "tooling",
                "last_verified": "2026-01-01",
                "sources": [self.server.url],
                "auto_update": True,
            },
            "Tracks an upstream source.\n",
        )
        write_page(
            "dependent-page",
            {
                "title": "Dependent Page",
                "category": "tooling",
                "last_verified": "2026-01-01",
                "sources": [],
            },
            "Depends on [[source-page]] for its facts.\n",
        )
        # Build .graph.json edges (dependent-page -> source-page) so the
        # propagation test below has a real graph to walk.
        index_result = run_lore("index", "--lore-dir", self.lore_dir)
        self.assertEqual(index_result.returncode, 0, index_result.stderr)
        # First fetch: stores the initial hash, no diff yet.
        first = run_lore("refresh", "--lore-dir", self.lore_dir)
        self.assertEqual(first.returncode, 0, first.stderr)

    def tearDown(self):
        self.server.shutdown()
        self.tmp.cleanup()

    def test_changed_content_writes_diff_and_flags_needs_review(self):
        self.server.set_content(b"version two, materially different")
        result = run_lore("refresh", "--lore-dir", self.lore_dir)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("[CHANGED]", result.stdout)
        self.assertIn("needs LLM review -> queue/", result.stdout)

        diffs = list((Path(self.lore_dir) / "queue").glob("source-page-*.diff.md"))
        self.assertEqual(len(diffs), 1)
        diff_text = diffs[0].read_text()
        self.assertIn("slug: source-page", diff_text)
        self.assertIn("version two, materially different", diff_text)

        source_text = (Path(self.lore_dir) / "pages" / "source-page.md").read_text()
        self.assertIn("needs_review: true", source_text)

    def test_changed_content_propagates_needs_review_to_dependent(self):
        self.server.set_content(b"version two, materially different")
        result = run_lore("refresh", "--lore-dir", self.lore_dir)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("1 dependent(s) flagged for review", result.stdout)

        dependent_text = (Path(self.lore_dir) / "pages" / "dependent-page.md").read_text()
        self.assertIn("needs_review: true", dependent_text)

    def test_unchanged_content_does_not_write_diff_and_bumps_last_verified(self):
        # Content is identical to the first fetch: no diff, and since the
        # page carries no needs_review flag, last_verified auto-bumps.
        result = run_lore("refresh", "--lore-dir", self.lore_dir)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("[OK]", result.stdout)
        self.assertIn("1 sources checked: 1 unchanged, 0 changed/new, 0 failed, 0 quarantine-skipped.", result.stdout)
        self.assertIn("1 pages auto-verified", result.stdout)

        diffs = list((Path(self.lore_dir) / "queue").glob("*.diff.md"))
        self.assertEqual(diffs, [])

    def test_needs_review_page_is_not_auto_verified_even_when_unchanged(self):
        # First mark it changed (sets needs_review), then refresh again with
        # the SAME (now-current) content: hash matches so it's "unchanged",
        # but the still-set needs_review flag must block the last_verified
        # auto-bump (skill/scripts/lore.py:2132's `and not
        # meta.get("needs_review")` gate).
        self.server.set_content(b"version two, materially different")
        run_lore("refresh", "--lore-dir", self.lore_dir)
        result = run_lore("refresh", "--lore-dir", self.lore_dir)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("0 pages auto-verified", result.stdout)


class RefreshFailureAndQuarantineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = bootstrap_tmp()
        self.lore_dir = self.tmp.name
        self.bad_url = _unreachable_url()
        write_page(
            "flaky-page",
            {
                "title": "Flaky Page",
                "category": "tooling",
                "last_verified": "2026-01-01",
                "sources": [self.bad_url],
                "auto_update": True,
            },
            "Tracks an unreachable source.\n",
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_single_failure_writes_error_record_no_quarantine_yet(self):
        result = run_lore("refresh", "--lore-dir", self.lore_dir)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("1 sources checked: 0 unchanged, 0 changed/new, 1 failed, 0 quarantine-skipped.", result.stdout)

        failed_files = list((Path(self.lore_dir) / "failed").glob("flaky-page-*.err"))
        self.assertEqual(len(failed_files), 1)

        hashes = json.loads((Path(self.lore_dir) / ".hashes.json").read_text())
        self.assertEqual(hashes[self.bad_url]["consecutive_failures"], 1)
        self.assertNotIn("quarantined_until", hashes[self.bad_url])

    def test_third_consecutive_failure_sets_quarantined_until(self):
        run_lore("refresh", "--lore-dir", self.lore_dir)
        run_lore("refresh", "--lore-dir", self.lore_dir)
        result = run_lore("refresh", "--lore-dir", self.lore_dir)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("[QUARANTINE]", result.stdout)

        hashes = json.loads((Path(self.lore_dir) / ".hashes.json").read_text())
        entry = hashes[self.bad_url]
        self.assertEqual(entry["consecutive_failures"], 3)
        self.assertIn("quarantined_until", entry)

    def test_quarantined_source_is_skipped_without_force(self):
        run_lore("refresh", "--lore-dir", self.lore_dir)
        run_lore("refresh", "--lore-dir", self.lore_dir)
        run_lore("refresh", "--lore-dir", self.lore_dir)  # 3rd failure -> quarantined

        result = run_lore("refresh", "--lore-dir", self.lore_dir)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("[SKIP quarantined]", result.stdout)
        self.assertIn("0 failed, 1 quarantine-skipped", result.stdout)

    def test_force_overrides_quarantine_gate(self):
        run_lore("refresh", "--lore-dir", self.lore_dir)
        run_lore("refresh", "--lore-dir", self.lore_dir)
        run_lore("refresh", "--lore-dir", self.lore_dir)  # 3rd failure -> quarantined

        result = run_lore("refresh", "--force", "--lore-dir", self.lore_dir)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("[SKIP quarantined]", result.stdout)
        # --force still attempts the fetch, which still fails (source is
        # genuinely unreachable) and records a 4th consecutive failure.
        hashes = json.loads((Path(self.lore_dir) / ".hashes.json").read_text())
        self.assertEqual(hashes[self.bad_url]["consecutive_failures"], 4)


class RefreshSlugFilterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = bootstrap_tmp()
        self.lore_dir = self.tmp.name
        self.server_a = _LocalContentServer(b"a content")
        self.server_b = _LocalContentServer(b"b content")
        write_page(
            "page-a",
            {
                "title": "Page A",
                "category": "tooling",
                "last_verified": "2026-01-01",
                "sources": [self.server_a.url],
                "auto_update": True,
            },
            "Body A.\n",
        )
        write_page(
            "page-b",
            {
                "title": "Page B",
                "category": "tooling",
                "last_verified": "2026-01-01",
                "sources": [self.server_b.url],
                "auto_update": True,
            },
            "Body B.\n",
        )

    def tearDown(self):
        self.server_a.shutdown()
        self.server_b.shutdown()
        self.tmp.cleanup()

    def test_slug_filter_only_touches_named_page(self):
        result = run_lore("refresh", "--slug", "page-a", "--lore-dir", self.lore_dir)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("1 sources checked", result.stdout)

        hashes = json.loads((Path(self.lore_dir) / ".hashes.json").read_text())
        self.assertIn(self.server_a.url, hashes)
        self.assertNotIn(self.server_b.url, hashes)

    def test_unknown_slug_filter_errors(self):
        result = run_lore("refresh", "--slug", "no-such-page", "--lore-dir", self.lore_dir)
        self.assertEqual(result.returncode, 1)
        self.assertIn("No page found with slug 'no-such-page'", result.stderr)


def _load_lore_py() -> Any:
    """Load lore.py standalone via importlib, the same way test_connectors.py's
    own _load_lore_py() does (see that file's module docstring). This is the
    one exception to this suite's usual "exercise lore.py only through
    run_lore()" convention: _HTMLTextExtractor/_html_to_text are internal
    helpers with no CLI subcommand of their own, so testing the extractor
    hygiene C19 task 2 requires (drop script/style/noscript/template/svg
    content and comments, never emit attribute values) needs direct access."""
    spec = importlib.util.spec_from_file_location("_lore_test_refresh_extractor", LORE_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class HTMLTextExtractorHygieneTests(unittest.TestCase):
    """C19 task 2 (skill/scripts/lore.py:_HTMLTextExtractor). Comments and
    attribute values were already dropped correctly before this phase
    (HTMLParser's default handle_comment() is a no-op unless overridden, and
    handle_starttag() never touches its attrs argument); svg content was not,
    until this phase added it to _SKIP_TAGS. One fixture covers all seven
    categories the task lists, so a regression in any of them is caught here
    directly, rather than only showing up later as a change-detection
    false-negative."""

    def test_dropped_categories_are_absent_from_extracted_text(self):
        lore = _load_lore_py()
        html = (
            "<html><body>"
            "<!-- a comment that must never appear -->"
            "<script>var scriptMarker = 1;</script>"
            "<style>.styleMarker { color: red; }</style>"
            "<noscript>noscriptMarker</noscript>"
            "<template><span>templateMarker</span></template>"
            "<svg><text>svgMarker</text></svg>"
            '<p title="attrMarker" data-x="attrMarker">Visible text survives.</p>'
            "</body></html>"
        )
        extracted = lore._html_to_text(html)
        for marker in (
            "a comment that must never appear",
            "scriptMarker",
            "styleMarker",
            "noscriptMarker",
            "templateMarker",
            "svgMarker",
            "attrMarker",
        ):
            self.assertNotIn(marker, extracted, f"{marker!r} leaked into extracted text")
        self.assertIn("Visible text survives.", extracted)


def _html_doc(*, build_id: str, nonce: str, css_class: str, sentence: str) -> bytes:
    """An HTML fixture with the three kinds of per-response churn C19's
    _change_hash must ignore (a <meta> build id, a <script> nonce, and an
    attribute value) around one visible sentence, so a test can hold the
    sentence fixed while churning the markup, or change the sentence while
    holding the markup fixed."""
    return (
        "<!doctype html>\n"
        "<html><head>\n"
        f'<meta name="build-id" content="{build_id}">\n'
        f'<script>window.__NONCE__ = "{nonce}";</script>\n'
        "</head><body>\n"
        f'<p id="x" class="{css_class}">{sentence}</p>\n'
        "</body></html>\n"
    ).encode("utf-8")


HTML_V1 = _html_doc(
    build_id="build-0001", nonce="aaa111", css_class="old-class",
    sentence="Hello world, this is the tracked sentence.",
)
HTML_V1_MARKUP_ONLY_CHANGE = _html_doc(
    build_id="build-0002", nonce="bbb222", css_class="new-class",
    sentence="Hello world, this is the tracked sentence.",  # same sentence
)
HTML_V2_TEXT_CHANGED = _html_doc(
    build_id="build-0002", nonce="bbb222", css_class="new-class",
    sentence="Hello world, this is the changed sentence.",
)


class RefreshChangeHashTests(unittest.TestCase):
    """C19 (skill/scripts/lore.py:_change_hash, cmd_refresh's legacy-migration
    branch, and its .refresh-cache/ writes): the hash tracks the extracted,
    normalized text of an HTML source, not its raw bytes, a pre-C19
    .hashes.json entry upgrades losslessly, and the previous payload is
    cached for the next diff. Tests (a)-(f) of task 6."""

    def setUp(self):
        self.tmp = bootstrap_tmp()
        self.lore_dir = self.tmp.name
        self.server = _LocalContentServer(HTML_V1)
        write_page(
            "html-source-page",
            {
                "title": "HTML Source Page",
                "category": "tooling",
                "last_verified": "2026-01-01",
                "sources": [self.server.url],
                "auto_update": True,
            },
            "Tracks an HTML upstream source.\n",
        )

    def tearDown(self):
        self.server.shutdown()
        self.tmp.cleanup()

    def test_a_markup_only_diff_is_reported_unchanged_and_writes_no_diff(self):
        first = run_lore("refresh", "--lore-dir", self.lore_dir)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertIn("[NEW]", first.stdout)

        # Build id, nonce and an attribute value all change; the one visible
        # sentence does not.
        self.server.set_content(HTML_V1_MARKUP_ONLY_CHANGE)
        result = run_lore("refresh", "--lore-dir", self.lore_dir)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("unchanged", result.stdout)
        self.assertNotIn("[CHANGED]", result.stdout)

        diffs = list((Path(self.lore_dir) / "queue").glob("html-source-page-*.diff.md"))
        self.assertEqual(diffs, [], "markup-only churn must not write a diff record")

    def test_b_visible_text_change_writes_diff_section_before_full_payload(self):
        first = run_lore("refresh", "--lore-dir", self.lore_dir)
        self.assertEqual(first.returncode, 0, first.stderr)  # caches the first payload

        self.server.set_content(HTML_V2_TEXT_CHANGED)
        result = run_lore("refresh", "--lore-dir", self.lore_dir)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("[CHANGED]", result.stdout)

        diffs = list((Path(self.lore_dir) / "queue").glob("html-source-page-*.diff.md"))
        self.assertEqual(len(diffs), 1)
        diff_text = diffs[0].read_text()
        self.assertIn("## Diff against the previous payload", diff_text)
        self.assertIn("-Hello world, this is the tracked sentence.", diff_text)
        self.assertIn("+Hello world, this is the changed sentence.", diff_text)

        diff_idx = diff_text.index("## Diff against the previous payload")
        payload_idx = diff_text.index("Source changed. New content follows for LLM review")
        self.assertLess(diff_idx, payload_idx, "the diff section must come before the full payload")

    def test_c_legacy_entry_with_matching_raw_hash_upgrades_silently(self):
        # A pre-C19 .hashes.json entry: sha256 of the raw fetched bytes, no
        # hash_scheme marker, written directly rather than via refresh (which
        # always writes the new scheme going forward).
        raw_hash = hashlib.sha256(HTML_V1).hexdigest()
        hashes_path = Path(self.lore_dir) / ".hashes.json"
        hashes_path.write_text(
            json.dumps({
                self.server.url: {
                    "sha256": raw_hash,
                    "etag": None,
                    "last_fetched": "2026-01-01T00:00:00Z",
                    "slug": "html-source-page",
                }
            }),
            encoding="utf-8",
        )

        result = run_lore("refresh", "--lore-dir", self.lore_dir)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("unchanged (hash scheme upgraded)", result.stdout)
        self.assertNotIn("[CHANGED]", result.stdout)

        diffs = list((Path(self.lore_dir) / "queue").glob("html-source-page-*.diff.md"))
        self.assertEqual(diffs, [], "a scheme upgrade with no real change must not write a diff record")

        hashes = json.loads(hashes_path.read_text())
        entry = hashes[self.server.url]
        self.assertEqual(entry.get("hash_scheme"), "text-v1")
        self.assertNotEqual(entry["sha256"], raw_hash, "the entry must be rewritten under the new scheme")

    def test_d_legacy_entry_with_stale_raw_hash_and_changed_text_is_reported_changed(self):
        stale_raw_hash = hashlib.sha256(b"some earlier version of the page").hexdigest()
        hashes_path = Path(self.lore_dir) / ".hashes.json"
        hashes_path.write_text(
            json.dumps({
                self.server.url: {
                    "sha256": stale_raw_hash,
                    "etag": None,
                    "last_fetched": "2026-01-01T00:00:00Z",
                    "slug": "html-source-page",
                }
            }),
            encoding="utf-8",
        )

        result = run_lore("refresh", "--lore-dir", self.lore_dir)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("[CHANGED]", result.stdout)

        hashes = json.loads(hashes_path.read_text())
        self.assertEqual(hashes[self.server.url].get("hash_scheme"), "text-v1")

    def test_e_json_body_is_hashed_exactly_as_connector_hash_content(self):
        json_server = _LocalContentServer(b'{"version": 1}')
        try:
            write_page(
                "json-source-page",
                {
                    "title": "JSON Source Page",
                    "category": "tooling",
                    "last_verified": "2026-01-01",
                    "sources": [json_server.url],
                    "auto_update": True,
                },
                "Tracks a JSON upstream source.\n",
            )
            result = run_lore("refresh", "--slug", "json-source-page", "--lore-dir", self.lore_dir)
            self.assertEqual(result.returncode, 0, result.stderr)

            hashes = json.loads((Path(self.lore_dir) / ".hashes.json").read_text())
            expected = hashlib.sha256(b'{"version": 1}').hexdigest()
            self.assertEqual(hashes[json_server.url]["sha256"], expected)
        finally:
            json_server.shutdown()

    def test_f_cache_file_exists_after_first_fetch_and_holds_new_payload_after_change(self):
        result = run_lore("refresh", "--lore-dir", self.lore_dir)
        self.assertEqual(result.returncode, 0, result.stderr)

        cache_name = hashlib.sha256(self.server.url.encode("utf-8")).hexdigest() + ".txt"
        cache_path = Path(self.lore_dir) / ".refresh-cache" / cache_name
        self.assertTrue(cache_path.is_file())
        self.assertIn("Hello world, this is the tracked sentence.", cache_path.read_text())

        self.server.set_content(HTML_V2_TEXT_CHANGED)
        result = run_lore("refresh", "--lore-dir", self.lore_dir)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Hello world, this is the changed sentence.", cache_path.read_text())


if __name__ == "__main__":
    unittest.main()
