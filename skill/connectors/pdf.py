"""PDF connector. Downloads (if URL) and shells out to a Mistral-OCR Python subprocess.

The subprocess imports `mistralai` so this connector is the only one that depends on a non-stdlib
package. The dependency is isolated in the subprocess; lore.py and the rest of connectors stay
stdlib-only.

Downloading is curl-first via the shared _http.py helper's curl_fetch (which
returns raw bytes, not the text-decoding fetch()/urllib_fetch(): a PDF is
binary, and decoding it as UTF-8 would corrupt it). urllib is the fallback
when curl is not installed, kept local to this file for that same reason; see
_http.py's own docstring for why every connector loads it standalone rather
than importing it as a package.
"""
# Postponed evaluation of annotations: this file's signatures use PEP 604
# `X | Y` unions, which only evaluate natively on Python 3.10+.
from __future__ import annotations

import hashlib
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import urllib.request
import urllib.error

_UA = "lore-refresh/1.0"


def _load_http():
    spec = importlib.util.spec_from_file_location("_lore_http", Path(__file__).with_name("_http.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_http = _load_http()


_OCR_INVOCATION = """
import os
import sys
from mistralai import Mistral

api_key = os.environ.get("MISTRAL_API_KEY")
if not api_key:
    sys.stderr.write("MISTRAL_API_KEY not set\\n")
    sys.exit(2)

path = sys.argv[1]
client = Mistral(api_key=api_key)
with open(path, "rb") as f:
    uploaded = client.files.upload(file={"file_name": os.path.basename(path), "content": f}, purpose="ocr")
signed = client.files.get_signed_url(file_id=uploaded.id)
result = client.ocr.process(model="mistral-ocr-latest", document={"type": "document_url", "document_url": signed.url})
out = []
for page in result.pages:
    out.append(page.markdown)
sys.stdout.write("\\n\\n".join(out))
"""


def _urllib_download(url: str, timeout: int) -> bytes:
    """urllib binary fallback used only when curl is missing.

    Kept local rather than in _http.py: this must return raw bytes for the
    OCR subprocess, while _http.py's urllib_fetch decodes to str, which would
    corrupt binary PDF data. Bounded read matches _http.py's MAX_FETCH_BYTES
    cap (C10).
    """
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(_http.MAX_FETCH_BYTES + 1)
            if len(raw) > _http.MAX_FETCH_BYTES:
                raise RuntimeError(f"response for {url} exceeds {_http.MAX_FETCH_BYTES}-byte cap")
            return raw
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code} downloading PDF {url}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"URL error downloading PDF {url}: {e.reason}") from e
    except TimeoutError as e:
        raise RuntimeError(f"Timeout after {timeout}s downloading PDF {url}") from e


def _download(url: str, timeout: int) -> str:
    """Download URL to a temp file (curl-first, binary-safe), return the path."""
    try:
        data, _etag = _http.curl_fetch(url, timeout)
    except FileNotFoundError:
        data = _urllib_download(url, timeout)
    fd, path = tempfile.mkstemp(suffix=".pdf", prefix="lore-")
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    return path


def fetch(url: str, timeout: int = 60) -> tuple[str, str | None]:
    """Return (markdown_text_of_pdf, None). The OCR subprocess gets up to 300s wall-clock."""
    if not os.environ.get("MISTRAL_API_KEY"):
        raise RuntimeError("MISTRAL_API_KEY not set; pdf connector cannot run")

    if url.startswith(("http://", "https://")):
        local_path = _download(url, timeout=timeout)
        cleanup = True
    else:
        local_path = url
        cleanup = False

    try:
        proc = subprocess.run(
            [sys.executable, "-c", _OCR_INVOCATION, local_path],
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        if cleanup:
            os.unlink(local_path)
        raise RuntimeError(f"Mistral OCR subprocess timed out after 300s for {url}") from e

    if cleanup:
        try:
            os.unlink(local_path)
        except OSError:
            pass

    if proc.returncode != 0:
        raise RuntimeError(f"Mistral OCR failed (rc={proc.returncode}): {proc.stderr.strip()}")
    return proc.stdout, None


def hash_content(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()
