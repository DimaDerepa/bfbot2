#!/usr/bin/env bash
# Save state.json as the single commit of the `state` branch (force-push, so history never grows).
set -euo pipefail
f="${1:-state.json}"
if [ ! -s "$f" ]; then echo "no state file, nothing to save"; exit 0; fi
if ! python -c "import json,sys; json.load(open(sys.argv[1]))" "$f"; then
  echo "state file is not valid JSON, not saving"; exit 0
fi
tmp="$(mktemp -d)"
cp "$f" "$tmp/state.json"
cd "$tmp"
git init -q -b state
git add state.json
git -c user.name="price-bot" -c user.email="price-bot@users.noreply.github.com" \
    commit -qm "state $(date -u +%Y-%m-%dT%H:%MZ)"
for i in 1 2 3; do
  if git push -qf "https://x-access-token:${GITHUB_TOKEN}@github.com/${GITHUB_REPOSITORY}.git" state:state; then
    echo "state saved"; exit 0
  fi
  sleep $((i * 3))
done
echo "failed to push state" >&2
exit 1
