# Lore

A persistent, LLM-maintained knowledge base for AI coding agents: a place for an agent to
write down what it learns about your projects, your infrastructure, and technical facts,
so it never has to relearn or re-derive them, and never has to guess when its training data
is stale or wrong.

> Based on Andrej Karpathy's LLM Wiki pattern (published April 3, 2026), as credited in
> `skill/references/karpathy-llm-wiki.md`. Lore is an independent implementation of that
> pattern, not a fork of his code.

## Claude Code plugin

This repository is also a Claude Code plugin marketplace. The `lore` plugin is deterministic plumbing between Claude Code and Lore: a MEMORY.md write guard, a session-start index loader, `mcp__lore__recall` and `mcp__lore__remember` tools, a `/lore` status command, and a compaction reminder. It makes no model call, sends nothing to any network service, and never writes files itself.

    /plugin install lore --marketplace PedroGruvhagen/lore

Install the Lore tool first (`bash install.sh --config-dir ~/.claude`); full steps, options and the exact list of what the plugin reads and runs are in `docs/claude-code-plugin.md`. The plugin's code is in `hooks/` and `.claude-plugin/`.

## Why

Model training data goes stale the moment training ends, but a model answers with full
confidence regardless. Lore gives an agent a place to keep facts current itself: a directory
of Markdown pages it searches before answering, updates when it learns something new, and
refreshes on a schedule by re-checking the sources those pages came from. The agent is both
author and reader, so the format stays exactly as complex as an agent needs it to be, no
more.

## What's in the box

- **`skill/scripts/lore.py`**: the CLI: `bootstrap`, `index`, `search`, `lint`,
  `check-entity`, `ingest`, `stale`, `log`, `sync`, `refresh`, `fact`, `graph`, `inbox`,
  `pending`, `seen`, `prune`, `doctor`, `review`. Run `lore.py --help` for the full list, or
  `lore.py <command> --help` for one command. Standard library only; see
  `docs/connectors.md` for the one documented exception.
- **`skill/scripts/lore-mcp-server.py`**: a read-only MCP server exposing `lore_search`,
  `lore_get_page`, `lore_list_pages`, and `lore_fact` over stdio JSON-RPC, for MCP clients
  like Claude Desktop. See `docs/mcp-server.md`.
- **`skill/scripts/lore-gardener.sh`, `skill/scripts/lore-refresh.sh`,
  `skill/scripts/lore-watchdog.sh`, `skill/scripts/lore-common.sh`**: the unattended
  maintenance loop: a nightly agent run that reconciles queued changes, a periodic
  source-hash check that queues them, and a watchdog that kills and restarts a wedged
  refresh, or only alerts (no kill, no restart) on a stale gardener tick. Runs under
  launchd (macOS), systemd (Linux), or cron (either), through the same portable shell
  layer. See `docs/maintenance-loop.md`.
  ```
  lore-refresh.sh (every 6h)        lore-gardener.sh (nightly)
    hash sources, queue diffs  ->     resolve queue, update pages
                                       rebuild index + graph
  lore-watchdog.sh (every 5m): on a stale refresh tick, kills the refresh
  process and restarts it; on a stale gardener tick, only alerts.
  ```
- **`skill/connectors/`**: one Python module per source type (`web`, `pypi`, `npm`,
  `github_releases`, `rss`, `pdf`, `git_repo`) sharing a common `_http.py` fetch helper,
  dispatched by URL pattern. See `docs/connectors.md`.
- **`install.sh`**: copies `skill/` into place, bootstraps a lore data directory, and
  installs whichever scheduler your platform actually has. See `docs/install-macos.md` and
  `docs/install-linux.md`.
- **`docs/`**: architecture, page format, the maintenance loop, connectors, both install
  guides, the MCP server, project scoping, and troubleshooting.
- **`examples/`**: a runnable quickstart script, a Python equivalent, an example page, and
  an example lint config.
- **`tests/`**: a stdlib `unittest` suite (`tests/run.sh`) covering the CLI, the connectors,
  and the MCP server.

## Quickstart

```bash
python3 skill/scripts/lore.py bootstrap --lore-dir /tmp/my-lore
# ... write a page under /tmp/my-lore/pages/, see examples/pages/example-api.md ...
python3 skill/scripts/lore.py index --lore-dir /tmp/my-lore
python3 skill/scripts/lore.py search "example" --lore-dir /tmp/my-lore
python3 skill/scripts/lore.py fact example-api endpoint --lore-dir /tmp/my-lore
python3 skill/scripts/lore.py doctor --lore-dir /tmp/my-lore --offline
```

`examples/quickstart.sh` runs this sequence end to end (adding an `extracts/` file and a
`lint-config.json`, and running `lint` in place of `doctor`) and prints the looked-up fact;
`examples/deterministic_facts.py` does the bootstrap, index, and fact steps from Python.

## Installing for real use

Requires Python 3.12 or newer.

```bash
bash install.sh --config-dir ~/.claude
```

Auto-detects launchd on macOS, systemd (falling back to cron) on Linux, copies `skill/`
into `<config-dir>/skills/lore/`, bootstraps a data directory, and schedules the
maintenance loop. See `docs/install-macos.md` or `docs/install-linux.md` for what it does
step by step, and `docs/project-scope.md` for wiring a per-project lore directory alongside
a global one.

Built for Claude Code; runs any CLI agent that honours the gardener command contract: the
command named by `LORE_GARDENER_CMD` is launched with `--dangerously-skip-permissions
--permission-mode bypassPermissions`, then `$LORE_GARDENER_MODEL_ARGS` if set, then the
rendered prompt as the next positional argument, then `--output-format stream-json`; the run
counts as successful only if that command exits `0` and writes `.gardener/report-<date>.md`,
and it should print one JSON `type:result` line at the end so a failure can be classified.
See `docs/maintenance-loop.md`.

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
`docs/connectors.md`). Beyond that, Lore has no telemetry and no phone-home.
`scripts/privacy-grep.sh` scans a directory or git history for credential shapes and for
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
