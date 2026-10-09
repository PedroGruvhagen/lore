# Lore Claude Code plugin

Deterministic plumbing between Claude Code and the Lore memory system. The model still decides what is worth saving, guided by the Lore rules in CLAUDE.md. The mod makes no model call and sends nothing to any network service.

Tested against Claude Code **2.1.295** (mods need 2.1.287 or newer). The mods API is early access and changes between releases; re-run `claude plugin validate --strict` and `claude plugin test` after every Claude Code update.

## What it does

| Part | Hook | Behaviour |
|---|---|---|
| MEMORY.md guard | `tool.call` on Write, Edit, MultiEdit, Bash | Refuses a write to any `.../memory/MEMORY.md` that makes the index worse than it is now: more lines than `maxLines`, more bytes than `maxBytes`, longer or more lines over `maxLineChars`, or a new list line with no pointer to a Lore page. Edits that shorten or fix lines, and rewrites that shrink an oversize file, pass. A new file must be compliant. The refusal tells Claude to put the detail in the Lore page and add one short pointer line. |
| Session load | `prompt.context` | Adds the cwd's MEMORY.md as an instruction file of kind `memory` in the first message's `claudeMd` block, exactly where Claude Code puts it when auto-memory is on (so subagents get it too), plus a fixed `loreRules` block. The text depends only on the file, with no timestamps, so it stays stable for the prompt cache. When core already supplies a memory file, the mod adds nothing, so the index is never loaded twice. `session.start` cannot inject, which is why it is not used for this. |
| Tools | `session.start` registers, `tool.call` serves | `mcp__lore__recall` runs `lore.py search`; `mcp__lore__remember` runs `lore.py inbox` (the gardener files the note). Both use argv arrays, no shell. |
| Compaction | `session.compact` | Never blocks. Shows a one-line reminder for manual and automatic compaction. |
| `/lore` | `command.run` (immediate, no model turn) | Index lines and bytes, over-cap line numbers, newest gardener report, whether index loading is on, and whether `autoMemoryEnabled` is false in each configured account dir. |

`hooks/extension.ts` holds `decideWhatToSave`, an empty extension point that nothing calls.

## Install

Requirements: Claude Code 2.1.287 or newer (the mods API is early access), and a Lore install from this repository (`install.sh`), which provides `lore.py` and your Lore data directory.

1. Install the Lore tool and create a data directory:

       bash install.sh --config-dir ~/.claude

   This puts `lore.py` at `~/.claude/skills/lore/scripts/lore.py` and creates `~/.claude/lore`.
2. Add the marketplace and install the plugin, then reload:

       /plugin install lore --marketplace PedroGruvhagen/lore
       /reload-plugins

3. Set the options. The path options have no defaults and are required; Claude Code asks for them, or run `claude plugin configure lore`. Use absolute paths (a leading `~` is not expanded):
   - `projectsDir`: your Claude Code projects directory, for example `/home/you/.claude/projects`.
   - `python`: a Python 3.12 or newer interpreter.
   - `lorePy`: `/home/you/.claude/skills/lore/scripts/lore.py`.
   - `loreDir`: `/home/you/.claude/lore`.
   - `accountDirs` (optional): config directories that `/lore` checks for `autoMemoryEnabled: false`.
4. Recommended: set `"autoMemoryEnabled": false` in `settings.json`, so this plugin is the only index loader.

Develop and try it without installing: `claude --plugin-dir /path/to/this/repo --debug-file ./lore-debug.log`. Installed copies are cached by version, so bump `version` in `.claude-plugin/plugin.json` after each change.

Disable: one mod in `/plugin` (Installed), all mods for one session with `--safe-mode`, everywhere with `"disableAllHooks": true` (this also stops settings hooks and the status line).

Options with defaults (edit under `/config` or `pluginConfigs.lore.options`): `maxLines` 150, `maxLineChars` 200, `maxBytes` 19500, `loadIndex` true (set false to disable the loader only). If a required path option is unset, the recall and remember tools say so and the index loader stays off; nothing breaks.

## Auto-memory and loading

With `autoMemoryEnabled: false` (or `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`) Claude Code stops loading MEMORY.md and stops its memory-writing prompt; this mod then becomes the only loader. While auto-memory is on, core already supplies the memory file and the mod stands aside, so there is no double load. `/lore` reports each account's state.

## What the mod can read, write and run

Read:
- The cwd's `MEMORY.md` under `projectsDir` (session load and `/lore`), and the file a Write/Edit/MultiEdit call targets when its path ends in `memory/MEMORY.md`.
- The names in `<loreDir>/.gardener/` (directory listing only).
- Tool-call arguments and the session's working directory (`$.session.cwd`, `$.session.root`).

Write:
- Nothing directly. The mod never writes a file. Lore changes happen only through `lore.py inbox`.

Run (argv arrays, never a shell):
- `<python> <lorePy> search --lore-dir <loreDir> --limit 5 -- <query>`
- `<python> <lorePy> inbox --lore-dir <loreDir> -- <text>`
- `/usr/bin/git -C <cwd> rev-parse --path-format=absolute --git-common-dir` (finds the repo's canonical root, because Claude Code names the memory folder after it; subdirectories and worktrees share the main checkout's memory)
- `/usr/bin/grep -Eq '"autoMemoryEnabled"...false' <accountDir>/settings.json` (exit code only; the mod never sees the file's content)

It does not use `$.http`, `$.model`, `$.env`, `$.settings`, `$.store`, or any provider API, and reads no environment variable, credential file or secret. `claude plugin validate --strict` lists the calls the module makes: `$.command.register`, `$.fs.exists`, `$.fs.list`, `$.fs.read`, `$.process.run`, `$.session.cwd`, `$.session.root`, `$.tool.register`, `$.ui.log`, `$.ui.toast`.

`remember` hands the text to `lore.py` exactly as given. The mod never filters, scans, redacts or refuses what you ask Lore to save. The only content check in the mod is the MEMORY.md guard, which looks at line count, line length, size and pointer shape.

Logging: the mod writes nothing but metadata to the debug log (decisions, counts, exit codes). Prompts, tool arguments, file contents and remembered text are never logged; tests assert this.

## What the guard cannot catch

- Shell writes it cannot see: variables, scripts, other interpreters, a relative `MEMORY.md` from inside the memory dir, `eval`.
- Writes by tools it does not hook, or by anything outside Claude Code.
- Anything when a hook times out or errors: the mod fails open, so the write goes through.
- Whether a pointer line really names an existing Lore page; it checks shape only.

The nightly gardener is the backstop for these, once it runs again.

## Tests

`claude plugin test` runs the tests in `hooks/`: guard cases (over-cap append, long line, compliant pointer, in-place update, unrelated files, both account dirs, shrinking vs growing rewrite of an oversize file, bare list line, read failure, shell writes), index loading via `prompt.context` (cwd, subdirectory, worktree, outside git, already-supplied by core, no index), byte stability, tools, secret passthrough, no content in logs, `/lore`, and compaction.

Manual headless check (run once before any install):
`cd <empty dir> && claude -p "reply with OK" --plugin-dir /path/to/this/repo --output-format stream-json --verbose --debug-file ./lore-debug.log`, then confirm the stream ends with a result line, `lore-debug.log` shows `hooks module lore@inline loaded`, and the log contains no prompt or memory text.

