"""RSS/Atom feed connector. Returns last 5 items as a plain-text summary.

Fetching goes through the shared _http.py helper; see _http.py's own
docstring for why every connector loads it standalone rather than importing
it as a package.
"""
# Postponed evaluation of annotations: this file's signatures use PEP 604
# `X | Y` unions, which only evaluate natively on Python 3.10+.
from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path
import xml.etree.ElementTree as ET


def _load_http():
    spec = importlib.util.spec_from_file_location("_lore_http", Path(__file__).with_name("_http.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_http = _load_http()


def fetch(url: str, timeout: int = 30) -> tuple[str, str | None]:
    raw, etag = _http.fetch(url, timeout)

    summary_lines = []
    try:
        root = ET.fromstring(raw)
        # RSS 2.0: channel/item; Atom: feed/entry. Handle both.
        items = root.findall(".//item") or root.findall("{http://www.w3.org/2005/Atom}entry")
        for item in items[:5]:
            # `elem_a or elem_b` is wrong here: ElementTree's __bool__ is
            # based on child-element count, not text content or None-ness,
            # so an <item><title>Foo</title></item> (a title with text but
            # no child elements) is falsy and the `or` always fell through
            # to the Atom-namespaced lookup, printing "(no title)" for
            # every plain RSS 2.0 item. Explicit `is not None` checks avoid
            # that gotcha.
            title_el = item.find("title")
            if title_el is None:
                title_el = item.find("{http://www.w3.org/2005/Atom}title")
            link_el = item.find("link")
            if link_el is None:
                link_el = item.find("{http://www.w3.org/2005/Atom}link")
            title = (title_el.text or "").strip() if title_el is not None else "(no title)"
            link = ""
            if link_el is not None:
                link = (link_el.text or link_el.get("href") or "").strip()
            summary_lines.append(f"- {title} :: {link}")
    except ET.ParseError as e:
        raise RuntimeError(f"Feed parse error for {url}: {e}") from e

    content = "\n".join(summary_lines) if summary_lines else "(empty feed)"
    return content, etag


def hash_content(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()
