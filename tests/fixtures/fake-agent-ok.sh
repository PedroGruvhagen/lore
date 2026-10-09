#!/bin/bash
# Fake headless-agent fixture simulating a SUCCESSFUL gardener run. Stands in
# for $LORE_GARDENER_CMD alongside fake-agent.sh's failure twin. Ignores
# every argument (flags, the prompt, and --output-format stream-json all
# arrive as plain positional args, none inspected).
#
# lore-gardener.sh's success gate (Step 4) requires BOTH the launched command
# to exit 0 AND $GDIR/report-$TODAY.md (i.e. $LORE_DIR/.gardener/report-<UTC
# date>.md) to exist afterward -- printing a result line alone is not
# enough. The gardener cd's into $LORE_DIR before launching
# $LORE_GARDENER_CMD, so this fixture writes the report relative to the
# current directory.
#
# Usage in a test:
#   LORE_GARDENER_CMD=/path/to/fake-agent-ok.sh \
#   LORE_GARDENER_AUTH_CHECK=true \
#   LORE_DIR=<tmp lore dir> bash skill/scripts/lore-gardener.sh

set -u
mkdir -p ".gardener" 2>/dev/null || true
# Matches lore-gardener.sh's own TODAY="$(date +%Y-%m-%d)" (local date, not
# UTC) so the report filename this fixture writes lines up with the path the
# gardener itself checks for.
TODAY="$(date +%Y-%m-%d)"
echo "test fixture report ($TODAY)" >".gardener/report-$TODAY.md"
echo '{"type":"result","is_error":false,"result":"ok"}'
exit 0
