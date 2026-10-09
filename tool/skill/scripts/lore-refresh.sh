#!/bin/bash
# Lore refresh primary. Fired every 6 hours by a launchd/cron/systemd job with
# default label com.lore.refresh ($LORE_LAUNCHD_LABEL_PREFIX + "refresh").
# Implements the four-layer background-job discipline:
#
# Layer 1: tick line written before any python call
# Layer 2: self-watchdog hard wall-clock kill
# Layer 3: external watchdog runs every 5 min (separate plist/job)
# Layer 4: explicit timeouts inside every connector

set -u
set -o pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./lore-common.sh
source "$SCRIPT_DIR/lore-common.sh"

LOG_FILE="$LORE_DIR/.refresh.log"
DEADLINE_SEC="${LORE_REFRESH_DEADLINE_SEC:-3600}"
LOCK_DIR="$LORE_DIR/.lore.lock"
LOCK_STALE_SEC=14400
LOCK_HELD=0

mkdir -p "$LORE_DIR"

# Rotate: keep the log bounded (the kept tail is far more than the watchdog scan needs).
if [ -f "$LOG_FILE" ] && [ "$(wc -l < "$LOG_FILE" | tr -d ' ')" -gt 4000 ]; then
    tail -n 1000 "$LOG_FILE" > "$LOG_FILE.tmp" 2>/dev/null && mv "$LOG_FILE.tmp" "$LOG_FILE" || true
fi

# Layer 1: tick line. Plain shell builtins, no helpers that could hang.
# This MUST be the first write before any python or network call.
printf '[%s] tick pid=%d\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$$" >>"$LOG_FILE" 2>/dev/null || true

# Optional multi-machine role guard: when $LORE_ROLE_FILE is set, this lore
# chain only runs where that file's content is not "secondary". Unset by
# default (single-machine installs run everywhere).
if [ -n "$LORE_ROLE_FILE" ] && [ "$(cat "$LORE_ROLE_FILE" 2>/dev/null)" = "secondary" ]; then
  printf '[%s] role=secondary on %s, lore chain idle here (LORE_ROLE_FILE)\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$(hostname -s)" >>"$LOG_FILE" 2>/dev/null || true
  exit 0
fi

# Generic pause sentinel: touch $LORE_DIR/.paused to idle all three scripts
# (this one, lore-gardener.sh, lore-watchdog.sh) without editing any config.
# `lore.py doctor` reports whether it is present. Checked before the mutation
# lock so a paused chain never even contends for it.
if [ -e "$LORE_DIR/.paused" ]; then
  printf '[%s] paused ($LORE_DIR/.paused present), lore chain idle here\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >>"$LOG_FILE" 2>/dev/null || true
  exit 0
fi

# Layer 2: self-watchdog hard kill of the process group after DEADLINE_SEC.
# Without this, a hung connector can suppress the scheduler's interval indefinitely.
( sleep "$DEADLINE_SEC" && kill -TERM -$$ 2>/dev/null ) &
WATCHDOG_PID=$!
trap 'kill "$WATCHDOG_PID" 2>/dev/null || true; { [ "${LOCK_HELD:-0}" = "1" ] && rm -rf "$LOCK_DIR" 2>/dev/null; } || true' EXIT

rlog() {
    printf '[%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1" >>"$LOG_FILE" 2>/dev/null || true
}

# Mutation lock: refresh and the nightly gardener both mutate .hashes.json,
# queue/, failed/, and page frontmatter, so only one may run at a time. Atomic
# mkdir is the lock (portable; no dependency on flock). A lock older than
# LOCK_STALE_SEC is presumed left by a crashed run and is stolen. Released in
# the EXIT trap.
if mkdir "$LOCK_DIR" 2>/dev/null; then
    LOCK_HELD=1
    rlog "acquired lore lock ($LOCK_DIR)"
else
    lock_age=-1
    lock_mtime="$(mtime_of "$LOCK_DIR")"
    if [ -n "$lock_mtime" ]; then
        lock_age=$(( $(date +%s) - lock_mtime ))
    fi
    if [ "$lock_age" -ge 0 ] && [ "$lock_age" -lt "$LOCK_STALE_SEC" ]; then
        rlog "skipping: lock held by another lore job (lock age ${lock_age}s)"
        exit 0
    fi
    rlog "stale lore lock (age ${lock_age}s, threshold ${LOCK_STALE_SEC}s); stealing it"
    rm -rf "$LOCK_DIR" 2>/dev/null || true
    if mkdir "$LOCK_DIR" 2>/dev/null; then
        LOCK_HELD=1
        rlog "acquired lore lock after clearing stale lock"
    else
        rlog "skipping: could not acquire lore lock after clearing stale lock"
        exit 0
    fi
fi

# Source ~/.env if it exists so MISTRAL_API_KEY and other env vars are available
# to connectors. Background schedulers run without shell rc files, so this is required.
if [ -f "$HOME/.env" ]; then
    set -a
    # shellcheck source=/dev/null
    source "$HOME/.env" 2>/dev/null || true
    set +a
fi

# Run the refresh. Connector network calls have explicit timeouts (Layer 4).
python3 "$SKILL_DIR/scripts/lore.py" refresh --lore-dir "$LORE_DIR" \
    >>"$LOG_FILE" 2>&1
RC=$?

printf '[%s] done rc=%d\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$RC" >>"$LOG_FILE"
exit $RC
