"""Generic HTTP/HTTPS connector. Always-available fallback when no more specific dispatcher matches.

Fetching goes through the shared _http.py helper (curl-first: the system curl
binary is far more reliable than Python's urllib on this class of network,
hung IPv6 routes make urllib time out on hosts curl reaches fine; urllib
remains as the fallback when curl is not installed). See _http.py's own
docstring for why every connector loads it standalone rather than importing
it as a package.
"""
# Postponed evaluation of annotations: this file's signatures use PEP 604
# `X | Y` unions, which only evaluate natively on Python 3.10+.
from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path


def _load_http():
    spec = importlib.util.spec_from_file_location("_lore_http", Path(__file__).with_name("_http.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_http = _load_http()


def fetch(url: str, timeout: int = 30) -> tuple[str, str | None]:
    """GET the URL and return (content_str, etag_or_none). curl-first, urllib fallback. Follows redirects."""
    return _http.fetch(url, timeout)


def hash_content(content: str) -> str:
    """SHA-256 hex digest of the UTF-8-encoded content."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()
