"""npm registry connector. Accepts package name or registry URL.

Fetching goes through the shared _http.py helper; see _http.py's own
docstring for why every connector loads it standalone rather than importing
it as a package.
"""
# Postponed evaluation of annotations: this file's signatures use PEP 604
# `X | Y` unions, which only evaluate natively on Python 3.10+.
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path


def _load_http():
    spec = importlib.util.spec_from_file_location("_lore_http", Path(__file__).with_name("_http.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_http = _load_http()


def _normalize(url_or_pkg: str) -> str:
    if url_or_pkg.startswith("http"):
        return url_or_pkg
    return f"https://registry.npmjs.org/{url_or_pkg}/latest"


def fetch(url: str, timeout: int = 30) -> tuple[str, str | None]:
    return _http.fetch(_normalize(url), timeout)


def hash_content(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def extract(content: str) -> dict:
    """Return {name, version, description, dependencies}."""
    data = json.loads(content)
    return {
        "name": data.get("name"),
        "version": data.get("version"),
        "description": data.get("description"),
        "dependencies": data.get("dependencies", {}),
    }
