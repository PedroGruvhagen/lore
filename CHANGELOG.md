# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

## [Unreleased]

### Added

- Claude Code plugin `lore` (`.claude-plugin/`, `hooks/`, `docs/claude-code-plugin.md`): MEMORY.md
  write guard, session index loader, `recall` and `remember` tools, `/lore` status command and
  compaction reminder. Path options have no machine-specific defaults and are required.
- This repository is also a Claude Code plugin marketplace (`.claude-plugin/marketplace.json`).

### Changed

- License changed from PolyForm Noncommercial 1.0.0 to Apache License 2.0.
- `scripts/privacy-grep.sh` no longer carries a built-in list of personal strings. It ships
  credential shapes only and reads personal strings from a private file named by
  `LORE_PRIVACY_BLOCKLIST`.
- Repository history was restarted from a clean initial commit.

## [1.0.0] - First public release

This is the first public release of Lore. Everything below is described relative to the
private install this package was rebuilt from; there is no earlier public version to diff
against.

### Added

- `install.sh`: installs the skill into `<config-dir>/skills/lore/`, bootstraps a lore data
  directory, writes `.lore.env` from a commented example, and installs a scheduler job
  (launchd, systemd `--user`, or a three-line crontab entry) with auto-detection by
  platform. Supports `--dry-run`, `--render-only`, and `--uninstall`.
- Portable scheduler templates: `launchd/com.lore.{gardener,refresh,watchdog}.plist.template`
  and `systemd/lore-{gardener,refresh,watchdog}.{service,timer}.template`, rendered with a
  fixed set of `__TOKEN__` placeholders (`__HOME__`, `__SKILL_DIR__`, `__LORE_DIR__`,
  `__LABEL_PREFIX__`, `__PATH__`). The installer refuses to write a rendered file that still
  contains `__`.
- `skill/scripts/lore-common.sh`: a shared portability layer sourced by the four other shell
  scripts (`lore-refresh.sh`, `lore-gardener.sh`, `lore-watchdog.sh`, `lore-alert.sh`).
  Provides the platform-specific helpers `mtime_of()`, `notify_desktop()`, `inhibit_sleep()`,
  `scheduler_kick_refresh()`, and `scheduler_has_job()` (each with a macOS path and a Linux
  fallback, or a documented no-op), plus `classify_result_line()` (pure string matching, no
  platform branch) and `alert()` (dispatches to the configured provider, calling
  `notify_desktop()` for the `desktop` case).
- Failure classification for the nightly gardener: the last `type:result` line of a run is
  matched against fixed patterns and classified as `network`, `quota`, `auth`, `sleep`,
  `killed`, or `unknown`; network and sleep failures retry once, a quota failure retries only
  if the stated reset time is still inside the run's deadline.
- Alert providers, selected by `LORE_ALERT_PROVIDER`: `log` (default, always-on), `desktop`
  (AppleScript on macOS, `notify-send` on Linux), `telegram` (`_send_telegram_alert()` in
  `skill/scripts/lore-common.sh`, a direct `curl` to the Telegram Bot API with the request
  body piped over stdin so the token never appears in a process listing), and `command`
  (runs a configured relay script with the subject on argv and the body on stdin).
- `lore.py review list` and `lore.py review clear <slug>|--propagated`: lists or clears
  `needs_review` flags; `--propagated` clears only flags with no supporting evidence (no
  `auto_update: true`, no matching `queue/` diff, no recent `failed/*.err`).
- `lore.py prune --failed-older-than N`, `--orphan-hashes`, `--run-logs-older-than N`, each
  opt-in and each supporting `--dry-run`.
- `lore.py --version` and a `version` line in `lore.py doctor`.
- FTS5 query quoting: every whitespace-separated search token is now wrapped in double
  quotes before being sent to SQLite, so hyphenated or punctuated queries no longer raise
  `OperationalError` or silently fall back to a worse-ranked substring search.
- A shared `connectors/_http.py` (`curl_fetch`, `urllib_fetch`, `fetch`, `parse_curl_headers`,
  `MAX_FETCH_BYTES`) used by every connector, replacing seven duplicated fetch
  implementations.
- A response size cap (`MAX_FETCH_BYTES`, 25 MB) on every connector fetch.
- `tests/`: a stdlib `unittest` suite (`tests/run.sh`) covering bootstrap, indexing and
  search, lint, fact extraction, refresh, review, prune, connectors, the MCP server, and
  every shell script (`bash -n`, `lore-common.sh` helpers, and `lore-gardener.sh` run end to
  end, not as a dry run, against fake agent scripts).
- `.github/workflows/ci.yml`: seven jobs. `test` runs `bash tests/run.sh` on a matrix of
  `ubuntu-latest`/`macos-latest` and Python `3.12`/`3.14`. `shell` runs
  `shellcheck -S warning` over every shipped `.sh` script plus `install.sh`. `install` runs
  `install.sh --scheduler none` into a temporary config dir, then `lore.py doctor --offline`
  against it. `systemd-render` renders the systemd templates and runs
  `systemd-analyze verify` on them (Ubuntu only). `privacy` runs `scripts/privacy-grep.sh`
  over the checkout. `docs-fences` fails on any fenced code block in `docs/` longer than 40
  lines. `examples` runs `examples/quickstart.sh` and `examples/deterministic_facts.py`.
- `scripts/privacy-grep.sh`: an anonymization gate scanning the working tree (or, with
  `--stdin`, full git history) for personal identifiers outside a narrow, file-scoped
  allowlist.
- `docs/`, `examples/`, `CONTRIBUTING.md`, `SECURITY.md`, `LICENSE`, `NOTICE` (this package).

### Changed

- Every hard-coded personal path, hostname, and scheduler label became an environment
  variable with a portable default: `LORE_DIR`, `LORE_LAUNCHD_LABEL_PREFIX`,
  `LORE_AGENT_CONFIG_DIR`, `LORE_GARDENER_CMD`, `LORE_GARDENER_MODEL_ARGS`,
  `LORE_KEEP_API_KEY`, `LORE_ROLE_FILE`, `LORE_GARDENER_AUTH_CHECK`,
  `LORE_GARDENER_NETWORK_CHECK_URL/RETRIES/INTERVAL_SEC`, `LORE_GARDENER_DEADLINE_SEC`,
  `LORE_GARDENER_CLAUDE_TIMEOUT_SEC`, `LORE_GARDENER_STALE_SEC`, `LORE_REFRESH_DEADLINE_SEC`,
  `LORE_WATCHDOG_STALE_SEC`, `LORE_WATCH_FILES`, `LORE_WATCH_FILE_MAX_BYTES`,
  `LORE_DOCTOR_URL`, `LORE_REFRESH_INTERVAL_SEC`. See `skill/lore.env.example`.
- Data directory resolution order: `lore.py` tries an explicit `--lore-dir` argument, then
  `$LORE_DIR`, then `./lore` if it exists in the current directory (project scope), then a
  lore directory next to the running script's own install, then `~/.claude/lore`.
  `lore-mcp-server.py` applies the same script-relative and `~/.claude/lore` fallback, minus
  the `--lore-dir` and `./lore` steps, since an MCP server has no per-invocation flags or
  working directory of its own.
- The gardener's headless-agent invocation is now expressed as a documented contract
  (`LORE_GARDENER_CMD`, launched with `--dangerously-skip-permissions --permission-mode
  bypassPermissions`, then `$LORE_GARDENER_MODEL_ARGS` if set, then the rendered prompt as
  the next positional argument, then `--output-format stream-json`; a run counts as
  successful only if the command exits `0` and writes `.gardener/report-<date>.md`, and it
  should print one JSON `type:result` line at the end so a failure can be classified) instead
  of a hard-coded `claude -p` call, so any CLI agent that accepts that layout and success
  condition can run the nightly pass. `LORE_GARDENER_CMD` still defaults to `claude -p`.
  `LORE_GARDENER_MODEL_ARGS` (default empty) lets an install pin a model or effort level
  without editing the script.
  The `claude` binary itself is resolved with `command -v claude`, then
  `$HOME/.local/bin/claude`, then `$LORE_GARDENER_CMD`, removing a dead version-pinned
  fallback path.
- `lore.py doctor`'s Python floor check is `>= (3, 12)` (3.10 and 3.11 are security-only and
  3.10 reaches end of life in October 2026; the connectors' PEP 604 `X | Y` union annotations
  only need 3.10, added `from __future__ import annotations` to every connector so those
  signatures also parse on any 3.9+ interpreter that only imports the module without
  executing PEP 604 code paths).
- `install.sh` now fails fast, before doing any work, if `python3` is older than 3.12,
  matching `lore.py doctor`'s check and the CI matrix (`ubuntu-latest`/`macos-latest`,
  Python `3.12`/`3.14`).
- The bootstrapped `schema.md` describes the knowledge base as "maintained by your AI agent"
  instead of naming a single vendor; `skill/SKILL.md` itself stays Claude Code specific, since
  it is a Claude Code skill file.
- Every doc and example uses fictional, vendor-neutral model names (`example-model-large`,
  `example-model-mid`, `example-model-small-20260101`) instead of real model IDs, so they do
  not go stale and never reference a model below any floor a downstream user might set. The
  one functional exception is the PDF connector's OCR call (`skill/connectors/pdf.py`), which
  names a real, live Mistral model alias because that alias is what the Mistral OCR API
  actually requires; see `docs/connectors.md` for how that connector's one dependency is
  isolated.
- The bootstrap `.gitignore` template additionally ignores `queue/`, `failed/`, `.DS_Store`,
  `*.bak*`, `.lore.env`, `.gardener/run-*.jsonl`, `.gardener/launchd.*.log`,
  `.refresh.stderr.log`, `.lore.lock/`, and `.paused`.
- The contradiction lint's generic-key list is no longer hard-coded in `lore.py`; it is
  loaded from `$LORE_DIR/lint-config.json` (`contradiction_generic_keys`), written by
  `bootstrap` with a vendor-neutral default. The check also skips two-cell table rows
  (label/value tables are not fact tables) and only compares rows from tables that share an
  identical header.
- The stale-failure lint check now skips `failed/*.err` records already marked reviewed via
  `lore.py seen`.
- `lore.py doctor`'s scheduler check is scoped to `LORE_LAUNCHD_LABEL_PREFIX` on macOS and
  falls back to `systemctl --user list-timers` then `crontab -l` elsewhere, reporting `WARN`
  (never `FAIL`) when no scheduler job is found.
- The gardener's failure alert reports a classified cause and a trimmed vendor message
  (`cause=<class> rc=<n> result=<message>`) instead of a raw log tail.

### Fixed

- `git_repo.py` allowlists URL schemes (`https://`, `ssh://`, `git://`, `git+https://`,
  scp-style `user@host:path`), rejects arguments starting with `-`, passes `--` before the
  URL, and sets `GIT_TERMINAL_PROMPT=0`.
- `github_releases.py` sends its `Authorization` header through `curl --config -` on stdin
  instead of a `-H` argument, so a bearer token no longer appears in the process list.
- A path-traversal fix: page and extract slugs are validated with a single `valid_slug()`
  check (`^[a-z0-9][a-z0-9._-]{0,127}$`, rejecting `..`) shared by `lore.py fact`,
  `lore.py review clear <slug>`, and the equivalent `lore_get_page`/`lore_fact` MCP tools.
- `lore.py fact` and the MCP `lore_fact` tool return identical values (including list-index
  paths such as `a.b.1`) and the same error class for a missing path or a missing slug.
- An orphaned `.hashes.json` entry (a source no page references any more) is dropped during
  `refresh` instead of accumulating; `lore.py lint` reports any left as `INFO orphan-hash`,
  and `lore.py prune --orphan-hashes` removes them on demand.
- `lore.py refresh` no longer reports an HTML source as changed on every run because of a
  build id, nonce, or other per-request markup embedded in the page: the change hash is now
  computed from the same normalized, extracted text a queue diff shows, instead of the raw
  fetched bytes. An existing `.hashes.json` entry from before this fix upgrades to the new
  hash scheme automatically the first time its content still matches. A `queue/` diff record
  for a real change now opens with a unified diff against the source's previously cached
  payload (kept under `.refresh-cache/`, one file per source), ahead of the full new payload;
  `lore.py prune --orphan-hashes` also removes a cached payload for a source no page
  references any more.
- Dead-link checking now goes through the same fetch path (HEAD first, GET fallback) that
  page sources use, instead of a separate, unbounded code path.
- The RSS connector no longer drops an entry's title when the parsed XML element is falsy
  but not `None` (an `ElementTree` truthiness bug: an empty-child element is falsy in a
  boolean context even though it is a real, present element).
- `set_frontmatter_field` no longer drops the blank line(s) between a page's frontmatter
  and its body: `FRONTMATTER_RE`'s greedy trailing pattern consumed them along with the
  closing `---` line, so every write through this function (`review clear`, `refresh`'s
  `needs_review`/`last_verified` updates) silently collapsed that gap. The body's start
  is now located from the closing `---` line alone, leaving every byte after it untouched.

### Removed

- The private install's six seed page templates are not shipped; `examples/pages/example-api.md`
  is one complete example page instead.
