#!/bin/bash
# Runs the full stdlib unittest suite (X1: no third-party test runner anywhere in this repo).
# Usage: bash tests/run.sh
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT" || exit 1

python3 -m unittest discover -s tests -v
exit $?
