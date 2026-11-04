"""State sync with a git branch through the GitHub REST API (no git needed — works on any self-hosted runner).

  python -m bot.statesync load <branch> <file>   # missing branch -> {}; any other failure -> exit 1, no file
  python -m bot.statesync save <branch> <file>   # store <file> as state.json, single orphan commit on <branch>

Needs GITHUB_TOKEN and GITHUB_REPOSITORY (GitHub Actions provides both).
"""
from __future__ import annotations

import base64
import json
import os
import sys
import time

import requests

API = "https://api.github.com"


def _env() -> tuple[str, dict]:
    token, repo = os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_REPOSITORY")
    if not token or not repo:
        raise SystemExit("GITHUB_TOKEN / GITHUB_REPOSITORY not set")
    return repo, {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                  "X-GitHub-Api-Version": "2022-11-28"}


def _req(method: str, url: str, headers: dict, **kw) -> requests.Response:
    last = None
    for attempt in range(4):
        try:
            r = requests.request(method, url, headers=headers, timeout=30, **kw)
            if r.status_code < 500 and r.status_code != 429:
                return r
            last = r
        except requests.RequestException as e:
            last = e
        time.sleep(3 * (attempt + 1))
    if hasattr(last, "status_code"):
        return last
    raise SystemExit(f"GitHub API unreachable: {last}")


def load(branch: str, path: str) -> int:
    repo, h = _env()
    r = _req("GET", f"{API}/repos/{repo}/branches/{branch}", h)
    if r.status_code == 404:
        with open(path, "w", encoding="utf-8") as f:
            f.write("{}")
        print(f"branch {branch} does not exist yet, starting fresh")
        return 0
    if r.status_code != 200:
        print(f"could not read branch {branch}: HTTP {r.status_code}; refusing to start with stale state", file=sys.stderr)
        if os.path.exists(path):   # self-hosted workspaces persist: never let "Save state" push an old file
            os.remove(path)
        return 1
    r = _req("GET", f"{API}/repos/{repo}/contents/state.json", {**h, "Accept": "application/vnd.github.raw+json"},
             params={"ref": branch})
    if r.status_code == 404:
        with open(path, "w", encoding="utf-8") as f:
            f.write("{}")
        print(f"no state.json on {branch}, starting fresh")
        return 0
    if r.status_code != 200:
        print(f"could not load {branch}: HTTP {r.status_code}; refusing to start with empty state", file=sys.stderr)
        if os.path.exists(path):
            os.remove(path)
        return 1
    text = r.text
    json.loads(text or "{}")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    print(f"state loaded from {branch} ({len(text)} bytes)")
    return 0


def save(branch: str, path: str) -> int:
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        print("no state file, nothing to save")
        return 0
    raw = open(path, "rb").read()
    try:
        json.loads(raw.decode("utf-8"))
    except ValueError:
        print("state file is not valid JSON, not saving")
        return 0
    repo, h = _env()
    blob = _req("POST", f"{API}/repos/{repo}/git/blobs", h,
                json={"content": base64.b64encode(raw).decode(), "encoding": "base64"})
    if blob.status_code != 201:
        print(f"blob failed: HTTP {blob.status_code} {blob.text[:200]}", file=sys.stderr)
        return 1
    tree = _req("POST", f"{API}/repos/{repo}/git/trees", h,
                json={"tree": [{"path": "state.json", "mode": "100644", "type": "blob", "sha": blob.json()["sha"]}]})
    if tree.status_code != 201:
        print(f"tree failed: HTTP {tree.status_code}", file=sys.stderr)
        return 1
    commit = _req("POST", f"{API}/repos/{repo}/git/commits", h,
                  json={"message": f"state {time.strftime('%Y-%m-%dT%H:%MZ', time.gmtime())}",
                        "tree": tree.json()["sha"], "parents": []})
    if commit.status_code != 201:
        print(f"commit failed: HTTP {commit.status_code}", file=sys.stderr)
        return 1
    sha = commit.json()["sha"]
    r = _req("PATCH", f"{API}/repos/{repo}/git/refs/heads/{branch}", h, json={"sha": sha, "force": True})
    if r.status_code == 422 or r.status_code == 404:   # branch does not exist yet
        r = _req("POST", f"{API}/repos/{repo}/git/refs", h, json={"ref": f"refs/heads/{branch}", "sha": sha})
    if r.status_code not in (200, 201):
        print(f"ref update failed: HTTP {r.status_code} {r.text[:200]}", file=sys.stderr)
        return 1
    print(f"state saved to {branch}")
    return 0


def next_run(workflow: str, min_interval: int = 600) -> int:
    """Keep a self-hosted chain going: wait until `min_interval` s after this job started, then dispatch again."""
    started = float(os.environ.get("BOT_JOB_STARTED") or time.time())
    wait = min_interval - (time.time() - started)
    if wait > 0:
        time.sleep(min(wait, min_interval))
    repo, h = _env()
    ref = os.environ.get("GITHUB_REF_NAME") or "main"
    r = _req("POST", f"{API}/repos/{repo}/actions/workflows/{workflow}/dispatches", h, json={"ref": ref})
    print(f"next {workflow} run requested: HTTP {r.status_code}")
    return 0 if r.status_code in (201, 204) else 1


def main(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[0] == "next":
        return next_run(argv[1], int(argv[2]) if len(argv) > 2 else 600)
    if len(argv) != 3 or argv[0] not in ("load", "save"):
        print(__doc__)
        return 2
    return (load if argv[0] == "load" else save)(argv[1], argv[2])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
