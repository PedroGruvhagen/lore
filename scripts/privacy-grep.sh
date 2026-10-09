#!/bin/bash
# Privacy gate for a Lore checkout or its git history.
#
# Scans files for credential shapes (built in) and for any personal strings you
# keep in a private blocklist file (not part of this repository). Run it
# before publishing anything derived from your own lore.
#
# Usage:
#   scripts/privacy-grep.sh <root-dir>          # scan the files under root-dir
#   <content> | scripts/privacy-grep.sh --stdin # scan piped content (e.g. full git history)
#
# Blocklist: set LORE_PRIVACY_BLOCKLIST to a text file, one string per line,
# matched case-insensitively as plain substrings. Blank lines and lines
# starting with # are ignored. A line starting with + is also scanned but is
# excused inside the files named in ALLOWLIST_FILES (owner attribution).
#
# Exit 0: no hits outside the allowlist. Exit 1: at least one hit. Last line
# is always "privacy grep: N hits outside allowlist".

set -uo pipefail

# Built-in credential shapes, base64-encoded so this script's own source does
# not contain the literal prefixes (which would make it flag itself).
# Decoded below, matched case-insensitively as plain substrings.
PATTERNS_B64=(
    "c2stYW50"
    "Z2hwXw=="
    "QUtJQQ=="
    "QkVHSU4gUFJJVkFURSBLRVk="
    "QkVHSU4gT1BFTlNTSCBQUklWQVRFIEtFWQ=="
)

# True-regex patterns: a Telegram-bot-token shape, "<digits>:AA<digits...>".
REGEX_PATTERNS=(
    ':AA[0-9]'
)

# Files in which blocklist lines starting with + are excused.
ALLOWLIST_FILES=(
    "LICENSE"
    "NOTICE"
    "README.md"
    "skill/SKILL.md"
    "docs/claude-code-plugin.md"
    ".claude-plugin/plugin.json"
    ".claude-plugin/marketplace.json"
)

# Paths never scanned: git internals and gitignored scratch that legitimately
# references the real owner (task files, handoffs, run logs) but is never
# shipped.
PRUNE_DIRS=(".git" ".tasks" ".deferred")

PATTERNS=()
for enc in "${PATTERNS_B64[@]}"; do
    PATTERNS+=("$(printf '%s' "$enc" | base64 -d)")
done
ALLOWLIST_PATTERNS=()
if [ -n "${LORE_PRIVACY_BLOCKLIST:-}" ] && [ -f "$LORE_PRIVACY_BLOCKLIST" ]; then
    while IFS= read -r line || [ -n "$line" ]; do
        case "$line" in
            ''|'#'*) continue ;;
            '+'*) line="${line#+}"; [ -n "$line" ] && ALLOWLIST_PATTERNS+=("$line") ;;
        esac
        [ -n "$line" ] && PATTERNS+=("$line")
    done <"$LORE_PRIVACY_BLOCKLIST"
fi

is_allowlisted_file() {
    local path="$1" a
    for a in "${ALLOWLIST_FILES[@]}"; do
        [ "$path" = "$a" ] && return 0
    done
    return 1
}

is_allowlisted_pattern() {
    local pat="$1" a
    for a in "${ALLOWLIST_PATTERNS[@]}"; do
        [ "$pat" = "$a" ] && return 0
    done
    return 1
}

hit_count=0

# Runs one grep (fixed-string or regex) against $2, drops any line the mode
# says is allowed, prints survivors, and adds their count to hit_count.
# $1 = grep flag for match mode ("-F" fixed or "-E" regex), $2 = pattern,
# $3 = target file, $4 = label used in the HIT header.
scan_one() {
    local mode="$1" pat="$2" target="$3" label="$4" matches n
    matches="$(grep -a -ni"${mode#-}" -- "$pat" "$target" 2>/dev/null || true)"
    [ -z "$matches" ] && return 0
    n=$(printf '%s\n' "$matches" | grep -c '')
    echo "HIT ($label): $n line(s)"
    printf '%s\n' "$matches" | sed 's#^#  #'
    hit_count=$((hit_count + n))
}

if [ "${1:-}" = "--stdin" ]; then
    # Buffer stdin: it is scanned once per pattern below, and a pipe can only
    # be read once. -a forces text treatment; git cat-file --batch output
    # mixes object headers with blob content and can contain bytes grep
    # guesses are binary, which without -a only reports "Binary file
    # matches" instead of showing what matched.
    tmp="$(mktemp)"
    trap 'rm -f "$tmp"' EXIT
    cat >"$tmp"

    # Commit "author"/"committer" lines are an explicit allowance (the repo
    # is authored under the real name/email by explicit instruction); every
    # other hit in history - blob content, tree entries, tag bodies - must
    # be clean. git cat-file --batch writes these as literal lines starting
    # with "author " / "committer ", so on the numbered grep output
    # ("N:content") that is "^N:(author|committer) ".
    #
    # A blob in "git cat-file --batch" output carries no file path (the
    # A2-literal history pipeline discards path info before cat-file ever
    # sees it), so the directory scan's per-file allowlist below cannot be
    # replicated here. The three ALLOWLIST_PATTERNS are exempted globally
    # instead: every other pattern still fails the gate no matter where it
    # appears in history, and the directory scan already enforces the
    # narrower per-file rule on every commit before it ever reaches history.
    filtered_scan() {
        local mode="$1" pat="$2" label="$3" matches n
        if [ "$mode" = "-F" ] && is_allowlisted_pattern "$pat"; then
            return 0
        fi
        matches="$(grep -a -ni"${mode#-}" -- "$pat" "$tmp" 2>/dev/null | grep -v -E '^[0-9]+:(author|committer) ' || true)"
        [ -z "$matches" ] && return 0
        n=$(printf '%s\n' "$matches" | grep -c '')
        echo "HIT ($label): $n line(s)"
        printf '%s\n' "$matches" | sed 's#^#  #'
        hit_count=$((hit_count + n))
    }

    for p in "${PATTERNS[@]}"; do
        filtered_scan -F "$p" "stdin"
    done
    for rp in "${REGEX_PATTERNS[@]}"; do
        filtered_scan -E "$rp" "stdin"
    done
else
    root="${1:?usage: privacy-grep.sh <root-dir> | privacy-grep.sh --stdin}"
    prune_expr=()
    for d in "${PRUNE_DIRS[@]}"; do
        prune_expr+=(-path "$root/$d" -o)
    done
    # Python bytecode caches are never shipped (.gitignore'd) and can appear
    # anywhere a .py file was imported/compiled during local testing; prune
    # by name rather than a fixed root-relative path.
    prune_expr+=(-name "__pycache__" -o)
    unset 'prune_expr[${#prune_expr[@]}-1]' # drop trailing -o

    while IFS= read -r -d '' file; do
        rel="${file#"$root"/}"
        file_allowlisted=0
        is_allowlisted_file "$rel" && file_allowlisted=1

        for p in "${PATTERNS[@]}"; do
            if [ "$file_allowlisted" = "1" ] && is_allowlisted_pattern "$p"; then
                continue
            fi
            scan_one -F "$p" "$file" "$rel"
        done
        for rp in "${REGEX_PATTERNS[@]}"; do
            scan_one -E "$rp" "$file" "$rel"
        done
    done < <(find "$root" \( "${prune_expr[@]}" \) -prune -o -type f -print0)
fi

echo "privacy grep: $hit_count hits outside allowlist"
[ "$hit_count" -eq 0 ]
