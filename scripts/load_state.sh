#!/usr/bin/env bash
# Restore state.json from the `state` branch.
#   branch exists and was read   -> state.json = its content, exit 0
#   branch does not exist yet    -> state.json = {} (first run), exit 0
#   anything else (network/auth error, GitHub 5xx, unreadable or broken state.json on the branch)
#                                -> NO state.json, marker file <state>.load-failed, exit 1.
# Never start from {} when the branch exists: the run would forget every /add watch, the /start chat and
# all notified prices, and save_state.sh would then force-push that empty state over the good one.
set -uo pipefail
PY="$(command -v python3 || command -v python)"   # Windows runners only have "python"
f="${1:-state.json}"
marker="$f.load-failed"
remote="${STATE_REMOTE:-origin}"
tries="${STATE_LOAD_TRIES:-4}"
rm -f "$f" "$f.tmp" "$marker"

valid_json() { "$PY" -c "import json,sys; d=json.load(open(sys.argv[1], encoding='utf-8')); sys.exit(0 if isinstance(d, dict) else 1)" "$1" 2>/dev/null; }

why=""
for i in $(seq 1 "$tries"); do
  # the full ref name: a pattern "state" would also match e.g. refs/heads/old/state, and a tag "state" would win
  git ls-remote --exit-code "$remote" refs/heads/state >/dev/null 2>&1
  rc=$?
  if [ "$rc" -eq 2 ]; then
    echo '{}' > "$f"
    echo "no state branch yet, starting fresh"
    exit 0
  fi
  if [ "$rc" -eq 0 ]; then
    if git fetch -q --depth=1 "$remote" refs/heads/state 2>/dev/null; then
      if git show FETCH_HEAD:state.json > "$f.tmp" 2>/dev/null; then
        if valid_json "$f.tmp"; then
          mv "$f.tmp" "$f"
          echo "state loaded ($(wc -c < "$f") bytes)"
          exit 0
        fi
        why="state.json on the state branch is not a valid JSON object"
        break   # retrying does not help; a human has to look at the branch
      fi
      why="the state branch has no state.json"
      break
    fi
    why="git fetch of the state branch failed"
  else
    why="git ls-remote failed (exit $rc)"
  fi
  [ "$i" -lt "$tries" ] && sleep $((i * 5))
done
rm -f "$f" "$f.tmp"
: > "$marker"
echo "::error::could not load the saved state ($why). The check is skipped and the saved state is left untouched." >&2
exit 1
