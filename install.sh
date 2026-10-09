#!/bin/bash
# Lore installer (R11). Copies skill/ into a Claude config directory,
# bootstraps (or reuses) a lore data directory, writes a .lore.env from the
# shipped example if none exists yet, and optionally installs a scheduler job
# (launchd on macOS, systemd --user timers on Linux, or a three-line
# crontab entry marked "# lore" as a last resort).
#
# Usage:
#   install.sh --config-dir <dir> [--data-dir <dir>]
#               [--scheduler launchd|systemd|cron|none]
#               [--dry-run] [--uninstall] [--render-only <dir>]
#
#   --config-dir <dir>   Claude config dir to install into (its skills/lore/
#                         subtree is created/overwritten). Default: ~/.claude.
#   --data-dir <dir>     Lore data repository. Default: <config-dir>/lore.
#   --scheduler <name>   launchd | systemd | cron | none. Default: auto-detect
#                         (launchd on macOS when launchctl is present, systemd
#                         on Linux when a --user manager is reachable, cron
#                         when only crontab is present, else none).
#   --dry-run            Print the planned actions; write nothing under
#                         --config-dir or --data-dir, and never touch a real
#                         scheduler. Also gates --uninstall (prints what would
#                         be removed instead of removing it). Not the same
#                         thing as --render-only: rendering to a throwaway
#                         directory has no effect on the running system, so
#                         --render-only always writes its output even when
#                         --dry-run is also given.
#   --render-only <dir>  Render --scheduler's job file(s) into <dir> and
#                         exit, without copying, bootstrapping, or touching a
#                         real scheduler. Requires --scheduler launchd or
#                         systemd (cron and none have no file to render).
#   --uninstall           Remove the scheduler job(s) this installer knows
#                         how to install (by label / unit name / crontab
#                         marker). Never touches --config-dir or --data-dir
#                         content.

set -u
set -o pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

CONFIG_DIR=""
DATA_DIR=""
SCHEDULER=""
DRY_RUN=0
RENDER_ONLY=""
UNINSTALL=0
LABEL_PREFIX="${LORE_LAUNCHD_LABEL_PREFIX:-com.lore.}"
PATH_ENV="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
CRON_MARKER="# lore"

usage() {
    cat <<'EOF'
Usage: install.sh [--config-dir <dir>] [options]

  --config-dir <dir>   Claude config dir to install into (default: ~/.claude)
  --data-dir <dir>     Lore data directory (default: <config-dir>/lore)
  --scheduler <name>   launchd | systemd | cron | none (default: auto-detect)
  --dry-run            Print planned actions; write nothing
  --render-only <dir>  Render --scheduler's job file(s) into <dir> and exit
  --uninstall          Remove this installer's scheduler job(s)
  -h, --help           Show this help
EOF
}

# detect_scheduler: pick a scheduler when --scheduler was not given (R11
# "auto-detect by default"). launchd on macOS when launchctl is present (it
# always is on a real Mac; absent only in unusual minimal environments),
# systemd --user on Linux when a user manager is actually reachable (not just
# installed: a container or a session started outside a login manager can
# have the binary but no running --user instance), cron as a last resort
# when only crontab is present, otherwise none.
detect_scheduler() {
    case "$(uname -s 2>/dev/null)" in
        Darwin)
            if command -v launchctl >/dev/null 2>&1; then
                echo launchd
                return
            fi
            ;;
        *)
            if command -v systemctl >/dev/null 2>&1 && systemctl --user list-units >/dev/null 2>&1; then
                echo systemd
                return
            fi
            ;;
    esac
    if command -v crontab >/dev/null 2>&1; then
        echo cron
        return
    fi
    echo none
}

while [ $# -gt 0 ]; do
    case "$1" in
        --config-dir) CONFIG_DIR="${2:-}"; shift 2 ;;
        --data-dir) DATA_DIR="${2:-}"; shift 2 ;;
        --scheduler) SCHEDULER="${2:-}"; shift 2 ;;
        --dry-run) DRY_RUN=1; shift ;;
        --render-only) RENDER_ONLY="${2:-}"; shift 2 ;;
        --uninstall) UNINSTALL=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *)
            echo "install.sh: unknown argument: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

CONFIG_DIR="${CONFIG_DIR:-$HOME/.claude}"
if [ -z "$SCHEDULER" ]; then
    SCHEDULER="$(detect_scheduler)"
fi
case "$SCHEDULER" in
    launchd|systemd|cron|none) ;;
    *)
        echo "install.sh: --scheduler must be one of launchd, systemd, cron, none (got: $SCHEDULER)" >&2
        exit 2
        ;;
esac

SKILL_DIR="$CONFIG_DIR/skills/lore"
DATA_DIR="${DATA_DIR:-$CONFIG_DIR/lore}"
LORE_DIR="$DATA_DIR"

# render_template <template-file> <dest-file>: substitutes every __TOKEN__ in
# the template with this install's resolved paths, then refuses to write the
# result if any __ survives (a template gained a new token this installer
# doesn't know how to fill, or a substitution value itself produced a
# double-underscore some other way; either way, writing a half-rendered job
# file for a real scheduler to load would be worse than failing loudly here).
render_template() {
    local src="$1" dest="$2"
    sed \
        -e "s#__HOME__#$HOME#g" \
        -e "s#__SKILL_DIR__#$SKILL_DIR#g" \
        -e "s#__LORE_DIR__#$LORE_DIR#g" \
        -e "s#__LABEL_PREFIX__#$LABEL_PREFIX#g" \
        -e "s#__PATH__#$PATH_ENV#g" \
        "$src" >"$dest"
    if grep -q '__' "$dest"; then
        echo "install.sh: refusing to write $dest: an unrendered __token__ remains" >&2
        rm -f "$dest"
        return 1
    fi
    return 0
}

# render_all_templates <launchd|systemd> <target-dir>: renders all three
# jobs' template file(s) for the given scheduler into target-dir (created if
# missing). Prints nothing on success; each render_template call reports its
# own failure.
render_all_templates() {
    local sched="$1" target="$2"
    mkdir -p "$target" || return 1
    case "$sched" in
        launchd)
            for name in gardener refresh watchdog; do
                render_template "$REPO_ROOT/launchd/com.lore.${name}.plist.template" \
                    "$target/${LABEL_PREFIX}${name}.plist" || return 1
            done
            ;;
        systemd)
            for name in gardener refresh watchdog; do
                render_template "$REPO_ROOT/systemd/lore-${name}.service.template" \
                    "$target/lore-${name}.service" || return 1
                render_template "$REPO_ROOT/systemd/lore-${name}.timer.template" \
                    "$target/lore-${name}.timer" || return 1
            done
            ;;
        *)
            echo "install.sh: nothing to render for scheduler '$sched'" >&2
            return 1
            ;;
    esac
    return 0
}

do_uninstall() {
    case "$SCHEDULER" in
        launchd)
            for name in gardener refresh watchdog; do
                local label="${LABEL_PREFIX}${name}"
                local plist="$HOME/Library/LaunchAgents/${label}.plist"
                if [ -f "$plist" ]; then
                    launchctl bootout "gui/$(id -u)/${label}" 2>/dev/null \
                        || launchctl unload "$plist" 2>/dev/null || true
                    rm -f "$plist"
                    echo "install.sh: removed $plist"
                fi
            done
            ;;
        systemd)
            for name in gardener refresh watchdog; do
                systemctl --user disable --now "lore-${name}.timer" 2>/dev/null || true
                rm -f "$HOME/.config/systemd/user/lore-${name}.service" \
                    "$HOME/.config/systemd/user/lore-${name}.timer"
            done
            systemctl --user daemon-reload 2>/dev/null || true
            echo "install.sh: removed lore systemd --user units"
            ;;
        cron)
            if command -v crontab >/dev/null 2>&1; then
                crontab -l 2>/dev/null | grep -v "$CRON_MARKER" | crontab -
                echo "install.sh: removed lore crontab lines"
            fi
            ;;
        none)
            echo "install.sh: --scheduler none, nothing to uninstall"
            ;;
    esac
}

install_cron() {
    local existing kept new_lines
    existing="$(crontab -l 2>/dev/null || true)"
    # Idempotent: strip any previously installed lore lines first, then
    # re-add the current three, so a second run never duplicates entries.
    kept="$(printf '%s\n' "$existing" | grep -v "$CRON_MARKER" || true)"
    new_lines="$(cat <<CRON
30 3 * * * LORE_DIR=$LORE_DIR HOME=$HOME PATH=$PATH_ENV /bin/bash $SKILL_DIR/scripts/lore-gardener.sh $CRON_MARKER
0 */6 * * * LORE_DIR=$LORE_DIR HOME=$HOME PATH=$PATH_ENV /bin/bash $SKILL_DIR/scripts/lore-refresh.sh $CRON_MARKER
*/5 * * * * LORE_DIR=$LORE_DIR HOME=$HOME PATH=$PATH_ENV /bin/bash $SKILL_DIR/scripts/lore-watchdog.sh $CRON_MARKER
CRON
)"
    printf '%s\n%s\n' "$kept" "$new_lines" | crontab -
}

if [ "$UNINSTALL" -eq 1 ]; then
    if [ "$DRY_RUN" -eq 1 ]; then
        echo "install.sh: DRY RUN, would uninstall scheduler '$SCHEDULER' job(s); config-dir and data-dir are never touched by --uninstall"
        exit 0
    fi
    do_uninstall
    exit $?
fi

if [ -n "$RENDER_ONLY" ]; then
    case "$SCHEDULER" in
        launchd|systemd) ;;
        *)
            echo "install.sh: --render-only requires --scheduler launchd or systemd (got: $SCHEDULER)" >&2
            exit 2
            ;;
    esac
    render_all_templates "$SCHEDULER" "$RENDER_ONLY" || exit 1
    echo "install.sh: rendered $SCHEDULER job file(s) into $RENDER_ONLY"
    exit 0
fi

# Build the plan before touching anything: every line below is a read-only
# check (existence tests, ls), so a --dry-run exit here leaves --config-dir
# and --data-dir completely untouched.
PLAN=()
PLAN+=("copy $REPO_ROOT/skill -> $SKILL_DIR")
if [ ! -d "$DATA_DIR" ] || [ -z "$(ls -A "$DATA_DIR" 2>/dev/null)" ]; then
    PLAN+=("bootstrap lore data dir at $DATA_DIR")
else
    PLAN+=("reuse existing non-empty lore data dir at $DATA_DIR")
fi
if [ ! -f "$DATA_DIR/.lore.env" ]; then
    PLAN+=("write $DATA_DIR/.lore.env from lore.env.example")
fi
case "$SCHEDULER" in
    launchd) PLAN+=("install 3 launchd jobs under $HOME/Library/LaunchAgents (label prefix $LABEL_PREFIX)") ;;
    systemd) PLAN+=("install 3 systemd --user timers under $HOME/.config/systemd/user") ;;
    cron) PLAN+=("add 3 crontab lines marked '$CRON_MARKER'") ;;
    none) PLAN+=("skip scheduler install (--scheduler none)") ;;
esac

if [ "$DRY_RUN" -eq 1 ]; then
    echo "install.sh: DRY RUN, planned actions:"
    for line in "${PLAN[@]}"; do
        echo "  - $line"
    done
    exit 0
fi

# Python floor check (PYTHON FLOOR 3.12): the two python3 invocations below
# (bootstrap, then doctor) need 3.12 or newer, so fail fast here, before the
# first byte is written under --config-dir or --data-dir, rather than
# midway through the copy.
if ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 12))'; then
    echo "install.sh: python3 must be 3.12 or newer (found: $(python3 --version 2>&1))" >&2
    exit 1
fi

echo "install.sh: installing lore into $CONFIG_DIR (data dir: $DATA_DIR, scheduler: $SCHEDULER)"

# 1. Copy the skill. Re-chmod the known executables explicitly afterward
# rather than relying on cp to preserve the source mode bits: plain cp's
# preserved permissions can still be narrowed by the destination's umask.
mkdir -p "$SKILL_DIR"
cp -R "$REPO_ROOT/skill/." "$SKILL_DIR/"
for exe in lore.py lore-mcp-server.py lore-alert.sh lore-common.sh lore-gardener.sh lore-refresh.sh lore-watchdog.sh; do
    [ -f "$SKILL_DIR/scripts/$exe" ] && chmod +x "$SKILL_DIR/scripts/$exe"
done

# 2. Bootstrap the data dir only if it doesn't already hold something (never
# overwrite an existing install's lore repository).
if [ ! -d "$DATA_DIR" ] || [ -z "$(ls -A "$DATA_DIR" 2>/dev/null)" ]; then
    mkdir -p "$DATA_DIR"
    python3 "$SKILL_DIR/scripts/lore.py" bootstrap --lore-dir "$DATA_DIR"
fi

# 3. Seed .lore.env from the shipped example, only if this install has none
# yet (never overwrite a configured install's secrets/overrides).
if [ ! -f "$DATA_DIR/.lore.env" ] && [ -f "$SKILL_DIR/lore.env.example" ]; then
    cp "$SKILL_DIR/lore.env.example" "$DATA_DIR/.lore.env"
fi

# 4. Scheduler install.
case "$SCHEDULER" in
    launchd)
        render_all_templates launchd "$HOME/Library/LaunchAgents" || exit 1
        for name in gardener refresh watchdog; do
            plist="$HOME/Library/LaunchAgents/${LABEL_PREFIX}${name}.plist"
            launchctl bootstrap "gui/$(id -u)" "$plist" 2>/dev/null \
                || launchctl load "$plist" 2>/dev/null || true
        done
        echo "install.sh: installed 3 launchd jobs (label prefix $LABEL_PREFIX)"
        ;;
    systemd)
        render_all_templates systemd "$HOME/.config/systemd/user" || exit 1
        systemctl --user daemon-reload 2>/dev/null || true
        for name in gardener refresh watchdog; do
            systemctl --user enable --now "lore-${name}.timer" 2>/dev/null || true
        done
        echo "install.sh: installed 3 systemd --user timers"
        ;;
    cron)
        install_cron
        echo "install.sh: installed 3 crontab lines marked '$CRON_MARKER'"
        ;;
    none)
        echo "install.sh: --scheduler none, skipping scheduler install"
        ;;
esac

# 5. Informational health check. --offline so a fresh install never depends
# on outbound network access to finish; never gates the installer's own exit
# code either way, since a fresh install with no sources configured yet is
# expected to show WARNs.
python3 "$SKILL_DIR/scripts/lore.py" doctor --lore-dir "$DATA_DIR" --offline || true

echo "install.sh: done"
exit 0
