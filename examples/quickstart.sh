#!/usr/bin/env bash
# Quickstart: bootstrap a lore directory, add one page and its deterministic
# extract, then run index/search/fact/lint against it. Uses only python3 and
# bash; safe to run from a clean checkout, writes only under a temp directory.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
LORE_PY="$REPO_ROOT/skill/scripts/lore.py"

WORK_DIR="$(mktemp -d)"
trap 'rm -rf "$WORK_DIR"' EXIT
LORE_DIR="$WORK_DIR/lore"

echo "== bootstrap =="
python3 "$LORE_PY" bootstrap --lore-dir "$LORE_DIR"

echo "== add a page and its extract =="
cp "$SCRIPT_DIR/pages/example-api.md" "$LORE_DIR/pages/example-api.md"
cp "$SCRIPT_DIR/lint-config.json" "$LORE_DIR/lint-config.json"
mkdir -p "$LORE_DIR/extracts"
cat > "$LORE_DIR/extracts/example-api.json" <<'JSON'
{
  "endpoint": "https://api.example.com/v1/complete",
  "models": {
    "large": "example-model-large",
    "mid": "example-model-mid"
  }
}
JSON

echo "== index =="
python3 "$LORE_PY" index --lore-dir "$LORE_DIR"

echo "== search =="
python3 "$LORE_PY" search "example cloud api" --lore-dir "$LORE_DIR"

echo "== fact =="
FACT_VALUE="$(python3 "$LORE_PY" fact example-api endpoint --lore-dir "$LORE_DIR")"
echo "endpoint fact: $FACT_VALUE"

echo "== lint =="
python3 "$LORE_PY" lint --lore-dir "$LORE_DIR"

echo "OK"
