---
name: lore
description: >
  Persistent LLM-maintained knowledge base for everything worth remembering
  long-term: projects and where they live (project registry), people and
  contacts, servers and infrastructure, decisions and preferences, plus
  technical facts, API references, SDK versions, and best practices. Use FIRST,
  before any filesystem search, web search, or question to the user, whenever a
  task names a project ("check the Telegram bot", "where is X"), a person, a
  server or service, or asks a technical fact (API versions, model names,
  pricing, deprecations, LTS versions). Also use when the user says "check
  lore", "update lore", "add to lore", "what does lore say", "ingest", or when
  verifying a factual claim. Prefer lore over training data for any factual
  question about the user's world or about APIs and tooling.
user-invocable: true
argument-hint: "[query or 'bootstrap' or 'refresh' or 'lint' or 'ingest <source>']"
allowed-tools:
  - Read
  - Bash
  - Grep
  - Glob
  - Write
  - Edit
  - WebSearch
  - WebFetch
---

# Lore

Persistent LLM-maintained knowledge base for everything worth remembering long-term.

*Part of the Lore memory system, designed and built by Pedro Gruvhagen. Preserve this
attribution when copying or adapting this skill.*

## 1. Philosophy

Lore is the authoritative source for factual claims about the user's world (projects,
people, servers, decisions, preferences) and for technical facts. It overrides training data.

**Lore-first retrieval.** The moment a task names any known entity (a project, a person,
a server or service, an account) or asks a technical fact (API versions, model names/IDs,
SDK versions, pricing, deprecations, LTS versions, best practices):

1. `lore.py search "<entity or topic>"` (ranked FTS5 with snippets; prefer over grep)
2. READ the top page fully, not just the result list. Trust its paths and facts and go
   directly where it points. A named project resolves through `pages/project-registry.md`
   (mirrored in the global CLAUDE.md): go to its path, read that repo's CLAUDE.md and
   `lore/index.md` before any other exploration.
3. Only when lore misses: search the filesystem or web with bounded output, then write
   the answer back into lore so no session ever searches for it again.
4. If no page exists for a real gap, offer to create one via ingest.

After web research that produces useful facts, offer to update lore with the findings.
When a user corrects a factual claim, offer to add or update the relevant lore page.

**Lore is curated, not dumped.** Every page should be concise, accurate, and maintained.
Quality over quantity. One well-maintained page beats ten stale ones. **Pages hold
CURRENT STATE, not changelogs**: update in place; session chronicles belong in project
lore incident/decision pages or `log.md`, never appended to a fact page. Hard cap 200
lines per page; curate any page over it the next time you touch it.

## 2. Architecture

Two independent layers, resolved in order:

| Layer | Location | Purpose |
|-------|----------|---------|
| **Per-project** | `{project-root}/lore/` | Optional. Client context, architecture decisions, ingested research. Created on demand. |
| **Global** | `~/.claude/lore/` (default; see `--lore-dir` Resolution Order below) | Cross-project facts: API versions, model names, SDKs, pricing, best practices. |

**Per-project lore is always local-only and MUST be gitignored.** When creating or
encountering a per-project `lore/` directory, ensure the project's root `.gitignore`
contains a `lore/` entry. Per-project lore may contain client context, internal
research, credentials references, or ingested third-party material that should never
be pushed to a remote. If existing files are already tracked in git, leave them in
place (do not rewrite history) but add `lore/` to `.gitignore` so future changes stay
local. This rule does not apply to the global lore directory (see `--lore-dir`
Resolution Order below), which has its own sync policy.

### Directory Structure

```
lore/
  index.md          # Auto-generated table of all pages (rebuilt by `index` command)
  log.md            # Append-only activity log
  schema.md         # Structure and category definitions
  entities.json     # Entity registry (slug -> aliases)
  .hashes.json      # Source-content SHA-256 + ETag map (change detection)
  .graph.json       # Page-dependency DAG built from [[wikilinks]]
  .search.db        # SQLite FTS5 database (derived, gitignored)
  .gitignore        # Ignores .search.db
  sources/          # Raw fetched content (URLs, articles, docs snapshots)
  pages/            # Curated knowledge pages (one topic per file)
  extracts/         # Deterministic structured facts (one JSON per high-value page)
  connectors/       # Typed source-fetch modules (one per source kind)
    __init__.py
    web.py            # Generic HTTP GET fallback
    pypi.py           # https://pypi.org/pypi/{pkg}/json
    npm.py            # https://registry.npmjs.org/{pkg}/latest
    github_releases.py
    rss.py            # RSS / Atom feeds
    git_repo.py       # git ls-remote HEAD
    pdf.py            # PDF via Mistral OCR subprocess
  failed/           # Failed-refresh records: {slug}-{date}.err (+ {name}.seen markers)
  queue/            # Machine-generated source diffs awaiting reconciliation (gardener consumes)
  inbox/            # Quick notes dropped by any session; gardener files them into pages
  .gardener/        # Nightly gardener run logs and reports (gitignored)
  .lore.env         # Local overrides incl. alert channel (gitignored; see lore.env.example)
```

All HTTP fetching is curl-first (system `curl` subprocess with explicit timeouts) with a
urllib fallback only when curl is absent. Sources that fail 3 consecutive runs are
quarantined in `.hashes.json` (skipped, retried weekly) instead of failing forever.

Per-project pages can cross-reference global lore with `[[global:page-slug]]` syntax.
Global pages cross-reference each other with `[[page-slug]]`.

## 3. CLI Reference

Single script at `~/.claude/skills/lore/scripts/lore.py`. Zero external
dependencies (stdlib only: sqlite3, json, re, pathlib, datetime, argparse).

### Subcommands

```bash
# Initialize a new lore directory
python3 ~/.claude/skills/lore/scripts/lore.py bootstrap [--scope global|project] [--lore-dir PATH]

# Rebuild index.md and FTS database from pages/
python3 ~/.claude/skills/lore/scripts/lore.py index [--lore-dir PATH]

# Full-text search across all pages
python3 ~/.claude/skills/lore/scripts/lore.py search <query> [--lore-dir PATH] [--limit N]

# Check for issues (stale, orphans, broken refs, missing frontmatter)
python3 ~/.claude/skills/lore/scripts/lore.py lint [--lore-dir PATH]

# List pages past their refresh interval
python3 ~/.claude/skills/lore/scripts/lore.py stale [--lore-dir PATH]

# Append entry to log.md
python3 ~/.claude/skills/lore/scripts/lore.py log <message> [--lore-dir PATH]

# Archive source file and prepare for page creation
python3 ~/.claude/skills/lore/scripts/lore.py ingest <source-path> [--lore-dir PATH]

# Check entity registry for duplicate prevention
python3 ~/.claude/skills/lore/scripts/lore.py check-entity <name> [--lore-dir PATH]

# Git sync (add, commit, pull --rebase, push) if lore dir is a git repo
python3 ~/.claude/skills/lore/scripts/lore.py sync [--lore-dir PATH]

# Hash-based refresh of every auto_update page (or only one with --slug)
python3 ~/.claude/skills/lore/scripts/lore.py refresh [--force] [--slug SLUG] [--lore-dir PATH]

# Read a deterministic fact from extracts/{slug}.json by dotted path
python3 ~/.claude/skills/lore/scripts/lore.py fact <slug> <dotted.path> [--lore-dir PATH]

# Rebuild the page-dependency DAG (.graph.json) from [[wikilinks]]
python3 ~/.claude/skills/lore/scripts/lore.py graph [--lore-dir PATH]

# Everything actionable right now (queued diffs, unseen failures, quarantined sources,
# stale pages, inbox notes). --summary prints ONE line (nothing when all clear).
python3 ~/.claude/skills/lore/scripts/lore.py pending [--json|--summary] [--lore-dir PATH]

# Mark failed/*.err records as seen (creates {name}.seen markers)
python3 ~/.claude/skills/lore/scripts/lore.py seen [--all | name1.err ...] [--lore-dir PATH]

# End-to-end self-test of the whole maintenance chain (fetch, index, timers, logs)
python3 ~/.claude/skills/lore/scripts/lore.py doctor [--json] [--offline] [--lore-dir PATH]

# Drop a quick note mid-task; the nightly gardener files it into the right page
python3 ~/.claude/skills/lore/scripts/lore.py inbox "note text" [--lore-dir PATH]
```

### `--lore-dir` Resolution Order

1. Explicit `--lore-dir` argument
2. `$LORE_DIR` environment variable
3. `{cwd}/lore/` if it exists (per-project lore)
4. Global default, first that applies: the `lore/` directory installed next to this
   skill (inferred from the running script's own path, i.e. `<config-dir>/lore/` when
   this skill lives at `<config-dir>/skills/lore/scripts/lore.py`) if that directory
   already exists; otherwise `~/.claude/lore/` if it already exists; otherwise the
   install-relative path, created fresh there.

`lore.py doctor` reports which of these four rules matched (`lore-dir-source`), so a
misconfigured install is diagnosable without reading the source.

### Command Reference

| Command | What it does | Output |
|---------|-------------|--------|
| `bootstrap` | Creates directory tree, writes schema.md from template, empty log.md, empty index.md | `OK: Lore initialized at {path}` |
| `index` | Scans pages/*.md, extracts frontmatter, rebuilds index.md + .search.db (SQLite FTS5) | `OK: Index rebuilt. {N} pages indexed.` |
| `search` | FTS5 query on .search.db, returns top N results with slug + snippet. Falls back to file scan if no DB. | Structured text results |
| `lint` | Checks: missing frontmatter, orphan pages, stale auto_update, broken cross-refs, index/page mismatch | Report text |
| `stale` | Lists pages where `auto_update=true` and `last_verified + refresh_interval < today` | List of stale page paths |
| `log` | Appends `- {ISO-8601} | {message}` to log.md | `OK: Logged.` |
| `ingest` | Copies source file to `sources/`, logs the ingest. Claude then creates/updates the page. | `OK: Ingested {path} to sources/{slug}.md` |
| `check-entity` | Searches `entities.json` for matching canonical slugs or aliases. Prevents duplicate pages. | Matching entities or `No matches found.` |
| `sync` | If .git exists: add, commit, pull --rebase, push. Otherwise error. | `OK: Synced.` or `ERROR: Not a git repo.` |
| `refresh` | For every `auto_update: true` page (or only `--slug SLUG`), dispatches each `sources:` entry to its typed connector (curl-first), hashes the response, compares against `.hashes.json`, writes changed-source diffs to `queue/`, auto-bumps `last_verified` when every source is OK and unchanged, quarantines sources after 3 consecutive failures (skip + weekly retry), and writes `failed/{slug}-{date}.err` on failure. `--force` recomputes hashes and bypasses quarantine skips. | Per-source status lines, summary count |
| `pending` | Aggregates everything actionable: queued diffs, unseen failure records, quarantined sources, stale pages, inbox notes. `--json` for automation, `--summary` for the one-line session hook (prints nothing when clear). | Grouped list / JSON / one line |
| `seen` | Marks `failed/*.err` records as processed by creating `{name}.seen` markers (`--all` or named files). | `Marked N failure record(s) as seen` |
| `doctor` | End-to-end self-test: python version, dir resolution, live fetch through the real connector path, index/graph freshness, refresh+watchdog log recency, launchd jobs, failure/quarantine counts, queue age, git state. Exit 0 unless a FAIL. `--offline` skips the network check. | PASS/WARN/FAIL lines or `--json` |
| `inbox` | With text: drops a timestamped note into `inbox/` for the nightly gardener to file. Without text: lists current inbox notes. | Note path / note list |
| `fact` | Reads `lore/extracts/{slug}.json`, resolves a dotted path (for example `models.opus.context_window`), prints the value to stdout. Returns non-zero if the slug or the path does not resolve. | Raw value of the resolved field |
| `graph` | (Re)builds `lore/.graph.json` from `[[wikilinks]]` across all pages. Also called automatically from `index`. | `OK: Graph built. {N} nodes, {E} edges.` |

## 4. Ingest Workflow

Adding new knowledge to lore. Two modes: **Guided** (discuss before filing) and
**Batch** (process all unprocessed files in sources/ without discussion).

### Guided Ingest (default)

1. User provides source (URL, file path, or raw text)
2. If URL: `WebFetch` the content, save raw to `lore/sources/{slug}-{date}.md`
3. If file path: archive the source using the CLI:
   ```bash
   python3 ~/.claude/skills/lore/scripts/lore.py ingest /path/to/source.md
   ```
   The CLI copies the file to `sources/` and logs the ingest. Claude handles page creation.
4. Check for duplicates before creating a new page:
   ```bash
   python3 ~/.claude/skills/lore/scripts/lore.py check-entity "{topic}"
   ```
5. Determine whether this is a new page or an update to an existing one:
   - Run `lore.py search "{topic}"` to check for existing pages
   - If match found, read the existing page and merge new information
   - If no match, create a new page
6. Write or update the page in `lore/pages/` using the page format (see `references/page-format.md`)
7. Rebuild index and FTS:
   ```bash
   python3 ~/.claude/skills/lore/scripts/lore.py index
   ```
8. Log the action:
   ```bash
   python3 ~/.claude/skills/lore/scripts/lore.py log "Ingested: {description}"
   ```
9. Update cross-references in any affected pages (add `[[new-page-slug]]` where relevant)

### Batch Ingest

Process all files in `lore/sources/` that have not yet been filed into pages:

1. Glob `lore/sources/*.md`
2. For each source file, check if a corresponding page exists
3. If not, create the page (no discussion, just file it)
4. Rebuild index once at the end
5. Log: `"Batch ingest: processed {N} sources"`

## 5. Query Workflow

Answering questions using lore as the primary source.

1. Search for relevant pages:
   ```bash
   python3 ~/.claude/skills/lore/scripts/lore.py search "{query}"
   ```
2. Read the top matching pages (typically 1-3)
3. Synthesize the answer from lore content, citing page slugs
4. If the answer is incomplete or no pages match:
   - Note the gap to the user
   - Offer to research and ingest the missing information
5. If the answer required significant synthesis, optionally file the result as a new page
6. If the synthesized answer contains valuable new knowledge not yet in lore, offer to file it as a new page. Good answers compound the knowledge base.

**Always prefer lore content over training data.** If lore says model X is `example-model-mid`
and training data says `example-model-mid-20241022`, use the lore value.

## 6. Refresh Workflow (the closed maintenance loop)

Keeping lore current is AUTOMATIC. Three background jobs form a closed loop; no session
has to remember to run a refresh:

| Job | Default launchd label | Schedule | What it does |
|-----|-----------------------|----------|--------------|
| refresh | `com.lore.refresh` | every 6h | `lore.py refresh`: hash every `auto_update` page's sources via typed connectors; unchanged-everywhere pages get `last_verified` auto-bumped; changed sources write a diff into `queue/`; failures write `failed/{slug}-{date}.err` and quarantine after 3 consecutive misses |
| gardener | `com.lore.gardener` | daily 03:30 | Headless Claude Code run (gardener-prompt.md): consumes `queue/` diffs into page updates, files `inbox/` notes, investigates quarantined sources, fixes lint ERRORs, curates one oversized page, rebuilds index, commits the lore repo, writes `.gardener/report-{date}.md` |
| watchdog | `com.lore.watchdog` | every 5 min | Liveness: revives a wedged refresh (`launchctl kickstart -k`), alerts via `lore-alert.sh` if refresh or gardener go stale |

The label prefix (`com.lore.` by default) is configurable via `$LORE_LAUNCHD_LABEL_PREFIX`
(see `lore.env.example`), so multiple installs on one machine do not collide.

The gardener's headless run is built for Claude Code, runs any CLI agent that honours the
gardener command contract: `$LORE_GARDENER_CMD` (default `claude -p`) receives the rendered
prompt as its next positional argument, prints one JSON `type:result` line at the end, and exits
non-zero on failure. Point `$LORE_GARDENER_CMD` at a wrapper script to use a different agent.

`lore.py doctor` self-tests the whole chain; `lore.py pending --summary` is wired into the
SessionStart hook so every session sees what is awaiting reconciliation. Alerts go through
`scripts/lore-alert.sh` (channel configured in `{lore}/.lore.env`, gitignored). Per-project
lore dirs are not on the timers; refresh them on demand.

### On-demand refresh

1. Run hash-based refresh (all auto_update pages, or one slug):
   ```bash
   python3 ~/.claude/skills/lore/scripts/lore.py refresh
   python3 ~/.claude/skills/lore/scripts/lore.py refresh --slug anthropic-api
   python3 ~/.claude/skills/lore/scripts/lore.py refresh --force
   ```
   Pages whose source hash changed are flagged `needs_review: true` for the next session.
2. Find pages flagged for review or past their refresh interval:
   ```bash
   python3 ~/.claude/skills/lore/scripts/lore.py stale
   ```
3. For each flagged page:
   - Read the page to get its `sources:` list
   - `WebFetch` each source URL
   - Compare fetched content against the page
   - Update the page if information has changed
   - Set `last_verified` to today's date and clear `needs_review`
4. Rebuild the index:
   ```bash
   python3 ~/.claude/skills/lore/scripts/lore.py index
   ```
5. Log the refresh:
   ```bash
   python3 ~/.claude/skills/lore/scripts/lore.py log "Refresh: updated {N} pages"
   ```

## 7. Lint Workflow

Checking lore health and consistency.

1. Run the lint check:
   ```bash
   python3 ~/.claude/skills/lore/scripts/lore.py lint
   ```
2. Review the report. Full 10-point check protocol with severity tiers:

   **ERROR** (must fix before commit):
   - Credential leaks (API keys, tokens, passwords in page content)
   - Missing required frontmatter fields (`title`, `category`, `last_verified`, `sources`)

   **WARN** (fix soon):
   - Contradictions between pages (same fact, different values)
   - Dead external links in sources (opt-in with `--check-links`)
   - Stale `auto_update` pages past their refresh interval
   - Orphan pages not referenced in index
   - Index entries pointing to missing pages
   - Broken `[[cross-refs]]` to non-existent pages
   - `[WARN] [missing-graph]`: `.graph.json` is missing; run `lore.py index` to rebuild.
   - `[WARN] [needs-review]`: page frontmatter has `needs_review: true`; a referenced source or wikilink target changed.
   - `[WARN] [missing-hashes]`: page is `auto_update: true` but has source URLs not present in `.hashes.json`.
   - `[WARN] [stale-failures]`: entries in `lore/failed/` older than 24h that have not been retried.

   **INFO** (advisory):
   - Data gaps (referenced concepts that lack their own page)
   - Suggest new questions to investigate based on page content

3. Fix ERROR issues immediately. Address WARN issues before next commit. INFO items are optional improvements.
4. Log the result:
   ```bash
   python3 ~/.claude/skills/lore/scripts/lore.py log "Lint: {summary}"
   ```

## 8. Auto-Patch Workflow

Detecting drift between lore and project CLAUDE.md files.

**This workflow never applies changes silently.** Always present a diff for approval.

1. Confirm lore is fresh (refuse to patch from stale data):
   ```bash
   python3 ~/.claude/skills/lore/scripts/lore.py stale
   ```
   If stale pages exist that are relevant to the patch, run refresh first.
2. Scan the project's CLAUDE.md for version numbers, model references, SDK versions
3. Compare each reference against lore pages that have `auto_update: true`
4. Generate a diff of proposed changes
5. Present the diff to the user for review and approval
6. If approved, apply the changes to CLAUDE.md
7. Log the action:
   ```bash
   python3 ~/.claude/skills/lore/scripts/lore.py log "Patched: {project} CLAUDE.md"
   ```

## 9. Page Format Quick Reference

Full specification in `references/page-format.md`. Summary below.

### Frontmatter

```yaml
---
title: "Page Title"                  # Required
category: "api-reference"            # Required
auto_update: true                    # Optional, default false
last_verified: "2026-04-08"          # Required (ISO date)
refresh_interval: "5d"               # Optional, default "30d"
confidence: "high"                   # Optional: high, medium, low
sources:                             # Required (at least one)
  - "https://docs.example.com/..."
tags:                                # Optional
  - tag1
  - tag2
needs_review: false                  # Optional, default false
---
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `needs_review` | boolean | `false` | Set by `lore.py index` when a wikilink target or referenced source has changed since this page's `last_verified`; cleared when the page is refreshed. |

### Categories

| Category | Use for |
|----------|---------|
| `api-reference` | API endpoints, model IDs, authentication, rate limits |
| `best-practices` | Recommended patterns, conventions, style guides |
| `deprecations` | Sunset dates, migration paths, removed features |
| `tooling` | CLIs, build tools, package managers, editors |
| `infrastructure` | Cloud services, deployment, CI/CD, monitoring |
| `custom` | Anything that does not fit the above |

### Confidence Values

| Level | Meaning |
|-------|---------|
| `high` | Multiple authoritative sources confirm, recently verified |
| `medium` | Single source or not recently verified |
| `low` | Inferred, unverified, or potentially stale |

### Body Conventions

- **First line**: one-sentence summary (used for index.md entries)
- `## Section` headers for organizing topics
- `**Bold**` for key facts (model names, version numbers, dates)
- Cross-reference other pages with `[[page-slug]]`
- Keep pages under 200 lines; split larger topics into multiple pages

## 10. Entity Registry

Lore maintains an `entities.json` file mapping canonical page slugs to aliases.
This prevents duplicate pages for the same concept (e.g., "anthropic-api" vs "claude-api").

- Auto-generated from page titles and tags during `index` rebuild
- Manual entries are preserved (not overwritten)
- Check before creating a new page:
  ```bash
  python3 ~/.claude/skills/lore/scripts/lore.py check-entity "anthropic"
  ```
