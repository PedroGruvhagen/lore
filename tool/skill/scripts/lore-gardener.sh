#!/bin/bash
# Lore night gardener primary. Fired nightly at 03:30 by a launchd/cron/systemd
# job with default label com.lore.gardener ($LORE_LAUNCHD_LABEL_PREFIX + "gardener").
# Runs a refresh, checks what is actionable, then launches a headless Claude
# session with the mandate in references/gardener-prompt.md to reconcile diffs,
# file inbox notes, fix lint errors and commit the lore repo.
#
# Four-layer background-job discipline:
#   Layer 1: tick line written before any python/network call
#   Layer 2: self-watchdog hard wall-clock kill (LORE_GARDENER_DEADLINE_SEC, default 10800s)
#   Layer 3: external watchdog (lore-watchdog.sh) alerts if this log goes stale >26h
#   Layer 4: explicit timeouts on every step (refresh 1800s, pending 120s,
#            claude LORE_GARDENER_CLAUDE_TIMEOUT_SEC, default 9600s)
#
# A single-holder mutation lock ($LORE_DIR/.lore.lock, atomic mkdir) serializes
# this run against lore-refresh.sh so the two never mutate the queue, hashes, or
# page frontmatter at once; a lock older than 14400s is presumed abandoned and stolen.
#
# LORE_GARDENER_DRYRUN=1: run refresh + pending, log what WOULD happen, then
# exit before launching claude. Used for testing the wrapper.
#
# Failure resilience (H1, six recorded production failure nights, S3): a
# network preflight and an auth preflight (I13) run before the agent is ever
# launched; after a run, the last `type:result` line is classified into
# network/quota/auth/sleep/killed/unknown (classify_result_line,
# lore-common.sh) and the gardener retries once for network or sleep, or
# waits for a stated quota reset time if that is still reachable inside the
# deadline below. A final failure alerts with `cause=<class> rc=<n>
# result=<vendor message>` (C13) instead of a raw log tail.

set -u
set -o pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./lore-common.sh
source "$SCRIPT_DIR/lore-common.sh"

LORE_PY="$SKILL_DIR/scripts/lore.py"
PROMPT_FILE="$SKILL_DIR/references/gardener-prompt.md"
GDIR="$LORE_DIR/.gardener"
LOG="$GDIR/gardener.log"
SCRIPT_START_TS=$(date +%s)
DEADLINE_SEC="${LORE_GARDENER_DEADLINE_SEC:-10800}"
CLAUDE_TIMEOUT_SEC="${LORE_GARDENER_CLAUDE_TIMEOUT_SEC:-9600}"
DRYRUN="${LORE_GARDENER_DRYRUN:-0}"
LOCK_DIR="$LORE_DIR/.lore.lock"
LOCK_STALE_SEC=14400
LOCK_HELD=0

# Network preflight target and retry schedule (H1a). Overridable so a
# hermetic test (or a CI runner with no route to the real API host) can
# point this at a target it controls and skip the 5-minute waits; the
# shipped default matches the recorded incident fix exactly: 6 attempts, 5
# minutes apart, https://api.anthropic.com/.
NETWORK_CHECK_URL="${LORE_GARDENER_NETWORK_CHECK_URL:-https://api.anthropic.com/}"
NETWORK_CHECK_RETRIES="${LORE_GARDENER_NETWORK_CHECK_RETRIES:-6}"
NETWORK_CHECK_INTERVAL_SEC="${LORE_GARDENER_NETWORK_CHECK_INTERVAL_SEC:-300}"

# launchd (and similar schedulers) hand a minimal PATH; set a sane one explicitly.
PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export PATH

# Resolved claude binary, used only to (a) make sure its directory is on
# PATH before $LORE_GARDENER_CMD is invoked and (b) gate step 4 with a clear
# error if no binary can be found at all. A minimal scheduler PATH means
# `command -v` alone can miss a per-user install, so ~/.local/bin is checked
# explicitly too (the stable-symlink location the official installer
# commonly uses). Falls back to the first word of $LORE_GARDENER_CMD (default
# "claude -p") so a differently named binary or wrapper script is still
# found by the same two checks.
CLAUDE_BIN=""
CLAUDE_CMD_WORD="${LORE_GARDENER_CMD%% *}"
for c in \
    "$(command -v claude 2>/dev/null || true)" \
    "$HOME/.local/bin/claude" \
    "$(command -v "$CLAUDE_CMD_WORD" 2>/dev/null || true)" \
    "$HOME/.local/bin/$CLAUDE_CMD_WORD"; do
    # -x alone is true for an executable *directory* too (virtually all
    # directories carry the execute bit), so without -f a stray directory at
    # one of these candidate paths would be silently accepted as CLAUDE_BIN;
    # PATH would then be set to a real directory but every later
    # $LORE_GARDENER_CMD invocation would fail with a confusing "not found"
    # or "is a directory" error instead of this loop's own clear gate below
    # (W1).
    if [ -n "$c" ] && [ -f "$c" ] && [ -x "$c" ]; then
        CLAUDE_BIN="$c"
        break
    fi
done
if [ -n "$CLAUDE_BIN" ]; then
    PATH="$(dirname "$CLAUDE_BIN"):$PATH"
fi

mkdir -p "$GDIR" 2>/dev/null || true

# Keep the gardener log append-only and small: rotate at start if oversized.
if [ -f "$LOG" ] && [ "$(wc -l <"$LOG" | tr -d ' ')" -gt 2000 ]; then
    tail -n 500 "$LOG" >"$LOG.rot" 2>/dev/null && mv "$LOG.rot" "$LOG"
fi

# Layer 1: tick line. Plain shell builtins, before any python or network call.
printf '[%s] tick pid=%d dryrun=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$$" "$DRYRUN" >>"$LOG" 2>/dev/null || true

# Optional multi-machine role guard: when $LORE_ROLE_FILE is set, this lore
# chain only runs where that file's content is not "secondary". Unset by
# default (single-machine installs run everywhere).
if [ -n "$LORE_ROLE_FILE" ] && [ "$(cat "$LORE_ROLE_FILE" 2>/dev/null)" = "secondary" ]; then
  printf '[%s] role=secondary on %s, lore chain idle here (LORE_ROLE_FILE)\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$(hostname -s)" >>"$LOG" 2>/dev/null || true
  exit 0
fi

# Generic pause sentinel: touch $LORE_DIR/.paused to idle all three scripts
# (this one, lore-refresh.sh, lore-watchdog.sh) without editing any config,
# e.g. during a manual edit session or a migration. `lore.py doctor` reports
# whether it is present. Checked before the mutation lock so a paused chain
# never even contends for it.
if [ -e "$LORE_DIR/.paused" ]; then
  printf '[%s] paused ($LORE_DIR/.paused present), lore chain idle here\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >>"$LOG" 2>/dev/null || true
  exit 0
fi

# Layer 2: self-watchdog hard kill of the process group after DEADLINE_SEC.
( sleep "$DEADLINE_SEC" && kill -TERM -$$ 2>/dev/null ) &
WATCHDOG_PID=$!

# Hold a sleep-inhibiting assertion for exactly this run's lifetime
# (inhibit_sleep, lore-common.sh: the macOS sleep-assertion tool when
# present, systemd-inhibit on Linux, a silent no-op elsewhere). Prevents the
# machine sleeping mid-run
# during an unattended window from killing the API connection with no
# graceful recovery (X4: kept, not removed, after the 08-13 sleep-killed
# incident). Scoped to this script only, does not touch system-wide sleep
# settings.
inhibit_sleep

trap 'kill "$WATCHDOG_PID" 2>/dev/null || true; [ -n "${LORE_INHIBIT_PID:-}" ] && kill "$LORE_INHIBIT_PID" 2>/dev/null || true; { [ "${LOCK_HELD:-0}" = "1" ] && rm -rf "$LOCK_DIR" 2>/dev/null; } || true' EXIT

glog() {
    printf '[%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1" >>"$LOG" 2>/dev/null || true
}

# If the Layer 2 deadline (or anything else) TERMs us, log and alert before
# dying; otherwise a killed night would stay silent until the external
# watchdog's 26h staleness check.
on_term() {
    glog "TERM received (deadline ${DEADLINE_SEC}s reached or external kill); aborting"
    alert "Lore gardener failed" \
        "Gardener was terminated before finishing (deadline ${DEADLINE_SEC}s reached or killed externally). Log: $LOG"
    glog "done rc=143"
    exit 143
}
trap on_term TERM

# Layer 4 helper: run a command with its own wall-clock timeout.
run_with_timeout() {
    local secs="$1"
    shift
    "$@" &
    local pid=$!
    ( sleep "$secs" && kill -TERM "$pid" 2>/dev/null ) &
    local killer=$!
    wait "$pid"
    local rc=$?
    kill "$killer" 2>/dev/null || true
    return $rc
}

fail_and_exit() {
    # fail_and_exit "<short reason for log>" "<alert body>"
    glog "ERROR $1"
    alert "Lore gardener failed" "$2"
    glog "done rc=1"
    exit 1
}

# last_result_json <run-log-path>: prints the "result" field of the last
# `{"type": "result", ...}` JSON line in a stream-json run log, or an empty
# string if none is found or the file cannot be parsed at all.
last_result_json() {
    python3 - "$1" <<'PY' 2>/dev/null || true
import json, sys

path = sys.argv[1]
result = ""
try:
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if isinstance(obj, dict) and obj.get("type") == "result":
                result = str(obj.get("result", ""))
except Exception:
    pass
print(result)
PY
}

# seconds_until_reset <text>: parses a "resets HH(am|pm)[ (TZ)]" quota
# message and prints the number of seconds from now until that clock time
# next occurs (today if still ahead, else tomorrow), or nothing if no such
# pattern is found. TZ, when present, is an IANA zone name resolved via the
# stdlib zoneinfo (Python 3.9+); on an older Python (the floor elsewhere is
# 3.8, S7) or an unrecognized zone name this falls back to naive local time
# rather than failing the caller.
seconds_until_reset() {
    python3 - "$1" <<'PY' 2>/dev/null || true
import re, sys
from datetime import datetime, timedelta

text = sys.argv[1] if len(sys.argv) > 1 else ""
m = re.search(r'resets\s+(\d{1,2})\s*(am|pm)\b(?:\s*\(([^)]+)\))?', text, re.IGNORECASE)
if not m:
    sys.exit(0)

hour = int(m.group(1)) % 12
if m.group(2).lower() == "pm":
    hour += 12

tz = None
tzname = m.group(3)
if tzname:
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(tzname)
    except Exception:
        tz = None

now = datetime.now(tz)
target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
if target <= now:
    target += timedelta(days=1)
print(int((target - now).total_seconds()))
PY
}

# Source ~/.env so refresh connectors have their API keys (schedulers like
# launchd/cron/systemd run without shell rc files). ANTHROPIC_API_KEY is
# force-blanked again on the claude invocation below unless LORE_KEEP_API_KEY=1.
if [ -f "$HOME/.env" ]; then
    set -a
    # shellcheck source=/dev/null
    source "$HOME/.env" 2>/dev/null || true
    set +a
fi

# Mutation lock: refresh and gardener both mutate .hashes.json, queue/, failed/,
# and page frontmatter, so only one may run at a time. Atomic mkdir is the lock
# (portable; no dependency on flock). A lock older than LOCK_STALE_SEC is
# presumed left by a crashed run and is stolen. Released in the EXIT trap.
if mkdir "$LOCK_DIR" 2>/dev/null; then
    LOCK_HELD=1
    glog "acquired lore lock ($LOCK_DIR)"
else
    lock_age=-1
    lock_mtime="$(mtime_of "$LOCK_DIR")"
    if [ -n "$lock_mtime" ]; then
        lock_age=$(( $(date +%s) - lock_mtime ))
    fi
    if [ "$lock_age" -ge 0 ] && [ "$lock_age" -lt "$LOCK_STALE_SEC" ]; then
        glog "another lore job holds the lock; skipping this run (lock age ${lock_age}s)"
        glog "done rc=0"
        exit 0
    fi
    glog "stale lore lock (age ${lock_age}s, threshold ${LOCK_STALE_SEC}s); stealing it"
    rm -rf "$LOCK_DIR" 2>/dev/null || true
    if mkdir "$LOCK_DIR" 2>/dev/null; then
        LOCK_HELD=1
        glog "acquired lore lock after clearing stale lock"
    else
        glog "could not acquire lore lock even after clearing stale lock; skipping this run"
        glog "done rc=0"
        exit 0
    fi
fi

# Step 0: archive stale run logs into a tarball, then let lore.py prune them.
# Never delete without a backup first (S13); run-*.jsonl older than 30 days
# is tarred into .gardener/archive/ before `prune --run-logs-older-than 30`
# is allowed to remove them.
ARCHIVE_DIR="$GDIR/archive"
mkdir -p "$ARCHIVE_DIR" 2>/dev/null || true
OLD_LOGS_LIST="$(mktemp 2>/dev/null || echo "$GDIR/.old-logs.$$")"
find "$GDIR" -maxdepth 1 -name 'run-*.jsonl' -mtime +30 -print 2>/dev/null \
    | sed "s#^$GDIR/##" >"$OLD_LOGS_LIST"
if [ -s "$OLD_LOGS_LIST" ]; then
    ARCHIVE_TAR="$ARCHIVE_DIR/run-logs-$(date -u +%Y%m%dT%H%M%SZ).tar.gz"
    if tar -czf "$ARCHIVE_TAR" -C "$GDIR" -T "$OLD_LOGS_LIST" 2>>"$LOG"; then
        glog "archived $(wc -l <"$OLD_LOGS_LIST" | tr -d ' ') old run log(s) to $ARCHIVE_TAR"
        run_with_timeout 120 python3 "$LORE_PY" prune --run-logs-older-than 30 --lore-dir "$LORE_DIR" >>"$LOG" 2>&1
        glog "prune --run-logs-older-than 30 rc=$?"
    else
        glog "WARNING: failed to archive old run logs into $ARCHIVE_TAR; skipping prune this run"
    fi
fi
rm -f "$OLD_LOGS_LIST"

# Step 1: refresh sources (30-min cap; the Layer 2 deadline is the hard wall).
glog "step 1: lore.py refresh"
run_with_timeout 1800 python3 "$LORE_PY" refresh --lore-dir "$LORE_DIR" >>"$LOG" 2>&1
RRC=$?
glog "refresh rc=$RRC"

# Step 2: collect the pending work list.
PENDING_FILE="$GDIR/pending-latest.json"
run_with_timeout 120 python3 "$LORE_PY" pending --json --lore-dir "$LORE_DIR" >"$PENDING_FILE" 2>>"$LOG"
PRC=$?
if [ "$PRC" -ne 0 ] || [ ! -s "$PENDING_FILE" ]; then
    fail_and_exit "pending --json failed rc=$PRC" \
        "lore.py pending --json failed (rc=$PRC); gardener did not run. Log: $LOG"
fi

COUNTS="$(python3 - "$PENDING_FILE" <<'PY'
import json, sys
try:
    d = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception:
    print("parse-error")
    sys.exit(0)
diffs = len(d.get("diffs", []))
failed = int(d.get("failed_unseen", {}).get("count", 0) or 0)
quar = len(d.get("quarantined", []))
inbox = int(d.get("inbox", {}).get("count", 0) or 0)
stale = len(d.get("stale", []))
print(f"{diffs} {failed} {quar} {inbox} {stale}")
PY
)"
if [ "$COUNTS" = "parse-error" ] || [ -z "$COUNTS" ]; then
    fail_and_exit "pending JSON unparseable" \
        "lore.py pending --json produced unparseable output; gardener did not run. See $PENDING_FILE"
fi
read -r N_DIFFS N_FAILED N_QUAR N_INBOX N_STALE <<<"$COUNTS"
ACTIONABLE=$((N_DIFFS + N_FAILED + N_QUAR + N_INBOX))
glog "pending: diffs=$N_DIFFS failed_unseen=$N_FAILED quarantined=$N_QUAR inbox=$N_INBOX stale=$N_STALE actionable=$ACTIONABLE"

# Dry-run gate: log the decision, never launch claude, never touch the
# network/auth preflights below either.
if [ "$DRYRUN" = "1" ]; then
    if [ "$ACTIONABLE" -eq 0 ]; then
        glog "DRYRUN: nothing to do; would exit 0 without launching claude"
    else
        glog "DRYRUN: would launch \$LORE_GARDENER_CMD ($LORE_GARDENER_CMD) with gardener-prompt.md substituted (LORE_DIR=$LORE_DIR)"
    fi
    glog "done rc=0 (dryrun)"
    exit 0
fi

# Nothing-to-do gate: diffs, unseen failures, quarantined sources and inbox
# notes are actionable. Stale pages alone are not (step 1 just refreshed; a
# stale page whose sources changed produces a queued diff, which counts above).
if [ "$ACTIONABLE" -eq 0 ]; then
    glog "nothing to do"
    glog "done rc=0"
    exit 0
fi

# Step 3: build the prompt from the mandate file.
if [ ! -f "$PROMPT_FILE" ]; then
    fail_and_exit "prompt file missing: $PROMPT_FILE" \
        "gardener-prompt.md missing at $PROMPT_FILE; gardener did not run."
fi
if [ -z "$CLAUDE_BIN" ] || [ ! -x "$CLAUDE_BIN" ]; then
    fail_and_exit "claude binary not found" \
        "No executable claude binary found (checked PATH and ~/.local/bin for both 'claude' and the first word of \$LORE_GARDENER_CMD=$LORE_GARDENER_CMD); gardener did not run."
fi
PROMPT="$(cat "$PROMPT_FILE")"
PROMPT="${PROMPT//\$LORE_DIR/$LORE_DIR}"
PROMPT="${PROMPT//\$LORE_PY/$LORE_PY}"
PROMPT="${PROMPT//\$LORE_ALERT/$SKILL_DIR/scripts/lore-alert.sh}"

# Step 3.5: preflights (H1a, I13). Skipping the agent launch entirely on
# failure means a known-bad night never burns its CLAUDE_TIMEOUT_SEC window
# for nothing.
NET_OK=0
attempt=1
while [ "$attempt" -le "$NETWORK_CHECK_RETRIES" ]; do
    if curl -sS -m 10 -o /dev/null "$NETWORK_CHECK_URL" 2>>"$LOG"; then
        NET_OK=1
        break
    fi
    glog "network preflight attempt $attempt/$NETWORK_CHECK_RETRIES failed ($NETWORK_CHECK_URL)"
    if [ "$attempt" -lt "$NETWORK_CHECK_RETRIES" ]; then
        sleep "$NETWORK_CHECK_INTERVAL_SEC"
    fi
    attempt=$((attempt + 1))
done
if [ "$NET_OK" -ne 1 ]; then
    MSG="cause=network rc=- result=network preflight failed after $NETWORK_CHECK_RETRIES attempts ($NETWORK_CHECK_URL unreachable)"
    fail_and_exit "$MSG" "$MSG"
fi

read -ra AUTH_CMD_ARR <<<"$LORE_GARDENER_AUTH_CHECK"
# LORE_GARDENER_AUTH_CHECK defaults to "claude auth status" (never empty),
# but an admin overriding it to an empty string would otherwise make
# "${AUTH_CMD_ARR[@]}" trip `set -u` on bash 3.2's zero-element-array
# handling. "${#AUTH_CMD_ARR[@]}" (a count, not an element expansion) is
# safe on bash 3.2 even for a zero-element array, so check it explicitly and
# fail the same way an actual failed auth check would, rather than letting
# an accidentally-empty command line either crash the script or silently
# execute as a no-op that reports success.
if [ "${#AUTH_CMD_ARR[@]}" -eq 0 ]; then
    MSG="cause=auth rc=- result=LORE_GARDENER_AUTH_CHECK is set to an empty string"
    fail_and_exit "$MSG" "$MSG"
fi
AUTH_OUT="$("${AUTH_CMD_ARR[@]}" 2>&1)"
AUTH_RC=$?
if [ "$AUTH_RC" -ne 0 ]; then
    MSG="cause=auth rc=$AUTH_RC result=$(printf '%.300s' "$AUTH_OUT" | tr '\n' ' ')"
    fail_and_exit "$MSG" "$MSG"
fi
glog "preflights OK (network: $NETWORK_CHECK_URL, auth: $LORE_GARDENER_AUTH_CHECK)"

# Step 4: launch the headless gardener session (cwd = lore dir) under its own
# CLAUDE_TIMEOUT_SEC wall-clock timeout; the Layer 2 deadline is the outer
# wall. ANTHROPIC_API_KEY is blanked by default to force OAuth login instead
# of API-key billing; set LORE_KEEP_API_KEY=1 to keep it (e.g. API-key-only
# installs). CLAUDE_CONFIG_DIR pins which account/config the headless session
# uses ($LORE_AGENT_CONFIG_DIR, default ~/.claude). Inline env prefixes do not
# survive being passed to run_with_timeout as arguments, so `env` sets them.
TODAY="$(date +%Y-%m-%d)"
RUN_LOG="$GDIR/run-$TODAY.jsonl"
REPORT="$GDIR/report-$TODAY.md"
cd "$LORE_DIR" || fail_and_exit "cd $LORE_DIR failed" "Could not cd into $LORE_DIR; gardener did not run."
GARDENER_ENV=(CLAUDE_CONFIG_DIR="$LORE_AGENT_CONFIG_DIR")
if [ "$LORE_KEEP_API_KEY" != "1" ]; then
    GARDENER_ENV+=(ANTHROPIC_API_KEY=)
fi
# $LORE_GARDENER_CMD (default "claude -p") and $LORE_GARDENER_MODEL_ARGS are
# split into arrays with `read -ra` rather than left as raw unquoted
# expansions, so the words become argv entries via IFS splitting only (no
# pathname/glob expansion of a stray "*" in either variable); "$PROMPT" is
# then the next positional argument, exactly as "claude -p <prompt>" expects.
# A single argument that itself needs an embedded space still is not
# representable this way, the same limit any IFS-only split has.
#
# $LORE_GARDENER_MODEL_ARGS is empty by default (lore-common.sh), which
# means MODEL_ARGS_ARR is a zero-element array on every default install.
# macOS's shipped /bin/bash is 3.2 (pre-4.4), where "${arr[@]}" on a
# zero-element array trips "unbound variable" under `set -u` even though the
# array itself is declared; a launchd/cron/systemd job always runs this
# script through that literal shebang, never through a newer PATH-resolved
# bash. "${MODEL_ARGS_ARR[@]+"${MODEL_ARGS_ARR[@]}"}" expands to nothing at
# all when the array is empty instead of dereferencing it, which is safe on
# bash 3.2; CMD_ARR is not guarded the same way because $LORE_GARDENER_CMD
# always has a non-empty default and an admin overriding it to empty is a
# misconfiguration, not a state this script should paper over.
read -ra CMD_ARR <<<"$LORE_GARDENER_CMD"
read -ra MODEL_ARGS_ARR <<<"$LORE_GARDENER_MODEL_ARGS"

MAX_ATTEMPTS=2
ATTEMPT=1
CRC=1
CAUSE="unknown"
RESULT_MSG=""
while [ "$ATTEMPT" -le "$MAX_ATTEMPTS" ]; do
    glog "step 4: launching \$LORE_GARDENER_CMD ($LORE_GARDENER_CMD), attempt $ATTEMPT/$MAX_ATTEMPTS, run log $RUN_LOG"
    # $RUN_LOG always holds the CURRENT attempt so downstream readers
    # (last_result_json, doctor, a human tailing it live) keep using one
    # fixed, predictable path. A previous attempt's log is preserved, not
    # overwritten: renamed to run-$TODAY.attemptN.jsonl before truncating
    # $RUN_LOG fresh for this attempt, so a retried run still leaves attempt
    # 1's failure on disk for post-mortem instead of losing it to the retry.
    if [ "$ATTEMPT" -gt 1 ] && [ -f "$RUN_LOG" ]; then
        mv "$RUN_LOG" "$GDIR/run-$TODAY.attempt$((ATTEMPT - 1)).jsonl" 2>/dev/null || true
    fi
    : >"$RUN_LOG"
    run_with_timeout "$CLAUDE_TIMEOUT_SEC" env "${GARDENER_ENV[@]}" "${CMD_ARR[@]}" \
        --dangerously-skip-permissions --permission-mode bypassPermissions \
        ${MODEL_ARGS_ARR[@]+"${MODEL_ARGS_ARR[@]}"} \
        "$PROMPT" --output-format stream-json \
        >>"$RUN_LOG" 2>&1
    CRC=$?
    glog "claude rc=$CRC (attempt $ATTEMPT/$MAX_ATTEMPTS)"

    # A run only counts if claude exited 0 AND wrote today's report.
    if [ "$CRC" -eq 0 ] && [ -f "$REPORT" ]; then
        glog "done rc=0 report=$REPORT"
        exit 0
    fi

    RESULT_MSG="$(last_result_json "$RUN_LOG")"
    if [ -z "$RESULT_MSG" ]; then
        RESULT_MSG="$(tail -c 4000 "$RUN_LOG" 2>/dev/null | tail -n 5 | cut -c1-1200)"
    fi
    CAUSE="$(classify_result_line "$RESULT_MSG" "$CRC")"
    glog "cause=$CAUSE rc=$CRC result=$(printf '%.300s' "$RESULT_MSG" | tr '\n' ' ')"

    if [ "$ATTEMPT" -ge "$MAX_ATTEMPTS" ]; then
        break
    fi

    case "$CAUSE" in
        network|sleep)
            glog "retrying once after cause=$CAUSE"
            ATTEMPT=$((ATTEMPT + 1))
            continue
            ;;
        quota)
            WAIT_SEC="$(seconds_until_reset "$RESULT_MSG")"
            NOW_TS=$(date +%s)
            ELAPSED=$((NOW_TS - SCRIPT_START_TS))
            REMAINING=$((DEADLINE_SEC - ELAPSED))
            if [ -n "$WAIT_SEC" ] && [ "$WAIT_SEC" -gt 0 ] && [ "$WAIT_SEC" -lt "$REMAINING" ]; then
                glog "retrying after cause=quota, sleeping ${WAIT_SEC}s until reset (${REMAINING}s left in deadline)"
                sleep "$WAIT_SEC"
                ATTEMPT=$((ATTEMPT + 1))
                continue
            fi
            glog "cause=quota reset time not reachable inside remaining deadline (${REMAINING}s left); not retrying"
            break
            ;;
        *)
            break
            ;;
    esac
done

# Step 5: exhausted every retry the failure class allows. Alert with the
# classified cause instead of a raw log tail (C13).
MSG="cause=$CAUSE rc=$CRC result=$(printf '%.300s' "$RESULT_MSG" | tr '\n' ' ')"
fail_and_exit "$MSG" "$MSG"
