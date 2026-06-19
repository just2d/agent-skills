#!/usr/bin/env bash
# sync-lib.sh — re-vendor just2d/multi-llm-lib into THIS plugin's scripts/lib/.
#
# This plugin is self-contained (pure stdlib, no pip): the multi-llm-lib driver
# library is copied in and SHA-pinned. Run this after multi-llm-lib changes, to
# bump (or simply re-pin) the vendored copy that ships in the plugin.
#
# Usage (from the plugin dir, or via absolute path):
#   ./sync-lib.sh                # re-vendor the currently pinned SHA (idempotent)
#   ./sync-lib.sh <git-sha>      # bump to a new SHA (short SHA / tag / branch ok)
#
# After running: py_compile the scripts, then commit + push the plugin.
set -euo pipefail

LIB_REPO="https://github.com/just2d/multi-llm-lib.git"
HERE="$(cd "$(dirname "$0")" && pwd)"
DEST="$HERE/skills/multi-llm-research-coordinator/scripts/lib"

LIB_SHA="${1:-}"
if [[ -z "$LIB_SHA" ]]; then
    if [[ -f "$DEST/.synced_sha" ]]; then
        LIB_SHA="$(cat "$DEST/.synced_sha")"
        echo "no SHA given — re-vendoring the currently pinned $LIB_SHA"
    else
        echo "ERROR: no SHA given and no $DEST/.synced_sha — pass a git SHA." >&2
        exit 2
    fi
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

git clone --quiet "$LIB_REPO" "$TMP"
git -C "$TMP" checkout --quiet "$LIB_SHA"
LIB_SHA="$(git -C "$TMP" rev-parse HEAD)"   # resolve to the full 40-char SHA

rm -rf "$DEST"
mkdir -p "$(dirname "$DEST")"
cp -R "$TMP/lib" "$DEST"
find "$DEST" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
echo "$LIB_SHA" > "$DEST/.synced_sha"

echo "synced $LIB_SHA -> ${DEST/#$HERE\//}"
echo "next: py_compile scripts/*.py, then commit + push the plugin."
