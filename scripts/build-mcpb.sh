#!/usr/bin/env bash
#
# Build the Context Keeper MCPB desktop bundle (.mcpb) reproducibly.
#
# The bundle is a zip with manifest.json at its root plus the Python server
# under server/. This server has ZERO third-party dependencies (stdlib only),
# so there is no server/lib to vendor — but every FIRST-PARTY module must
# still be staged by hand, because there is no package for a tool to sweep.
#
# Stage every top-level *.py in the repo root. Do not hand-pick: server.py
# imports mirror.py inside a try/except (so a missing module degrades
# silently, with no error for anyone to notice) and this script used to copy
# only server.py + semantic_index.py, which shipped a desktop bundle with the
# mirror feature quietly absent. Same class of bug as the pip package losing
# mirror.py (b5acffa) and hooks/constraint_reinject.py.
# tests/test_server.py::TestMcpbBundleStaging enforces this.
#
# Packs with the official mcpb CLI when available (it validates the manifest);
# otherwise falls back to a deterministic `zip`. Both produce a valid bundle.
#
# Usage: scripts/build-mcpb.sh [output-dir]   (default: dist/)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_DIR="${1:-$ROOT/dist}"
STAGE="$ROOT/dist/mcpb-stage"

VERSION="$(python3 -c "import json,sys; print(json.load(open('$ROOT/mcpb/manifest.json'))['version'])")"
BUNDLE="$OUT_DIR/context-keeper-$VERSION.mcpb"

echo "==> Building Context Keeper MCPB v$VERSION"

# --- stage a clean tree: manifest + icon at root, server modules under server/
rm -rf "$STAGE"
mkdir -p "$STAGE/server" "$OUT_DIR"
cp "$ROOT/mcpb/manifest.json" "$STAGE/manifest.json"
cp "$ROOT/mcpb/icon.png"      "$STAGE/icon.png"
cp "$ROOT/server.py"          "$STAGE/server/server.py"
cp "$ROOT/mirror.py"          "$STAGE/server/mirror.py"
cp "$ROOT/semantic_index.py"  "$STAGE/server/semantic_index.py"
cp "$ROOT/code_drift.py"      "$STAGE/server/code_drift.py"
cp "$ROOT/usage.py"           "$STAGE/server/usage.py"

rm -f "$BUNDLE"

if command -v mcpb >/dev/null 2>&1; then
  PACK=(mcpb pack "$STAGE" "$BUNDLE")
elif command -v npx >/dev/null 2>&1; then
  PACK=(npx --yes @anthropic-ai/mcpb@2 pack "$STAGE" "$BUNDLE")
else
  PACK=()
fi

if [ "${#PACK[@]}" -gt 0 ]; then
  echo "==> Packing with mcpb CLI: ${PACK[*]}"
  "${PACK[@]}"
else
  echo "==> mcpb CLI not found; packing deterministically with zip"
  # -X strips extra file attributes; sorted input keeps entry order stable.
  ( cd "$STAGE" && find . -type f | LC_ALL=C sort | zip -X -q "$BUNDLE" -@ )
fi

echo "==> Built: $BUNDLE"
ls -l "$BUNDLE"
