# MCP server

`tool/skill/scripts/lore-mcp-server.py` exposes a lore directory to any MCP client (Claude
Desktop, or another tool that speaks MCP) over stdio JSON-RPC, read-only. It implements the
protocol directly with the Python standard library (`json`, `sqlite3`, `re`, `pathlib`); no
`pip install` is needed to run it, and it never writes to the lore directory.

## Directory resolution

If `$LORE_DIR` is set, it is used as-is. Otherwise the script infers `<config-dir>/lore` from
its own install path, expecting the standard `<config-dir>/skills/lore/scripts/` layout. If
that inference fails it uses `~/.claude/lore`; if the inferred directory does not exist but
`~/.claude/lore` does, it uses `~/.claude/lore`; if neither exists yet, the inferred path
stands, so an error names the directory the install layout implies. This mirrors `lore.py`'s
own resolution order (see `architecture.md`), minus the explicit-flag and project-scope
(`./lore`) steps, since an MCP server is launched once by its client's own config rather than
from a project's working directory.

## Protocol

Implements MCP protocol version `2024-11-05` and three JSON-RPC methods: `initialize`
(returns `protocolVersion`, `capabilities: {"tools": {}}`, and `serverInfo`), `tools/list`
(returns the four tool schemas below), and `tools/call` (dispatches to one of the four tools
by name and wraps its JSON result as MCP text content). `ping` is also handled.

## Tools

- **`lore_search(query, limit=5)`**: runs the same FTS5 query, quoted the same way as
  `lore.py search` (see `architecture.md`), against `.search.db` opened read-only
  (`file:...?mode=ro`); falls back to a substring scan across `pages/*.md` if the database is
  missing or the query still fails.
- **`lore_get_page(slug)`**: returns the raw content of `pages/<slug>.md`. The slug is
  validated first; an invalid slug or a missing page returns an `error` field rather than
  raising.
- **`lore_list_pages(category=None)`**: lists every page's slug, title, category, and
  `last_verified` date, optionally filtered to one category.
- **`lore_fact(slug, path)`**: reads `extracts/<slug>.json` and resolves `path` the same way
  `lore.py fact` does: dict keys and list indices (`items.0.name`), so the CLI and the MCP
  tool always agree on the value for the same slug and path.

Every tool that takes a `slug` validates it with the same rule `lore.py` uses
(`^[a-z0-9][a-z0-9._-]{0,127}$`, rejecting `..`) before touching the filesystem, so a slug
cannot be used to read a file outside `pages/` or `extracts/`.

## Running it

Point an MCP client's server config at
`python3 <config-dir>/skills/lore/scripts/lore-mcp-server.py`, with `LORE_DIR` set in that
client's environment block if the lore directory is not at the inferred default path.
`tool/tests/test_mcp_server.py` exercises the server the same way a real client would: JSON-RPC
messages written to its stdin over a pipe, responses read from its stdout.
