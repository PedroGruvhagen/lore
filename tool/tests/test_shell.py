"""Tests for the shipped shell scripts: syntax-checks every script, exercises
classify_result_line's six failure classes (lore-common.sh), a real
lore-gardener.sh run against the fake-agent-ok.sh fixture, and a
lore-watchdog.sh watched-file-size alert with same-day dedup.
"""
from __future__ import annotations

import http.server
import os
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

from tests.helpers import REPO_ROOT, SKILL_DIR, bootstrap_tmp, run_lore

LORE_COMMON_SH = SKILL_DIR / "scripts" / "lore-common.sh"
LORE_GARDENER_SH = SKILL_DIR / "scripts" / "lore-gardener.sh"
LORE_WATCHDOG_SH = SKILL_DIR / "scripts" / "lore-watchdog.sh"
FAKE_AGENT_OK_SH = REPO_ROOT / "tests" / "fixtures" / "fake-agent-ok.sh"
FAKE_AGENT_SH = REPO_ROOT / "tests" / "fixtures" / "fake-agent.sh"


def _shell_scripts():
    """Every shipped .sh script (skill/, scripts/, tests/, root install.sh).
    Excludes .tasks/ (gitignored run scratch space, not shipped code)."""
    paths = []
    for base in (SKILL_DIR, REPO_ROOT / "scripts", REPO_ROOT / "tests"):
        if base.is_dir():
            paths.extend(sorted(base.rglob("*.sh")))
    install_sh = REPO_ROOT / "install.sh"
    if install_sh.is_file():
        paths.append(install_sh)
    return paths


class ShellSyntaxTests(unittest.TestCase):
    def test_every_shipped_script_parses_with_bash_dash_n(self):
        scripts = _shell_scripts()
        self.assertGreaterEqual(len(scripts), 7, scripts)
        for script in scripts:
            with self.subTest(script=str(script.relative_to(REPO_ROOT))):
                result = subprocess.run(
                    ["bash", "-n", str(script)], capture_output=True, text=True, timeout=10
                )
                self.assertEqual(result.returncode, 0, result.stderr)


class ClassifyResultLineTests(unittest.TestCase):
    """The six S3 failure classes classify_result_line must distinguish."""

    def _classify(self, line: str, rc: str = "") -> str:
        env = dict(os.environ)
        env["LORE_DIR"] = self._tmp.name
        script = f'source "{LORE_COMMON_SH}" >/dev/null 2>&1; classify_result_line "$1" "$2"'
        result = subprocess.run(
            ["bash", "-c", script, "_", line, rc],
            capture_output=True,
            text=True,
            env=env,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def setUp(self):
        self._tmp = bootstrap_tmp()

    def tearDown(self):
        self._tmp.cleanup()

    def test_network(self):
        self.assertEqual(self._classify("ENOTFOUND api.anthropic.com"), "network")

    def test_quota(self):
        self.assertEqual(self._classify("429 rate limit exceeded, limit · resets 3pm"), "quota")

    def test_auth(self):
        self.assertEqual(self._classify("please authenticate: 401 Unauthorized"), "auth")

    def test_sleep(self):
        self.assertEqual(self._classify("the system went to sleep mid-request"), "sleep")

    def test_killed_by_rc_regardless_of_line_content(self):
        self.assertEqual(self._classify("some ordinary output, not obviously a failure", "137"), "killed")

    def test_unknown(self):
        self.assertEqual(self._classify("a completely unrecognized error message"), "unknown")


class _LoopbackOkServer:
    """Minimal always-200 loopback HTTP server, for the gardener's
    network preflight (curl -sS -m 10 -o /dev/null $URL) to hit instead of
    the real api.anthropic.com."""

    def __init__(self):
        self._server = http.server.HTTPServer(("127.0.0.1", 0), self._make_handler())
        self.port = self._server.server_port
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @staticmethod
    def _make_handler():
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, fmt, *args):
                pass

        return Handler

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/"

    def shutdown(self) -> None:
        self._server.shutdown()
        self._server.server_close()


class GardenerFakeAgentTests(unittest.TestCase):
    """Runs the real lore-gardener.sh end to end (not a dry run) against the
    fake-agent-ok.sh fixture: an actionable inbox note makes it past the
    nothing-to-do gate, a local HTTP server stands in for the network
    preflight, and LORE_GARDENER_AUTH_CHECK=true stands in for the auth
    preflight, so the script reaches step 4 and launches the fixture instead
    of a real `claude -p`."""

    def setUp(self):
        self.tmp = bootstrap_tmp()
        self.lore_dir = self.tmp.name
        note_result = run_lore("inbox", "test note so pending is actionable", "--lore-dir", self.lore_dir)
        self.assertEqual(note_result.returncode, 0, note_result.stderr)
        self.server = _LoopbackOkServer()

    def tearDown(self):
        self.server.shutdown()
        self.tmp.cleanup()

    def test_gardener_run_with_fake_agent_completes_and_logs_report(self):
        env = dict(os.environ)
        env.update(
            {
                "LORE_DIR": self.lore_dir,
                "LORE_GARDENER_CMD": str(FAKE_AGENT_OK_SH),
                "LORE_GARDENER_AUTH_CHECK": "true",
                "LORE_GARDENER_NETWORK_CHECK_URL": self.server.url,
                "LORE_GARDENER_NETWORK_CHECK_RETRIES": "1",
                "LORE_GARDENER_DEADLINE_SEC": "60",
                "LORE_GARDENER_CLAUDE_TIMEOUT_SEC": "30",
            }
        )
        # lore-gardener.sh backgrounds a self-watchdog subshell
        # (`( sleep "$DEADLINE_SEC" && kill -TERM -$$ ) &`) that inherits
        # this process's stdout/stderr; its EXIT trap kills the subshell but
        # not the `sleep` child underneath it, which is then reparented and
        # keeps running (and keeps those file descriptors open) until its
        # own DEADLINE_SEC elapses. capture_output=True (pipes) would make
        # subprocess.run() block on that orphan for the full deadline before
        # returning, even though the real script already exited; redirecting
        # to plain files avoids that, since nothing is blocked waiting for
        # the writer to close them.
        with tempfile.TemporaryDirectory(prefix="lore-gardener-out-") as out_dir:
            stdout_path = Path(out_dir) / "stdout.log"
            with open(stdout_path, "wb") as out_f:
                result = subprocess.run(
                    ["bash", str(LORE_GARDENER_SH)],
                    env=env,
                    stdout=out_f,
                    stderr=subprocess.STDOUT,
                    timeout=60,
                )
            captured = stdout_path.read_text(encoding="utf-8", errors="replace")
        self.assertEqual(result.returncode, 0, captured)

        log_path = Path(self.lore_dir) / ".gardener" / "gardener.log"
        self.assertTrue(log_path.is_file())
        log_text = log_path.read_text(encoding="utf-8")
        self.assertIn("done rc=0 report=", log_text)

        today = time.strftime("%Y-%m-%d")
        report_path = Path(self.lore_dir) / ".gardener" / f"report-{today}.md"
        self.assertTrue(report_path.is_file(), log_text)

    def test_gardener_run_with_failing_fake_agent_retries_once_then_fails(self):
        """fake-agent.sh (the failure twin of fake-agent-ok.sh) always
        prints an ENOTFOUND result and exits 1. classify_result_line must
        call that "network", the gardener's MAX_ATTEMPTS=2 retry loop must
        retry it exactly once (network is retryable), and after both
        attempts fail it must call fail_and_exit: rc=1, "done rc=1" logged,
        and alert() must have appended a "Lore gardener failed" line to
        .alerts.log (lore-gardener.sh:454-525)."""
        env = dict(os.environ)
        env.update(
            {
                "LORE_DIR": self.lore_dir,
                "LORE_GARDENER_CMD": str(FAKE_AGENT_SH),
                "LORE_GARDENER_AUTH_CHECK": "true",
                "LORE_GARDENER_NETWORK_CHECK_URL": self.server.url,
                "LORE_GARDENER_NETWORK_CHECK_RETRIES": "1",
                "LORE_GARDENER_DEADLINE_SEC": "60",
                "LORE_GARDENER_CLAUDE_TIMEOUT_SEC": "30",
            }
        )
        # Same pipe-inheritance hazard as the success-path test above: redirect
        # to a real file rather than capture_output=True/pipes.
        with tempfile.TemporaryDirectory(prefix="lore-gardener-fail-out-") as out_dir:
            stdout_path = Path(out_dir) / "stdout.log"
            with open(stdout_path, "wb") as out_f:
                result = subprocess.run(
                    ["bash", str(LORE_GARDENER_SH)],
                    env=env,
                    stdout=out_f,
                    stderr=subprocess.STDOUT,
                    timeout=60,
                )
            captured = stdout_path.read_text(encoding="utf-8", errors="replace")
        self.assertEqual(result.returncode, 1, captured)

        log_path = Path(self.lore_dir) / ".gardener" / "gardener.log"
        self.assertTrue(log_path.is_file())
        log_text = log_path.read_text(encoding="utf-8")
        self.assertIn("cause=network", log_text)
        self.assertIn("retrying once after cause=network", log_text)
        self.assertIn("attempt 2/2", log_text)
        self.assertIn("done rc=1", log_text)

        alerts_log = Path(self.lore_dir) / ".alerts.log"
        self.assertTrue(alerts_log.is_file())
        self.assertIn("Lore gardener failed", alerts_log.read_text(encoding="utf-8"))

        # The retried-away attempt 1 log is preserved, not overwritten
        # (lore-gardener.sh:461-468).
        today = time.strftime("%Y-%m-%d")
        attempt1_log = Path(self.lore_dir) / ".gardener" / f"run-{today}.attempt1.jsonl"
        self.assertTrue(attempt1_log.is_file(), list((Path(self.lore_dir) / ".gardener").iterdir()))


class WatchdogWatchFileTests(unittest.TestCase):
    """LORE_WATCH_FILES (H7): a file over LORE_WATCH_FILE_MAX_BYTES (default
    19500) gets exactly one alert per day, via the marker-file dedup in
    check_watch_files()."""

    def setUp(self):
        self.tmp = bootstrap_tmp()
        self.lore_dir = self.tmp.name
        self.watched_file = Path(self.lore_dir) / "oversized.log"
        self.watched_file.write_bytes(b"x" * 20000)
        self.env = dict(os.environ)
        self.env.update(
            {
                "LORE_DIR": self.lore_dir,
                "LORE_WATCH_FILES": str(self.watched_file),
                # lore-watchdog.sh sources lore-common.sh itself, which reads
                # LORE_ENV_FILE="${LORE_ENV_FILE:-$LORE_DIR/.lore.env}": if an
                # ambient shell already exported LORE_ENV_FILE (because
                # something else sourced lore-common.sh earlier in that same
                # shell, e.g. a real local install's .lore.env), that stale
                # value wins over $LORE_DIR above and gets re-sourced with
                # set -a, silently overwriting LORE_WATCH_FILES (and
                # LORE_ALERT_PROVIDER/LORE_ALERT_CMD) with whatever that real
                # install's .lore.env contains before check_watch_files()
                # ever runs. Pinning all four here to values scoped to this
                # test's own tmp lore_dir keeps the subprocess hermetic
                # regardless of what the invoking shell has already exported.
                "LORE_ENV_FILE": str(Path(self.lore_dir) / ".lore.env"),
                "LORE_ALERT_PROVIDER": "log",
                "LORE_ALERT_CMD": "",
            }
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _run_watchdog(self):
        result = subprocess.run(
            ["bash", str(LORE_WATCHDOG_SH)], env=self.env, capture_output=True, text=True, timeout=30
        )
        self.assertEqual(result.returncode, 0, f"stdout={result.stdout!r} stderr={result.stderr!r}")

    def test_first_run_alerts_once_second_same_day_run_alerts_none(self):
        self._run_watchdog()
        alerts_log = Path(self.lore_dir) / ".alerts.log"
        self.assertTrue(alerts_log.is_file())
        first_lines = alerts_log.read_text(encoding="utf-8").splitlines()
        watch_alerts = [ln for ln in first_lines if "watched file too large" in ln]
        self.assertEqual(len(watch_alerts), 1, first_lines)

        self._run_watchdog()
        second_lines = alerts_log.read_text(encoding="utf-8").splitlines()
        watch_alerts_after = [ln for ln in second_lines if "watched file too large" in ln]
        self.assertEqual(len(watch_alerts_after), 1, second_lines)


if __name__ == "__main__":
    unittest.main()
