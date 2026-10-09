#!/bin/bash
# Shared configuration and portability helpers for every lore-*.sh script.
# Resolves SKILL_DIR and LORE_DIR, sources an optional .lore.env override
# file, fills in a default for every LORE_* variable the scripts below read,
# then defines the handful of functions that hide every platform-specific
# primitive (macOS vs Linux) the rest of the scripts would otherwise call
# directly. Every lore-*.sh script sources this file first, before doing
# anything else:
#
#   SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
#   source "$SCRIPT_DIR/lore-common.sh"
#
# LORE_DIR resolution mirrors lore.py's resolve_lore_dir(): an already-set
# $LORE_DIR wins (matches the CLI's env-var override), else the lore dir next
# to this skill's own install (<config-dir>/lore, inferred from the standard
# <config-dir>/skills/lore/scripts/ layout), else ~/.claude/lore.

# SKILL_DIR: this skill's own install root, inferred from this file's path
# (<config-dir>/skills/lore/scripts/lore-common.sh -> <config-dir>/skills/lore).
SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG_DIR="$(cd "$SKILL_DIR/../.." && pwd)"

if [ -z "${LORE_DIR:-}" ]; then
    if [ -d "$CONFIG_DIR/lore" ]; then
        LORE_DIR="$CONFIG_DIR/lore"
    else
        LORE_DIR="$HOME/.claude/lore"
    fi
fi
export LORE_DIR SKILL_DIR

# Optional per-install overrides, read from inside $LORE_DIR (the user's own
# lore data repository, separate from this package's repo). lore.py's own
# bootstrap .gitignore (GITIGNORE_CONTENT) already lists .lore.env, so a
# $LORE_DIR created by `lore.py bootstrap` won't track it; an existing
# $LORE_DIR whose .gitignore predates that entry still needs the user to add
# it themselves. Never shipped as part of this package, never committed here.
LORE_ENV_FILE="${LORE_ENV_FILE:-$LORE_DIR/.lore.env}"
if [ -f "$LORE_ENV_FILE" ]; then
    set -a
    # shellcheck source=/dev/null
    source "$LORE_ENV_FILE"
    set +a
fi

# Defaults for every variable a lore-*.sh script may read. .lore.env above,
# or the calling environment, can override any of these; unset ones fall
# back to the value here. See lore.env.example for what each one does.
: "${LORE_LAUNCHD_LABEL_PREFIX:=com.lore.}"
: "${LORE_AGENT_CONFIG_DIR:=$HOME/.claude}"
: "${LORE_GARDENER_CMD:=claude -p}"
: "${LORE_GARDENER_MODEL_ARGS:=}"
: "${LORE_KEEP_API_KEY:=0}"
: "${LORE_ROLE_FILE:=}"
: "${LORE_ALERT_PROVIDER:=log}"
: "${LORE_ALERT_CMD:=}"
: "${LORE_TELEGRAM_BOT_TOKEN:=}"
: "${LORE_TELEGRAM_CHAT_ID:=}"
: "${LORE_GARDENER_AUTH_CHECK:=claude auth status}"
: "${LORE_WATCH_FILES:=}"
: "${LORE_WATCH_FILE_MAX_BYTES:=19500}"

export LORE_ENV_FILE LORE_LAUNCHD_LABEL_PREFIX LORE_AGENT_CONFIG_DIR \
    LORE_GARDENER_CMD LORE_GARDENER_MODEL_ARGS LORE_KEEP_API_KEY \
    LORE_ROLE_FILE LORE_ALERT_PROVIDER LORE_ALERT_CMD \
    LORE_TELEGRAM_BOT_TOKEN LORE_TELEGRAM_CHAT_ID LORE_GARDENER_AUTH_CHECK \
    LORE_WATCH_FILES LORE_WATCH_FILE_MAX_BYTES

# -----------------------------------------------------------------------
# Portability helpers (I2). Every macOS-only primitive used anywhere in the
# lore-*.sh scripts is confined to one of the function bodies below; nothing
# outside this file calls the BSD stat flavor, AppleScript's notifier,
# the sleep-inhibitor daemon or the macOS service-management CLI directly,
# so lore-gardener.sh, lore-refresh.sh and lore-watchdog.sh run
# unmodified on Linux (falling back to notify-send/systemd-inhibit/systemctl,
# or a portable no-op when even that is unavailable).
# -----------------------------------------------------------------------

# mtime_of <path>: prints a path's modification time as a Unix epoch second
# count, or an empty string if the path does not exist. BSD stat (macOS,
# flag "-f %m") and GNU stat (Linux, flag "-c %Y") take incompatible flags;
# GNU form is tried first because GNU stat accepts more than one FILE
# operand and, given a flag it does not otherwise recognize as an operand,
# still processes any operand that IS a real path and prints its result to
# stdout even though the overall command then exits non-zero. Trying the
# GNU-first order means that stray stdout never happens: BSD stat rejects an
# unrecognized "-c" outright, with nothing on stdout, so the fallback to the
# BSD form only ever fires cleanly. Verified against GNU coreutils 9.4 on
# Linux and BSD stat on macOS; neither uname check nor a capability probe is
# needed.
mtime_of() {
    stat -c %Y "$1" 2>/dev/null || stat -f %m "$1" 2>/dev/null || echo ''
}

# notify_desktop <subject> <body> [sound-name]: best-effort desktop
# notification. osascript on macOS (subject/body/sound passed through
# environment variables and read back with AppleScript's `system attribute`,
# so quotes or backslashes in either string cannot break out of the
# osascript expression), notify-send on Linux when present, otherwise a
# silent no-op. Always returns 0: a notification failing must never fail
# whatever called it.
notify_desktop() {
    local subject="${1:-lore}" body="${2:-}" sound="${3:-}"
    if command -v osascript >/dev/null 2>&1; then
        local expr='display notification (system attribute "LORE_NOTIFY_BODY") with title (system attribute "LORE_NOTIFY_SUBJ")'
        if [ -n "$sound" ]; then
            expr="$expr sound name (system attribute \"LORE_NOTIFY_SOUND\")"
        fi
        LORE_NOTIFY_SUBJ="$subject" LORE_NOTIFY_BODY="$(printf '%.200s' "$body")" LORE_NOTIFY_SOUND="$sound" \
            osascript -e "$expr" >/dev/null 2>&1 || true
    elif command -v notify-send >/dev/null 2>&1; then
        notify-send -- "$subject" "$(printf '%.200s' "$body")" >/dev/null 2>&1 || true
    fi
    return 0
}

# inhibit_sleep: starts a best-effort background process that keeps the
# machine from sleeping for as long as the CALLING SCRIPT (not this
# function) keeps running, and sets LORE_INHIBIT_PID to its pid (empty if no
# inhibitor mechanism is available on this platform). The caller is
# responsible for `kill "$LORE_INHIBIT_PID" 2>/dev/null` on exit; nothing
# here cleans itself up automatically except caffeinate's own -w flag.
#
# caffeinate (macOS, "-w $$" so it exits on its own once this process exits,
# no separate kill strictly required, X4) when present; systemd-inhibit
# (Linux) holding a `tail -f /dev/null` placeholder when present; a silent
# no-op (LORE_INHIBIT_PID left empty) otherwise.
inhibit_sleep() {
    export LORE_INHIBIT_PID=""
    if command -v caffeinate >/dev/null 2>&1; then
        caffeinate -dims -w "$$" &
        export LORE_INHIBIT_PID=$!
    elif command -v systemd-inhibit >/dev/null 2>&1; then
        systemd-inhibit --what=sleep:idle --who=lore --why="lore gardener run" tail -f /dev/null &
        export LORE_INHIBIT_PID=$!
    fi
}

# scheduler_kick_refresh: force an out-of-schedule run of the refresh job
# right now. launchctl kickstart (macOS) when present, systemctl --user
# start (systemd) when present, otherwise just run lore-refresh.sh directly
# so the request still happens somehow. Prints nothing; returns the
# underlying command's exit status (0 for the direct-run fallback's own
# exit status).
scheduler_kick_refresh() {
    if command -v launchctl >/dev/null 2>&1; then
        launchctl kickstart -k "gui/$(id -u)/${LORE_LAUNCHD_LABEL_PREFIX}refresh" 2>/dev/null
        return $?
    elif command -v systemctl >/dev/null 2>&1; then
        systemctl --user start "lore-refresh.service" 2>/dev/null
        return $?
    else
        "$SKILL_DIR/scripts/lore-refresh.sh"
        return $?
    fi
}

# scheduler_has_job <name>: is a scheduled job for <name> (e.g. "gardener",
# "refresh") installed under whichever scheduler this platform uses? Checks
# launchctl list (macOS) or systemctl --user list-timers (systemd) for a
# label/unit containing "${LORE_LAUNCHD_LABEL_PREFIX}<name>" or
# "lore-<name>" respectively.
#
# Returns 0 (found), 1 (queried successfully, not found) or 2 (this platform
# has neither launchctl nor systemctl, so the question could not be asked at
# all). Callers must treat 2 as "unknown", never as "absent": on a cron or
# no-scheduler install this always returns 2, and skipping a check on that
# basis would silently disable it everywhere but macOS/systemd.
scheduler_has_job() {
    local name="$1"
    if command -v launchctl >/dev/null 2>&1; then
        launchctl list 2>/dev/null | grep -q "${LORE_LAUNCHD_LABEL_PREFIX}${name}" && return 0
        return 1
    elif command -v systemctl >/dev/null 2>&1; then
        systemctl --user list-timers 2>/dev/null | grep -q "lore-${name}" && return 0
        return 1
    fi
    return 2
}

# classify_result_line <line> [rc]: prints exactly one of network, quota,
# auth, sleep, killed, unknown, based on the fixed H1 patterns below (six
# recorded production failure nights, S3). rc=137 (SIGKILL) always classifies
# as "killed" regardless of line content, since a killed process rarely gets
# to print a coherent result line at all; every other case is decided by
# matching <line> (normally the last `type:result` JSON line's "result"
# field, or any other vendor error text) against fixed substrings, in a
# fixed priority order (a line can plausibly match more than one class, e.g.
# a quota message that also mentions "network", so order matters and is not
# alphabetical: network, quota, auth, sleep, then unknown).
classify_result_line() {
    local line="${1:-}" rc="${2:-}"
    if [ "$rc" = "137" ]; then
        echo killed
        return 0
    fi
    case "$line" in
        *ENOTFOUND*|*ECONNREFUSED*|*ConnectionRefused*|*ETIMEDOUT*|*EAI_AGAIN*)
            echo network ;;
        *429*|*'limit · resets'*|*'rate limit'*)
            echo quota ;;
        *authenticate*|*OAuth*|*401*)
            echo auth ;;
        *'went to sleep'*)
            echo sleep ;;
        *)
            echo unknown ;;
    esac
}

# _send_telegram_alert <subject> <body>: direct-curl telegram delivery for
# the "telegram" provider (I10). Not for use outside alert() below: the
# bot token is never written to any log and never appears in curl's argv or
# in `ps` output, because the request (url, token, and the message text) is
# fed to curl via --config on stdin as a single here-string, the same
# pattern connectors/_http.py and github_releases.py use for their own
# tokens. Silently does nothing if either LORE_TELEGRAM_BOT_TOKEN or
# LORE_TELEGRAM_CHAT_ID is unset: an install that has not configured
# telegram should not fail whatever called alert().
_send_telegram_alert() {
    local subject="$1" body="$2"
    [ -n "$LORE_TELEGRAM_BOT_TOKEN" ] && [ -n "$LORE_TELEGRAM_CHAT_ID" ] || return 0

    # Real newline between subject and body; body truncated to 3500 chars
    # (Telegram sendMessage caps text at 4096).
    local text cfg
    text="${subject}"$'\n'"$(printf '%.3500s' "$body")"

    # Escape a value for a curl --config double-quoted string. Backslash
    # first, then double quote, then the control characters curl's config
    # parser treats specially.
    local s
    _cfg_escape() {
        s="$1"
        s="${s//\\/\\\\}"
        s="${s//\"/\\\"}"
        s="${s//$'\n'/\\n}"
        s="${s//$'\r'/\\r}"
        s="${s//$'\t'/\\t}"
        printf '%s' "$s"
    }

    cfg="$(printf 'url = "https://api.telegram.org/bot%s/sendMessage"\ndata-urlencode = "chat_id=%s"\ndata-urlencode = "text=%s"\n' \
        "$LORE_TELEGRAM_BOT_TOKEN" "$(_cfg_escape "$LORE_TELEGRAM_CHAT_ID")" "$(_cfg_escape "$text")")"

    printf '%s' "$cfg" | curl -sS --max-time 15 --config - >/dev/null 2>&1 || true
    return 0
}

# alert <subject> <body>: always appends one line to $LORE_DIR/.alerts.log
# (body truncated to 600 chars), then additionally delivers via whichever
# provider $LORE_ALERT_PROVIDER selects (I10):
#   telegram - _send_telegram_alert above (LORE_TELEGRAM_BOT_TOKEN /
#              LORE_TELEGRAM_CHAT_ID)
#   command  - runs $LORE_ALERT_CMD "<subject>" with <body> piped to its
#              stdin (a user-supplied relay, e.g. an ssh hop to a host with
#              reliable egress)
#   desktop  - notify_desktop "<subject>" "<body>"
#   log      - (default) nothing beyond the always-on log line above
# Never exits non-zero and never lets a delivery failure propagate: alerting
# must never crash the gardener or watchdog run that called it.
#
# This is also what lore-alert.sh delegates to when the gardener's headless
# agent calls $LORE_ALERT directly to escalate mid-session (see
# references/gardener-prompt.md and lore-alert.sh): every escalation gets
# the same always-on log line and the same provider dispatch as any other
# alert() call in this package, not just a telegram-only special case.
alert() {
    local subject="${1:-lore alert}" body="${2:-}"
    mkdir -p "$LORE_DIR" 2>/dev/null || true
    printf '[%s] %s | %s\n' \
        "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
        "$(printf '%s' "$subject" | tr '\n' ' ')" \
        "$(printf '%s' "$body" | tr '\n' ' ' | cut -c1-600)" \
        >>"$LORE_DIR/.alerts.log" 2>/dev/null || true
    case "$LORE_ALERT_PROVIDER" in
        telegram)
            _send_telegram_alert "$subject" "$body"
            ;;
        command)
            if [ -n "$LORE_ALERT_CMD" ]; then
                # shellcheck disable=SC2086  # LORE_ALERT_CMD is a command line the admin controls, word-splitting into argv is intentional
                printf '%s' "$body" | $LORE_ALERT_CMD "$subject" >/dev/null 2>&1 || true
            fi
            ;;
        desktop)
            notify_desktop "$subject" "$body"
            ;;
        *)
            : # log (default): the always-on line above already recorded it
            ;;
    esac
    return 0
}
