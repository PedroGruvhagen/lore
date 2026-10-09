"""GitHub releases connector. Reads GITHUB_TOKEN from env if set; otherwise unauthenticated (60 req/hr/IP).

Fetching goes through the shared _http.py helper (see _http.py's own
docstring for why every connector loads it standalone rather than importing
it as a package). The Authorization header built in _api_headers() never
reaches curl's argv: _http.curl_fetch writes every header into a
`curl --config -` file fed on stdin, so a GITHUB_TOKEN is never visible to
another process on the machine via `ps` (C9).
"""
# Postponed evaluation of annotations: this file's signatures use PEP 604
# `X | Y` unions, which only evaluate natively on Python 3.10+.
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path


def _load_http():
    spec = importlib.util.spec_from_file_location("_lore_http", Path(__file__).with_name("_http.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_http = _load_http()


def _normalize(url_or_repo: str) -> str:
    if url_or_repo.startswith("http"):
        # Convert github.com/{owner}/{repo}/releases to api form if needed.
        if "api.github.com" in url_or_repo:
            return url_or_repo
        prefix = "https://github.com/"
        if url_or_repo.startswith(prefix):
            owner_repo = url_or_repo[len(prefix):].rstrip("/").replace("/releases", "")
            return f"https://api.github.com/repos/{owner_repo}/releases/latest"
        return url_or_repo
    # Plain "owner/repo" form.
    return f"https://api.github.com/repos/{url_or_repo}/releases/latest"


def _api_headers() -> dict:
    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def fetch(url: str, timeout: int = 30) -> tuple[str, str | None]:
    target = _normalize(url)
    return _http.fetch(target, timeout, headers=_api_headers())


def hash_content(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def extract(content: str) -> dict:
    data = json.loads(content)
    return {
        "tag_name": data.get("tag_name"),
        "name": data.get("name"),
        "published_at": data.get("published_at"),
        "body": (data.get("body") or "")[:500],
    }
