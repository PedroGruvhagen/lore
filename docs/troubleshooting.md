# Troubleshooting

Start with `lore.py doctor` (add `--offline` to skip the live outbound-fetch check, `--json`
for machine-readable output). Every check prints `[PASS]`, `[WARN]`, `[FAIL]`, `[INFO]`, or
`[SKIP]`; the process exits non-zero only if any check is `[FAIL]`.

## Reading `doctor`'s checks

- **`version`**: the installed `lore.py`'s `LORE_VERSION` (`INFO`, always present).
- **`python`**: `FAIL`s below Python 3.12 (3.10 and 3.11 are security-only and 3.10 reaches
  end of life in October 2026; the connectors' `X | Y` union annotations only need 3.10).
- **`lore-dir-source`**: which resolution rule matched: explicit `--lore-dir`, `$LORE_DIR`,
  `{cwd}/lore` (project scope, see `project-scope.md`), a script-relative default, or the
  `~/.claude/lore` portable fallback.
- **`lore-dir`**: `FAIL`s if the resolved directory does not exist at all (run
  `lore.py bootstrap` first).
- **`paused`**: `WARN`s if `$LORE_DIR/.paused` exists; the whole maintenance loop idles
  while it does (see `maintenance-loop.md`).
- **`pages`**: `WARN`s at 0 pages, otherwise `PASS`es with the count.
- **`fetch`**: a live outbound-fetch self-test against `LORE_DOCTOR_URL` (default
  `https://example.com`), through the same connector dispatch `refresh` uses; `SKIP`ped with
  `--offline`.
- **`search-db`**: `WARN`s if `.search.db` is missing (run `lore.py index`).
- **`graph`**: whether `.graph.json` exists.
- **`refresh-log`**: `FAIL`s if `.refresh.log` has no parseable tick, or its last tick is
  older than twice `LORE_REFRESH_INTERVAL_SEC` (default 6 hours, so a 12-hour-old tick
  fails); this is the check that tells you the scheduled refresh job has stopped running.
- **`watchdog`**: `WARN`s if `.watchdog.log`'s last tick is more than 30 minutes old (only
  checked if that file exists at all).
- **`scheduler`**: on macOS, `PASS`es if `launchctl list` shows a label containing
  `LORE_LAUNCHD_LABEL_PREFIX` (default `com.lore.`); elsewhere, tries
  `systemctl --user list-timers` then `crontab -l` for a `lore-`-named entry. `WARN`s (never
  `FAIL`s) if none is found: a manual or no-scheduler setup is valid, not broken.
- **`failed`**: unseen `failed/*.err` records (mark reviewed with `lore.py seen --all`, or
  investigate them first).
- **`quarantine`**: how many sources are currently quarantined (see `maintenance-loop.md`).
- **`queue`**: how many `queue/` diffs are older than 48 hours, unreviewed.
- **`git`**: whether the lore directory is a git repository.

## Gardener failure classes

If the nightly gardener alerts, its message has the shape
`cause=<class> rc=<n> result=<message>`. The class comes from `classify_result_line()` in
`lore-common.sh`, matched against the run's last `type:result` line:

| Class | Matched on |
|-------|------------|
| `killed` | exit code 137 |
| `network` | `ENOTFOUND`, `ECONNREFUSED`, `ConnectionRefused`, `ETIMEDOUT`, `EAI_AGAIN` |
| `quota` | `429`, `limit · resets`, `rate limit` |
| `auth` | `authenticate`, `OAuth`, `401` |
| `sleep` | `went to sleep` |
| `unknown` | anything else |

`network` and `sleep` are retried once automatically; `quota` is retried only if the message
states a reset time still inside the run's deadline; `auth` and `unknown` are not retried.
See `maintenance-loop.md` for the preflight checks that catch a `network` or `auth` failure
before the agent is even launched, and the alert providers a failure is reported through.

## Common problems

- **`lore.py search` returns nothing you expect**: check `.search.db` exists
  (`doctor`'s `search-db` check); if it does not, or was built before pages changed, run
  `lore.py index`.
- **A hyphenated or punctuated search query errors instead of matching**: this was fixed by
  quoting every query token before it reaches SQLite FTS5 (see `architecture.md`); if you see
  it again, it is a regression, not expected behaviour.
- **`lore.py fact` or the MCP `lore_fact` tool errors "no extract for slug"**: the page
  exists but `extracts/<slug>.json` does not; extracts are written separately from pages (see
  `architecture.md`), not generated automatically from a page's body.
- **The gardener never runs, but `doctor`'s `scheduler` check shows `PASS`**: the scheduler
  job is loaded, but check `$LORE_DIR/.paused` and `LORE_ROLE_FILE` (if set, the scripts idle
  when its content is `secondary`); both are silent by design, so `doctor`'s `paused` line
  and this file are the way to notice them.
- **An alert never arrives**: confirm `LORE_ALERT_PROVIDER` in `.lore.env` is not left at
  its `log`-only default if you expected `desktop`, `telegram`, or `command`; every alert
  still lands in `$LORE_DIR/.alerts.log` regardless, so that file is the first place to check
  when a provider seems silent.
- **A source shows changed on every refresh**: see `maintenance-loop.md`'s paragraph on
  `refresh`'s change hash and its `.refresh-cache/` payload cache. An old `.hashes.json` entry
  (written before this fix) upgrades to the new scheme automatically the next time its content
  still matches, so this should be a one-time, self-healing situation; if a source keeps
  showing `[CHANGED]` after that, the visible text is genuinely changing, not just the
  surrounding markup.
