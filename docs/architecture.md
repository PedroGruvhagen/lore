# Architecture

What a lore directory contains, and how the pieces fit together. For the page file format
itself, see `page-format.md`. For the refresh/gardener/watchdog loop, see
`maintenance-loop.md`.

## The lore directory

`lore.py bootstrap --lore-dir <path>` creates the directories, the three markdown files,
`.gitignore`, an empty `.hashes.json`, an empty `.graph.json`, and `lint-config.json`
(seeded from a shipped default) listed below, and seeds `connectors/` from the shipped
`skill/connectors/` when it is empty; only `.search.db` is written later, by `lore.py index`:

- `index.md`: a generated catalog, one row per page: title, category, confidence,
  auto-update flag, last-verified date. Rebuilt by `lore.py index`; never hand-edited.
- `log.md`: a chronological record, appended to by `lore.py log`.
- `schema.md`: the bootstrapped copy of `lore.py`'s `SCHEMA_TEMPLATE` constant: what the
  directory holds and the category list, written once at bootstrap time so a fresh install
  documents itself without depending on this repository being nearby.
- `pages/`: the knowledge pages themselves, one markdown file per topic.
- `sources/`: raw source documents kept for `lore.py ingest`, immutable once written.
- `queue/`: machine-generated diff records, written by `lore.py refresh` when a source's
  content hash changes, awaiting an agent's review.
- `inbox/`: free-text notes from `lore.py inbox "<text>"`, awaiting triage into a page.
- `failed/`: one `.err` record per failed fetch, reviewed and dismissed with `lore.py seen`.
- `extracts/`: JSON files read by `lore.py fact <slug> <dotted.path>` (see below).
- `connectors/`: the fetcher modules `lore.py refresh` and `lore.py doctor` load (see
  `connectors.md`).
- `.hashes.json`: one SHA-256 content hash per tracked source URL. Bootstrap writes an empty
  `{}`; `lore.py refresh` populates and updates it.
- `.graph.json`: the wikilink graph. Bootstrap writes an empty graph; rebuilt by
  `lore.py index` or `lore.py graph`.
- `lint-config.json`: the contradiction lint's configurable generic-key list. Bootstrap
  seeds it from a shipped default; `lore.py lint` loads it if present.
- `.search.db`: the SQLite FTS5 index described below; safe to delete, `lore.py index`
  rebuilds it from `pages/*.md`. The only one of these not written at bootstrap time.
- `.gitignore`: written at bootstrap so `.search.db`, `__pycache__/`, `queue/`, `failed/`,
  and the other derived or private paths are never committed by a project that tracks its
  lore directory in git.

## Directory resolution

`resolve_lore_dir()` in `lore.py` resolves in this order: an explicit `--lore-dir` argument,
then the `$LORE_DIR` environment variable, then `./lore` if it exists in the current working
directory (project scope, see `project-scope.md`), then a lore directory next to the running
script's own install (`<config-dir>/skills/lore/scripts/lore.py` implies `<config-dir>/lore`)
if it exists, then `~/.claude/lore` if it exists, and if neither exists yet the
script-relative path again (or `~/.claude/lore` when the script is not in that layout), so
`bootstrap` creates the directory next to the skill that manages it. `lore.py doctor` prints
which rule matched, as its `lore-dir-source` check. `lore-mcp-server.py` applies the same
`$LORE_DIR`, script-relative, and `~/.claude/lore` rules but has no `--lore-dir` flag and no
`./lore` step (see `mcp-server.md`).

## Index and graph

`lore.py index` does three things in one pass: rewrites `index.md` from every page's
frontmatter, rebuilds `.graph.json` from `[[wikilink]]` references found in page bodies, and
rebuilds `.search.db`. It does not change `needs_review` flags; that propagation only
happens inside `lore.py refresh`, gated on the diffs that specific refresh run produced, so
running `index` repeatedly is always safe and idempotent.

## Search (FTS5)

`.search.db` is a SQLite database with one FTS5 virtual table, `pages`, columns `slug`,
`title`, `category`, `tags`, `confidence`, `content`, tokenized with `porter unicode61`.
`lore.py search "<query>"` splits the query on whitespace, wraps each token in double quotes
(doubling any embedded quote) so punctuation inside a token is treated as literal text
rather than FTS5 query syntax, joins the tokens with FTS5's implicit AND, and ranks results
with `bm25()`. If the query still raises `sqlite3.OperationalError`, or `.search.db` does not
exist yet, search falls back to a plain case-insensitive substring scan across
`pages/*.md`. The MCP server's `lore_search` tool (see `mcp-server.md`) uses the same
quoting.

## Deterministic facts (extracts)

A page can be backed by a JSON file at `extracts/<slug>.json` holding values an agent
extracted from that page's sources. `lore.py fact <slug> <dotted.path>` reads that file and
walks the dotted path (dict keys and list indices, for example `a.b.1`), printing a bare
scalar or pretty-printed JSON for an object or array, and exits non-zero with a message on
stderr if the slug, file, or path does not resolve. The slug is validated first
(`^[a-z0-9][a-z0-9._-]{0,127}$`, rejecting `..`) so it cannot be used to read a file outside
`extracts/`. The MCP `lore_fact` tool and the CLI command share the same dotted-path
resolution and the same error classes.

## needs_review propagation

Two independent triggers set `needs_review: true` on a page, both inside `cmd_refresh`: a
direct source-content change on the page itself, and a hash-based propagation pass that only
flags a dependent page when a `queue/` diff was written for one of its dependencies in that
same refresh run. `lore.py review list` shows every flagged page; `lore.py review clear
<slug>` clears one page by hand; `lore.py review clear --propagated` clears only pages that
have no `auto_update: true`, no matching file in `queue/`, and no `failed/<slug>-*.err` newer
than the page's `last_verified`, so a page with real, unreviewed evidence is never cleared
automatically.
