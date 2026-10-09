"""Git repository connector. Returns HEAD SHA via `git ls-remote {url} HEAD` with explicit timeout."""
# Postponed evaluation of annotations: this file's signatures use PEP 604
# `X | Y` unions, which only evaluate natively on Python 3.10+.
from __future__ import annotations

import hashlib
import os
import re
import subprocess

# Allowed URL forms for `git ls-remote`. A page's frontmatter sources list is
# untrusted input (S7): without this allowlist and the leading-dash reject
# below, a source URL string could be interpreted as a git/ssh option (for
# example an `-oProxyCommand=...`-style argument) rather than a repository
# address (C8).
_SCP_STYLE_RE = re.compile(r"^[^@\s/-][^@\s]*@[^:\s]+:.+$")


def _validate_url(url: str) -> None:
    if not url or url.startswith("-"):
        raise RuntimeError(f"refusing git URL starting with '-': {url!r}")
    if url.startswith(("https://", "ssh://", "git://", "git+https://")):
        return
    if _SCP_STYLE_RE.match(url):
        return
    raise RuntimeError(f"unrecognized git URL scheme: {url!r}")


def fetch(url: str, timeout: int = 30) -> tuple[str, str | None]:
    """Return (head_sha_string, None). Useful for tracking 'latest commit on default branch'."""
    _validate_url(url)
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        result = subprocess.run(
            ["git", "ls-remote", "--", url, "HEAD"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=True,
            env=env,
        )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"git ls-remote timeout after {timeout}s for {url}") from e
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"git ls-remote failed for {url}: {e.stderr.strip()}") from e
    except FileNotFoundError as e:
        raise RuntimeError("git binary not found on PATH") from e

    out = result.stdout.strip()
    if not out:
        raise RuntimeError(f"git ls-remote returned empty output for {url}")
    sha = out.split()[0]
    return sha, None


def hash_content(content: str) -> str:
    """For git: the SHA itself is already a content hash, but we re-hash for uniformity with other connectors."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()
