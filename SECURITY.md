# Security Policy

## Supported versions

Lore is a single, current release line (`1.0.0`, see `CHANGELOG.md`). Security fixes are
made against the latest `1.x` release; there is no older supported line.

## What counts as a vulnerability here

Lore is a local, file-based tool: it reads and writes markdown and JSON under a lore
directory you control, and it makes outbound requests only when you run `refresh`,
`lint --check-links`, `doctor` (unless `--offline`), `sync` (a `git pull`/`git push` of the
lore directory), the nightly gardener/refresh scripts, or `lore-watchdog.sh` (which can send
an alert through your configured provider and, on a stale refresh tick, kick a fresh
`lore-refresh.sh` invocation itself) (`ingest` reads a local file and makes none). Reports in
scope include, for example:

- A crafted lore page, source document, or `queue/` diff that causes `lore.py` or
  `lore-mcp-server.py` to execute code, read, or write outside the resolved lore directory
  (see `tool/docs/architecture.md` for what that directory is expected to contain).
- A connector (`tool/skill/connectors/*.py`) that can be made to run an arbitrary command, follow
  a redirect to an unintended scheme, or leak a credential (an API key, a bearer token) into
  a log, an argument list visible to other local users, or a response body.
- A scheduler template (`tool/launchd/*.template`, `tool/systemd/*.template`) or `tool/install.sh` that
  installs a job running with unintended privileges or an unintended, unrendered path.
- The MCP server (`tool/skill/scripts/lore-mcp-server.py`) accepting a request that reads a file
  outside the lore directory (see the slug validation described in `tool/docs/mcp-server.md`).

Out of scope: anything that requires you to already control the lore directory's content in
a way no connector or agent would write on its own, and anything that only reproduces in a
locally misconfigured install (for example, a `.lore.env` you wrote yourself with an unsafe
alert command).

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting for this repository (the "Report a
vulnerability" link under the repository's Security tab) rather than a public issue. Include
the command or request that triggers the problem, the version (`lore.py --version`), and
your platform.

There is no bug bounty. Reports are read and, for a confirmed issue, a fix is released as a
patch version with a `CHANGELOG.md` entry.
