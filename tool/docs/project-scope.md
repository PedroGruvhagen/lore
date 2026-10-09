# Project-scoped lore

A lore directory does not have to be global. `resolve_lore_dir()` (see `architecture.md`)
checks `./lore` in the current working directory, right after an explicit `--lore-dir` flag
and `$LORE_DIR`, and before falling back to a directory next to the skill's own install. This
means running `lore.py` from inside a project that has its own `lore/` subdirectory uses that
project's knowledge base automatically, with no flag and no environment variable needed;
`lore-mcp-server.py` does not perform this cwd check, since an MCP server is launched once by
its client rather than run from a project's working directory (point `LORE_DIR` at the
project's `lore/` explicitly if you want project scope through MCP).

## Why keep it gitignored

`tool/docs/templates/project-gitignore.snippet` adds a single `lore/` line to a consuming
project's `.gitignore`. A project-scoped lore directory is the agent's own working notes
about that project: useful for future sessions, not something the project's collaborators or
CI need to see, and it will contain content that changes on every refresh
(`.search.db`, `.hashes.json`, `queue/`) that has no business in a diff. Keep it local and
gitignored; if you want to share a specific fact, put it in the project's own documentation
instead, in prose, the way any other project knowledge is shared.

## Wiring a project

1. Add `tool/docs/templates/project-gitignore.snippet`'s line to the project's `.gitignore`.
2. Add `tool/docs/templates/CLAUDE.md.snippet`'s block to the project's `CLAUDE.md` (or
   equivalent agent-instructions file), pointing at wherever this repository is installed.
3. Bootstrap the directory: `python3 <path-to-lore>/scripts/lore.py bootstrap --lore-dir
   lore` from the project root.

From then on, running `lore.py` (or having an agent run it) from inside that project reads
and writes `./lore` automatically.

## Naming collisions

Because project scope is triggered purely by a directory named `lore` existing in the
current working directory, a project that already has an unrelated `lore/` directory (for
some other purpose) will collide with this convention. Check for one before wiring a project
in: if `lore/` already exists and is not this system's lore directory, either rename the
existing directory or set `$LORE_DIR` explicitly to a different path (for example
`.lore-kb/`) and reference that explicit path everywhere instead of relying on the `./lore`
default, since only the exact name `lore` in the current working directory triggers project
scope.

## Global lore

A global, cross-project lore directory (person, infrastructure, or preference knowledge that
is not specific to one project) is just a lore directory nothing's cwd check matches: set
`LORE_DIR` in your shell profile, or rely on the final fallback next to the skill's own
install (`<config-dir>/lore`, see `architecture.md`). Cross-project facts belong there;
project-specific facts belong in that project's own `lore/`, so one project's agent does not
have to read through every other project's knowledge to find what applies to it.
