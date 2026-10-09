#!/bin/bash
# Lore alert entrypoint (I10). Usage: lore-alert.sh "<subject>" "<body>"
#
# This is $LORE_ALERT as substituted into the gardener's headless-agent
# prompt (references/gardener-prompt.md): the agent calls this script
# directly, as its own subprocess, to escalate mid-session. It delegates
# straight to lore-common.sh's alert() dispatcher, so an agent escalation
# gets exactly the same treatment as every other alert() call in this
# package: the always-on $LORE_DIR/.alerts.log line, plus whichever
# provider LORE_ALERT_PROVIDER selects (telegram/command/desktop/log).
# Kept as a standalone, directly executable script rather than inlined
# where $LORE_ALERT is substituted, because a freshly spawned agent
# subprocess has no access to shell functions defined in lore-common.sh's
# own (unrelated) interactive or background shell.
#
# ALWAYS exits 0: an alert delivery failure must never crash its caller.

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./lore-common.sh
source "$SCRIPT_DIR/lore-common.sh"

alert "${1:-lore alert}" "${2:-}"
exit 0
