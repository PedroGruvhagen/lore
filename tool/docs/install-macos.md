# Installing on macOS

```bash
bash tool/install.sh --config-dir ~/.claude
```

`--config-dir` defaults to `~/.claude` if omitted. This copies `tool/skill/` to
`<config-dir>/skills/lore/`, makes the known scripts executable again, bootstraps a lore
data directory at `<config-dir>/lore` if one does not already exist there (an existing,
non-empty data directory is reused, never overwritten), writes `<data-dir>/.lore.env` from
`tool/skill/lore.env.example` if none exists yet, and finishes with an informational
`lore.py doctor --offline` (its exit code never fails the install; a fresh install with no
sources configured is expected to show `WARN` lines).

## Scheduler

On macOS, with `launchctl` present, `tool/install.sh` auto-detects
`--scheduler launchd` and renders the three templates under `tool/launchd/`:
`com.lore.gardener.plist.template`, `com.lore.refresh.plist.template`,
`com.lore.watchdog.plist.template`, substituting `__HOME__`, `__SKILL_DIR__`,
`__LORE_DIR__`, `__LABEL_PREFIX__` (default `com.lore.`), and `__PATH__`, into
`~/Library/LaunchAgents/`, then loads each with `launchctl bootstrap` (falling back to the
older `launchctl load` if `bootstrap` is unavailable). The rendered gardener job runs at
03:30 daily (`StartCalendarInterval`); refresh and watchdog run on the intervals described in
`maintenance-loop.md`. Pass `--scheduler none` to skip this and manage the schedule yourself.

Override the label prefix (to run more than one lore install on the same machine) by setting
`LORE_LAUNCHD_LABEL_PREFIX` in your shell before running `tool/install.sh`. The prefix is baked
into the rendered plist filenames and labels at install time, so relabeling an existing
install means re-running `tool/install.sh` with the new value (`--uninstall` first if you want the
old jobs gone). Editing `<data-dir>/.lore.env` afterward does not relabel anything: that file
is read by the scheduled scripts themselves at run time (via `lore-common.sh`), not by
`tool/install.sh`. Confirm the jobs loaded:

```bash
launchctl list | grep com.lore.
```

## Other flags

- `--data-dir <path>`: put the lore data directory somewhere other than
  `<config-dir>/lore`.
- `--dry-run`: print the plan (what would be copied, bootstrapped, and scheduled) and write
  nothing.
- `--render-only <dir>`: render the launchd plists into `<dir>` and exit, without touching
  `~/Library/LaunchAgents` or the real config/data directories; useful for inspecting a
  rendered job file before trusting it.
- `--uninstall`: unloads and removes the three launchd jobs this installer knows about, by
  label; never touches `--config-dir` or `--data-dir` content.

Re-running `tool/install.sh` is safe: it never overwrites an existing data directory or an
existing `.lore.env`, and it reports what it verified.

## Alerting on macOS

`LORE_ALERT_PROVIDER=desktop` in `.lore.env` uses `osascript` for a local notification; `log`
(the default) only appends to `.alerts.log`; see `maintenance-loop.md` for `telegram` and
`command`.

## Troubleshooting an install

See `troubleshooting.md` for what each `lore.py doctor` line means, and for the failure
classes the gardener alerts on.
