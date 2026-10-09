# Contributing

Lore is a small, file-based tool. The bar for a change is: does it make the CLI, the
scripts, or the docs more correct or more portable, without adding a dependency or a
placeholder.

## Running the tests

```bash
bash tests/run.sh
```

This runs `python3 -m unittest discover -s tests -v`. The suite needs only the Python
standard library and a `bash` on `$PATH`; nothing is installed first. `tests/test_shell.py`
additionally shells out to `bash -n` on every script under `skill/scripts/`, so a shell
syntax error fails the suite the same way a Python one does.

The floor is Python 3.12 (3.10 and 3.11 are security-only and 3.10 reaches end of life in
October 2026; the connectors' `X | Y` union type annotations only need 3.10 or newer, the
floor is set higher for support reasons, not syntax). Match the CI matrix in
`.github/workflows/ci.yml` before opening a change: `ubuntu-latest` and `macos-latest`,
Python `3.12` and `3.14`.

Before sending a change, also run:

```bash
bash examples/quickstart.sh
bash scripts/privacy-grep.sh .
```

The privacy grep must print `0 hits outside allowlist`. It exists to keep personal data out
of this public repository; a change that adds a real name, path, hostname, or credential
anywhere outside `LICENSE`, `NOTICE`, `README.md`, or `skill/SKILL.md`'s attribution line
will fail it, and CI runs the same check.

## No dependencies

`skill/` imports only the Python standard library. The one documented exception is the PDF
connector (`skill/connectors/pdf.py`), which shells out to a separate Python subprocess that
imports `mistralai`; that subprocess only runs when `MISTRAL_API_KEY` is set, and its
dependency is isolated from the rest of the codebase (see `docs/connectors.md`). Do not add
an import to `lore.py`, `lore-mcp-server.py`, or any other connector that is not in the
standard library; if a feature genuinely needs one, isolate it in its own subprocess the same
way and document it in `docs/connectors.md` and `skill/requirements.txt`.

Shell scripts target `bash`, sourcing `skill/scripts/lore-common.sh` for anything that
differs between macOS and Linux (see `docs/maintenance-loop.md`). Do not add a
platform-specific command directly to `lore-gardener.sh`, `lore-refresh.sh`, or
`lore-watchdog.sh`; add a portable helper to `lore-common.sh` instead, following the pattern
already there (one form tried first, the other tried second, a documented no-op last, with
whichever order avoids a partial or misleading result from the form that fails). Most
helpers try macOS first; `mtime_of()` tries the GNU `stat` form first instead, because GNU
stat can print stray output to stdout before failing when given a flag it does not
recognize, while BSD stat fails cleanly with nothing on stdout in the same situation, so
ordering GNU's own flag first means the only failure the fallback chain can ever hit is
BSD's clean one (see the comment above `mtime_of()` in `lore-common.sh` for the exact
reasoning).

## Style

- Shell: `bash -n` must pass on every script; keep `set -u`/`set -o pipefail` at the top of
  new scripts, following the existing ones.
- Python: standard library only (see above); type-annotate new function signatures the way
  the surrounding code already does.
- Docs and comments: no em dashes; no real Claude model IDs (use fictional names such as
  `example-model-large`, matching `skill/references/page-format.md`); no time, effort, or
  cost estimates.
- One change per pull request, with a commit message that says what changed and why.

## Reporting a bug

Open an issue with the command you ran, the output, your OS, and `python3 --version`. For a
security issue, see `SECURITY.md` instead of opening a public issue.
