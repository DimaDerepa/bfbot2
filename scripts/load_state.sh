#!/usr/bin/env bash
# Restore state.json from the `state` branch (or start empty).
set -uo pipefail
f="${1:-state.json}"
if git fetch -q --depth=1 origin state 2>/dev/null && git show FETCH_HEAD:state.json > "$f" 2>/dev/null; then
  echo "state loaded ($(wc -c < "$f") bytes)"
else
  echo '{}' > "$f"
  echo "no previous state, starting fresh"
fi
