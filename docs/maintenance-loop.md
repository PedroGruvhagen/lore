# The maintenance loop

Three scheduled shell scripts under `skill/scripts/` keep a lore directory current without a
human running commands by hand: `lore-refresh.sh`, `lore-gardener.sh`, and
`lore-watchdog.sh`. All three source `lore-common.sh` first, which resolves `SKILL_DIR` and
`LORE_DIR`, sources an optional `.lore.env` from the lore directory (see
`skill/lore.env.example` for every variable and its default), and provides the portability
helpers described below. `install.sh` schedules all three (see `install-macos.md` /
`install-linux.md`).

`lore-gardener.sh` and `lore-refresh.sh`, the two primary scripts, share a four-layer
reliability pattern: a tick line written to its own log on every run (so a watching process
can tell a script is alive, not just that it once succeeded), a wall-clock deadline enforced
inside the script itself (`LORE_GARDENER_DEADLINE_SEC`, `LORE_REFRESH_DEADLINE_SEC`), a
fourth, per-step layer against a single hung subprocess (`lore-gardener.sh` wraps its own
steps in `run_with_timeout`; `lore-refresh.sh` instead relies on the explicit timeout every
connector's own `fetch()` call takes), and `lore-watchdog.sh` as the external layer common to
both: a separate script, on its own 5-minute schedule, that notices a stale tick from either
primary from the outside and can act on it (see below) when the primary itself is too wedged
to notice.

## `lore-refresh.sh`

Runs `lore.py refresh --lore-dir $LORE_DIR`: walks every page's `sources:` list, fetches each
one through the connector `_pick_connector_name()` selects (see `connectors.md`), compares a
SHA-256 hash against `.hashes.json`, and writes a `queue/` diff record when content changed.
Ticks `.refresh.log` on every run. Guarded by `LORE_REFRESH_DEADLINE_SEC` (default 3600s).
Shares the mutation lock described below with the gardener, so the two never write the lore
directory at the same time. Scheduled every 6 hours by the shipped templates.

## `lore-gardener.sh`

The nightly maintenance pass: runs refresh, collects `lore.py pending --json` (queued diffs,
unseen failures, quarantined sources, stale auto-update pages, inbox notes), and, only if
the count of actionable items (diffs, unseen failures, quarantined sources, inbox notes;
stale pages alone do not count, since refresh has just run) is non-zero, launches a headless
CLI agent with a rendered prompt (`skill/references/gardener-prompt.md`) describing the work
queue and the priority order to work through it (reconcile diffs, investigate quarantined
sources, mark reviewed failures, file inbox notes, fix lint errors, curate at most one
oversized page, rebuild the index, verify). `LORE_GARDENER_DRYRUN=1` runs everything up to
that point, logs what it would do, and exits 0 before the preflights and before ever
launching the agent. The test suite (`tests/test_shell.py`) exercises the full launch path
instead, running the gardener against the fake agent scripts under `tests/fixtures/`.

Before launching the agent: a network preflight (`curl` against
`LORE_GARDENER_NETWORK_CHECK_URL`, default `https://api.anthropic.com/`, retried
`LORE_GARDENER_NETWORK_CHECK_RETRIES` times `..._INTERVAL_SEC` apart) and an auth preflight
(`${LORE_GARDENER_AUTH_CHECK:-claude auth status}`); either failure ends the run before the
agent is launched, classified (`network` or `auth`) and alerted on, without spending the
night's attempt. The agent itself is launched by word-splitting `$LORE_GARDENER_CMD` (default
`claude -p`), appending `--dangerously-skip-permissions --permission-mode
bypassPermissions`, then `$LORE_GARDENER_MODEL_ARGS` if set, then the rendered prompt as the
next positional argument, then `--output-format stream-json`. This is the gardener command
contract: any CLI agent that accepts that flag layout, exits `0`, and writes
`.gardener/report-<date>.md` can stand in for `claude -p` (the run counts as successful only
if both the exit code and the report file agree); it should also print one JSON `type:result`
line at the end so a failure can be classified. `LORE_KEEP_API_KEY=0` (the default) blanks
`ANTHROPIC_API_KEY` in the child process so the agent authenticates the same way an
interactive session would.

After the run, the last `type:result` line is classified by `classify_result_line()` (see
below) and the gardener retries once for a `network` or `sleep` failure, or waits for a
stated quota reset time if that time is still inside the deadline; a final failure is
reported through `alert()` as `cause=<class> rc=<n> result=<vendor message, 300 chars>`.
Guarded by `LORE_GARDENER_DEADLINE_SEC` (default 10800s) and, per attempt,
`LORE_GARDENER_CLAUDE_TIMEOUT_SEC` (default 9600s). Scheduled nightly at 03:30 by the shipped
templates.

The gardener writes its own tick line to `.gardener/gardener.log` first (Layer 1, plain shell
builtins, before any Python or network call), then acquires `$LORE_DIR/.lore.lock` (an atomic
`mkdir`) before touching anything refresh also mutates (`.hashes.json`, `queue/`, `failed/`,
page frontmatter), and treats a lock older than 14400 seconds as abandoned rather than held
by a live process.
`$LORE_DIR/.paused`, checked by all three scripts, idles the whole loop without editing any
scheduler configuration; `lore.py doctor` reports it if present.

## `lore-watchdog.sh`

Runs frequently (every 5 minutes in the shipped templates). It never launches the gardener's
agent itself, but a stale refresh tick does make it act, not just alert. Three checks: refresh
staleness (`.refresh.log`'s last tick older than `LORE_WATCHDOG_STALE_SEC`, default 46800s,
triggers an alert, a `SIGKILL` of any `lore-refresh.sh` or `lore.py` process still running,
then `scheduler_kick_refresh()` to start refresh immediately); gardener staleness
(`.gardener/gardener.log`'s last tick older than `LORE_GARDENER_STALE_SEC`, default 93600s,
alert only, once per day, deliberately no kickstart, since a missed night should be reported,
not retried at a random daytime hour); and watched-file size (`LORE_WATCH_FILES`, a
colon-separated list, each checked against `LORE_WATCH_FILE_MAX_BYTES`, default 19500 bytes,
alerting once per file per day; a path that does not exist is silently skipped).

## Portability layer (`lore-common.sh`)

Sourced by all three scripts above (and by `lore-alert.sh`), this file hides every
platform-specific primitive behind a function with a macOS branch, a Linux branch, and,
where neither applies, a documented no-op:

- `mtime_of(path)`: tries `stat -c %Y` (GNU/Linux) first, then `stat -f %m` (BSD/macOS).
- `notify_desktop(subject, body)`: `osascript` on macOS, `notify-send` on Linux if present,
  otherwise a no-op.
- `inhibit_sleep()`: takes no argument; starts `caffeinate` on macOS if present, or
  `systemd-inhibit --what=sleep:idle` on Linux if present, in the background and exports its
  PID as `LORE_INHIBIT_PID` for the caller's exit trap to kill, otherwise a no-op (the script
  still runs, it just is not protected from the machine sleeping).
- `scheduler_kick_refresh()`: `launchctl kickstart` on macOS,
  `systemctl --user start lore-refresh.service` on systemd, otherwise runs `lore-refresh.sh`
  directly.
- `scheduler_has_job(name)`: `launchctl list` on macOS, `systemctl --user list-timers` on
  systemd, otherwise returns 2 for "unknown"; used by `lore-watchdog.sh` to decide whether a
  gardener job is installed at all before judging its log stale. (`lore.py doctor`'s
  scheduler check is separate Python code; see `troubleshooting.md`.)
- `classify_result_line(line)`: the failure classifier: `rc=137` maps to `killed`;
  `ENOTFOUND`, `ECONNREFUSED`, `ConnectionRefused`, `ETIMEDOUT`, or `EAI_AGAIN` map to
  `network`; `429`, `limit · resets`, or `rate limit` map to `quota`; `authenticate`,
  `OAuth`, or `401` map to `auth`; `went to sleep` maps to `sleep`; anything else maps to
  `unknown`.
- `alert(subject, body)`: always appends one line to `$LORE_DIR/.alerts.log`, then dispatches
  to whichever provider `LORE_ALERT_PROVIDER` names (see below). `lore-alert.sh` is a thin
  standalone wrapper around this function, used as `$LORE_ALERT` inside the gardener's prompt
  so the agent itself can escalate mid-session.

## Alerting

`LORE_ALERT_PROVIDER` selects what `alert()` does beyond the always-on log line: `log`
(default, nothing more), `desktop` (`notify_desktop()` above), `telegram` (direct `curl` to
the Telegram Bot API using `LORE_TELEGRAM_BOT_TOKEN`/`LORE_TELEGRAM_CHAT_ID`, request body
piped over stdin so neither value nor the message ever appears in `ps` output), or `command`
(runs `$LORE_ALERT_CMD "<subject>"` with `<body>` piped to its stdin, for a relay script of
your own). See the README's Privacy section for the full list of places this loop, and Lore
generally, makes an outbound request.

## Queue flow, quarantine, and `.hashes.json`

`lore.py refresh` writes `queue/<slug>-<date>.diff.md` when a source's content hash changes; the
gardener's priority list works through these first. A source that fails to fetch repeatedly
is quarantined (its `.hashes.json` entry gets a `quarantined_until` date after enough
consecutive failures) so a broken source stops being retried every run; `lore.py doctor`
reports the current quarantine count. `lore.py prune --orphan-hashes` removes a
`.hashes.json` entry no page's `sources:` list references any more.

The hash `refresh` compares is not a hash of the raw fetched bytes. For an HTML source it
hashes the same whitespace-normalized extracted text a queue diff shows, so a build id,
nonce, or other per-request markup embedded in the page never trips a false `[CHANGED]`;
every other source kind (JSON, RSS/Atom, PDF text, git output) still hashes its raw content,
since those have no such churn problem. Every successful `.hashes.json` entry carries a
`hash_scheme` marker; an older entry without one is compared against its original raw-bytes
hash instead, and silently upgraded in place (logged as "unchanged (hash scheme upgraded)")
the first time its content still matches, so migrating to the new scheme never misses a
real change, and only a source whose raw bytes also changed produces a diff on the first run
after the upgrade, instead of a one-time flood of false changes. `refresh` also caches each
source's built payload under `.refresh-cache/` (one file per source, named by the SHA-256 of
its URL), and a `[CHANGED]` diff record in `queue/` opens with a unified diff against that
cached payload, ahead of the full new payload, so a real change is visible at a glance.
