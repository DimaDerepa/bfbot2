#!/usr/bin/env bash
# Save state.json as the single commit of the `state` branch (force-push, so history never grows).
# Refuses to push when the state was not loaded properly (see load_state.sh), so a failed fetch can never
# overwrite the good state with an empty one.
set -euo pipefail
PY="$(command -v python3 || command -v python)"   # Windows runners only have "python"
f="${1:-state.json}"
remote="${STATE_REMOTE:-https://x-access-token:${GITHUB_TOKEN:-}@github.com/${GITHUB_REPOSITORY:-}.git}"
if [ -e "$f.load-failed" ]; then echo "state was not loaded, not saving (the saved state stays as it is)"; exit 0; fi
if [ ! -s "$f" ]; then echo "no state file, nothing to save"; exit 0; fi
if ! "$PY" -c "import json,sys; d=json.load(open(sys.argv[1], encoding='utf-8')); sys.exit(0 if isinstance(d, dict) else 1)" "$f"; then
  echo "state file is not valid JSON, not saving"; exit 0
fi
# an empty state ({}: the bot never ran) may only create the branch, never replace an existing one
if "$PY" -c "import json,sys; sys.exit(0 if not json.load(open(sys.argv[1], encoding='utf-8')) else 1)" "$f"; then
  set +e
  git ls-remote --exit-code --heads "$remote" state >/dev/null 2>&1
  rc=$?
  set -e
  if [ "$rc" -ne 2 ]; then echo "state is empty and the state branch exists (or cannot be checked), not saving"; exit 0; fi
fi
tmp="$(mktemp -d)"
cp "$f" "$tmp/state.json"
cd "$tmp"
git init -q -b state
git add state.json
git -c user.name="price-bot" -c user.email="price-bot@users.noreply.github.com" \
    commit -qm "state $(date -u +%Y-%m-%dT%H:%MZ)"
for i in 1 2 3; do
  if git push -qf "$remote" state:state; then
    echo "state saved"; exit 0
  fi
  sleep $((i * 3))
done
echo "failed to push state" >&2
exit 1
