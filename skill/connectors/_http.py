"""Shared HTTP fetch helpers for lore connectors (C8-C11).

Every connector in this directory is loaded standalone by file path
(lore.py's _load_connector uses importlib.util.spec_from_file_location, never
a package import), so a connector cannot `from . import _http`. Instead each
connector loads this module the same way lore.py loads connectors:

    import importlib.util
    from pathlib import Path

    def _load_http():
        spec = importlib.util.spec_from_file_location(
            "_lore_http", Path(__file__).with_name("_http.py")
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    _http = _load_http()

This file is named with a leading underscore precisely so lore.py's connector
dispatch (_pick_connector_name / _load_connector) never mistakes it for a
URL-dispatchable connector (C11): it exports fetch-helper functions, not the
(fetch, hash_content) connector contract every real connector implements.
"""
# Postponed evaluation of annotations: this file's signatures use PEP 604
# `X | Y` unions, which only evaluate natively on Python 3.10+.
from __future__ import annotations

import os
import re
import subprocess
import tempfile
import urllib.request
import urllib.error

_UA = "lore-refresh/1.0"

# curl exit codes that indicate transient network trouble worth one IPv4-only retry
# (6 DNS, 7 connect failed, 28 timeout, 35 TLS handshake, 52 empty reply, 55/56 send/recv).
_CURL_RETRY_EXITS = {6, 7, 28, 35, 52, 55, 56}

# Cap on a single fetched response (C10): a source that suddenly serves a huge
# or infinite body (a misconfigured redirect, a runaway API) must not be read
# into memory or written to queue/ whole. Matches the DIFF_CONTENT_MAX_CHARS
# philosophy in lore.py (a hard ceiling per source), sized well above any real
# page, feed or package-metadata payload lore currently tracks.
MAX_FETCH_BYTES = 25 * 1024 * 1024  # 26214400


def parse_curl_headers(header_text: str) -> tuple[int, str, str | None]:
    """Parse a `curl -D` header dump into (status_code, reason, etag_or_none).

    With -L each redirect hop appends its own header block; the LAST block belongs
    to the final response, so status and ETag are read from that block only.
    """
    blocks = [b for b in re.split(r"\r?\n\r?\n", header_text.strip()) if b.strip()]
    if not blocks:
        raise RuntimeError("curl returned no response headers")
    lines = blocks[-1].splitlines()
    m = re.match(r"^HTTP/[\d.]+\s+(\d{3})(?:\s+(.*))?$", lines[0].strip())
    if not m:
        raise RuntimeError(f"unparseable curl status line: {lines[0].strip()!r}")
    status = int(m.group(1))
    reason = (m.group(2) or "").strip()
    etag = None
    for line in lines[1:]:
        if line.lower().startswith("etag:"):
            etag = line.split(":", 1)[1].strip()
            break
    return status, reason, etag


def _curl_config_escape(s: str) -> str:
    """Escape a value for curl's --config double-quoted string syntax.

    Backslash first (so later escapes are not themselves re-escaped), then the
    double quote that would otherwise terminate the string, then the control
    characters curl's config parser recognizes. Mirrors lore-alert.sh's shell
    cfg_escape(), which uses this same pattern for the same reason: keeping a
    secret out of argv (C9).
    """
    s = s.replace("\\", "\\\\")
    s = s.replace('"', '\\"')
    s = s.replace("\n", "\\n")
    s = s.replace("\r", "\\r")
    s = s.replace("\t", "\\t")
    return s


def curl_fetch(
    url: str, timeout: int, headers: dict | None = None, method: str = "GET"
) -> tuple[bytes, str | None]:
    """Fetch url via the system curl binary. Returns (body_bytes, etag_or_none).

    Every option, including any header, rides in a --config file fed on
    curl's stdin rather than on argv (C9): a header such as
    `Authorization: Bearer <token>` passed as `-H` argv is visible to any
    other process on the machine via `ps`; a --config file on stdin never
    appears in argv at all. Raises FileNotFoundError when curl is not
    installed (callers fall back to urllib_fetch), and RuntimeError on curl
    failure or HTTP status >= 400. Tries the default IP stack first and
    retries once IPv4-only on transient network errors. Caps the response at
    MAX_FETCH_BYTES via --max-filesize (C10). method="HEAD" is used by
    lore.py's dead-link check (C12); curl's --head writes the response
    headers to stdout (verified: identical bytes to the --dump-header file),
    not an empty body, so the returned bytes are header text rather than
    page content for a HEAD call. That is fine for every current caller,
    which only inspects success/failure and the parsed etag, never the body,
    but a future caller wanting the real resource body must use method="GET".
    """
    hdr_fd, hdr_path = tempfile.mkstemp(prefix="lore-curl-", suffix=".hdr")
    os.close(hdr_fd)
    cfg_lines = [
        f'url = "{_curl_config_escape(url)}"',
        f'max-time = "{int(timeout)}"',
        f'connect-timeout = "{min(int(timeout), 15)}"',
        f'user-agent = "{_curl_config_escape(_UA)}"',
        f'dump-header = "{_curl_config_escape(hdr_path)}"',
        f'max-filesize = "{MAX_FETCH_BYTES}"',
        "silent",
        "show-error",
        "location",
    ]
    if method == "HEAD":
        cfg_lines.append("head")
    for key, value in (headers or {}).items():
        cfg_lines.append(f'header = "{_curl_config_escape(key)}: {_curl_config_escape(value)}"')
    cfg = ("\n".join(cfg_lines) + "\n").encode("utf-8")
    last_err: Exception = RuntimeError(f"curl failed for {url}")
    try:
        for extra in ([], ["-4"]):
            try:
                proc = subprocess.run(
                    ["curl"] + extra + ["--config", "-"],
                    input=cfg,
                    capture_output=True,
                    timeout=int(timeout) + 30,
                )
            except subprocess.TimeoutExpired:
                last_err = RuntimeError(f"Timeout after {timeout}s fetching {url}")
                continue
            if proc.returncode == 0:
                with open(hdr_path, "r", encoding="utf-8", errors="replace") as f:
                    status, reason, etag = parse_curl_headers(f.read())
                if status >= 400:
                    raise RuntimeError(f"HTTP {status}: {reason or 'error'}")
                return proc.stdout, etag
            stderr = proc.stderr.decode("utf-8", errors="replace").strip()
            last_err = RuntimeError(f"curl failed (exit {proc.returncode}) for {url}: {stderr}")
            if proc.returncode not in _CURL_RETRY_EXITS:
                break
    finally:
        try:
            os.unlink(hdr_path)
        except OSError:
            pass
    raise last_err


def urllib_fetch(
    url: str, timeout: int, headers: dict | None = None, method: str = "GET"
) -> tuple[str, str | None]:
    """urllib fallback used only when the curl binary is missing.

    Bounded read (C10): reads at most MAX_FETCH_BYTES + 1 bytes so an
    oversized response raises RuntimeError instead of being buffered whole
    into memory. On an HTTPError that carries an X-RateLimit-Remaining header
    (GitHub's rate-limit signal), that value is folded into the error message
    regardless of status code: a superset of github_releases.py's old
    403-only special case, not a narrowing of it. method="HEAD" (used by
    lore.py's dead-link check, C12) sends no body and returns an empty string
    for content; callers using HEAD only care about the status/etag.
    """
    req_headers = {"User-Agent": _UA}
    req_headers.update(headers or {})
    req = urllib.request.Request(url, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(MAX_FETCH_BYTES + 1)
            if len(raw) > MAX_FETCH_BYTES:
                raise RuntimeError(f"response for {url} exceeds {MAX_FETCH_BYTES}-byte cap")
            etag = resp.headers.get("ETag")
            return raw.decode("utf-8", errors="replace"), etag
    except urllib.error.HTTPError as e:
        remaining = e.headers.get("X-RateLimit-Remaining") if e.headers else None
        if remaining is not None:
            raise RuntimeError(f"HTTP {e.code} (X-RateLimit-Remaining={remaining}) for {url}") from e
        raise RuntimeError(f"HTTP {e.code} fetching {url}: {e.reason}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"URL error fetching {url}: {e.reason}") from e
    except TimeoutError as e:
        raise RuntimeError(f"Timeout after {timeout}s fetching {url}") from e


def fetch(
    url: str, timeout: int = 30, headers: dict | None = None, method: str = "GET"
) -> tuple[str, str | None]:
    """Fetch the URL and return (content_str, etag_or_none). curl-first, urllib fallback."""
    try:
        body, etag = curl_fetch(url, timeout, headers=headers, method=method)
    except FileNotFoundError:
        return urllib_fetch(url, timeout, headers=headers, method=method)
    return body.decode("utf-8", errors="replace"), etag
