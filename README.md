# Lore: a memory plugin for Claude Code

Lore is a long-term memory plugin for Claude Code. Claude keeps what it learns about your projects, servers and technical facts as small Markdown pages, looks them up before answering, and keeps its MEMORY.md as a short index into those pages instead of a pile of notes.

The plugin is deterministic plumbing. It makes no model call, sends nothing to any network service, and never writes files itself.

| Part | What it does |
|---|---|
| MEMORY.md guard | Refuses a write that makes a MEMORY.md index worse: too many lines, too long, too big, or a list line with no pointer to a Lore page. |
| Index loader | Loads the project's MEMORY.md index at session start, so Claude Code's own auto-memory can stay off. |
| `recall` and `remember` | Tools that search Lore and drop a durable fact into its inbox. |
| `/lore` | Shows index size, over-cap lines and the last gardener report. |
| Compaction reminder | A one-line toast before compaction; it never blocks. |

## Install

    /plugin install lore --marketplace PedroGruvhagen/lore

The plugin runs the Lore tool in `tool/` (`lore.py`), so install that first:

    bash tool/install.sh --config-dir ~/.claude

Then set the four required path options. Steps, options, and the exact list of what the plugin reads and runs are in `docs/claude-code-plugin.md`. Requires Claude Code 2.1.287 or newer.

## The Lore tool (`tool/`)

The plugin sits on top of the Lore tool: a file-based, LLM-maintained knowledge base with a self-maintaining refresh, gardener and watchdog loop. Everything below describes the tool; all paths in this part are relative to the repository root.

> Based on Andrej Karpathy's LLM Wiki pattern (published April 3, 2026), as credited in
> `tool/skill/references/karpathy-llm-wiki.md`. Lore is an independent implementation of that
> pattern, not a fork of his code.

## Why

Model training data goes stale the moment training ends, but a model answers with full
confidence regardless. Lore gives an agent a place to keep facts current itself: a directory
of Markdown pages it searches before answering, updates when it learns something new, and
refreshes on a schedule by re-checking the sources those pages came from. The agent is both
author and reader, so the format stays exactly as complex as an agent needs it to be, no
more.

## What's in the box

- **`tool/skill/scripts/lore.py`**: the CLI: `bootstrap`, `index`, `search`, `lint`,
  `check-entity`, `ingest`, `stale`, `log`, `sync`, `refresh`, `fact`, `graph`, `inbox`,
  `pending`, `seen`, `prune`, `doctor`, `review`. Run `lore.py --help` for the full list, or
  `lore.py <command> --help` for one command. Standard library only; see
  `tool/docs/connectors.md` for the one documented exception.
- **`tool/skill/scripts/lore-mcp-server.py`**: a read-only MCP server exposing `lore_search`,
  `lore_get_page`, `lore_list_pages`, and `lore_fact` over stdio JSON-RPC, for MCP clients
  like Claude Desktop. See `tool/docs/mcp-server.md`.
- **`tool/skill/scripts/lore-gardener.sh`, `tool/skill/scripts/lore-refresh.sh`,
  `tool/skill/scripts/lore-watchdog.sh`, `tool/skill/scripts/lore-common.sh`**: the unattended
  maintenance loop: a nightly agent run that reconciles queued changes, a periodic
  source-hash check that queues them, and a watchdog that kills and restarts a wedged
  refresh, or only alerts (no kill, no restart) on a stale gardener tick. Runs under
  launchd (macOS), systemd (Linux), or cron (either), through the same portable shell
  layer. See `tool/docs/maintenance-loop.md`.
  ```
  lore-refresh.sh (every 6h)        lore-gardener.sh (nightly)
    hash sources, queue diffs  ->     resolve queue, update pages
                                       rebuild index + graph
  lore-watchdog.sh (every 5m): on a stale refresh tick, kills the refresh
  process and restarts it; on a stale gardener tick, only alerts.
  ```
- **`tool/skill/connectors/`**: one Python module per source type (`web`, `pypi`, `npm`,
  `github_releases`, `rss`, `pdf`, `git_repo`) sharing a common `_http.py` fetch helper,
  dispatched by URL pattern. See `tool/docs/connectors.md`.
- **`tool/install.sh`**: copies `tool/skill/` into place, bootstraps a lore data directory, and
  installs whichever scheduler your platform actually has. See `tool/docs/install-macos.md` and
  `tool/docs/install-linux.md`.
- **`tool/docs/`**: architecture, page format, the maintenance loop, connectors, both install
  guides, the MCP server, project scoping, and troubleshooting.
- **`tool/examples/`**: a runnable quickstart script, a Python equivalent, an example page, and
  an example lint config.
- **`tool/tests/`**: a stdlib `unittest` suite (`tool/tests/run.sh`) covering the CLI, the connectors,
  and the MCP server.

## Quickstart

```bash
python3 tool/skill/scripts/lore.py bootstrap --lore-dir /tmp/my-lore
# ... write a page under /tmp/my-lore/pages/, see tool/examples/pages/example-api.md ...
python3 tool/skill/scripts/lore.py index --lore-dir /tmp/my-lore
python3 tool/skill/scripts/lore.py search "example" --lore-dir /tmp/my-lore
python3 tool/skill/scripts/lore.py fact example-api endpoint --lore-dir /tmp/my-lore
python3 tool/skill/scripts/lore.py doctor --lore-dir /tmp/my-lore --offline
```

`tool/examples/quickstart.sh` runs this sequence end to end (adding an `extracts/` file and a
`lint-config.json`, and running `lint` in place of `doctor`) and prints the looked-up fact;
`tool/examples/deterministic_facts.py` does the bootstrap, index, and fact steps from Python.

## Installing for real use

Requires Python 3.12 or newer.

```bash
bash tool/install.sh --config-dir ~/.claude
```

Auto-detects launchd on macOS, systemd (falling back to cron) on Linux, copies `tool/skill/`
into `<config-dir>/skills/lore/`, bootstraps a data directory, and schedules the
maintenance loop. See `tool/docs/install-macos.md` or `tool/docs/install-linux.md` for what it does
step by step, and `tool/docs/project-scope.md` for wiring a per-project lore directory alongside
a global one.

Built for Claude Code; runs any CLI agent that honours the gardener command contract: the
command named by `LORE_GARDENER_CMD` is launched with `--dangerously-skip-permissions
--permission-mode bypassPermissions`, then `$LORE_GARDENER_MODEL_ARGS` if set, then the
rendered prompt as the next positional argument, then `--output-format stream-json`; the run
counts as successful only if that command exits `0` and writes `.gardener/report-<date>.md`,
and it should print one JSON `type:result` line at the end so a failure can be classified.
See `tool/docs/maintenance-loop.md`.

## Privacy

Outbound network traffic happens in these places, each either your own choice or a normal
part of running the tool you pointed it at: the agent's own API traffic (to whichever
coding-agent CLI you point `$LORE_GARDENER_CMD` at during a gardener run), preceded by that
gardener's own network preflight, a `curl` to `$LORE_GARDENER_NETWORK_CHECK_URL` (default
`https://api.anthropic.com/`), retried up to six times before the run gives up rather than
spend its deadline against a dead network; the alert provider you configure (`log` by
default, which never leaves the machine, `telegram` posting to the Telegram Bot API,
`command` running a relay script of your own); a fetch of the source URL a page cites, made
by `lore.py refresh` and `lore.py lint --check-links` (opt-in); `lore.py doctor`'s live fetch
self-test (unless `--offline`), which fetches a fixed test URL named by `$LORE_DOCTOR_URL`
(default `https://example.com`), not any page's own source; `lore.py sync`, a `git pull`/
`git push` of the lore directory itself, if you choose to track it in a remote; and, only for
a page with a `.pdf` source, the PDF connector sending that document to the OCR service named
by `MISTRAL_API_KEY`, which sends nothing at all when that variable is unset (see
`tool/docs/connectors.md`). Beyond that, Lore has no telemetry and no phone-home.
`tool/scripts/privacy-grep.sh` scans a directory or git history for credential shapes and for
any personal strings you list in a private blocklist file (`LORE_PRIVACY_BLOCKLIST`, kept
outside the repository); run it against your own lore pages before publishing anything
derived from this project.

## License

Apache License 2.0. See `LICENSE` and `NOTICE`.

## A note from the author

The track record behind this is real daily use:
two years of my memory approach, Lore since spring 2026. It is the thing that has held up
best out of everything I have tried, and a few friends who had also tried a lot of
alternatives have taken to it too. That is not a claim that it is the only thing that
works, or the best possible approach, only that it works for us. If it is useful to you,
I am glad. Good criticism and better ideas are genuinely welcome. And if it is not for
you, that is completely fine: leave it and use what works for you.
