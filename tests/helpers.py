"""Shared test infrastructure for the lore test suite.

Every test module in this package imports from here rather than
reimplementing subprocess plumbing, so the suite exercises the shipped
CLI (skill/scripts/lore.py) the same way a real user or the gardener
would: as a subprocess, never by importing lore.py's functions directly
into the test process (the one exception is test_connectors.py, which
loads connector modules the same standalone way lore.py itself does).
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILL_DIR = REPO_ROOT / "skill"
LORE_PY = SKILL_DIR / "scripts" / "lore.py"
MCP_SERVER_PY = SKILL_DIR / "scripts" / "lore-mcp-server.py"
CONNECTORS_DIR = SKILL_DIR / "connectors"


def run_lore(*args: str, cwd: Optional[str] = None, env: Optional[Dict[str, str]] = None,
             timeout: int = 60) -> subprocess.CompletedProcess:
    """Run `python3 skill/scripts/lore.py <args>` as a subprocess.

    Uses sys.executable (not a hardcoded "python3") so the suite exercises
    whichever interpreter is currently running the tests, matching the
    completion criteria of passing under both system python3 and
    python3.12.
    """
    cmd = [sys.executable, str(LORE_PY), *args]
    run_env = dict(os.environ)
    if env:
        run_env.update(env)
    return subprocess.run(
        cmd, capture_output=True, text=True, cwd=cwd, env=run_env, timeout=timeout
    )


def bootstrap_tmp(scope: str = "global") -> tempfile.TemporaryDirectory:
    """Create a fresh temp dir, run `lore.py bootstrap` against it, and
    export LORE_DIR so subprocess children (including run_lore calls that
    omit --lore-dir) pick it up.

    Returns the TemporaryDirectory object. The caller owns cleanup: either
    use it as a context manager (`with bootstrap_tmp() as d:`) or call
    `.cleanup()` explicitly. os.environ["LORE_DIR"] is left pointing at the
    directory until the caller resets or overwrites it; tests that care
    about isolation from other tests should restore the previous value (or
    pop the key) in tearDown.
    """
    d = tempfile.TemporaryDirectory(prefix="lore-test-")
    os.environ["LORE_DIR"] = d.name
    result = run_lore("bootstrap", "--lore-dir", d.name, "--scope", scope)
    assert result.returncode == 0, (
        f"bootstrap_tmp: `lore.py bootstrap` failed (rc={result.returncode}): "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    return d


def _serialize_frontmatter_value(key: str, value: Any) -> str:
    """Serialize one frontmatter field into lines matching the exact
    syntax parse_frontmatter() (skill/scripts/lore.py) understands: flat
    "key: value" pairs, and lists as an empty "key:" line followed by
    "  - item" continuation lines.
    """
    if isinstance(value, bool):
        return f"{key}: {'true' if value else 'false'}\n"
    if isinstance(value, (list, tuple)):
        lines = [f"{key}:\n"]
        for item in value:
            lines.append(f"  - {item}\n")
        return "".join(lines)
    return f"{key}: {value}\n"


def write_page(slug: str, frontmatter: Dict[str, Any], body: str,
                lore_dir: Optional[str] = None) -> Path:
    """Write pages/{slug}.md under lore_dir (default: $LORE_DIR) with the
    given frontmatter dict and body text, in the exact YAML-ish shape
    parse_frontmatter() parses. Returns the written Path.
    """
    base = Path(lore_dir) if lore_dir else Path(os.environ["LORE_DIR"])
    pages_dir = base / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)
    fm_lines = "".join(_serialize_frontmatter_value(k, v) for k, v in frontmatter.items())
    text = f"---\n{fm_lines}---\n{body}"
    page_path = pages_dir / f"{slug}.md"
    page_path.write_text(text, encoding="utf-8")
    return page_path
