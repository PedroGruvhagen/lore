#!/bin/bash
# Lore external watchdog. Fired every 5 minutes by a launchd/cron/systemd job
# with default label com.lore.watchdog ($LORE_LAUNCHD_LABEL_PREFIX + "watchdog").
# Independent process: the primary cannot watch itself, only an outsider can revive a wedged primary.
#
# Check 1 (refresh): if .refresh.log has no parseable tick newer than STALE_THRESHOLD_SEC, it:
#   1. Appends a STALE alert to .watchdog.log
#   2. Posts a desktop notification (notify_desktop: AppleScript on macOS,
#      notify-send on Linux, or a no-op)
#   3. Alerts via alert() (lore-common.sh; whichever provider LORE_ALERT_PROVIDER selects)
#   4. SIGKILLs any wedged lore-refresh children
#   5. Kicks a fresh primary invocation (scheduler_kick_refresh)
#
# Check 2 (gardener): if the gardener job is loaded and .gardener/gardener.log
# has no parseable tick newer than GARDENER_STALE_SEC (26h), alert once per day.
# Deliberately NO kickstart: a missed night should alert, not fire a heavy job
# at a random daytime hour.
#
# Check 3 (H7, watched file size): any path listed in LORE_WATCH_FILES
# (colon-separated) larger than LORE_WATCH_FILE_MAX_BYTES gets one alert per
# file per day. Guards against an unbounded log/run-log growing silently
# between gardener runs.

set -u
set -o pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./lore-common.sh
source "$SCRIPT_DIR/lore-common.sh"

WATCHDOG_LOG="$LORE_DIR/.watchdog.log"
REFRESH_LOG="$LORE_DIR/.refresh.log"
GARDENER_LOG="$LORE_DIR/.gardener/gardener.log"
# Refresh fires every 6h; two missed intervals + 1h grace = 13h.
STALE_THRESHOLD_SEC="${LORE_WATCHDOG_STALE_SEC:-46800}"
# Gardener fires nightly; one missed night + 2h grace = 26h.
GARDENER_STALE_SEC="${LORE_GARDENER_STALE_SEC:-93600}"

mkdir -p "$LORE_DIR"

# Rotate own log: 5-min ticks accumulate fast; keep it bounded.
if [ -f "$WATCHDOG_LOG" ] && [ "$(wc -l < "$WATCHDOG_LOG" | tr -d ' ')" -gt 4000 ]; then
    tail -n 1000 "$WATCHDOG_LOG" > "$WATCHDOG_LOG.tmp" 2>/dev/null && mv "$WATCHDOG_LOG.tmp" "$WATCHDOG_LOG" || true
fi

# Watchdog tick line: proves this script entered even if subsequent steps fail.
printf '[%s] tick pid=%d\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$$" >>"$WATCHDOG_LOG" 2>/dev/null || true

# Optional multi-machine role guard: when $LORE_ROLE_FILE is set, this lore
# chain only runs where that file's content is not "secondary". Unset by
# default (single-machine installs run everywhere).
if [ -n "$LORE_ROLE_FILE" ] && [ "$(cat "$LORE_ROLE_FILE" 2>/dev/null)" = "secondary" ]; then
  printf '[%s] role=secondary on %s, lore chain idle here (LORE_ROLE_FILE)\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$(hostname -s)" >>"$WATCHDOG_LOG" 2>/dev/null || true
  exit 0
fi

# Generic pause sentinel: touch $LORE_DIR/.paused to idle all three scripts
# (this one, lore-gardener.sh, lore-refresh.sh) without editing any config.
# Skipping the stale-log check here too matters: without this, an
# intentionally paused chain would still page as "gardener log stale" once
# the usual run interval passed. `lore.py doctor` reports whether it is
# present.
if [ -e "$LORE_DIR/.paused" ]; then
  printf '[%s] paused ($LORE_DIR/.paused present), lore chain idle here\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >>"$WATCHDOG_LOG" 2>/dev/null || true
  exit 0
fi

wlog() {
    printf '[%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1" >>"$WATCHDOG_LOG" 2>/dev/null || true
}

# Parse the newest [YYYY-MM-DDTHH:MM:SSZ] timestamp from a log by scanning
# BACKWARD from the end. The literal last line is often python output (tracebacks,
# refresh summaries), so anchoring on it alone made the old watchdog blind.
last_log_ts() {
    python3 - "$1" <<'PY' 2>/dev/null || true
import os, re, sys
p = sys.argv[1]
if not os.path.exists(p):
    sys.exit(1)
with open(p, "r", encoding="utf-8", errors="replace") as f:
    lines = [ln.strip() for ln in f.readlines() if ln.strip()]
for ln in reversed(lines):
    m = re.match(r"\[(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)\]", ln)
    if m:
        print(m.group(1))
        sys.exit(0)
sys.exit(1)
PY
}

# Seconds elapsed since a [YYYY-MM-DDTHH:MM:SSZ] timestamp.
age_seconds() {
    python3 -c "
import datetime, sys
ts = datetime.datetime.strptime(sys.argv[1], '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=datetime.timezone.utc)
now = datetime.datetime.now(datetime.timezone.utc)
print(int((now - ts).total_seconds()))
" "$1" 2>/dev/null || true
}

# ---------------------------------------------------------------------------
# Check 1: refresh primary freshness (+ recovery).
# ---------------------------------------------------------------------------
check_refresh() {
    if [ ! -f "$REFRESH_LOG" ]; then
        wlog "watchdog: refresh log missing at $REFRESH_LOG"
        return 0
    fi

    local last_ts age_sec age_min
    last_ts="$(last_log_ts "$REFRESH_LOG")"
    if [ -z "${last_ts:-}" ]; then
        wlog "watchdog: could not parse any refresh timestamp"
        return 0
    fi

    age_sec="$(age_seconds "$last_ts")"
    age_sec="${age_sec:-0}"

    if [ "$age_sec" -le "$STALE_THRESHOLD_SEC" ]; then
        # Healthy. No log entry needed; avoid flooding watchdog log with noise.
        return 0
    fi

    # STALE. Take recovery action.
    age_min=$((age_sec / 60))
    wlog "STALE refresh_log: last_ts=$last_ts age=${age_sec}s (${age_min}m)"

    # 1. Desktop notification (best effort, no-op if no mechanism is available).
    notify_desktop "Lore refresh STALE" "Last refresh ${age_min}m ago. Kicking primary." "Basso"

    # 2. Collect recent failed-refresh records.
    local failed_summary=""
    if [ -d "$LORE_DIR/failed" ]; then
        failed_summary=$(find "$LORE_DIR/failed" -name '*.err' -mtime -1 -print 2>/dev/null | head -20)
    fi

    # 3. Alert via the configured channel (LORE_ALERT_PROVIDER).
    local body
    body=$(printf 'Lore refresh appears stale.\nLast refresh log entry: %s (%dm ago).\nLog: %s\nRecent failed records:\n%s' \
        "$last_ts" "$age_min" "$REFRESH_LOG" "${failed_summary:-(none)}")
    alert "Lore refresh stale: last entry ${age_min}m ago" "$body"

    # 4. SIGKILL any wedged lore-refresh children.
    local hung
    hung="$(ps -axo pid,command 2>/dev/null \
        | awk '/lore-refresh\.sh|lore\.py/ && !/awk/ && !/lore-watchdog/ {print $1}' \
        | tr '\n' ' ')"
    if [ -n "${hung:-}" ]; then
        wlog "killing hung pids: $hung"
        for pid in $hung; do
            kill -KILL "$pid" 2>/dev/null || true
        done
    fi

    # 5. Kick the primary out of schedule (portable: launchd/systemd/direct run).
    if scheduler_kick_refresh >>"$WATCHDOG_LOG" 2>&1; then
        wlog "scheduler_kick_refresh succeeded"
    else
        wlog "scheduler_kick_refresh FAILED (rc=$?)"
    fi

    return 0
}

# ---------------------------------------------------------------------------
# Check 2: gardener freshness (alert only, once per day, NO kickstart).
# ---------------------------------------------------------------------------
check_gardener() {
    # Only meaningful if the gardener job is actually installed and has run.
    # scheduler_has_job returns 0 (found), 1 (queried, not found) or 2
    # (this platform has no scheduler this helper can query, e.g. cron or a
    # manual install). rc=2 must NOT be treated the same as rc=1: skipping
    # the check on "unknown" would silently disable it on every platform but
    # macOS/systemd, which is exactly the install this check exists for.
    scheduler_has_job gardener
    local has_job_rc=$?
    if [ "$has_job_rc" -eq 1 ]; then
        return 0
    fi
    if [ ! -f "$GARDENER_LOG" ]; then
        return 0
    fi

    local last_ts age_sec age_hr marker
    last_ts="$(last_log_ts "$GARDENER_LOG")"
    if [ -z "${last_ts:-}" ]; then
        wlog "watchdog: could not parse any gardener timestamp"
        return 0
    fi

    age_sec="$(age_seconds "$last_ts")"
    age_sec="${age_sec:-0}"

    if [ "$age_sec" -le "$GARDENER_STALE_SEC" ]; then
        return 0
    fi

    # Stale. Alert at most once per day (marker file dedupe).
    marker="$LORE_DIR/.gardener/.alerted-$(date -u +%Y-%m-%d)"
    if [ -f "$marker" ]; then
        return 0
    fi
    touch "$marker" 2>/dev/null || true

    age_hr=$((age_sec / 3600))
    wlog "STALE gardener_log: last_ts=$last_ts age=${age_sec}s (${age_hr}h); alerting (no kickstart)"

    alert "Lore gardener stale: last tick ${age_hr}h ago" \
        "The nightly lore gardener has not ticked since $last_ts (${age_hr}h ago). Not kickstarting (a missed night should alert, not fire a heavy job at a random hour). Check $GARDENER_LOG."

    return 0
}

# ---------------------------------------------------------------------------
# Check 3 (H7): watched file size. LORE_WATCH_FILES is a colon-separated list
# of paths (unset by default: nothing is watched unless configured); each one
# larger than LORE_WATCH_FILE_MAX_BYTES gets one alert per file per day
# (marker file dedupe, same pattern as check_gardener above). A path that
# doesn't exist yet is silently skipped, not an error: gardener.log, for
# instance, doesn't exist until the first nightly run.
# ---------------------------------------------------------------------------
check_watch_files() {
    [ -n "$LORE_WATCH_FILES" ] || return 0

    local old_ifs="$IFS" path size marker safe_name
    IFS=':'
    for path in $LORE_WATCH_FILES; do
        IFS="$old_ifs"
        [ -n "$path" ] || continue
        [ -f "$path" ] || continue

        size="$(wc -c <"$path" 2>/dev/null | tr -d ' ')"
        [ -n "$size" ] || continue
        if [ "$size" -le "$LORE_WATCH_FILE_MAX_BYTES" ]; then
            continue
        fi

        # One alert per watched file per day: derive a filesystem-safe marker
        # name from the full path so two different watched files never share
        # a marker.
        safe_name="$(printf '%s' "$path" | tr -c 'A-Za-z0-9' '_')"
        marker="$LORE_DIR/.watchdog-alerted-${safe_name}-$(date -u +%Y-%m-%d)"
        if [ -f "$marker" ]; then
            continue
        fi
        touch "$marker" 2>/dev/null || true

        wlog "WATCH file too large: $path is ${size} bytes (max ${LORE_WATCH_FILE_MAX_BYTES})"
        alert "Lore watched file too large: $(basename "$path")" \
            "$path is ${size} bytes, over the ${LORE_WATCH_FILE_MAX_BYTES}-byte limit (LORE_WATCH_FILE_MAX_BYTES). Consider rotating or truncating it."
        IFS=':'
    done
    IFS="$old_ifs"
    return 0
}

check_refresh
check_gardener
check_watch_files

exit 0
