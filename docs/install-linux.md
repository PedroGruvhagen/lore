# Installing on Linux

```bash
bash install.sh --config-dir ~/.claude
```

The copy, bootstrap, and `.lore.env` steps are identical to the macOS install (see
`install-macos.md`); this page only covers what differs: scheduler detection and the
portability fallbacks used when a Linux command a script normally prefers is not installed.

## Scheduler

`install.sh` auto-detects `--scheduler systemd` when `systemctl` is on `$PATH` **and** a
`--user` manager is actually reachable (`systemctl --user list-units` succeeds; having the
binary installed is not enough, since a container or a session started outside a login
manager can lack a running `--user` instance). When detected, it renders the six templates
under `systemd/`: a `.service` and a `.timer` for each of `lore-gardener`, `lore-refresh`,
`lore-watchdog`, substituting the same `__HOME__`/`__SKILL_DIR__`/`__LORE_DIR__`/`__PATH__`
tokens as the launchd templates, into `~/.config/systemd/user/`, runs
`systemctl --user daemon-reload`, and enables each timer with
`systemctl --user enable --now`. Confirm:

```bash
systemctl --user list-timers | grep lore-
```

If no `--user` systemd manager is reachable but `crontab` is, `install.sh` falls back to
`--scheduler cron`: three lines marked with a trailing `# lore` comment, each setting
`LORE_DIR`, `HOME`, and a fixed `PATH` inline (`crontab -l | grep lore` shows them). Reruns
are idempotent: the installer strips any previously installed `# lore` lines before adding
the current three, so a second run never duplicates entries. `--scheduler none` skips
scheduling entirely on any platform.

`--uninstall` removes whichever scheduler's jobs this installer knows about: `systemctl
--user disable --now` plus the two unit files per job for systemd, or the `# lore`-marked
crontab lines for cron; `--config-dir` and `--data-dir` content is never touched.

## Portability fallbacks used on Linux

`lore-common.sh` (see `maintenance-loop.md`) is what lets the same three shell scripts run
unchanged on Linux: `stat -c %Y` instead of macOS's `stat -f %m`, `notify-send` instead of
`osascript` (skipped silently if neither is installed), `systemd-inhibit
--what=sleep:idle` instead of `caffeinate` (also skipped silently if unavailable, the script
still runs, just without a sleep-inhibit guarantee), and `systemctl --user start
lore-refresh.service` instead of `launchctl kickstart` for the watchdog's refresh kickstart.
`lore.py doctor`'s scheduler check likewise tries `systemctl --user list-timers` then
`crontab -l` on any non-macOS platform, matching unit or crontab lines containing `lore-`.

## Python floor

`install.sh` and `lore.py doctor` both expect Python 3.12 or newer. 3.10 and 3.11 are
security-only and 3.10 reaches end of life in October 2026; the connectors' `X | Y` union
type annotations only need 3.10, the floor is set higher for support reasons. Most current
Linux distributions ship this by default; if yours does not, install a newer `python3` and
point your shell's `python3` at it before running `install.sh`.
