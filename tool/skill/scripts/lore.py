#!/usr/bin/env python3
"""
Lore CLI - Persistent LLM-maintained knowledge base.

Single-file CLI with argparse subcommands. Zero external dependencies.
Manages a knowledge base of interlinked markdown pages with FTS5 search.
See SKILL.md for the attribution notice this package ships under.

Usage:
    python3 lore.py bootstrap      [--scope global|project] [--lore-dir PATH]
    python3 lore.py index          [--lore-dir PATH]
    python3 lore.py search         <query> [--lore-dir PATH] [--limit N]
    python3 lore.py lint           [--lore-dir PATH] [--check-links]
    python3 lore.py stale          [--lore-dir PATH]
    python3 lore.py log            <message> [--lore-dir PATH]
    python3 lore.py sync           [--lore-dir PATH]
    python3 lore.py check-entity   <name> [--lore-dir PATH]
    python3 lore.py ingest         <source-path> [--lore-dir PATH]
    python3 lore.py refresh        [--lore-dir PATH] [--force] [--slug SLUG]
    python3 lore.py fact           <slug> <dotted.path> [--lore-dir PATH]
    python3 lore.py graph          [--lore-dir PATH]
    python3 lore.py inbox          ["note text"] [--lore-dir PATH]
    python3 lore.py pending        [--json | --summary] [--lore-dir PATH]
    python3 lore.py seen           [names... | --all] [--lore-dir PATH]
    python3 lore.py doctor         [--json] [--offline] [--lore-dir PATH]
    python3 lore.py review list    [--lore-dir PATH]
    python3 lore.py review clear   <slug> [--lore-dir PATH]
    python3 lore.py review clear   --propagated [--dry-run] [--lore-dir PATH]
    python3 lore.py prune          [--failed-older-than N] [--orphan-hashes]
                                    [--run-logs-older-than N] [--dry-run] [--lore-dir PATH]
"""

# Postponed evaluation of annotations: kept for consistency with the other
# shipped scripts and connectors (several of which use PEP 604 `X | Y`
# unions that only evaluate natively on Python 3.10+); harmless here since
# this file's own annotations already use typing.Optional throughout.
from __future__ import annotations

import argparse
import difflib
import hashlib
import importlib
import importlib.util
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import traceback
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# C16: a single named version constant, referenced by --version below, by
# doctor's report, and by lore-mcp-server.py's serverInfo.version (loaded
# from this file rather than duplicated as its own literal).
LORE_VERSION = "1.0.0"

# Global lore dir resolution: prefer the lore dir that sits next to this
# script's own install (<config-dir>/lore, inferred from the standard
# <config-dir>/skills/lore/scripts/lore.py layout used by the install docs),
# else ~/.claude/lore. See _script_relative_lore_dir() and
# default_global_lore_dir() below.

def _script_relative_lore_dir() -> Optional[Path]:
    """Infer <config-dir>/lore from this script's own install path.

    Standard layout: <config-dir>/skills/lore/scripts/lore.py. Returns None
    when the script is not installed in that layout (run from an arbitrary
    checkout, a flattened copy, etc.), so callers fall back to a fixed
    default instead.
    """
    try:
        script_path = Path(__file__).resolve()
    except NameError:
        return None
    scripts_dir = script_path.parent
    if scripts_dir.name != "scripts":
        return None
    skill_dir = scripts_dir.parent
    if skill_dir.name != "lore":
        return None
    skills_dir = skill_dir.parent
    if skills_dir.name != "skills":
        return None
    return skills_dir.parent / "lore"


def _skill_root_dir() -> Path:
    """Return this script's own skill root: the dir holding scripts/, connectors/, etc.

    Works the same from a repo checkout (skill/scripts/lore.py -> skill/)
    and from an installed layout (<config-dir>/skills/lore/scripts/lore.py
    -> <config-dir>/skills/lore/): both put lore.py exactly two levels
    under the skill root, so callers that need a sibling asset shipped next
    to this script (lint-config.default.json, connectors/) can find it
    without depending on which of the two layouts is in play.
    """
    return Path(__file__).resolve().parent.parent


def default_global_lore_dir() -> Path:
    """Return the global lore dir.

    Preference order: the lore dir inferred from this script's own install
    path if it already exists, then ~/.claude/lore if it already exists,
    then the inferred path again (even if missing, so `bootstrap` creates it
    next to the skill that manages it), falling back to ~/.claude/lore when
    the script is not installed in the standard layout.
    """
    inferred = _script_relative_lore_dir()
    claude_lore = Path("~/.claude/lore").expanduser()
    if inferred is not None and inferred.is_dir():
        return inferred
    if claude_lore.is_dir():
        return claude_lore
    return inferred if inferred is not None else claude_lore

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
# Matches only the closing '---' delimiter line itself (optional trailing
# spaces/tabs, then exactly one newline). Used by set_frontmatter_field to
# find where the body actually starts without FRONTMATTER_RE's greedy
# trailing `\s*`, which also swallows any blank line(s) that follow the
# closing '---' (see set_frontmatter_field's docstring).
_FRONTMATTER_CLOSING_LINE_RE = re.compile(r"\n---[ \t]*\n")
CROSSREF_RE = re.compile(r"\[\[([^\]]+)\]\]")
FENCED_CODE_RE = re.compile(r"```.*?```", re.DOTALL)
INLINE_CODE_RE = re.compile(r"`[^`\n]*`")


def strip_code(text: str) -> str:
    """Remove fenced and inline code blocks so wikilink-like syntax inside code is not scanned."""
    return INLINE_CODE_RE.sub("", FENCED_CODE_RE.sub("", text))

SCHEMA_TEMPLATE = textwrap.dedent("""\
    # Lore Schema

    This knowledge base is maintained by your AI agent. It stores verified technical facts
    that are preferred over training data when answering questions.

    ## Structure

    - `index.md` - Compact catalog of all pages (one line each)
    - `log.md` - Chronological record of all changes
    - `schema.md` - This file (conventions and rules)
    - `pages/` - LLM-maintained knowledge pages
    - `sources/` - Raw source documents (immutable after ingest)
    - `queue/` - Machine-generated diff records awaiting LLM review
    - `inbox/` - Quick free-text notes awaiting triage into pages
    - `failed/` - Refresh failure records (mark reviewed with `lore.py seen`)
    - `.search.db` - SQLite FTS5 index (derived, safe to delete and rebuild)

    ## Categories

    | Category | Description |
    |----------|-------------|
    | api-reference | API models, endpoints, SDKs, pricing |
    | best-practices | Current recommended approaches |
    | deprecations | Old patterns mapped to new replacements |
    | tooling | Build tools, package managers, CLIs |
    | infrastructure | Servers, services, deployment |
    | custom | User-defined topics |

    ## Maintenance Rules

    1. Never delete a page without logging the reason
    2. When updating facts, update last_verified in frontmatter
    3. Sources are immutable - never modify files in sources/
    4. Index must be rebuilt after any page add/remove/rename
    5. Log every ingest, refresh, and significant edit
""")

# The AWS and GitHub token prefixes below are split across a string
# concatenation so this scanner's own source never contains either literal
# prefix contiguously (both are also entries in scripts/privacy-grep.sh's
# pattern list, which would otherwise flag this file for containing the very
# prefixes it exists to detect). The compiled regex is unaffected: Python
# concatenates adjacent string literals before re.compile ever sees them.
CREDENTIAL_PATTERNS_STRICT = [
    (re.compile(r"sk-[a-zA-Z0-9]{20,}"), "OpenAI-style API key"),
    (re.compile(r"AK" r"IA[A-Z0-9]{16}"), "AWS access key"),
    (re.compile(r"gh" r"p_[a-zA-Z0-9]{36}"), "GitHub personal access token"),
    (re.compile(r"Bearer\s+[a-zA-Z0-9._-]{20,}"), "bearer token"),
]

CREDENTIAL_PATTERNS_LOOSE = [
    (re.compile(r"password\s*[:=]\s*['\"]?[A-Za-z0-9!@#%^&*+_.-]{6,}['\"]?", re.IGNORECASE), "inline password"),
]

# C15: a fresh data dir's own .gitignore, so `git init` (or a data dir kept
# under version control) doesn't accidentally track machine-generated churn:
# the queue/failed dirs the gardener writes to every refresh, macOS's
# .DS_Store, backup files (.bak-*), the runtime env file (.lore.env, may hold
# secrets like alert webhook URLs), gardener run logs and launchd logs, the
# refresh stderr log, the refresh lockdir, the pause-file sentinel, and (C19)
# the previous-payload cache refresh diffs against.
GITIGNORE_CONTENT = (
    ".search.db\n"
    "__pycache__/\n"
    "*.pyc\n"
    ".refresh.log\n"
    ".watchdog.log\n"
    "queue/\n"
    "failed/\n"
    ".DS_Store\n"
    "*.bak*\n"
    ".lore.env\n"
    ".gardener/run-*.jsonl\n"
    ".gardener/launchd.*.log\n"
    ".refresh.stderr.log\n"
    ".lore.lock/\n"
    ".paused\n"
    ".refresh-cache/\n"
)

LOG_HEADER = "# Lore Log\n\nChronological record of all lore operations.\n"

INDEX_HEADER = "# Lore Index\n\nLast rebuilt: never\n"

# Fallback set of first-column table labels the contradiction check (Check 7)
# never treats as a fact worth comparing across pages, because the same
# generic label legitimately means something different on every page that
# uses it (a "Customer" row on one page and another have no relation to each
# other). Bootstrap copies lint-config.default.json into a fresh lore dir as
# $LORE_DIR/lint-config.json ({"contradiction_generic_keys": [...]}), which
# is the set actually used; this constant is only the last-resort default
# for a lore dir that predates that file or has it missing/malformed.
LINT_GENERIC_KEYS_DEFAULT = {
    "customer", "public ip", "work item board", "key file on server", "company infra",
    "vendor partner programs", "ide license", "anthropic", "openai",
    "google", "mistral", "**bun**",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def resolve_lore_dir(explicit: Optional[str]) -> Path:
    """Resolve lore directory.

    Order: explicit --lore-dir arg > $LORE_DIR env var > {cwd}/lore if it exists >
    first existing global candidate > final portable fallback (even if missing,
    so bootstrap can create it).
    """
    if explicit:
        return Path(explicit).expanduser().resolve()
    env_dir = os.environ.get("LORE_DIR", "").strip()
    if env_dir:
        return Path(env_dir).expanduser().resolve()
    cwd_lore = Path.cwd() / "lore"
    if cwd_lore.is_dir():
        return cwd_lore.resolve()
    return default_global_lore_dir()


def ok(msg: str) -> None:
    """Print success message to stdout and exit 0."""
    print(f"OK: {msg}")


def error(msg: str, code: int = 1) -> None:
    """Print error message to stderr and exit with code."""
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")


def valid_slug(slug: str) -> bool:
    """Return True iff `slug` is safe to interpolate into a lore-dir-relative path.

    Every caller that builds a path from a user-supplied slug (pages/{slug}.md,
    extracts/{slug}.json) must reject anything this returns False for, before
    touching the filesystem: an unvalidated slug containing '/' or '..'
    resolves outside its intended directory (e.g. `fact ../outside a` reading
    a file above extracts/). The character class already excludes '/' and a
    leading '.', but '..' is also rejected explicitly as defense in depth.
    """
    if not slug or ".." in slug:
        return False
    return bool(SLUG_RE.match(slug))


def now_iso() -> str:
    """Return current UTC time in ISO-8601 format."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def today_iso() -> str:
    """Return today's date in ISO-8601 format."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def parse_frontmatter(text: str) -> Tuple[Dict[str, Any], str]:
    """Parse YAML-like frontmatter from markdown text.

    Returns (metadata dict, body text after frontmatter).
    Handles flat key: value pairs and list items (  - value).
    """
    match = FRONTMATTER_RE.match(text)
    if not match:
        return {}, text

    raw = match.group(1)
    body = text[match.end():]
    meta: Dict[str, Any] = {}
    current_key: Optional[str] = None

    for line in raw.split("\n"):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        # List item continuation
        if line.startswith("  - ") and current_key is not None:
            item = line[4:].strip().strip('"').strip("'")
            if not isinstance(meta[current_key], list):
                meta[current_key] = []
            meta[current_key].append(item)
            continue

        # Key: value pair
        colon_idx = stripped.find(":")
        if colon_idx == -1:
            continue

        key = stripped[:colon_idx].strip()
        value = stripped[colon_idx + 1:].strip().strip('"').strip("'")
        current_key = key

        if value:
            # Coerce booleans
            if value.lower() == "true":
                meta[key] = True
            elif value.lower() == "false":
                meta[key] = False
            else:
                meta[key] = value
        else:
            # Empty value, might be start of a list
            meta[key] = []

    return meta, body


def parse_refresh_interval(interval_str: str) -> timedelta:
    """Parse refresh interval like '5d', '30d' into timedelta. Default 30d."""
    if not interval_str:
        return timedelta(days=30)
    m = re.match(r"^(\d+)d$", interval_str.strip())
    if m:
        return timedelta(days=int(m.group(1)))
    return timedelta(days=30)


def scan_pages(lore_dir: Path) -> List[Tuple[str, Dict[str, Any], str]]:
    """Scan all pages/*.md files and return list of (slug, meta, body)."""
    pages_dir = lore_dir / "pages"
    if not pages_dir.is_dir():
        return []

    results = []
    for md_file in sorted(pages_dir.glob("*.md")):
        slug = md_file.stem
        text = md_file.read_text(encoding="utf-8")
        meta, body = parse_frontmatter(text)
        results.append((slug, meta, body))

    return results


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

def cmd_bootstrap(args: argparse.Namespace) -> None:
    """Create lore directory structure with initial files."""
    if args.lore_dir:
        lore_dir = Path(args.lore_dir).expanduser().resolve()
    elif args.scope == "project":
        lore_dir = (Path.cwd() / "lore").resolve()
    else:
        env_dir = os.environ.get("LORE_DIR", "").strip()
        lore_dir = Path(env_dir).expanduser().resolve() if env_dir else default_global_lore_dir()

    # Create directory tree
    (lore_dir / "pages").mkdir(parents=True, exist_ok=True)
    (lore_dir / "sources").mkdir(parents=True, exist_ok=True)
    (lore_dir / "extracts").mkdir(parents=True, exist_ok=True)
    (lore_dir / "connectors").mkdir(parents=True, exist_ok=True)
    (lore_dir / "failed").mkdir(parents=True, exist_ok=True)
    (lore_dir / "queue").mkdir(parents=True, exist_ok=True)
    (lore_dir / "inbox").mkdir(parents=True, exist_ok=True)

    # Seed connectors/ from the shipped skill/connectors/ if the data dir's
    # own copy is empty (task item 11): before this, bootstrap created an
    # empty connectors/ directory and refresh had nothing to dispatch to
    # until a user copied the connectors in by hand. Only seeds when empty,
    # so re-running bootstrap on an existing lore dir never overwrites a
    # user's own customized connector.
    connectors_dir = lore_dir / "connectors"
    if not any(connectors_dir.iterdir()):
        shipped_connectors_dir = _skill_root_dir() / "connectors"
        if shipped_connectors_dir.is_dir():
            for src in shipped_connectors_dir.glob("*.py"):
                shutil.copy2(src, connectors_dir / src.name)

    # Write schema.md
    schema_path = lore_dir / "schema.md"
    if not schema_path.exists():
        schema_path.write_text(SCHEMA_TEMPLATE, encoding="utf-8")

    # Write log.md
    log_path = lore_dir / "log.md"
    if not log_path.exists():
        log_path.write_text(LOG_HEADER, encoding="utf-8")

    # Write index.md
    index_path = lore_dir / "index.md"
    if not index_path.exists():
        index_path.write_text(INDEX_HEADER, encoding="utf-8")

    # Write .gitignore
    gitignore_path = lore_dir / ".gitignore"
    if not gitignore_path.exists():
        gitignore_path.write_text(GITIGNORE_CONTENT, encoding="utf-8")

    # Initialize .hashes.json as empty dict if not present
    hashes_path = lore_dir / ".hashes.json"
    if not hashes_path.exists():
        hashes_path.write_text("{}\n", encoding="utf-8")

    # Initialize .graph.json as empty graph if not present
    graph_path = lore_dir / ".graph.json"
    if not graph_path.exists():
        empty_graph = {"built": now_iso(), "nodes": [], "edges": []}
        graph_path.write_text(json.dumps(empty_graph, indent=2) + "\n", encoding="utf-8")

    # Seed lint-config.json from the shipped default if not present, so a
    # fresh lore dir gets the same contradiction-check generic-key allowlist
    # as _load_lint_generic_keys's built-in fallback, but as an editable file.
    lint_config_path = lore_dir / "lint-config.json"
    if not lint_config_path.exists():
        default_lint_config = _skill_root_dir() / "lint-config.default.json"
        if default_lint_config.is_file():
            lint_config_path.write_text(
                default_lint_config.read_text(encoding="utf-8"), encoding="utf-8"
            )
        else:
            lint_config_path.write_text(
                json.dumps(
                    {"contradiction_generic_keys": sorted(LINT_GENERIC_KEYS_DEFAULT)},
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )

    ok(f"Lore initialized at {lore_dir}")


def _parse_date_safe(date_str: str) -> Optional[datetime]:
    """Parse an ISO date string YYYY-MM-DD into a date object, or return None on failure."""
    if not date_str:
        return None
    try:
        return datetime.strptime(str(date_str).strip(), "%Y-%m-%d")
    except ValueError:
        return None


def _propagate_needs_review_to_dependents(
    lore_dir: Path,
    graph: Dict[str, Any],
    changed_slugs: Set[str],
) -> int:
    """Flag pages that depend, via a graph edge, on a slug refreshed this run.

    A page P depends on page Q when there is a directed edge P -> Q in the
    graph (P links to Q). When Q's own source changed in THIS refresh run
    (Q is in changed_slugs), P may now describe stale facts and is flagged
    too.

    Gating this on the current run's own changes, rather than on any
    last_verified/hash timestamp difference, is what stops the flag count
    from growing on every unrelated `index` rebuild: bumping one page's
    last_verified used to re-flag its entire dependent subgraph every time
    `index` ran, even when nothing about the dependency's content had
    changed since the last flag was cleared.

    Returns the count of pages newly marked (pages already carrying
    needs_review: true are left alone, so re-running this is idempotent).
    """
    if not changed_slugs:
        return 0
    n_marked = 0
    seen_p_slugs: Set[str] = set()
    for edge in graph.get("edges", []):
        p_slug = edge.get("from", "")
        q_slug = edge.get("to", "")
        if not p_slug or q_slug not in changed_slugs or p_slug in seen_p_slugs:
            continue
        seen_p_slugs.add(p_slug)
        page_path = lore_dir / "pages" / f"{p_slug}.md"
        if not page_path.is_file():
            continue
        meta, _body = parse_frontmatter(page_path.read_text(encoding="utf-8"))
        if meta.get("needs_review"):
            continue
        set_frontmatter_field(page_path, "needs_review", True)
        n_marked += 1
    return n_marked


def _clear_propagated_needs_review(
    lore_dir: Path,
    pages: List[Tuple[str, Dict[str, Any], str]],
    dry_run: bool = False,
) -> int:
    """Clear needs_review on pages flagged only because a dependency changed.

    A page is eligible when ALL of the following hold:
      - needs_review is currently true;
      - auto_update is not true (an auto-update page's own source may still
        be the unresolved reason it was flagged, so it is left for
        `refresh` to resolve, not cleared here);
      - no file in queue/ names this slug (an unreconciled diff for the
        page's own source);
      - no failed/<slug>-*.err record is newer than the page's
        last_verified (an unresolved fetch failure for the page's own
        source). A page with no last_verified at all cannot prove any
        existing failure record predates it, so any failure record blocks
        clearing in that case.

    Returns the count of pages cleared (or that would be cleared, when
    dry_run is true).
    """
    queue_dir = lore_dir / "queue"
    failed_dir = lore_dir / "failed"
    n_cleared = 0
    for slug, meta, _body in pages:
        if not meta.get("needs_review"):
            continue
        if meta.get("auto_update"):
            continue
        if queue_dir.is_dir() and any(queue_dir.glob(f"{slug}-*.diff.md")):
            continue
        last_verified = _parse_date_safe(meta.get("last_verified", ""))
        blocked = False
        if failed_dir.is_dir():
            for err_path in failed_dir.glob(f"{slug}-*.err"):
                if last_verified is None:
                    blocked = True
                    break
                err_date = datetime.fromtimestamp(
                    err_path.stat().st_mtime, tz=timezone.utc
                ).date()
                if err_date > last_verified.date():
                    blocked = True
                    break
        if blocked:
            continue
        if not dry_run:
            page_path = lore_dir / "pages" / f"{slug}.md"
            if page_path.is_file():
                set_frontmatter_field(page_path, "needs_review", False)
        n_cleared += 1
    return n_cleared


def cmd_index(args: argparse.Namespace) -> None:
    """Rebuild index.md and .search.db from pages/*.md files."""
    lore_dir = resolve_lore_dir(args.lore_dir)

    if not lore_dir.is_dir():
        error(f"Lore directory not found: {lore_dir}")

    pages = scan_pages(lore_dir)

    # Rebuild index.md
    lines = [
        "# Lore Index",
        "",
        f"Last rebuilt: {now_iso()}",
        "",
        "| Page | Category | Confidence | Auto-Update | Last Verified |",
        "|------|----------|------------|-------------|---------------|",
    ]
    for slug, meta, _body in pages:
        title = meta.get("title", slug)
        category = meta.get("category", "")
        confidence = meta.get("confidence", "")
        auto_update = "yes" if meta.get("auto_update") else "no"
        last_verified = meta.get("last_verified", "")
        lines.append(
            f"| [{title}](pages/{slug}.md) | {category} | {confidence} | {auto_update} | {last_verified} |"
        )

    index_path = lore_dir / "index.md"
    index_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # Rebuild .search.db (SQLite FTS5)
    db_path = lore_dir / ".search.db"
    try:
        conn = sqlite3.connect(str(db_path))
        cur = conn.cursor()
        cur.execute("DROP TABLE IF EXISTS pages")
        cur.execute(
            "CREATE VIRTUAL TABLE pages USING fts5("
            "slug, title, category, tags, confidence, content, "
            "tokenize='porter unicode61'"
            ")"
        )
        for slug, meta, body in pages:
            title = meta.get("title", slug)
            category = meta.get("category", "")
            tags_list = meta.get("tags", [])
            tags = ", ".join(tags_list) if isinstance(tags_list, list) else str(tags_list)
            confidence = meta.get("confidence", "")
            cur.execute(
                "INSERT INTO pages (slug, title, category, tags, confidence, content) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (slug, title, category, tags, confidence, body),
            )
        conn.commit()
        conn.close()
    except Exception as exc:
        error(f"Failed to build search index: {exc}")

    # Auto-generate entities.json if it doesn't exist or is empty
    entities_path = lore_dir / "entities.json"
    _maybe_generate_entities(entities_path, pages)

    # Build .graph.json from wikilinks
    graph = build_graph(lore_dir, pages)
    n_nodes = len(graph["nodes"])
    n_edges = len(graph["edges"])

    # needs_review propagation to dependents happens in `refresh`, gated on
    # the current run's own diffs (see _propagate_needs_review_to_dependents).
    # `index` only rebuilds the graph/search artifacts; it never re-flags
    # pages on its own, since a page's last_verified alone says nothing
    # about whether anything it depends on has actually changed.
    n_marked = 0

    ok(
        f"Index rebuilt. {len(pages)} pages indexed. "
        f"Graph: {n_nodes} nodes, {n_edges} edges. "
        f"Marked {n_marked} pages as needs_review."
    )


def cmd_search(args: argparse.Namespace) -> None:
    """Search lore pages using FTS5 or fallback substring match."""
    lore_dir = resolve_lore_dir(args.lore_dir)
    query = args.query
    limit = args.limit

    if not lore_dir.is_dir():
        error(f"Lore directory not found: {lore_dir}")

    db_path = lore_dir / ".search.db"

    # Try FTS5 search first
    if db_path.is_file():
        try:
            results = _search_fts5(db_path, query, limit)
            if results is not None:
                _print_search_results(results, query)
                return
        except Exception:
            pass  # Fall through to substring search

    # Fallback: substring search across page files
    results = _search_substring(lore_dir, query, limit)
    _print_search_results(results, query)


def _quote_fts5_tokens(query: str) -> str:
    """Quote every whitespace-separated token for a literal FTS5 MATCH.

    FTS5 treats hyphens, plus signs and other punctuation inside an
    unquoted query as query-syntax operators, so a raw 'acme-cli' is parsed
    as the column reference 'cli' and 'C++' raises a syntax error. Wrapping
    each token in double quotes (doubling any embedded quote) forces it to
    match as literal text instead. Tokens are still implicitly ANDed by
    FTS5's default query syntax, so multi-word queries behave the same as
    before quoting was added.
    """
    tokens = query.split()
    if not tokens:
        return '""'
    return " ".join('"' + t.replace('"', '""') + '"' for t in tokens)


def _search_fts5(
    db_path: Path, query: str, limit: int
) -> Optional[List[Tuple[str, str, str, float]]]:
    """Run FTS5 MATCH query. Returns list of (slug, title, snippet, rank) or None on error."""
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT slug, title, snippet(pages, 5, '>>>', '<<<', '...', 32), bm25(pages) "
            "FROM pages WHERE pages MATCH ? ORDER BY bm25(pages) LIMIT ?",
            (_quote_fts5_tokens(query), limit),
        )
        rows = cur.fetchall()
        conn.close()
        return rows
    except sqlite3.OperationalError:
        conn.close()
        return None


def _search_substring(
    lore_dir: Path, query: str, limit: int
) -> List[Tuple[str, str, str, float]]:
    """Simple case-insensitive substring search across page files."""
    query_lower = query.lower()
    results: List[Tuple[str, str, str, float]] = []

    pages = scan_pages(lore_dir)
    for slug, meta, body in pages:
        title = meta.get("title", slug)
        full_text = (title + " " + body).lower()
        if query_lower in full_text:
            # Extract a rough snippet around the match
            idx = full_text.find(query_lower)
            start = max(0, idx - 40)
            end = min(len(full_text), idx + len(query_lower) + 40)
            snippet = "..." + full_text[start:end] + "..."
            results.append((slug, title, snippet, 0.0))
            if len(results) >= limit:
                break

    return results


def _print_search_results(
    results: List[Tuple[str, str, str, float]], query: str
) -> None:
    """Print formatted search results."""
    if not results:
        ok(f"No pages matching '{query}'")
        return

    for i, (slug, title, snippet, _rank) in enumerate(results, 1):
        print(f"[{i}] {title} (pages/{slug}.md)")
        # Extract category from snippet context if available
        print(f"    {snippet.strip()}")
        print()


def cmd_lint(args: argparse.Namespace) -> None:
    """Check lore pages for common issues with severity-tiered output."""
    lore_dir = resolve_lore_dir(args.lore_dir)
    check_links: bool = getattr(args, "check_links", False)

    if not lore_dir.is_dir():
        error(f"Lore directory not found: {lore_dir}")

    # Each issue is a tuple of (severity, message) where severity is 0=ERROR, 1=WARN, 2=INFO
    SEV_ERROR, SEV_WARN, SEV_INFO = 0, 1, 2
    SEV_PREFIX = {SEV_ERROR: "[ERROR]", SEV_WARN: "[WARN]", SEV_INFO: "[INFO]"}

    issues: List[Tuple[int, str]] = []
    pages = scan_pages(lore_dir)
    page_slugs = {slug for slug, _, _ in pages}

    required_fields = ["title", "category", "last_verified", "sources"]

    # Check 1: Missing required frontmatter [ERROR]
    for slug, meta, _body in pages:
        for field in required_fields:
            if field not in meta or not meta[field]:
                issues.append(
                    (SEV_ERROR, f"[missing-field] pages/{slug}.md: missing '{field}'")
                )

    # Check 1b: Oversized pages (pages hold state, not changelogs) [WARN]
    for slug, _meta, body in pages:
        n_lines = body.count("\n") + 1
        if n_lines > 200:
            issues.append(
                (SEV_WARN, f"[oversized] pages/{slug}.md: {n_lines} body lines (cap 200; curate on next touch, move chronicles to project lore)")
            )

    # Check 2: Pages not listed in index.md [WARN]
    index_path = lore_dir / "index.md"
    index_slugs: set = set()
    if index_path.is_file():
        index_text = index_path.read_text(encoding="utf-8")
        for m in re.finditer(r"\(pages/([^)]+)\.md\)", index_text):
            index_slugs.add(m.group(1))

        for slug in page_slugs:
            if slug not in index_slugs:
                issues.append(
                    (SEV_WARN, f"[orphan-page] pages/{slug}.md: not in index.md")
                )

    # Check 3: Index entries pointing to missing files [WARN]
    for idx_slug in index_slugs:
        if idx_slug not in page_slugs:
            issues.append(
                (SEV_WARN, f"[missing-page] index.md references pages/{idx_slug}.md but file not found")
            )

    # Check 4: auto_update=true but empty last_verified [WARN]
    for slug, meta, _body in pages:
        if meta.get("auto_update") and not meta.get("last_verified"):
            issues.append(
                (SEV_WARN, f"[auto-no-date] pages/{slug}.md: auto_update=true but last_verified is empty")
            )

    # Check 5: Stale auto_update pages [WARN]
    today = datetime.now(timezone.utc).date()
    for slug, meta, _body in pages:
        if not meta.get("auto_update"):
            continue
        last_str = meta.get("last_verified", "")
        if not last_str:
            continue
        try:
            last_date = datetime.strptime(str(last_str), "%Y-%m-%d").date()
        except ValueError:
            issues.append(
                (SEV_WARN, f"[bad-date] pages/{slug}.md: invalid last_verified '{last_str}'")
            )
            continue
        interval = parse_refresh_interval(meta.get("refresh_interval", "30d"))
        if last_date + interval < today:
            issues.append(
                (SEV_WARN,
                 f"[stale] pages/{slug}.md: last verified {last_str}, overdue by "
                 f"{(today - last_date - interval).days} days")
            )

    # Check 6: Broken [[cross-refs]] [WARN]
    for slug, _meta, body in pages:
        for ref_match in CROSSREF_RE.finditer(strip_code(body)):
            ref = ref_match.group(1)
            # Cross-scope refs ([[global:X]] or [[project:X]]) point outside the current
            # scope by design; the current lint cannot validate them without loading the
            # other scope, so skip them here.
            if ":" in ref:
                continue
            if ref not in page_slugs:
                issues.append(
                    (SEV_WARN, f"[broken-ref] pages/{slug}.md: [[{ref}]] points to non-existent page")
                )

    # Check 7: Contradiction detection [WARN]
    issues.extend(_check_contradictions(pages, generic_keys=_load_lint_generic_keys(lore_dir)))

    # Check 8: Dead external link checking [WARN] (opt-in via --check-links)
    if check_links:
        issues.extend(_check_dead_links(pages, lore_dir))

    # Check 9: Credential leak detection [ERROR]
    issues.extend(_check_credential_leaks(pages))

    # Check 10: Data gaps [INFO] - wikilink targets without their own page
    all_ref_targets: set = set()
    for _slug, _meta, body in pages:
        for ref_match in CROSSREF_RE.finditer(strip_code(body)):
            ref = ref_match.group(1)
            # Skip cross-scope refs (see Check 6 comment).
            if ":" in ref:
                continue
            all_ref_targets.add(ref)

    for target in sorted(all_ref_targets):
        if target not in page_slugs:
            issues.append(
                (SEV_INFO, f"[data-gap] [[{target}]] is referenced but has no page yet")
            )

    # Check 11: Missing .graph.json [WARN]
    graph_path = lore_dir / ".graph.json"
    if not graph_path.is_file():
        issues.append(
            (SEV_WARN, "[missing-graph] .graph.json does not exist. Run `lore.py index` (or `lore.py graph`) to build it.")
        )

    # Check 12: Pages with needs_review: true [WARN]
    for slug, meta, _body in pages:
        if meta.get("needs_review"):
            issues.append(
                (SEV_WARN, f"[needs-review] pages/{slug}.md: needs_review is set. Re-verify this page and clear the flag.")
            )

    # Check 13: auto_update pages with source URLs missing from .hashes.json [WARN]
    hashes_path = lore_dir / ".hashes.json"
    hashes_data: Dict[str, Any] = {}
    if hashes_path.is_file():
        try:
            hashes_data = json.loads(hashes_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, ValueError):
            hashes_data = {}
    for slug, meta, _body in pages:
        if not meta.get("auto_update"):
            continue
        sources_list = meta.get("sources", [])
        if isinstance(sources_list, str):
            sources_list = [sources_list]
        if not isinstance(sources_list, list):
            continue
        for url in sources_list:
            url = str(url).strip()
            if not url.startswith(("http://", "https://", "git@", "git://", "git+")):
                continue
            entry = hashes_data.get(url)
            # Failure-only entries (quarantine bookkeeping) carry no sha256 yet and
            # still count as "never successfully fetched".
            if not isinstance(entry, dict) or "sha256" not in entry:
                issues.append(
                    (SEV_WARN,
                     f"[missing-hashes] pages/{slug}.md: source URL not in .hashes.json: {url}. "
                     f"Run `lore.py refresh --slug {slug} --force`.")
                )

    # Check 14: Stale failed/ entries older than 24h [INFO]. A record the
    # gardener (or `lore.py seen`) has already reviewed carries a {name}.seen
    # marker (see _list_unseen_failures) and is no longer "stale to look at":
    # it is reviewed, just not yet pruned. Only unseen records are noise.
    failed_dir = lore_dir / "failed"
    if failed_dir.is_dir():
        now_ts = datetime.now(timezone.utc).timestamp()
        for err_file in _list_unseen_failures(failed_dir):
            try:
                file_mtime = err_file.stat().st_mtime
            except OSError:
                continue
            age_hours = (now_ts - file_mtime) / 3600
            if age_hours > 24:
                issues.append(
                    (SEV_INFO,
                     f"[stale-failures] failed/{err_file.name}: unreviewed refresh failure is {age_hours:.0f}h old. Investigate or `lore.py seen`.")
                )

    # Check 15: Orphan .hashes.json entries [INFO]. `cmd_refresh` drops these
    # automatically as it encounters them (C5); this check surfaces any left
    # over from a .hashes.json edited by hand, or from a refresh that never
    # ran after a page's sources list changed.
    for url in _find_orphan_hash_urls(pages, hashes_data):
        issues.append(
            (SEV_INFO,
             f"[orphan-hash] .hashes.json: {url} is on no page's sources list. Run `lore.py prune --orphan-hashes`.")
        )

    # Sort by severity (errors first, then warnings, then info)
    issues.sort(key=lambda x: x[0])

    # Count severities
    n_errors = sum(1 for sev, _ in issues if sev == SEV_ERROR)
    n_warnings = sum(1 for sev, _ in issues if sev == SEV_WARN)
    n_info = sum(1 for sev, _ in issues if sev == SEV_INFO)

    if issues:
        print("Lint report:\n")
        for sev, msg in issues:
            print(f"  {SEV_PREFIX[sev]} {msg}")
        print()

    print(f"Lint complete. {n_errors} errors, {n_warnings} warnings, {n_info} info.")


def _load_lint_generic_keys(lore_dir: Path) -> Set[str]:
    """Load the contradiction-check generic-key allowlist for this lore dir.

    Reads $LORE_DIR/lint-config.json ({"contradiction_generic_keys": [...]}),
    the file bootstrap copies in from lint-config.default.json. Falls back to
    LINT_GENERIC_KEYS_DEFAULT when the file is missing or malformed, so lint
    still runs on a lore dir bootstrapped before this file existed.
    """
    config_path = lore_dir / "lint-config.json"
    if config_path.is_file():
        try:
            data = json.loads(config_path.read_text(encoding="utf-8"))
            keys = data.get("contradiction_generic_keys")
            if isinstance(keys, list):
                return {str(k).strip().lower() for k in keys}
        except (json.JSONDecodeError, ValueError):
            pass
    return set(LINT_GENERIC_KEYS_DEFAULT)


def _parse_markdown_tables(body: str) -> List[Tuple[str, List[List[str]]]]:
    """Split a page body into its markdown tables.

    Returns one (header_signature, data_rows) pair per table found. A table
    is recognized structurally: a '|'-delimited row immediately followed by
    a '|'-delimited separator row (cells matching only dashes/colons), then
    zero or more further '|'-delimited rows. header_signature is the '|'
    joined, lowercased header cell text, so two tables only compare as "the
    same shape" when their columns actually match; data_rows excludes both
    the header and the separator row.
    """
    lines = body.splitlines()
    row_re = re.compile(r"^\s*\|(.+)\|\s*$")
    sep_cell_re = re.compile(r"^[-:]+$")
    tables: List[Tuple[str, List[List[str]]]] = []

    i = 0
    n = len(lines)
    while i < n - 1:
        header_match = row_re.match(lines[i])
        sep_match = row_re.match(lines[i + 1]) if header_match else None
        if not header_match or not sep_match:
            i += 1
            continue
        sep_cells = [c.strip() for c in sep_match.group(1).split("|")]
        sep_cells = [c for c in sep_cells if c]
        if not sep_cells or not all(sep_cell_re.match(c) for c in sep_cells):
            i += 1
            continue
        header_cells = [c.strip() for c in header_match.group(1).split("|")]
        header_sig = "|".join(c.lower() for c in header_cells)
        data_rows: List[List[str]] = []
        j = i + 2
        while j < n:
            row_match = row_re.match(lines[j])
            if not row_match:
                break
            data_rows.append([c.strip() for c in row_match.group(1).split("|")])
            j += 1
        tables.append((header_sig, data_rows))
        i = j

    return tables


def _check_contradictions(
    pages: List[Tuple[str, Dict[str, Any], str]],
    generic_keys: Optional[Set[str]] = None,
) -> List[Tuple[int, str]]:
    """Scan pages for potential contradictions in table data and bold-ID patterns."""
    SEV_WARN = 1
    issues: List[Tuple[int, str]] = []
    if generic_keys is None:
        generic_keys = LINT_GENERIC_KEYS_DEFAULT

    # Strategy 1: fact-table rows, compared only within tables shaped the
    # same way (identical header) across pages. A 2-cell row is a
    # label/value pair (e.g. a single "Email: a@b" line), not a fact with
    # its own attributes, so it is skipped outright: two unrelated 2-column
    # tables that both happen to start a row with "Email" are not a
    # contradiction, they are just two different label/value tables.
    entity_rows: Dict[Tuple[str, str], List[Tuple[str, str]]] = {}

    for slug, _meta, body in pages:
        for header_sig, data_rows in _parse_markdown_tables(body):
            for cells in data_rows:
                cells = [c for c in cells if c]
                if len(cells) < 3:
                    continue
                entity_key = cells[0].lower().strip()
                if not entity_key or len(entity_key) <= 3:
                    continue
                if entity_key in generic_keys:
                    continue
                normalized_rest = "|".join(cells[1:])
                entity_rows.setdefault((header_sig, entity_key), []).append(
                    (slug, normalized_rest)
                )

    for (_header_sig, entity), entries in entity_rows.items():
        if len(entries) < 2:
            continue
        slugs_involved = sorted(set(s for s, _ in entries))
        # Single-page "contradictions" are usually the same entity in multiple
        # table rows with different attributes (directories, deprecation tables, etc).
        if len(slugs_involved) < 2:
            continue
        unique_values = set(val for _, val in entries)
        if len(unique_values) > 1:
            issues.append(
                (SEV_WARN,
                 f"[contradiction] entity '{entity}' has conflicting values across: "
                 f"{', '.join('pages/' + s + '.md' for s in slugs_involved)}")
            )

    # Strategy 2: Look for **name** (ID: **value**) patterns
    id_pattern = re.compile(r"\*\*([^*]+)\*\*\s*\((?:ID|id):\s*\*\*([^*]+)\*\*\)")
    name_to_ids: Dict[str, List[Tuple[str, str]]] = {}

    for slug, _meta, body in pages:
        for m in id_pattern.finditer(body):
            name = m.group(1).strip().lower()
            id_val = m.group(2).strip()
            if name not in name_to_ids:
                name_to_ids[name] = []
            name_to_ids[name].append((slug, id_val))

    for name, entries in name_to_ids.items():
        if len(entries) < 2:
            continue
        unique_ids = set(v for _, v in entries)
        if len(unique_ids) > 1:
            slugs_involved = sorted(set(s for s, _ in entries))
            issues.append(
                (SEV_WARN,
                 f"[contradiction] '{name}' maps to different IDs across: "
                 f"{', '.join('pages/' + s + '.md' for s in slugs_involved)}")
            )

    return issues


def _load_http_helper(lore_dir: Path):
    """Load connectors/_http.py the same way _load_connector loads a connector.

    Tries the live data dir's own connectors/ first (so a customized _http.py
    there wins), then this script's own bundled skill/connectors/ (via
    _skill_root_dir(), so a repo checkout or a fresh install that has not yet
    run `bootstrap`'s connector-seeding step still finds one). Returns None if
    neither copy exists or it fails to load, so callers can degrade instead of
    crashing lint.
    """
    for candidate_dir in (lore_dir / "connectors", _skill_root_dir() / "connectors"):
        mod_path = candidate_dir / "_http.py"
        if not mod_path.is_file():
            continue
        try:
            spec = importlib.util.spec_from_file_location("_lore_http_lint", str(mod_path))
            if spec is None or spec.loader is None:
                continue
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod
        except Exception:
            continue
    return None


def _check_dead_links(
    pages: List[Tuple[str, Dict[str, Any], str]], lore_dir: Path
) -> List[Tuple[int, str]]:
    """Check source URLs in frontmatter for dead links.

    Routes through connectors/_http.py (HEAD first, GET fallback, same 10s
    timeout) so this check fetches the same way page sources are fetched
    during refresh (curl-first, then urllib) instead of always using bare
    urllib (C12): a host that blocks or throttles urllib specifically (but
    not curl) previously produced a false dead-link warning here even though
    a real refresh of that same source would have succeeded. Falls back to
    the original urllib-only HEAD check if _http.py cannot be loaded at all
    (e.g. an older install that predates C11's shared helper).
    """
    SEV_WARN = 1
    issues: List[Tuple[int, str]] = []
    http_mod = _load_http_helper(lore_dir)

    for slug, meta, _body in pages:
        sources = meta.get("sources", [])
        if isinstance(sources, str):
            sources = [sources]
        if not isinstance(sources, list):
            continue

        for url in sources:
            url = str(url).strip()
            if not url.startswith(("http://", "https://")):
                continue
            if http_mod is not None:
                try:
                    http_mod.fetch(url, timeout=10, method="HEAD")
                    continue
                except RuntimeError as head_exc:
                    try:
                        http_mod.fetch(url, timeout=10)
                        continue
                    except RuntimeError as get_exc:
                        issues.append(
                            (SEV_WARN,
                             f"[dead-link] pages/{slug}.md: {url} failed (HEAD: {head_exc}; GET: {get_exc})")
                        )
                        continue
            # Fallback: no _http.py available at all.
            try:
                req = urllib.request.Request(url, method="HEAD")
                req.add_header("User-Agent", "LoreCLI/1.0 link-checker")
                resp = urllib.request.urlopen(req, timeout=10)
                if resp.status >= 400:
                    issues.append(
                        (SEV_WARN, f"[dead-link] pages/{slug}.md: {url} returned HTTP {resp.status}")
                    )
            except urllib.error.HTTPError as exc:
                issues.append(
                    (SEV_WARN, f"[dead-link] pages/{slug}.md: {url} returned HTTP {exc.code}")
                )
            except Exception as exc:
                issues.append(
                    (SEV_WARN, f"[dead-link] pages/{slug}.md: {url} failed ({type(exc).__name__})")
                )

    return issues


def _check_credential_leaks(
    pages: List[Tuple[str, Dict[str, Any], str]]
) -> List[Tuple[int, str]]:
    """Scan page content for credential-like patterns."""
    SEV_ERROR = 0
    issues: List[Tuple[int, str]] = []

    for slug, _meta, body in pages:
        stripped = strip_code(body)
        scans = [(body, CREDENTIAL_PATTERNS_STRICT), (stripped, CREDENTIAL_PATTERNS_LOOSE)]
        for text, patterns in scans:
            for pattern, description in patterns:
                for m in pattern.finditer(text):
                    matched = m.group(0)
                    display = matched[:20] + "..." if len(matched) > 20 else matched
                    issues.append(
                        (SEV_ERROR,
                         f"[credential-leak] pages/{slug}.md: possible {description} found: '{display}'")
                    )

    return issues


def _maybe_generate_entities(
    entities_path: Path,
    pages: List[Tuple[str, Dict[str, Any], str]],
) -> None:
    """Auto-generate entities.json from page titles and tags if it doesn't exist or is empty."""
    if entities_path.is_file():
        try:
            existing = json.loads(entities_path.read_text(encoding="utf-8"))
            if existing:
                return  # Has manual entries, don't overwrite
        except (json.JSONDecodeError, ValueError):
            pass  # File is corrupt or empty, regenerate

    entities: Dict[str, List[str]] = {}
    for slug, meta, _body in pages:
        title = meta.get("title", slug)
        aliases: List[str] = []
        # Add the title as an alias (lowercased)
        if title.lower() != slug.lower():
            aliases.append(title.lower())
        # Add tags as aliases
        tags = meta.get("tags", [])
        if isinstance(tags, list):
            for tag in tags:
                tag_lower = str(tag).strip().lower()
                if tag_lower and tag_lower != slug.lower():
                    aliases.append(tag_lower)
        entities[slug] = aliases

    entities_path.write_text(
        json.dumps(entities, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def cmd_check_entity(args: argparse.Namespace) -> None:
    """Look up a name in the entity registry to find the canonical page slug."""
    lore_dir = resolve_lore_dir(args.lore_dir)
    entities_path = lore_dir / "entities.json"

    if not entities_path.is_file():
        error("entities.json not found. Run 'lore index' first to generate it.")

    try:
        entities: Dict[str, List[str]] = json.loads(
            entities_path.read_text(encoding="utf-8")
        )
    except (json.JSONDecodeError, ValueError) as exc:
        error(f"Failed to parse entities.json: {exc}")

    query = args.name.strip().lower()

    # Check canonical names first
    for canonical, aliases in entities.items():
        if canonical.lower() == query:
            print(f"Match: {canonical} (canonical)")
            return

    # Check aliases
    for canonical, aliases in entities.items():
        for alias in aliases:
            if alias.lower() == query:
                print(f"Match: {canonical} (via alias '{alias}')")
                return

    print("No matches found.")


def cmd_ingest(args: argparse.Namespace) -> None:
    """Ingest a source document into the lore knowledge base."""
    lore_dir = resolve_lore_dir(args.lore_dir)
    source_path = Path(args.source_path).expanduser().resolve()

    if not lore_dir.is_dir():
        error(f"Lore directory not found: {lore_dir}")

    if not source_path.is_file():
        error(f"Source file not found: {source_path}")

    sources_dir = lore_dir / "sources"
    sources_dir.mkdir(parents=True, exist_ok=True)

    # Read the source content
    try:
        content = source_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        content = f"[Binary file: {source_path.name}, {source_path.stat().st_size} bytes]"

    # Copy to sources/ with timestamp prefix if not already there
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    dest_name = f"{timestamp}_{source_path.name}"
    dest_path = sources_dir / dest_name

    # Check if this file already exists in sources/
    existing = list(sources_dir.glob(f"*_{source_path.name}"))
    if existing:
        print(f"Note: source already archived as {existing[0].name}")
    else:
        shutil.copy2(str(source_path), str(dest_path))
        print(f"Archived to: sources/{dest_name}")

    # Print summary
    preview = content[:500]
    if len(content) > 500:
        preview += "..."
    print(f"\n--- Source preview ({len(content)} chars total) ---")
    print(preview)
    print("--- End preview ---\n")

    # Suggest pages that might need updating based on content overlap
    pages = scan_pages(lore_dir)
    if pages:
        content_lower = content.lower()
        matching_pages = []
        for slug, meta, _body in pages:
            title = meta.get("title", slug)
            # Check if page title or slug appears in the source content
            if slug.lower() in content_lower or title.lower() in content_lower:
                matching_pages.append(f"  - pages/{slug}.md ({title})")
        if matching_pages:
            print("Pages that may need updating:")
            for p in matching_pages:
                print(p)
        else:
            print("No existing pages seem directly related to this source.")
    print()
    print("Next steps: Review the source and create/update lore pages as needed.")
    print("The LLM should parse this source and update relevant pages with new facts.")

    # Log the ingest action
    log_path = lore_dir / "log.md"
    if not log_path.is_file():
        log_path.write_text(LOG_HEADER, encoding="utf-8")

    entry = f"- {now_iso()} | Ingested: {source_path.name} ({len(content)} chars)\n"
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(entry)

    ok(f"Ingested {source_path.name} to sources/{dest_name}")


def _collect_stale_pages(
    pages: List[Tuple[str, Dict[str, Any], str]]
) -> List[Dict[str, Any]]:
    """Return auto_update pages overdue for verification (shared by cmd_stale and cmd_pending).

    Each entry is {"slug": str, "reason": str} plus "days_overdue" when applicable.
    """
    today = datetime.now(timezone.utc).date()
    stale: List[Dict[str, Any]] = []

    for slug, meta, _body in pages:
        if not meta.get("auto_update"):
            continue
        last_str = meta.get("last_verified", "")
        if not last_str:
            stale.append({"slug": slug, "reason": "never verified"})
            continue
        try:
            last_date = datetime.strptime(str(last_str), "%Y-%m-%d").date()
        except ValueError:
            stale.append({"slug": slug, "reason": f"invalid date: {last_str}"})
            continue
        interval = parse_refresh_interval(meta.get("refresh_interval", "30d"))
        if last_date + interval < today:
            days_overdue = (today - last_date - interval).days
            stale.append({
                "slug": slug,
                "reason": f"overdue by {days_overdue} days",
                "days_overdue": days_overdue,
            })

    return stale


def cmd_stale(args: argparse.Namespace) -> None:
    """List pages where auto_update is overdue for refresh."""
    lore_dir = resolve_lore_dir(args.lore_dir)

    if not lore_dir.is_dir():
        error(f"Lore directory not found: {lore_dir}")

    stale_pages = _collect_stale_pages(scan_pages(lore_dir))

    if stale_pages:
        for entry in stale_pages:
            print(f"pages/{entry['slug']}.md ({entry['reason']})")
    else:
        ok("All pages are fresh.")


def cmd_log(args: argparse.Namespace) -> None:
    """Append a timestamped entry to log.md."""
    lore_dir = resolve_lore_dir(args.lore_dir)

    if not lore_dir.is_dir():
        error(f"Lore directory not found: {lore_dir}")

    log_path = lore_dir / "log.md"
    if not log_path.is_file():
        log_path.write_text(LOG_HEADER, encoding="utf-8")

    entry = f"- {now_iso()} | {args.message}\n"

    with open(log_path, "a", encoding="utf-8") as f:
        f.write(entry)

    ok("Logged.")


def cmd_sync(args: argparse.Namespace) -> None:
    """Sync lore directory via git (add, commit, pull --rebase, push)."""
    lore_dir = resolve_lore_dir(args.lore_dir)

    if not lore_dir.is_dir():
        error(f"Lore directory not found: {lore_dir}")

    git_dir = lore_dir / ".git"
    if not git_dir.exists():
        error("Lore dir is not a git repository.")

    date_str = today_iso()

    try:
        subprocess.run(
            ["git", "add", "-A"],
            cwd=str(lore_dir),
            check=True,
            capture_output=True,
            text=True,
        )
        # Commit (may fail if nothing to commit, that's ok)
        commit_result = subprocess.run(
            ["git", "commit", "-m", f"lore sync {date_str}"],
            cwd=str(lore_dir),
            capture_output=True,
            text=True,
        )
        if commit_result.returncode != 0 and "nothing to commit" not in commit_result.stdout:
            error(f"git commit failed: {commit_result.stderr.strip()}")

        subprocess.run(
            ["git", "pull", "--rebase"],
            cwd=str(lore_dir),
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "push"],
            cwd=str(lore_dir),
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        error(f"git operation failed: {exc.stderr.strip() if exc.stderr else exc}")

    ok("Synced.")


# ---------------------------------------------------------------------------
# Connector loader and inline web fetcher
# ---------------------------------------------------------------------------

def _pick_connector_name(url: str) -> str:
    """Dispatch a source URL to a connector module name.

    Returns the module name (e.g. 'pypi', 'npm', 'web') based on URL pattern.
    """
    u = url.strip()
    if "pypi.org" in u:
        return "pypi"
    if "npmjs.com" in u or "npmjs.org" in u or "registry.npmjs.org" in u:
        return "npm"
    if ("api.github.com" in u and "/releases" in u) or (
        "github.com" in u and "/releases" in u
    ):
        return "github_releases"
    if u.endswith(".rss") or u.endswith(".atom") or "feed.xml" in u or "/feed" in u:
        return "rss"
    if u.endswith(".pdf"):
        return "pdf"
    if u.startswith("git@") or u.startswith("git://") or u.startswith("git+https://") or u.endswith(".git"):
        return "git_repo"
    return "web"


def _load_connector(url: str, connectors_dir: Path):
    """Load the appropriate connector module for url, falling back to inline web fetcher.

    Connector modules are loaded as STANDALONE modules directly from their file path
    via importlib.util.spec_from_file_location. The previous package-style import
    (`import connectors.{name}` after inserting the connectors dir itself into
    sys.path) only worked when the process cwd happened to be the lore dir, because
    the package's PARENT dir was never on sys.path; under launchd/script invocation
    the import always failed and refresh silently used the inline fetcher instead of
    the on-disk connectors. Path-based loading is cwd-independent and cannot collide
    with any other 'connectors' package.

    Returns an object with a fetch(url, timeout) -> (content_str, etag_or_none)
    method and a hash_content(content_str) -> str method.
    """
    name = _pick_connector_name(url)
    if name.startswith("_"):
        # Defensive (C11): _pick_connector_name is a fixed dispatch table today
        # and never returns an underscore-prefixed name, but a leading
        # underscore marks a shared helper module (connectors/_http.py), not a
        # dispatchable connector. Refuse to load one even if a future dispatch
        # rule ever routed a URL to one by mistake, rather than exec'ing an
        # arbitrary helper module as if it implemented the connector contract.
        return _InlineWebConnector()
    mod_path = connectors_dir / f"{name}.py"
    if mod_path.is_file():
        # Unique module name per connectors dir so two lore dirs never collide in sys.modules.
        dir_tag = hashlib.sha256(str(connectors_dir).encode("utf-8")).hexdigest()[:8]
        mod_name = f"lore_connector_{dir_tag}_{name}"
        try:
            mod = sys.modules.get(mod_name)
            if mod is None:
                spec = importlib.util.spec_from_file_location(mod_name, str(mod_path))
                if spec is None or spec.loader is None:
                    raise ImportError(f"cannot build import spec for {mod_path}")
                mod = importlib.util.module_from_spec(spec)
                sys.modules[mod_name] = mod
                spec.loader.exec_module(mod)
            if hasattr(mod, "fetch") and hasattr(mod, "hash_content"):
                return mod
            print(
                f"[WARN] connector '{mod_path}' missing fetch/hash_content; using inline web fetcher",
                file=sys.stderr,
            )
        except Exception as exc:
            sys.modules.pop(mod_name, None)
            print(
                f"[WARN] failed to load connector '{mod_path}' ({type(exc).__name__}: {exc}); using inline web fetcher",
                file=sys.stderr,
            )
    # Fall back to the inline web connector object defined below
    return _InlineWebConnector()


# curl exit codes that indicate transient network trouble worth one IPv4-only retry
# (6 DNS, 7 connect failed, 28 timeout, 35 TLS handshake, 52 empty reply, 55/56 send/recv).
_CURL_RETRY_EXITS = {6, 7, 28, 35, 52, 55, 56}


def _parse_curl_headers(header_text: str) -> Tuple[int, str, Optional[str]]:
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
    etag: Optional[str] = None
    for line in lines[1:]:
        if line.lower().startswith("etag:"):
            etag = line.split(":", 1)[1].strip()
            break
    return status, reason, etag


def _curl_fetch(url: str, timeout: int, headers: Optional[Dict[str, str]] = None) -> Tuple[bytes, Optional[str]]:
    """GET url via the system curl binary. Returns (body_bytes, etag_or_none).

    Raises FileNotFoundError when curl is not installed (callers fall back to urllib),
    and RuntimeError on curl failure or HTTP status >= 400. Tries the default IP stack
    first and retries once IPv4-only on transient network errors (hung IPv6 routes on
    this class of failure are exactly what curl-first fetching exists to survive).
    """
    hdr_fd, hdr_path = tempfile.mkstemp(prefix="lore-curl-", suffix=".hdr")
    os.close(hdr_fd)
    cmd = [
        "curl", "-sS", "-L",
        "--max-time", str(int(timeout)),
        "--connect-timeout", str(min(int(timeout), 15)),
        "-A", "lore-refresh/1.0",
        "-D", hdr_path,
    ]
    for key, value in (headers or {}).items():
        cmd += ["-H", f"{key}: {value}"]
    last_err: Exception = RuntimeError(f"curl failed for {url}")
    try:
        for extra in ([], ["-4"]):
            try:
                proc = subprocess.run(
                    cmd[:1] + extra + cmd[1:] + [url],
                    capture_output=True,
                    timeout=int(timeout) + 30,
                )
            except subprocess.TimeoutExpired:
                last_err = RuntimeError(f"Timeout after {timeout}s fetching {url}")
                continue
            if proc.returncode == 0:
                with open(hdr_path, "r", encoding="utf-8", errors="replace") as f:
                    status, reason, etag = _parse_curl_headers(f.read())
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


class _InlineWebConnector:
    """Minimal stdlib-only HTTP GET connector used when typed connectors are not installed."""

    @staticmethod
    def fetch(url: str, timeout: int = 30) -> Tuple[str, Optional[str]]:
        """Fetch url with explicit timeout, curl-first. Returns (content_str, etag_or_none)."""
        try:
            body, etag = _curl_fetch(url, timeout)
            return body.decode("utf-8", errors="replace"), etag
        except FileNotFoundError:
            pass  # curl not installed: fall back to urllib below
        req = urllib.request.Request(url)
        req.add_header("User-Agent", "LoreCLI/1.0 refresh")
        resp = urllib.request.urlopen(req, timeout=timeout)
        content_bytes = resp.read()
        content = content_bytes.decode("utf-8", errors="replace")
        etag = resp.headers.get("ETag")
        return content, etag

    @staticmethod
    def hash_content(content: str) -> str:
        """Return SHA-256 hex digest of content encoded as UTF-8."""
        return hashlib.sha256(content.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Readable diff payloads for queue/ (HTML-to-text extraction + .md endpoints)
# ---------------------------------------------------------------------------

# Docs hosts that serve clean markdown at the same URL + ".md". When a changed
# source lives on one of these hosts, the queued diff prefers that markdown as
# its readable payload (hash/change detection stays on the ORIGINAL url).
MD_SUFFIX_HOSTS = ["platform.claude.com", "docs.claude.com", "code.claude.com"]

# Cap on the readable content stored in a queue/ diff file (characters of the
# extracted or native text). Content beyond this gets a truncation marker.
DIFF_CONTENT_MAX_CHARS = 60 * 1024

# Marker written into every successful .hashes.json entry (C19). Bumping this
# string is how a future change-detection rework signals that every legacy
# entry needs re-migrating; see _change_hash() and cmd_refresh()'s legacy
# upgrade path.
HASH_SCHEME = "text-v1"


class _HTMLTextExtractor(HTMLParser):
    """Stdlib HTML-to-text: drops script/style/noscript/template/svg content
    entirely, emits newlines at block-tag boundaries, unescapes entities
    (convert_charrefs), and collapses runs of blank lines. Used to make
    queue/ diffs readable AND (since C19) to compute the change-detection
    hash, so anything that never affects what the gardener reads must not
    leak into handle_data(): HTML comments already don't (HTMLParser's
    default handle_comment() is a no-op unless overridden, and this class
    does not override it, so comment text never reaches _chunks), and
    attribute values already don't (handle_starttag() below only appends a
    block-boundary newline, it never touches the `attrs` argument)."""

    _SKIP_TAGS = {"script", "style", "noscript", "template", "svg"}
    _BLOCK_TAGS = {
        "address", "article", "aside", "blockquote", "br", "dd", "details",
        "div", "dl", "dt", "fieldset", "figcaption", "figure", "footer",
        "form", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "li",
        "main", "nav", "ol", "p", "pre", "section", "summary", "table",
        "tbody", "td", "tfoot", "th", "thead", "tr", "ul",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: List[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1
        elif tag in self._BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP_TAGS:
            if self._skip_depth > 0:
                self._skip_depth -= 1
        elif tag in self._BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_startendtag(self, tag: str, attrs: Any) -> None:
        if tag in self._BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0 and data:
            self._chunks.append(data)

    def text(self) -> str:
        """Return the extracted text with whitespace normalised: horizontal
        whitespace collapsed within lines, runs of blank lines collapsed to one."""
        lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in "".join(self._chunks).splitlines()]
        out: List[str] = []
        prev_blank = True  # also drops leading blank lines
        for ln in lines:
            if ln:
                out.append(ln)
                prev_blank = False
            elif not prev_blank:
                out.append("")
                prev_blank = True
        while out and not out[-1]:
            out.pop()
        return "\n".join(out)


def _content_looks_like_html(content: str) -> bool:
    """Heuristic: does this fetched content look like an HTML document?

    True for a leading <!doctype or <html, or an <html/<head/<body tag within the
    first KB (text/html hint; connectors do not surface the Content-Type header).
    JSON (pypi), markdown, and plain text all fall through as non-HTML.
    """
    head = content.lstrip()[:1024].lower()
    if head.startswith("<!doctype") or head.startswith("<html"):
        return True
    return "<html" in head or "<head>" in head or "<head " in head or "<body" in head


def _html_to_text(content: str) -> str:
    """Extract readable text from an HTML document. Returns '' when nothing
    textual survives (caller falls back to the native content)."""
    parser = _HTMLTextExtractor()
    try:
        parser.feed(content)
        parser.close()
    except Exception:
        pass  # malformed HTML: keep whatever was extracted before the error
    return parser.text()


def _change_hash(content: str, connector: Any) -> str:
    """Hash of what the gardener would actually read, not the raw fetched
    bytes (C19). Documentation sites embed build ids, nonces and other
    per-response markup that changes on every fetch even when the visible
    text does not; hashing the raw bytes (connector.hash_content) makes
    those pages show as changed on every refresh forever.

    When the content looks like HTML and text extraction yields something
    non-empty, hash the SHA-256 of that extracted text after whitespace
    normalization (every run of whitespace collapsed to one space, leading
    and trailing whitespace stripped), so markup-only churn never changes
    the hash. Otherwise defer to the connector's own raw-bytes hash: JSON
    (npm, pypi, github_releases), RSS/Atom XML, PDF text and git output
    have no such churn problem and keep today's behaviour unchanged.
    """
    if _content_looks_like_html(content):
        extracted = _html_to_text(content)
        if extracted.strip():
            normalized = re.sub(r"\s+", " ", extracted).strip()
            return hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return connector.hash_content(content)


def _build_diff_payload(url: str, content: str, connector: Any) -> Tuple[str, str, str]:
    """Build the human/LLM-readable payload for a queue/ diff file.

    Returns (payload, content_kind, content_url) where content_kind is one of
    'md-endpoint' (clean markdown fetched from url + '.md' on MD_SUFFIX_HOSTS),
    'extracted-text' (HTML reduced to text), or 'native' (stored as-is: JSON,
    markdown, plain text). Hashing/change detection is NOT affected: it already
    ran on the original url content before this is called.
    """
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    if host in MD_SUFFIX_HOSTS:
        md_url = url + ".md"
        try:
            md_content, _md_etag = connector.fetch(md_url, timeout=30)
        except Exception:
            md_content = ""
        if md_content and len(md_content) > 500 and not _content_looks_like_html(md_content):
            return md_content, "md-endpoint", md_url
    if _content_looks_like_html(content):
        extracted = _html_to_text(content)
        if extracted.strip():
            return extracted, "extracted-text", url
    return content, "native", url


# ---------------------------------------------------------------------------
# Frontmatter writer
# ---------------------------------------------------------------------------

def set_frontmatter_field(page_path: Path, field: str, value: Any) -> None:
    """Set a single frontmatter field in a markdown page, preserving all other content.

    Reads the file, sets or adds 'field: value' in the --- block, writes back.
    Booleans are serialised as lowercase true/false. Strings are written bare
    (no surrounding quotes) unless they contain a colon, in which case they are
    single-quoted to stay YAML-safe.

    Every byte after the closing '---' line is preserved verbatim, including
    any blank line(s) between the frontmatter and the body: FRONTMATTER_RE's
    match.end() cannot be used directly for that boundary, because its
    trailing whitespace-star pattern also consumes those blank lines. The
    body start is instead located with _FRONTMATTER_CLOSING_LINE_RE, which
    matches only the closing '---' delimiter line itself.
    """
    text = page_path.read_text(encoding="utf-8")
    match = FRONTMATTER_RE.match(text)
    if not match:
        # No frontmatter: prepend a minimal block
        if isinstance(value, bool):
            val_str = "true" if value else "false"
        else:
            val_str = str(value)
        new_fm = f"---\n{field}: {val_str}\n---\n"
        page_path.write_text(new_fm + text, encoding="utf-8")
        return

    raw = match.group(1)
    close = _FRONTMATTER_CLOSING_LINE_RE.match(text, match.end(1))
    body_after = text[close.end():] if close else text[match.end():]

    # Serialise value to YAML-ish string
    if isinstance(value, bool):
        val_str = "true" if value else "false"
    else:
        val_str = str(value)
        # Quote if value contains a colon to remain YAML-safe
        if ":" in val_str and not (val_str.startswith("'") or val_str.startswith('"')):
            val_str = f"'{val_str}'"

    # Replace existing field if present, otherwise append
    field_re = re.compile(r"^(" + re.escape(field) + r"\s*:.*)$", re.MULTILINE)
    if field_re.search(raw):
        new_raw = field_re.sub(f"{field}: {val_str}", raw)
    else:
        new_raw = raw.rstrip("\n") + f"\n{field}: {val_str}"

    page_path.write_text(f"---\n{new_raw}\n---\n{body_after}", encoding="utf-8")


# ---------------------------------------------------------------------------
# Graph builder (shared by cmd_index and cmd_graph)
# ---------------------------------------------------------------------------

WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")


def build_graph(lore_dir: Path, pages: List[Tuple[str, Dict[str, Any], str]]) -> Dict[str, Any]:
    """Build the page-dependency DAG from wikilinks.

    Returns the graph dict and atomically writes lore/.graph.json.
    """
    nodes = [slug for slug, _, _ in pages]
    edges: List[Dict[str, str]] = []

    for slug, _meta, body in pages:
        for m in WIKILINK_RE.finditer(strip_code(body)):
            ref_raw = m.group(1).strip()
            # Handle scoped refs like [[global:slug]] or [[project:slug]]
            if ":" in ref_raw:
                scope, ref_slug = ref_raw.split(":", 1)
                scope = scope.strip()
                ref_slug = ref_slug.strip()
            else:
                scope = "local"
                ref_slug = ref_raw

            edges.append({
                "from": slug,
                "to": ref_slug,
                "ref": f"[[{ref_raw}]]",
                "scope": scope,
            })

    graph: Dict[str, Any] = {
        "built": now_iso(),
        "nodes": nodes,
        "edges": edges,
    }

    # Atomic write
    graph_path = lore_dir / ".graph.json"
    tmp_path = lore_dir / ".graph.json.tmp"
    tmp_path.write_text(json.dumps(graph, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(str(tmp_path), str(graph_path))

    return graph


# ---------------------------------------------------------------------------
# Dotted-path resolver for the extracts layer
# ---------------------------------------------------------------------------

def _resolve_dotted_path(data: Any, dotted: str) -> Any:
    """Resolve a dotted path like 'models.opus_4_7.context_window' against data.

    Supports dict keys and list indices (e.g. 'items.0.name').
    Raises KeyError if any segment is missing.
    """
    current = data
    for part in dotted.split("."):
        if isinstance(current, dict):
            if part not in current:
                raise KeyError(part)
            current = current[part]
        elif isinstance(current, list):
            try:
                idx = int(part)
            except ValueError:
                raise KeyError(part)
            if idx < 0 or idx >= len(current):
                raise KeyError(part)
            current = current[idx]
        else:
            raise KeyError(part)
    return current


# ---------------------------------------------------------------------------
# New subcommands: refresh, fact, graph
# ---------------------------------------------------------------------------

def cmd_refresh(args: argparse.Namespace) -> None:
    """Check sources for changes using SHA-256 hashing; update .hashes.json atomically."""
    lore_dir = resolve_lore_dir(args.lore_dir)
    force: bool = getattr(args, "force", False)
    slug_filter: Optional[str] = getattr(args, "slug", None)

    if not lore_dir.is_dir():
        error(f"Lore directory not found: {lore_dir}")

    connectors_dir = lore_dir / "connectors"
    hashes_path = lore_dir / ".hashes.json"
    failed_dir = lore_dir / "failed"
    queue_dir = lore_dir / "queue"
    failed_dir.mkdir(parents=True, exist_ok=True)
    queue_dir.mkdir(parents=True, exist_ok=True)

    # Load existing hashes
    if hashes_path.is_file():
        try:
            hashes: Dict[str, Any] = json.loads(hashes_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, ValueError):
            hashes = {}
    else:
        hashes = {}

    all_pages = scan_pages(lore_dir)
    pages = all_pages
    if slug_filter:
        pages = [(s, m, b) for s, m, b in pages if s == slug_filter]
        if not pages:
            error(f"No page found with slug '{slug_filter}'")

    n_checked = 0
    n_unchanged = 0
    n_changed = 0
    n_failed = 0
    n_skipped = 0
    n_verified = 0
    changed_slugs: Set[str] = set()
    today_str = today_iso()
    today_date = datetime.now(timezone.utc).date()

    for slug, meta, _body in pages:
        if not meta.get("auto_update") and not force:
            continue

        sources_list = meta.get("sources", [])
        if isinstance(sources_list, str):
            sources_list = [sources_list]
        if not isinstance(sources_list, list):
            sources_list = []

        # Per-page outcome tracking for the last_verified auto-bump: a page is
        # auto-verified only when every fetchable source succeeded AND hash-matched.
        page_total = 0
        page_unverified = 0

        for url in sources_list:
            url = str(url).strip()
            if not url:
                continue
            if not url.startswith(("http://", "https://", "git@", "git://", "git+")):
                continue

            n_checked += 1
            page_total += 1
            existing = hashes.get(url, {})

            # Quarantine gate: a source that failed 3+ consecutive times is skipped
            # until its quarantined_until date has passed. --force overrides the gate
            # (an explicit manual retry, e.g. `refresh --slug X --force`).
            q_until_str = str(existing.get("quarantined_until", "") or "")
            q_until = _parse_date_safe(q_until_str)
            if q_until is not None and q_until.date() > today_date and not force:
                n_skipped += 1
                page_unverified += 1
                print(f"[SKIP quarantined] {slug} | {url} | until {q_until_str}")
                continue

            connector = _load_connector(url, connectors_dir)
            cache_dir = lore_dir / ".refresh-cache"
            cache_path = cache_dir / (hashlib.sha256(url.encode("utf-8")).hexdigest() + ".txt")

            try:
                content, etag = connector.fetch(url, timeout=30)
                new_hash = _change_hash(content, connector)
            except Exception as exc:
                n_failed += 1
                page_unverified += 1
                err_filename = f"{slug}-{today_str}.err"
                err_path = failed_dir / err_filename
                err_content = (
                    f"slug: {slug}\n"
                    f"url: {url}\n"
                    f"timestamp: {now_iso()}\n"
                    f"error: {type(exc).__name__}: {exc}\n\n"
                    f"traceback:\n{traceback.format_exc()}"
                )
                err_path.write_text(err_content, encoding="utf-8")
                print(f"[FAIL] {slug} | {url} | {type(exc).__name__}: {exc}", file=sys.stderr)
                # Failure bookkeeping + quarantine policy in .hashes.json
                entry = dict(existing)
                entry["slug"] = slug
                try:
                    n_fails = int(entry.get("consecutive_failures", 0) or 0) + 1
                except (TypeError, ValueError):
                    n_fails = 1
                entry["consecutive_failures"] = n_fails
                entry["last_error"] = f"{type(exc).__name__}: {exc}"[:200]
                if n_fails >= 3:
                    q_date = (today_date + timedelta(days=7)).isoformat()
                    entry["quarantined_until"] = q_date
                    print(
                        f"[QUARANTINE] {slug} | {url} | "
                        f"{n_fails} consecutive failures, quarantined until {q_date}"
                    )
                hashes[url] = entry
                continue

            old_hash = existing.get("sha256")
            # C19 lossless scheme migration: an entry with a sha256 but no
            # hash_scheme was written by the pre-C19 raw-bytes hash, which
            # can never coincidentally equal the new normalized-text hash.
            # Compare against the raw hash it was actually written with, so
            # a source whose visible text never changed is not reported as
            # changed purely because the hash scheme changed underneath it.
            is_legacy = old_hash is not None and not existing.get("hash_scheme")
            upgraded = False
            if is_legacy:
                legacy_hash = connector.hash_content(content)
                unchanged = legacy_hash == old_hash
                upgraded = unchanged
            else:
                unchanged = old_hash is not None and old_hash == new_hash
            if not unchanged:
                page_unverified += 1

            # Success: rebuild the entry, which also clears consecutive_failures,
            # last_error and quarantined_until (any success resets the counter).
            # Every success entry carries hash_scheme (C19) so a future scheme
            # change can tell a migrated entry from one still on the old one.
            hashes[url] = {
                "sha256": new_hash,
                "hash_scheme": HASH_SCHEME,
                "etag": etag,
                "last_fetched": now_iso(),
                "slug": slug,
            }

            if unchanged and not force:
                # Unchanged: bump last_fetched only
                n_unchanged += 1
                if upgraded:
                    print(f"[OK]   {slug} | {url} | unchanged (hash scheme upgraded)")
                else:
                    print(f"[OK]   {slug} | {url} | unchanged")
            else:
                # Changed (or first fetch, or forced)
                n_changed += 1
                is_first = old_hash is None

                if not is_first and not force:
                    # Write a diff record for LLM review into queue/ (sources/ is an
                    # immutable archive of ingested originals; machine diffs live here).
                    # The payload is READABLE text: markdown from a .md endpoint when
                    # the host offers one, extracted text for HTML, native otherwise.
                    # Hashing above already ran on the original url content.
                    diff_filename = f"{slug}-{today_str}.diff.md"
                    diff_path = queue_dir / diff_filename
                    payload, content_kind, content_url = _build_diff_payload(url, content, connector)
                    truncated = len(payload) > DIFF_CONTENT_MAX_CHARS
                    payload_display = payload[:DIFF_CONTENT_MAX_CHARS] if truncated else payload

                    # C19: a unified diff against the previously cached payload,
                    # placed ahead of the full payload so a real change is
                    # visible at a glance instead of buried in a large payload.
                    prev_payload = cache_path.read_text(encoding="utf-8") if cache_path.is_file() else None
                    if prev_payload is not None:
                        diff_lines = list(difflib.unified_diff(
                            prev_payload.splitlines(), payload.splitlines(),
                            fromfile="previous", tofile="current", n=3, lineterm="",
                        ))
                        diff_text = "\n".join(diff_lines)
                        diff_text_truncated = len(diff_text) > DIFF_CONTENT_MAX_CHARS
                        if diff_text_truncated:
                            diff_text = diff_text[:DIFF_CONTENT_MAX_CHARS]
                        prev_fetched = existing.get("last_fetched", "unknown")
                        diff_section = (
                            f"## Diff against the previous payload (cached {prev_fetched})\n\n"
                            f"{diff_text}"
                        )
                        if diff_text_truncated:
                            diff_section += (
                                f"\n\n[truncated: content exceeded the "
                                f"{DIFF_CONTENT_MAX_CHARS}-character diff cap]"
                            )
                    else:
                        diff_section = "no previous payload cached; full payload follows"

                    diff_content = (
                        f"---\n"
                        f"slug: {slug}\n"
                        f"url: {url}\n"
                        f"old_sha256: {old_hash}\n"
                        f"new_sha256: {new_hash}\n"
                        f"fetched: {now_iso()}\n"
                        f"content_kind: {content_kind}\n"
                        f"content_url: {content_url}\n"
                        f"---\n\n"
                        f"{diff_section}\n\n"
                        f"Source changed. New content follows for LLM review ({content_kind}).\n\n"
                        f"{payload_display}"
                    )
                    if truncated:
                        diff_content += (
                            f"\n\n[truncated: content exceeded the "
                            f"{DIFF_CONTENT_MAX_CHARS}-character diff cap]"
                        )
                    # Supersede: older-dated diffs for this slug are garbage the moment
                    # today's diff exists (the gardener only reads the newest anyway).
                    # Same-date writes keep their current behavior.
                    old_diff_re = re.compile(
                        r"^" + re.escape(slug) + r"-(\d{4}-\d{2}-\d{2})\.diff\.md$"
                    )
                    n_superseded = 0
                    for old_file in queue_dir.iterdir():
                        m_old = old_diff_re.match(old_file.name)
                        if m_old and m_old.group(1) != today_str:
                            try:
                                old_file.unlink()
                                n_superseded += 1
                            except OSError:
                                pass
                    if n_superseded:
                        print(f"[SUPERSEDE] {slug} | superseded {n_superseded} older diff(s) in queue/")
                    diff_path.write_text(diff_content, encoding="utf-8")
                    # C19: cache this payload so the NEXT change has something
                    # to diff against (directory created on demand).
                    cache_dir.mkdir(parents=True, exist_ok=True)
                    cache_path.write_text(payload, encoding="utf-8")
                    # Mark page as needing review, and remember it as changed
                    # THIS run so dependents can be flagged below.
                    page_path = lore_dir / "pages" / f"{slug}.md"
                    if page_path.is_file():
                        set_frontmatter_field(page_path, "needs_review", True)
                    changed_slugs.add(slug)
                    print(
                        f"[CHANGED] {slug} | {url} | "
                        f"old={old_hash[:8]} new={new_hash[:8]} | "
                        f"needs LLM review -> queue/{diff_filename}"
                    )
                elif is_first:
                    # C19: cache the payload on first fetch too, so the first
                    # real change afterwards has a previous payload to diff.
                    payload, _content_kind, _content_url = _build_diff_payload(url, content, connector)
                    cache_dir.mkdir(parents=True, exist_ok=True)
                    cache_path.write_text(payload, encoding="utf-8")
                    print(f"[NEW]  {slug} | {url} | first fetch, hash stored")
                else:
                    print(f"[FORCE] {slug} | {url} | hash recomputed (forced)")

        # Auto-bump last_verified: every source of this page fetched successfully
        # AND hash-matched this run. Pages with any changed, failed or quarantine-
        # skipped source, and pages with an unreconciled needs_review flag, stay put.
        if page_total > 0 and page_unverified == 0 and not meta.get("needs_review"):
            page_path = lore_dir / "pages" / f"{slug}.md"
            if page_path.is_file():
                set_frontmatter_field(page_path, "last_verified", today_str)
                n_verified += 1
                print(f"[VERIFIED] {slug} auto-bumped")

    # Drop hash entries no page's sources list references any more (C5).
    # Checked against the full, unfiltered page set even during a
    # --slug-scoped refresh: whether a URL is still referenced by anything
    # doesn't depend on which pages this run happened to touch.
    n_orphans_dropped = 0
    for url in _find_orphan_hash_urls(all_pages, hashes):
        print(f"[ORPHAN] {url} | source URL is on no page's sources list, dropping from .hashes.json")
        del hashes[url]
        n_orphans_dropped += 1

    # Atomic write of updated hashes
    tmp_path = hashes_path.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(hashes, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(str(tmp_path), str(hashes_path))

    # Flag dependents of slugs that genuinely changed THIS run (not on
    # timestamp drift; see _propagate_needs_review_to_dependents).
    n_propagated = 0
    graph_path = lore_dir / ".graph.json"
    if changed_slugs and graph_path.is_file():
        try:
            graph: Dict[str, Any] = json.loads(graph_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, ValueError):
            graph = {}
        n_propagated = _propagate_needs_review_to_dependents(lore_dir, graph, changed_slugs)

    print()
    print(
        f"Refresh complete. {n_checked} sources checked: "
        f"{n_unchanged} unchanged, {n_changed} changed/new, {n_failed} failed, "
        f"{n_skipped} quarantine-skipped. {n_verified} pages auto-verified, "
        f"{n_propagated} dependent(s) flagged for review, "
        f"{n_orphans_dropped} orphan hash entr{'y' if n_orphans_dropped == 1 else 'ies'} dropped."
    )


def cmd_review(args: argparse.Namespace) -> None:
    """List pages flagged needs_review, or clear the flag (see 'clear --propagated')."""
    lore_dir = resolve_lore_dir(args.lore_dir)
    if not lore_dir.is_dir():
        error(f"Lore directory not found: {lore_dir}")

    action = getattr(args, "review_action", None)
    pages = scan_pages(lore_dir)

    if action == "list":
        flagged = [(slug, meta) for slug, meta, _body in pages if meta.get("needs_review")]
        if not flagged:
            ok("No pages flagged needs_review.")
            return
        for slug, meta in flagged:
            print(f"{slug}\t{meta.get('title', slug)}")
        print()
        ok(f"{len(flagged)} page(s) need review.")
        return

    if action == "clear":
        propagated: bool = getattr(args, "propagated", False)
        dry_run: bool = getattr(args, "dry_run", False)
        slug_arg: Optional[str] = getattr(args, "slug", None)

        if propagated:
            n_cleared = _clear_propagated_needs_review(lore_dir, pages, dry_run=dry_run)
            verb = "Would clear" if dry_run else "Cleared"
            ok(f"{verb} needs_review on {n_cleared} propagated-only page(s).")
            return

        if not slug_arg:
            error("review clear requires a slug argument, or use --propagated")

        if not valid_slug(slug_arg):
            error(f"Invalid slug '{slug_arg}'", code=2)

        page_path = lore_dir / "pages" / f"{slug_arg}.md"
        if not page_path.is_file():
            error(f"No page found with slug '{slug_arg}'")
        set_frontmatter_field(page_path, "needs_review", False)
        ok(f"Cleared needs_review on '{slug_arg}'.")
        return

    error(f"Unknown review action: {action}")


def cmd_fact(args: argparse.Namespace) -> None:
    """Read a deterministic fact from lore/extracts/{slug}.json at a dotted path."""
    lore_dir = resolve_lore_dir(args.lore_dir)
    slug = args.slug
    dotted = args.dotted_path

    if not valid_slug(slug):
        print(f"ERROR: invalid slug '{slug}'", file=sys.stderr)
        sys.exit(2)

    extracts_path = lore_dir / "extracts" / f"{slug}.json"
    if not extracts_path.is_file():
        print(f"ERROR: no extract for slug '{slug}'", file=sys.stderr)
        sys.exit(2)

    try:
        data = json.loads(extracts_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, ValueError) as exc:
        print(f"ERROR: failed to parse extracts/{slug}.json: {exc}", file=sys.stderr)
        sys.exit(2)

    try:
        value = _resolve_dotted_path(data, dotted)
    except KeyError as exc:
        print(
            f"ERROR: Path '{dotted}' not found in extracts/{slug}.json (missing key: {exc})",
            file=sys.stderr,
        )
        sys.exit(1)

    # Print: scalars on one line without JSON wrapping quotes; objects/arrays pretty
    if isinstance(value, (dict, list)):
        print(json.dumps(value, indent=2, ensure_ascii=False))
    elif isinstance(value, bool):
        print(json.dumps(value))
    elif isinstance(value, (int, float)):
        print(json.dumps(value))
    else:
        # String: print bare (no surrounding quotes)
        print(value)


def cmd_graph(args: argparse.Namespace) -> None:
    """Build (or rebuild) lore/.graph.json from wikilinks across all pages."""
    lore_dir = resolve_lore_dir(args.lore_dir)

    if not lore_dir.is_dir():
        error(f"Lore directory not found: {lore_dir}")

    pages = scan_pages(lore_dir)
    graph = build_graph(lore_dir, pages)
    n_nodes = len(graph["nodes"])
    n_edges = len(graph["edges"])
    ok(f"Graph built. {n_nodes} nodes, {n_edges} edges.")


# ---------------------------------------------------------------------------
# New subcommands: inbox, pending, seen, doctor
# ---------------------------------------------------------------------------

def cmd_inbox(args: argparse.Namespace) -> None:
    """Write a quick free-text note into {lore}/inbox/, or list current notes."""
    lore_dir = resolve_lore_dir(args.lore_dir)

    if not lore_dir.is_dir():
        error(f"Lore directory not found: {lore_dir}")

    inbox_dir = lore_dir / "inbox"
    text: Optional[str] = getattr(args, "text", None)

    if text:
        inbox_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        note_path = inbox_dir / f"{ts}-note.md"
        n = 1
        while note_path.exists():
            note_path = inbox_dir / f"{ts}-{n}-note.md"
            n += 1
        note_path.write_text(f"# inbox note {now_iso()}\n\n{text}\n", encoding="utf-8")
        ok(f"Inbox note written: inbox/{note_path.name}")
        return

    # No text: list current inbox files (name + first line)
    if not inbox_dir.is_dir():
        ok("Inbox is empty.")
        return
    files = sorted(f for f in inbox_dir.iterdir() if f.is_file() and not f.name.startswith("."))
    if not files:
        ok("Inbox is empty.")
        return
    for f in files:
        try:
            first_line = f.read_text(encoding="utf-8", errors="replace").splitlines()[0] if f.stat().st_size else ""
        except (OSError, IndexError):
            first_line = ""
        print(f"inbox/{f.name}  {first_line}")


def _list_unseen_failures(failed_dir: Path) -> List[Path]:
    """Return failed/*.err files with no adjacent {name}.seen marker."""
    if not failed_dir.is_dir():
        return []
    return [
        errf
        for errf in sorted(failed_dir.glob("*.err"))
        if not (failed_dir / (errf.name + ".seen")).exists()
    ]


def _referenced_source_urls(pages: List[Tuple[str, Dict[str, Any], str]]) -> Set[str]:
    """Return every source URL any page's frontmatter `sources` list names."""
    urls: Set[str] = set()
    for _slug, meta, _body in pages:
        sources_list = meta.get("sources", [])
        if isinstance(sources_list, str):
            sources_list = [sources_list]
        if not isinstance(sources_list, list):
            continue
        for url in sources_list:
            urls.add(str(url).strip())
    return urls


def _find_orphan_hash_urls(
    pages: List[Tuple[str, Dict[str, Any], str]], hashes: Dict[str, Any]
) -> List[str]:
    """Return .hashes.json keys (URLs) that no page's sources list names.

    A hash entry becomes orphaned when a page's sources list changes or the
    page is deleted; the entry itself is harmless but stale bookkeeping
    (quarantine state, last_fetched) for a URL nothing tracks any more.
    """
    referenced = _referenced_source_urls(pages)
    return sorted(url for url in hashes if url not in referenced)


def _collect_pending(lore_dir: Path) -> Dict[str, Any]:
    """Aggregate everything actionable in a lore dir (shared by pending and doctor)."""
    today = datetime.now(timezone.utc).date()
    now_ts = datetime.now(timezone.utc).timestamp()

    # (a) unreconciled machine-generated diffs in queue/
    diffs: List[Dict[str, Any]] = []
    queue_dir = lore_dir / "queue"
    diff_name_re = re.compile(r"^(?P<slug>.+)-(?P<date>\d{4}-\d{2}-\d{2})\.diff\.md$")
    if queue_dir.is_dir():
        for f in sorted(queue_dir.glob("*.diff.md")):
            m = diff_name_re.match(f.name)
            if m:
                slug = m.group("slug")
                date_str = m.group("date")
                parsed = _parse_date_safe(date_str)
                if parsed is not None:
                    age_days = (today - parsed.date()).days
                else:
                    age_days = int((now_ts - f.stat().st_mtime) // 86400)
            else:
                slug = f.name[: -len(".diff.md")]
                date_str = ""
                age_days = int((now_ts - f.stat().st_mtime) // 86400)
            diffs.append({
                "file": f"queue/{f.name}",
                "slug": slug,
                "date": date_str,
                "age_days": age_days,
            })

    # (b) failed/*.err files with no .seen marker
    failed_dir = lore_dir / "failed"
    unseen = _list_unseen_failures(failed_dir)
    newest: Optional[Path] = max(unseen, key=lambda p: p.stat().st_mtime) if unseen else None
    failed_info: Dict[str, Any] = {
        "count": len(unseen),
        "newest": f"failed/{newest.name}" if newest else None,
    }

    # (c) quarantined sources from .hashes.json
    quarantined: List[Dict[str, Any]] = []
    hashes_path = lore_dir / ".hashes.json"
    if hashes_path.is_file():
        try:
            hashes: Dict[str, Any] = json.loads(hashes_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, ValueError):
            hashes = {}
        for url, entry in hashes.items():
            if isinstance(entry, dict) and entry.get("quarantined_until"):
                quarantined.append({
                    "url": url,
                    "slug": entry.get("slug", ""),
                    "quarantined_until": entry["quarantined_until"],
                })

    # (d) stale pages (shared helper with cmd_stale)
    stale = [
        dict(entry, page=f"pages/{entry['slug']}.md")
        for entry in _collect_stale_pages(scan_pages(lore_dir))
    ]

    # (e) inbox items
    inbox_dir = lore_dir / "inbox"
    inbox_files: List[str] = []
    if inbox_dir.is_dir():
        inbox_files = sorted(
            f.name for f in inbox_dir.iterdir() if f.is_file() and not f.name.startswith(".")
        )

    return {
        "diffs": diffs,
        "failed_unseen": failed_info,
        "quarantined": quarantined,
        "stale": stale,
        "inbox": {"count": len(inbox_files), "files": inbox_files},
    }


def _plural(n: int, word: str, plural: Optional[str] = None) -> str:
    """'1 diff' / '3 diffs' style formatting."""
    return f"{n} {word if n == 1 else (plural or word + 's')}"


def cmd_pending(args: argparse.Namespace) -> None:
    """Aggregate everything actionable: queued diffs, unseen failures, quarantined sources, stale pages, inbox notes."""
    lore_dir = resolve_lore_dir(args.lore_dir)

    if not lore_dir.is_dir():
        error(f"Lore directory not found: {lore_dir}")

    data = _collect_pending(lore_dir)
    n_diffs = len(data["diffs"])
    n_unseen = data["failed_unseen"]["count"]
    n_quar = len(data["quarantined"])
    n_stale = len(data["stale"])
    n_inbox = data["inbox"]["count"]
    total = n_diffs + n_unseen + n_quar + n_stale + n_inbox

    if getattr(args, "summary", False):
        if total == 0:
            return  # print NOTHING when nothing is pending (hook-friendly)
        segments: List[str] = []
        if n_diffs:
            segments.append(f"{_plural(n_diffs, 'diff')} queued")
        if n_unseen:
            segments.append(f"{_plural(n_unseen, 'unseen failure')}")
        if n_quar:
            segments.append(f"{_plural(n_quar, 'source')} quarantined")
        if n_stale:
            segments.append(f"{_plural(n_stale, 'page')} stale")
        if n_inbox:
            segments.append(f"{_plural(n_inbox, 'inbox note')}")
        print("lore: " + ", ".join(segments))
        return

    if getattr(args, "json", False):
        print(json.dumps(data, indent=2, ensure_ascii=False))
        return

    # Default: human-readable grouped list
    if total == 0:
        ok("Nothing pending.")
        return

    display_cap = 15
    print("Pending lore work:")
    print()
    if data["diffs"]:
        print(f"Queued diffs ({n_diffs}):")
        for d in data["diffs"][:display_cap]:
            print(f"  {d['file']} (slug {d['slug']}, {d['date'] or 'undated'}, {d['age_days']}d old)")
        if n_diffs > display_cap:
            print(f"  ... and {n_diffs - display_cap} more")
        print()
    if n_unseen:
        print(f"Unseen failures ({n_unseen}): newest {data['failed_unseen']['newest']}")
        print("  Mark reviewed with: lore.py seen --all")
        print()
    if data["quarantined"]:
        print(f"Quarantined sources ({n_quar}):")
        for q in data["quarantined"]:
            print(f"  {q['slug']} | {q['url']} | until {q['quarantined_until']}")
        print()
    if data["stale"]:
        print(f"Stale pages ({n_stale}):")
        for s in data["stale"]:
            print(f"  {s['page']} ({s['reason']})")
        print()
    if n_inbox:
        print(f"Inbox notes ({n_inbox}):")
        for name in data["inbox"]["files"][:display_cap]:
            print(f"  inbox/{name}")
        if n_inbox > display_cap:
            print(f"  ... and {n_inbox - display_cap} more")
        print()


def cmd_seen(args: argparse.Namespace) -> None:
    """Mark failed/*.err records as reviewed by writing {name}.seen marker files."""
    lore_dir = resolve_lore_dir(args.lore_dir)

    if not lore_dir.is_dir():
        error(f"Lore directory not found: {lore_dir}")

    failed_dir = lore_dir / "failed"
    mark_all: bool = getattr(args, "all", False)
    names: List[str] = getattr(args, "names", []) or []

    if not mark_all and not names:
        error("Provide .err file names to mark, or --all for every unmarked record.")

    if not failed_dir.is_dir():
        ok("Marked 0 failure records as seen (no failed/ directory).")
        return

    targets: List[Path] = []
    if mark_all:
        targets = _list_unseen_failures(failed_dir)
    else:
        for name in names:
            errf = failed_dir / Path(name).name
            if not errf.is_file():
                print(f"[WARN] failed/{Path(name).name} not found; skipping", file=sys.stderr)
                continue
            if (failed_dir / (errf.name + ".seen")).exists():
                continue
            targets.append(errf)

    stamp = now_iso()
    for errf in targets:
        (failed_dir / (errf.name + ".seen")).write_text(f"seen: {stamp}\n", encoding="utf-8")

    ok(f"Marked {len(targets)} failure record(s) as seen.")


def cmd_prune(args: argparse.Namespace) -> None:
    """Delete/drop retained bookkeeping data past its useful life.

    Three independent actions, each opt-in via its own flag so a bare
    `prune` with no flags does nothing destructive by accident:
      --failed-older-than N   delete failed/*.err records (and their .seen
                               marker, if any) older than N days (default 30)
      --orphan-hashes         drop .hashes.json entries no page's sources
                               list references any more (see C5), and (C19)
                               .refresh-cache/ files for URLs no page cites
      --run-logs-older-than N delete .gardener/run-*.jsonl records older
                               than N days (default 30)
    --dry-run reports counts for every requested action without deleting or
    writing anything.
    """
    lore_dir = resolve_lore_dir(args.lore_dir)
    if not lore_dir.is_dir():
        error(f"Lore directory not found: {lore_dir}")

    dry_run: bool = getattr(args, "dry_run", False)
    failed_older_than: Optional[int] = getattr(args, "failed_older_than", None)
    do_orphan_hashes: bool = getattr(args, "orphan_hashes", False)
    run_logs_older_than: Optional[int] = getattr(args, "run_logs_older_than", None)

    if failed_older_than is None and not do_orphan_hashes and run_logs_older_than is None:
        error("Provide at least one of --failed-older-than, --orphan-hashes, --run-logs-older-than.")

    now_ts = datetime.now(timezone.utc).timestamp()
    prefix = "[DRY-RUN] " if dry_run else ""

    if failed_older_than is not None:
        failed_dir = lore_dir / "failed"
        cutoff_seconds = failed_older_than * 86400
        n_deleted = 0
        if failed_dir.is_dir():
            for err_file in sorted(failed_dir.glob("*.err")):
                try:
                    age_seconds = now_ts - err_file.stat().st_mtime
                except OSError:
                    continue
                if age_seconds < cutoff_seconds:
                    continue
                seen_file = failed_dir / (err_file.name + ".seen")
                if not dry_run:
                    err_file.unlink(missing_ok=True)
                    seen_file.unlink(missing_ok=True)
                n_deleted += 1
        print(f"{prefix}Pruned {n_deleted} failed/ record(s) older than {failed_older_than}d.")

    if do_orphan_hashes:
        hashes_path = lore_dir / ".hashes.json"
        hashes: Dict[str, Any] = {}
        if hashes_path.is_file():
            try:
                hashes = json.loads(hashes_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, ValueError):
                hashes = {}
        pages = scan_pages(lore_dir)
        orphans = _find_orphan_hash_urls(pages, hashes)
        if orphans and not dry_run:
            for url in orphans:
                del hashes[url]
            tmp_path = hashes_path.with_suffix(".json.tmp")
            tmp_path.write_text(json.dumps(hashes, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            os.replace(str(tmp_path), str(hashes_path))
        for url in orphans:
            print(f"{prefix}Orphan hash entry: {url}")
        print(f"{prefix}Pruned {len(orphans)} orphan .hashes.json entr{'y' if len(orphans) == 1 else 'ies'}.")

        # C19: .refresh-cache/ files are keyed by sha256(url), independent of
        # .hashes.json membership, so this walks the same scan_pages() list
        # directly rather than reusing `orphans` above.
        cache_dir = lore_dir / ".refresh-cache"
        n_cache_deleted = 0
        if cache_dir.is_dir():
            referenced = _referenced_source_urls(pages)
            keep_names = {hashlib.sha256(u.encode("utf-8")).hexdigest() + ".txt" for u in referenced}
            for cache_file in sorted(cache_dir.glob("*.txt")):
                if cache_file.name in keep_names:
                    continue
                print(f"{prefix}Orphan cache file: {cache_file.name}")
                if not dry_run:
                    cache_file.unlink(missing_ok=True)
                n_cache_deleted += 1
        print(
            f"{prefix}Pruned {n_cache_deleted} orphan .refresh-cache/ "
            f"file{'' if n_cache_deleted == 1 else 's'}."
        )

    if run_logs_older_than is not None:
        gardener_dir = lore_dir / ".gardener"
        cutoff_seconds = run_logs_older_than * 86400
        n_deleted = 0
        if gardener_dir.is_dir():
            for run_log in sorted(gardener_dir.glob("run-*.jsonl")):
                try:
                    age_seconds = now_ts - run_log.stat().st_mtime
                except OSError:
                    continue
                if age_seconds < cutoff_seconds:
                    continue
                if not dry_run:
                    run_log.unlink(missing_ok=True)
                n_deleted += 1
        print(f"{prefix}Pruned {n_deleted} gardener run log(s) older than {run_logs_older_than}d.")


def cmd_doctor(args: argparse.Namespace) -> None:
    """End-to-end self-test. PASS/WARN/FAIL/INFO lines; exit 0 only when no FAIL."""
    results: List[Dict[str, str]] = []

    def add(status: str, check: str, detail: str) -> None:
        results.append({"status": status, "check": check, "detail": detail})

    def finish() -> None:
        has_fail = any(r["status"] == "FAIL" for r in results)
        if getattr(args, "json", False):
            print(json.dumps({"ok": not has_fail, "checks": results}, indent=2, ensure_ascii=False))
        else:
            for r in results:
                print(f"[{r['status']}] {r['check']}: {r['detail']}")
            n_fail = sum(1 for r in results if r["status"] == "FAIL")
            n_warn = sum(1 for r in results if r["status"] == "WARN")
            print()
            print(f"Doctor complete. {n_fail} FAIL, {n_warn} WARN, {len(results)} checks.")
        sys.exit(1 if has_fail else 0)

    # 0. Version (C16), reported first so it's visible without scrolling.
    add("INFO", "version", f"lore {LORE_VERSION}")

    # a. Python version. Floor is 3.12: 3.10 and 3.11 are security-only and
    # 3.10 reaches end of life in October 2026 (project-wide PYTHON FLOOR
    # 3.12 rule). The connectors' PEP 604 `X | Y` union annotations only
    # need 3.10 or newer (I11); that syntax constraint is unchanged, the
    # floor above it is set higher for support reasons. This is the CLI's
    # own advertised floor for running `lore.py`/`install.sh`/CI; it is
    # separate from the Python 3.9.6 compatibility this file and
    # lore-mcp-server.py keep at the source level for the Claude Desktop
    # MCP launch (A5), which runs `/usr/bin/python3` directly rather than
    # via this doctor check.
    if sys.version_info >= (3, 12):
        vi = sys.version_info
        add("PASS", "python", f"Python {vi.major}.{vi.minor}.{vi.micro} (>= 3.12)")
    else:
        add("FAIL", "python", f"Python {sys.version.split()[0]} is older than 3.12")

    # b. lore dir + pages
    lore_dir = resolve_lore_dir(args.lore_dir)
    if args.lore_dir:
        add("INFO", "lore-dir-source", "explicit --lore-dir")
    elif os.environ.get("LORE_DIR", "").strip():
        add("INFO", "lore-dir-source", "$LORE_DIR env var")
    elif (Path.cwd() / "lore").is_dir():
        add("INFO", "lore-dir-source", "{cwd}/lore")
    else:
        inferred = _script_relative_lore_dir()
        if inferred is not None and lore_dir == inferred:
            add("INFO", "lore-dir-source", f"script-relative default ({inferred})")
        else:
            add("INFO", "lore-dir-source", "~/.claude/lore (portable fallback)")
    if not lore_dir.is_dir():
        add("FAIL", "lore-dir", f"lore directory does not exist: {lore_dir}")
        finish()
        return
    add("PASS", "lore-dir", str(lore_dir))

    # c. pause sentinel: $LORE_DIR/.paused idles lore-gardener.sh, lore-refresh.sh
    # and lore-watchdog.sh without editing any config; surfaced here so a
    # forgotten sentinel is visible instead of silently starving the chain.
    if (lore_dir / ".paused").exists():
        add("WARN", "paused", f"{lore_dir / '.paused'} present; gardener/refresh/watchdog are all idling")
    else:
        add("PASS", "paused", "not paused")

    pages_dir = lore_dir / "pages"
    if not pages_dir.is_dir():
        add("FAIL", "pages", "pages/ directory missing")
        n_pages = 0
    else:
        n_pages = len(list(pages_dir.glob("*.md")))
        if n_pages == 0:
            add("WARN", "pages", "pages/ exists but holds 0 pages")
        else:
            add("PASS", "pages", f"{n_pages} pages")

    # c. live fetch self-test through the same path refresh uses
    if getattr(args, "offline", False):
        add("SKIP", "fetch", "live fetch skipped (--offline)")
    else:
        test_url = os.environ.get("LORE_DOCTOR_URL", "https://example.com")
        connector = _load_connector(test_url, lore_dir / "connectors")
        connector_name = getattr(connector, "__name__", type(connector).__name__)
        try:
            content, _etag = connector.fetch(test_url, timeout=10)
            add("PASS", "fetch", f"{test_url} fetched ({len(content)} chars) via {connector_name}")
        except Exception as exc:
            add("FAIL", "fetch", f"{test_url} failed via {connector_name}: {type(exc).__name__}: {exc}")

    # d. search index freshness
    db_path = lore_dir / ".search.db"
    if not db_path.is_file():
        add("WARN", "search-db", ".search.db missing; run lore.py index")
    else:
        newest_page_mtime = 0.0
        if pages_dir.is_dir():
            newest_page_mtime = max(
                (p.stat().st_mtime for p in pages_dir.glob("*.md")), default=0.0
            )
        if db_path.stat().st_mtime >= newest_page_mtime:
            add("PASS", "search-db", ".search.db is current")
        else:
            add("WARN", "search-db", "index stale, run lore.py index")

    # e. graph
    if (lore_dir / ".graph.json").is_file():
        add("PASS", "graph", ".graph.json exists")
    else:
        add("WARN", "graph", ".graph.json missing; run lore.py index or lore.py graph")

    # f. refresh log recency (scan BACKWARD for the last [timestamp] line)
    try:
        interval_sec = int(os.environ.get("LORE_REFRESH_INTERVAL_SEC", "21600") or 21600)
    except ValueError:
        interval_sec = 21600
    refresh_log = lore_dir / ".refresh.log"
    ts_re = re.compile(r"^\[(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)\]")
    if not refresh_log.is_file():
        add("WARN", "refresh-log", ".refresh.log not found (refresh timer may never have run here)")
    else:
        last_ts: Optional[str] = None
        try:
            log_lines = refresh_log.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            log_lines = []
        for line in reversed(log_lines):
            m = ts_re.match(line.strip())
            if m:
                last_ts = m.group(1)
                break
        if last_ts is None:
            add("FAIL", "refresh-log", "refresh timer not running (no parseable [timestamp] line in .refresh.log)")
        else:
            ts = datetime.strptime(last_ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            age_sec = (datetime.now(timezone.utc) - ts).total_seconds()
            if age_sec <= 2 * interval_sec:
                add("PASS", "refresh-log", f"last entry {last_ts} ({int(age_sec // 60)} min ago)")
            else:
                add(
                    "FAIL",
                    "refresh-log",
                    f"refresh timer not running (last entry {last_ts}, "
                    f"{int(age_sec // 3600)}h ago, threshold {2 * interval_sec // 3600}h)",
                )

    # g. watchdog log (skip silently when the file does not exist)
    watchdog_log = lore_dir / ".watchdog.log"
    if watchdog_log.is_file():
        wd_age_min = (datetime.now(timezone.utc).timestamp() - watchdog_log.stat().st_mtime) / 60
        if wd_age_min <= 30:
            add("PASS", "watchdog", f"last tick {int(wd_age_min)} min ago")
        else:
            add("WARN", "watchdog", f"last tick {int(wd_age_min)} min ago (> 30 min)")

    # h. scheduler job detection (C18). The old check matched a bare "lore"
    # regex against every launchctl label on the machine, which over-matched
    # unrelated labels containing that substring and, worse, kept reporting
    # PASS for a throwaway --lore-dir test install just because the *real*
    # install's labels were still loaded under the same user session. Scope
    # to LORE_LAUNCHD_LABEL_PREFIX (default "com.lore.") on macOS instead;
    # try `systemctl --user list-timers` (systemd) then `crontab -l` (cron)
    # as a last resort on other platforms, matching "lore-" (the systemd/cron
    # template naming convention) or the label prefix. WARN, never FAIL, when
    # nothing is found anywhere: a manual or no-scheduler install (someone
    # running the scripts by hand, or via a different scheduler entirely) is
    # a valid configuration, not a broken one.
    label_prefix = os.environ.get("LORE_LAUNCHD_LABEL_PREFIX", "com.lore.")
    scheduler_found = False
    if sys.platform == "darwin":
        try:
            proc = subprocess.run(
                ["launchctl", "list"], capture_output=True, text=True, timeout=15
            )
            labels = [
                ln.split()[-1]
                for ln in proc.stdout.splitlines()
                if label_prefix in ln
            ]
            if labels:
                add(
                    "PASS",
                    "scheduler",
                    f"{len(labels)} launchd label(s) matching '{label_prefix}*': {', '.join(labels[:3])}",
                )
                scheduler_found = True
            else:
                add("WARN", "scheduler", f"no launchd label matching '{label_prefix}*' found")
        except Exception as exc:
            add("WARN", "scheduler", f"could not run launchctl list: {type(exc).__name__}: {exc}")
    else:
        try:
            proc = subprocess.run(
                ["systemctl", "--user", "list-timers"],
                capture_output=True,
                text=True,
                timeout=15,
            )
            timers = [ln for ln in proc.stdout.splitlines() if "lore-" in ln]
            if timers:
                add("PASS", "scheduler", f"{len(timers)} systemd timer(s) matching 'lore-*'")
                scheduler_found = True
        except Exception:
            pass
        if not scheduler_found:
            try:
                proc = subprocess.run(
                    ["crontab", "-l"], capture_output=True, text=True, timeout=15
                )
                cron_lines = [
                    ln
                    for ln in proc.stdout.splitlines()
                    if "lore-" in ln or label_prefix in ln
                ]
                if cron_lines:
                    add("PASS", "scheduler", f"{len(cron_lines)} crontab line(s) matching 'lore-*'")
                    scheduler_found = True
            except Exception:
                pass
        if not scheduler_found:
            add("WARN", "scheduler", "no systemd timer or crontab entry matching 'lore-*' found")

    # i. unseen failures (WARN if > 0) + quarantined sources (INFO)
    unseen = _list_unseen_failures(lore_dir / "failed")
    if unseen:
        add("WARN", "failed", f"{len(unseen)} unseen failure record(s); review then `lore.py seen --all`")
    else:
        add("PASS", "failed", "no unseen failure records")

    n_quarantined = 0
    hashes_path = lore_dir / ".hashes.json"
    if hashes_path.is_file():
        try:
            hashes_data: Dict[str, Any] = json.loads(hashes_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, ValueError):
            hashes_data = {}
        n_quarantined = sum(
            1
            for entry in hashes_data.values()
            if isinstance(entry, dict) and entry.get("quarantined_until")
        )
    add("INFO", "quarantine", f"{n_quarantined} quarantined source(s)")

    # j. queue diffs older than 48h
    queue_dir = lore_dir / "queue"
    old_diffs = 0
    total_diffs = 0
    if queue_dir.is_dir():
        now_ts = datetime.now(timezone.utc).timestamp()
        for f in queue_dir.glob("*.diff.md"):
            total_diffs += 1
            if now_ts - f.stat().st_mtime > 48 * 3600:
                old_diffs += 1
    if old_diffs:
        add("WARN", "queue", f"gardener not consuming the queue: {old_diffs} diff(s) older than 48h (of {total_diffs})")
    else:
        add("PASS", "queue", f"no diffs older than 48h ({total_diffs} queued)")

    # k. git repo state (INFO only, never FAIL)
    if (lore_dir / ".git").exists():
        try:
            proc = subprocess.run(
                ["git", "-C", str(lore_dir), "status", "--porcelain"],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if proc.returncode == 0:
                n_dirty = len([ln for ln in proc.stdout.splitlines() if ln.strip()])
                add("INFO", "git", f"dirty ({n_dirty} changed path(s))" if n_dirty else "clean")
            else:
                add("INFO", "git", f"git status failed: {proc.stderr.strip()}")
        except Exception as exc:
            add("INFO", "git", f"git status unavailable: {type(exc).__name__}: {exc}")
    else:
        add("INFO", "git", "lore dir is not a git repository")

    finish()


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    """Build the argparse parser with all subcommands."""
    parser = argparse.ArgumentParser(
        prog="lore",
        description="Persistent LLM-maintained knowledge base CLI.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"lore {LORE_VERSION}",
        help="Show the lore CLI version and exit.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # bootstrap
    p_bootstrap = subparsers.add_parser(
        "bootstrap", help="Initialize a new lore directory."
    )
    p_bootstrap.add_argument(
        "--scope",
        choices=["global", "project"],
        default="global",
        help="Scope: global ($LORE_DIR or the default global lore dir) or project ({cwd}/lore/).",
    )
    p_bootstrap.add_argument("--lore-dir", help="Explicit lore directory path.")

    # index
    p_index = subparsers.add_parser(
        "index", help="Rebuild index.md and .search.db from pages."
    )
    p_index.add_argument("--lore-dir", help="Explicit lore directory path.")

    # search
    p_search = subparsers.add_parser("search", help="Search lore pages.")
    p_search.add_argument("query", help="Search query string.")
    p_search.add_argument("--lore-dir", help="Explicit lore directory path.")
    p_search.add_argument(
        "--limit", type=int, default=5, help="Max results (default: 5)."
    )

    # lint
    p_lint = subparsers.add_parser("lint", help="Check pages for common issues.")
    p_lint.add_argument("--lore-dir", help="Explicit lore directory path.")
    p_lint.add_argument(
        "--check-links",
        action="store_true",
        default=False,
        help="Check source URLs for dead links (slow, opt-in).",
    )

    # check-entity
    p_check_entity = subparsers.add_parser(
        "check-entity", help="Look up a name in the entity registry."
    )
    p_check_entity.add_argument("name", help="Entity name to look up.")
    p_check_entity.add_argument("--lore-dir", help="Explicit lore directory path.")

    # ingest
    p_ingest = subparsers.add_parser(
        "ingest", help="Ingest a source document into lore."
    )
    p_ingest.add_argument("source_path", help="Path to source file to ingest.")
    p_ingest.add_argument("--lore-dir", help="Explicit lore directory path.")

    # stale
    p_stale = subparsers.add_parser(
        "stale", help="List auto-update pages that are overdue."
    )
    p_stale.add_argument("--lore-dir", help="Explicit lore directory path.")

    # log
    p_log = subparsers.add_parser("log", help="Append a message to lore log.")
    p_log.add_argument("message", help="Log message text.")
    p_log.add_argument("--lore-dir", help="Explicit lore directory path.")

    # sync
    p_sync = subparsers.add_parser("sync", help="Sync lore directory via git.")
    p_sync.add_argument("--lore-dir", help="Explicit lore directory path.")

    # refresh
    p_refresh = subparsers.add_parser(
        "refresh",
        help="Check sources for changes using SHA-256 hashing; update .hashes.json.",
    )
    p_refresh.add_argument("--lore-dir", help="Explicit lore directory path.")
    p_refresh.add_argument(
        "--force",
        action="store_true",
        default=False,
        help="Recompute hashes even when the stored hash matches (forces an entry for first-fetch pages).",
    )
    p_refresh.add_argument(
        "--slug",
        default=None,
        help="Restrict refresh to a single page slug instead of all auto_update pages.",
    )

    # fact
    p_fact = subparsers.add_parser(
        "fact",
        help="Read a deterministic value from lore/extracts/{slug}.json at a dotted path.",
    )
    p_fact.add_argument("slug", help="Extract slug (e.g. 'anthropic-api').")
    p_fact.add_argument(
        "dotted_path",
        help="Dotted key path into the JSON (e.g. 'models.example-model-large.context_window').",
    )
    p_fact.add_argument("--lore-dir", help="Explicit lore directory path.")

    # graph
    p_graph = subparsers.add_parser(
        "graph",
        help="Build (or rebuild) lore/.graph.json from [[wikilinks]] across all pages.",
    )
    p_graph.add_argument("--lore-dir", help="Explicit lore directory path.")

    # inbox
    p_inbox = subparsers.add_parser(
        "inbox",
        help="Write a quick note into {lore}/inbox/, or list current inbox notes.",
    )
    p_inbox.add_argument(
        "text",
        nargs="?",
        default=None,
        help="Note text. Omit to list current inbox notes instead.",
    )
    p_inbox.add_argument("--lore-dir", help="Explicit lore directory path.")

    # pending
    p_pending = subparsers.add_parser(
        "pending",
        help="Aggregate everything actionable: queued diffs, unseen failures, quarantined sources, stale pages, inbox notes.",
    )
    p_pending.add_argument("--lore-dir", help="Explicit lore directory path.")
    p_pending.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="Full machine-readable JSON output.",
    )
    p_pending.add_argument(
        "--summary",
        action="store_true",
        default=False,
        help="Exactly one line for session-start hooks; prints nothing when idle.",
    )

    # seen
    p_seen = subparsers.add_parser(
        "seen",
        help="Mark failed/*.err records as reviewed by writing {name}.seen markers.",
    )
    p_seen.add_argument(
        "names",
        nargs="*",
        help=".err file names inside failed/ to mark (e.g. slug-2026-07-08.err).",
    )
    p_seen.add_argument(
        "--all",
        action="store_true",
        default=False,
        help="Mark every unmarked failed/*.err record.",
    )
    p_seen.add_argument("--lore-dir", help="Explicit lore directory path.")

    # prune
    p_prune = subparsers.add_parser(
        "prune",
        help="Delete/drop retained bookkeeping data past its useful life (failed/ records, orphan hashes, gardener run logs).",
    )
    p_prune.add_argument(
        "--failed-older-than",
        type=int,
        default=None,
        metavar="N",
        help="Delete failed/*.err records (and their .seen marker) older than N days.",
    )
    p_prune.add_argument(
        "--orphan-hashes",
        action="store_true",
        default=False,
        help="Drop .hashes.json entries whose URL no page's sources list references.",
    )
    p_prune.add_argument(
        "--run-logs-older-than",
        type=int,
        default=None,
        metavar="N",
        help="Delete .gardener/run-*.jsonl records older than N days.",
    )
    p_prune.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Report counts for every requested action without deleting or writing anything.",
    )
    p_prune.add_argument("--lore-dir", help="Explicit lore directory path.")

    # doctor
    p_doctor = subparsers.add_parser(
        "doctor",
        help="End-to-end self-test with PASS/WARN/FAIL lines; exit 0 only when no FAIL.",
    )
    p_doctor.add_argument("--lore-dir", help="Explicit lore directory path.")
    p_doctor.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="Machine-readable JSON output.",
    )
    p_doctor.add_argument(
        "--offline",
        action="store_true",
        default=False,
        help="Skip the live fetch self-test.",
    )

    # review
    p_review = subparsers.add_parser(
        "review", help="List or clear needs_review flags."
    )
    review_sub = p_review.add_subparsers(dest="review_action", required=True)

    p_review_list = review_sub.add_parser(
        "list", help="List pages currently flagged needs_review."
    )
    p_review_list.add_argument("--lore-dir", help="Explicit lore directory path.")

    p_review_clear = review_sub.add_parser(
        "clear", help="Clear needs_review on one page, or on every eligible propagated flag."
    )
    p_review_clear.add_argument(
        "slug", nargs="?", default=None, help="Page slug to clear. Omit with --propagated."
    )
    p_review_clear.add_argument(
        "--propagated",
        action="store_true",
        default=False,
        help="Clear every page flagged only because a dependency changed, not its own source.",
    )
    p_review_clear.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="With --propagated, print the count without writing any change.",
    )
    p_review_clear.add_argument("--lore-dir", help="Explicit lore directory path.")

    return parser


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

COMMAND_MAP = {
    "bootstrap": cmd_bootstrap,
    "index": cmd_index,
    "search": cmd_search,
    "lint": cmd_lint,
    "check-entity": cmd_check_entity,
    "ingest": cmd_ingest,
    "stale": cmd_stale,
    "log": cmd_log,
    "sync": cmd_sync,
    "refresh": cmd_refresh,
    "fact": cmd_fact,
    "graph": cmd_graph,
    "inbox": cmd_inbox,
    "pending": cmd_pending,
    "seen": cmd_seen,
    "doctor": cmd_doctor,
    "review": cmd_review,
    "prune": cmd_prune,
}


def main() -> None:
    """Entry point: parse args and dispatch to subcommand handler."""
    parser = build_parser()
    args = parser.parse_args()
    handler = COMMAND_MAP.get(args.command)
    if handler:
        handler(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
