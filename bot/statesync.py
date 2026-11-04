"""Cross-platform state sync with a git branch (works on Windows/macOS/Linux self-hosted runners).

  python -m bot.statesync load <branch> <file>   # missing branch -> {}; fetch failure -> exit 1, no file written
  python -m bot.statesync save <branch> <file>   # force-push <file> as state.json, single commit on <branch>

Needs `git` on PATH and, for save, GITHUB_TOKEN + GITHUB_REPOSITORY (provided by GitHub Actions).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time


def _git(*args: str, cwd: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def load(branch: str, path: str) -> int:
    for attempt in range(4):
        ls = _git("ls-remote", "--exit-code", "--heads", "origin", branch)
        if ls.returncode == 2:
            with open(path, "w", encoding="utf-8") as f:
                f.write("{}")
            print(f"branch {branch} does not exist yet, starting fresh")
            return 0
        if ls.returncode == 0:
            fe = _git("fetch", "-q", "--depth=1", "origin", branch)
            if fe.returncode == 0:
                show = _git("show", "FETCH_HEAD:state.json")
                if show.returncode == 0:
                    json.loads(show.stdout or "{}")  # validate
                    with open(path, "w", encoding="utf-8") as f:
                        f.write(show.stdout)
                    print(f"state loaded from {branch} ({len(show.stdout)} bytes)")
                    return 0
        time.sleep(3 * (attempt + 1))
    print(f"could not load {branch}; refusing to start with empty state", file=sys.stderr)
    if os.path.exists(path):
        os.remove(path)
    return 1


def save(branch: str, path: str) -> int:
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        print("no state file, nothing to save")
        return 0
    try:
        with open(path, encoding="utf-8") as f:
            json.load(f)
    except ValueError:
        print("state file is not valid JSON, not saving")
        return 0
    token, repo = os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_REPOSITORY")
    if not token or not repo:
        print("GITHUB_TOKEN / GITHUB_REPOSITORY not set", file=sys.stderr)
        return 1
    tmp = tempfile.mkdtemp(prefix="bfstate-")
    try:
        shutil.copy(path, os.path.join(tmp, "state.json"))
        _git("init", "-q", "-b", branch, cwd=tmp)
        _git("add", "state.json", cwd=tmp)
        _git("-c", "user.name=price-bot", "-c", "user.email=price-bot@users.noreply.github.com",
             "commit", "-qm", f"state {time.strftime('%Y-%m-%dT%H:%MZ', time.gmtime())}", cwd=tmp)
        url = f"https://x-access-token:{token}@github.com/{repo}.git"
        for attempt in range(3):
            if _git("push", "-qf", url, f"{branch}:{branch}", cwd=tmp).returncode == 0:
                print(f"state saved to {branch}")
                return 0
            time.sleep(3 * (attempt + 1))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("failed to push state", file=sys.stderr)
    return 1


def main(argv: list[str]) -> int:
    if len(argv) != 3 or argv[0] not in ("load", "save"):
        print(__doc__)
        return 2
    return (load if argv[0] == "load" else save)(argv[1], argv[2])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
