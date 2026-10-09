#!/bin/bash
# Fake headless-agent fixture for gardener testing. Stands in for
# $LORE_GARDENER_CMD; ignores every argument (flags, the prompt, and
# --output-format stream-json all arrive as plain positional args) and always
# prints exactly one stream-json "result" line simulating a network failure,
# then exits 1, so lore-gardener.sh's classify_result_line() call sees the
# same "ENOTFOUND" pattern recorded in one of the six real S3 failure nights.
#
# Usage in a test:
#   LORE_GARDENER_CMD=/path/to/fake-agent.sh \
#   LORE_GARDENER_AUTH_CHECK=true \
#   LORE_DIR=<tmp lore dir> bash skill/scripts/lore-gardener.sh
#
# LORE_GARDENER_AUTH_CHECK=true is required alongside this fixture: the
# gardener's auth preflight (I13) runs `${LORE_GARDENER_AUTH_CHECK:-claude
# auth status}` before ever invoking $LORE_GARDENER_CMD, and a sandboxed or
# CI environment with no authenticated claude CLI would otherwise fail at
# that earlier gate (classified "auth") and never reach this fixture at all.

echo '{"type":"result","is_error":true,"result":"ENOTFOUND api.anthropic.com"}'
exit 1
