#!/usr/bin/env python3
"""Lore MCP server: read-only access to a Lore knowledge base over MCP stdio.

Exposes four tools to any MCP client (Claude Desktop and others):
  - lore_search(query, limit=5)
  - lore_get_page(slug)
  - lore_list_pages(category=None)
  - lore_fact(slug, path)

Reads $LORE_DIR. If unset, resolves the lore dir next to this script's own
install (<config-dir>/lore, inferred from the standard
<config-dir>/skills/lore/scripts/ layout), else ~/.claude/lore. Never writes.
Implements the MCP stdio JSON-RPC protocol directly with the Python stdlib so
no `pip install` is required to run it.
"""

# Postponed evaluation of annotations: kept for consistency with the other
# shipped scripts and connectors, several of which use PEP 604 `X | Y`
# unions that only evaluate natively on Python 3.10+.
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple


def _default_lore_dir() -> Path:
    """Infer <config-dir>/lore from this script's own install path.

    Standard layout: <config-dir>/skills/lore/scripts/lore-mcp-server.py.
    Falls back to ~/.claude/lore when the script is not installed in that
    layout, or when neither candidate exists yet.
    """
    claude_lore = Path("~/.claude/lore").expanduser()
    try:
        script_path = Path(__file__).resolve()
    except NameError:
        return claude_lore
    scripts_dir = script_path.parent
    if scripts_dir.name != "scripts":
        return claude_lore
    skill_dir = scripts_dir.parent
    if skill_dir.name != "lore":
        return claude_lore
    skills_dir = skill_dir.parent
    if skills_dir.name != "skills":
        return claude_lore
    inferred = skills_dir.parent / "lore"
    if inferred.is_dir():
        return inferred
    return claude_lore if claude_lore.is_dir() else inferred


_env_lore_dir = os.environ.get("LORE_DIR", "").strip()
LORE_DIR = Path(_env_lore_dir).expanduser() if _env_lore_dir else _default_lore_dir()
PROTOCOL_VERSION = "2024-11-05"


def _lore_version() -> str:
    """Read LORE_VERSION from the sibling lore.py (C16), one named constant.

    Loaded standalone by file path, the same way lore.py's own
    _load_connector loads a connector (never a package import: this script
    is invoked directly by Claude Desktop, not installed as part of a
    package), so this works whether or not scripts/ happens to be on
    sys.path. Falls back to a literal if lore.py is missing or fails to
    load, so a version mismatch or an unusual install layout never prevents
    the MCP server from starting.
    """
    try:
        import importlib.util

        spec = importlib.util.spec_from_file_location("_lore_cli", Path(__file__).with_name("lore.py"))
        if spec is None or spec.loader is None:
            return "1.0.0"
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return str(getattr(mod, "LORE_VERSION", "1.0.0"))
    except Exception:
        return "1.0.0"


SERVER_INFO = {"name": "lore", "version": _lore_version()}

TOOLS = [
    {
        "name": "lore_search",
        "description": "Full-text search the Lore knowledge base. Returns top matching pages.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query."},
                "limit": {"type": "integer", "description": "Max results.", "default": 5},
            },
            "required": ["query"],
        },
    },
    {
        "name": "lore_get_page",
        "description": "Return the full content (frontmatter + body) of a Lore page.",
        "inputSchema": {
            "type": "object",
            "properties": {"slug": {"type": "string"}},
            "required": ["slug"],
        },
    },
    {
        "name": "lore_list_pages",
        "description": "List Lore pages, optionally filtered by category.",
        "inputSchema": {
            "type": "object",
            "properties": {"category": {"type": "string"}},
        },
    },
    {
        "name": "lore_fact",
        "description": "Resolve a deterministic fact from extracts/{slug}.json by dotted path.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "slug": {"type": "string"},
                "path": {"type": "string", "description": "Dotted path, e.g. models.opus.context_window"},
            },
            "required": ["slug", "path"],
        },
    },
]


SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")


def valid_slug(slug: str) -> bool:
    """Return True iff `slug` is safe to interpolate into a LORE_DIR-relative path.

    Mirrors lore.py's valid_slug: every tool that builds a path from a
    caller-supplied slug (pages/{slug}.md, extracts/{slug}.json) must reject
    anything this returns False for before touching the filesystem, or a
    slug containing '/' or '..' resolves outside its intended directory.
    """
    if not slug or ".." in slug:
        return False
    return bool(SLUG_RE.match(slug))


def _read_page(slug: str) -> Optional[str]:
    p = LORE_DIR / "pages" / f"{slug}.md"
    if not p.exists():
        return None
    return p.read_text(encoding="utf-8")


def _parse_frontmatter(text: str) -> Tuple[dict, str]:
    """Tiny YAML-ish frontmatter parser (key: value, list items)."""
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end == -1:
        return {}, text
    fm_text = text[4:end]
    body = text[end + 5:]
    meta: dict = {}
    current_key = None
    for line in fm_text.splitlines():
        if not line.strip():
            continue
        if line.startswith("  - "):
            if current_key:
                meta.setdefault(current_key, []).append(line[4:].strip().strip('"'))
            continue
        m = re.match(r'^([a-zA-Z_][a-zA-Z0-9_-]*):\s*(.*)$', line)
        if m:
            k, v = m.group(1), m.group(2).strip()
            current_key = k
            if v == "":
                meta[k] = []
            else:
                meta[k] = v.strip('"')
    return meta, body


def _quote_fts5_tokens(query: str) -> str:
    """Quote every whitespace-separated token for a literal FTS5 MATCH.

    Mirrors lore.py's _quote_fts5_tokens: without quoting, punctuation such
    as a hyphen or '++' inside a token is parsed as FTS5 query syntax
    (e.g. 'acme-cli' becomes the column reference 'cli') instead of literal
    text, so hyphenated or symbol-bearing queries raise sqlite3.Error or
    silently match the wrong thing.
    """
    tokens = query.split()
    if not tokens:
        return '""'
    return " ".join('"' + t.replace('"', '""') + '"' for t in tokens)


def tool_lore_search(query: str, limit: int = 5) -> List[dict]:
    db_path = LORE_DIR / ".search.db"
    if db_path.exists():
        try:
            uri = f"file:{db_path}?mode=ro"
            conn = sqlite3.connect(uri, uri=True)
            cur = conn.cursor()
            cur.execute(
                "SELECT slug, title, snippet(pages, 5, '>>>', '<<<', '...', 16) "
                "FROM pages WHERE pages MATCH ? ORDER BY bm25(pages) LIMIT ?",
                (_quote_fts5_tokens(query), limit),
            )
            rows = cur.fetchall()
            conn.close()
            return [{"slug": r[0], "title": r[1], "snippet": r[2]} for r in rows]
        except sqlite3.Error:
            pass
    # Substring fallback.
    results: List[dict] = []
    pages_dir = LORE_DIR / "pages"
    if not pages_dir.exists():
        return []
    q_low = query.lower()
    for path in sorted(pages_dir.glob("*.md")):
        text = path.read_text(encoding="utf-8", errors="replace")
        if q_low in text.lower():
            meta, body = _parse_frontmatter(text)
            results.append({
                "slug": path.stem,
                "title": meta.get("title", path.stem),
                "snippet": body[:240].replace("\n", " "),
            })
            if len(results) >= limit:
                break
    return results


def tool_lore_get_page(slug: str) -> dict:
    if not valid_slug(slug):
        return {"error": f"invalid slug: {slug}"}
    text = _read_page(slug)
    if text is None:
        return {"error": f"page not found: {slug}"}
    return {"slug": slug, "content": text}


def tool_lore_list_pages(category: Optional[str] = None) -> List[dict]:
    pages_dir = LORE_DIR / "pages"
    if not pages_dir.exists():
        return []
    out: List[dict] = []
    for path in sorted(pages_dir.glob("*.md")):
        text = path.read_text(encoding="utf-8", errors="replace")
        meta, _ = _parse_frontmatter(text)
        if category and meta.get("category") != category:
            continue
        out.append({
            "slug": path.stem,
            "title": meta.get("title", path.stem),
            "category": meta.get("category"),
            "last_verified": meta.get("last_verified"),
        })
    return out


def tool_lore_fact(slug: str, path: str) -> dict:
    if not valid_slug(slug):
        return {"error": f"invalid slug: {slug}"}
    extract_path = LORE_DIR / "extracts" / f"{slug}.json"
    if not extract_path.exists():
        return {"error": f"no extract for slug: {slug}"}
    try:
        data = json.loads(extract_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return {"error": f"extract is invalid JSON: {e}"}
    # Mirrors lore.py's _resolve_dotted_path(): supports dict keys AND list
    # indices (e.g. "items.0.name"), not dict keys alone, so lore_fact and
    # `lore.py fact` resolve the same dotted path to the same value.
    cur = data
    for part in path.split("."):
        if isinstance(cur, dict):
            if part not in cur:
                return {"error": f"path '{path}' not found in extracts/{slug}.json"}
            cur = cur[part]
        elif isinstance(cur, list):
            try:
                idx = int(part)
            except ValueError:
                return {"error": f"path '{path}' not found in extracts/{slug}.json"}
            if idx < 0 or idx >= len(cur):
                return {"error": f"path '{path}' not found in extracts/{slug}.json"}
            cur = cur[idx]
        else:
            return {"error": f"path '{path}' not found in extracts/{slug}.json"}
    return {"slug": slug, "path": path, "value": cur}


def call_tool(name: str, args: dict) -> dict:
    if name == "lore_search":
        return {"results": tool_lore_search(args.get("query", ""), int(args.get("limit", 5)))}
    if name == "lore_get_page":
        return tool_lore_get_page(args.get("slug", ""))
    if name == "lore_list_pages":
        return {"pages": tool_lore_list_pages(args.get("category"))}
    if name == "lore_fact":
        return tool_lore_fact(args.get("slug", ""), args.get("path", ""))
    return {"error": f"unknown tool: {name}"}


def write_response(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def handle(message: dict) -> None:
    method = message.get("method")
    msg_id = message.get("id")
    if method == "initialize":
        write_response({
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": SERVER_INFO,
            },
        })
    elif method == "tools/list":
        write_response({"jsonrpc": "2.0", "id": msg_id, "result": {"tools": TOOLS}})
    elif method == "tools/call":
        params = message.get("params", {}) or {}
        name = params.get("name", "")
        args = params.get("arguments", {}) or {}
        result = call_tool(name, args)
        write_response({
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {"content": [{"type": "text", "text": json.dumps(result, indent=2)}]},
        })
    elif method == "ping":
        write_response({"jsonrpc": "2.0", "id": msg_id, "result": {}})
    elif method and method.startswith("notifications/"):
        # Notifications expect no response.
        return
    else:
        if msg_id is not None:
            write_response({
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {"code": -32601, "message": f"method not found: {method}"},
            })


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        try:
            handle(message)
        except Exception as e:  # noqa: BLE001
            msg_id = message.get("id") if isinstance(message, dict) else None
            if msg_id is not None:
                write_response({
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "error": {"code": -32603, "message": f"internal error: {e}"},
                })


if __name__ == "__main__":
    main()
