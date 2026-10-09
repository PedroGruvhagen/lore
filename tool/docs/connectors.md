# Connectors

A connector fetches one source URL and turns it into the text a page's `queue/` diff or
`extracts/` file is built from. `lore.py refresh` and `lore.py doctor` (its live fetch
self-test) both go through the same dispatch. `lore.py ingest` does not fetch anything: it
archives a local file into `sources/` and never touches a connector.

## Dispatch

`_pick_connector_name(url)` in `lore.py` maps a URL to a module name with a fixed set of
rules, checked in order: `pypi.org` → `pypi`; `npmjs.com`/`npmjs.org`/`registry.npmjs.org` →
`npm`; a GitHub URL containing `/releases` → `github_releases`; a URL ending `.rss`/`.atom`,
containing `feed.xml`, or containing `/feed` → `rss`; a URL ending `.pdf` → `pdf`; a `git@`,
`git://`, `git+https://`, or `.git`-suffixed URL → `git_repo`; anything else → `web`.
`_load_connector()` then loads `connectors/<name>.py` as a standalone module via
`importlib.util.spec_from_file_location`, not a package import (the same technique the
connector loads its own `_http.py` helper with, see below), so a connector file can be
copied on its own and still work.

Every connector module exposes the same two functions: `fetch(url, timeout=30) ->
(content, etag_or_none)` (`pdf.py` alone defaults to `timeout=60`) and
`hash_content(content) -> str` (a SHA-256 hex digest, used against `.hashes.json` to detect
a change).

## The shared HTTP helper

`connectors/_http.py` is not a connector itself; `_load_connector` refuses to dispatch to any
module name starting with `_`. It holds `curl_fetch`, `urllib_fetch`, `fetch`,
`parse_curl_headers`, and the `MAX_FETCH_BYTES` constant (25 MB), used by every text-based
connector (`web.py`, `npm.py`, `pypi.py`, `rss.py`, `github_releases.py`) so the curl-first,
urllib-fallback logic and the size cap exist in one place instead of being duplicated per
connector. Each connector loads it the same way: `importlib.util.spec_from_file_location`
against `Path(__file__).with_name("_http.py")`, which works whether the connector is loaded
standalone or as part of a full `connectors/` directory.

`git_repo.py` does not use `_http.py`: it validates the URL scheme (`https://`, `ssh://`,
`git://`, `git+https://`, or a scp-style `user@host:path`; anything starting with `-` is
rejected, and `--` is passed before the URL) and runs `git ls-remote` with
`GIT_TERMINAL_PROMPT=0`, since a source's freshness there is a remote's `HEAD` ref rather
than an HTTP body.

`github_releases.py` sends its `Authorization: Bearer $GITHUB_TOKEN` header through
`curl --config -` on stdin rather than a `-H` command-line argument, the same pattern
`_send_telegram_alert()` in `tool/skill/scripts/lore-common.sh` uses for its Telegram bot token
(see `maintenance-loop.md`), so the token never appears in a process listing.

## The PDF connector's one non-stdlib dependency

Every connector above imports only the Python standard library. `pdf.py` is the one
exception: it shells out to a separate Python subprocess (`sys.executable -c <script>`) that
imports `mistralai` and calls the Mistral OCR API, so the dependency is isolated in that
subprocess and never imported by `lore.py`, `lore-mcp-server.py`, or any other connector.
`pdf.fetch()` raises `RuntimeError` immediately, before downloading anything, if
`MISTRAL_API_KEY` is not set in the environment; the OCR subprocess itself is given up to 300
seconds. `tool/skill/requirements.txt` documents this as the one optional dependency; install
`mistralai` yourself only if you have PDF sources and a Mistral API key. Every other
connector, and `lore.py`/`lore-mcp-server.py` themselves, need nothing beyond the standard
library.

## Writing your own connector

Drop a new `<name>.py` into `$LORE_DIR/connectors/` (or `tool/skill/connectors/` before an
install copies it) exposing `fetch(url, timeout=30) -> (content, etag)` and
`hash_content(content) -> str`, and add a rule for it to `_pick_connector_name()`. If your
connector needs a size cap, curl/urllib fallback, or header parsing, load `_http.py` the way
the shipped text connectors do rather than reimplementing it. If it needs a package outside
the standard library, isolate that import in a subprocess the way `pdf.py` does, and document
the exception in `tool/skill/requirements.txt` and here.
