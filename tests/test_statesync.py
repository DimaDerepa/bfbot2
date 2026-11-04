import base64
import json

import bot.statesync as ss


class FakeGitHub:
    """Minimal in-memory GitHub REST API for branches/contents/git data/dispatches."""

    def __init__(self):
        self.branches = {}      # branch -> state.json text
        self.blobs, self.commits = {}, {}
        self.dispatched = []
        self.fail_branch_read = False

    def __call__(self, method, url, headers=None, timeout=None, params=None, json=None):
        path = url.split("/repos/o/r", 1)[1]
        if self.fail_branch_read and path.startswith("/branches/"):
            return R(500)
        if method == "GET" and path.startswith("/branches/"):
            return R(200 if path.split("/branches/", 1)[1] in self.branches else 404)
        if method == "GET" and path == "/contents/state.json":
            b = params["ref"]
            return R(200, text=self.branches[b]) if b in self.branches else R(404)
        if method == "POST" and path == "/git/blobs":
            sha = f"b{len(self.blobs)}"
            self.blobs[sha] = base64.b64decode(json["content"]).decode()
            return R(201, {"sha": sha})
        if method == "POST" and path == "/git/trees":
            return R(201, {"sha": "t:" + json["tree"][0]["sha"]})
        if method == "POST" and path == "/git/commits":
            sha = f"c{len(self.commits)}"
            self.commits[sha] = json["tree"].split(":", 1)[1]
            return R(201, {"sha": sha})
        if method == "PATCH" and path.startswith("/git/refs/heads/"):
            b = path.split("/git/refs/heads/", 1)[1]
            if b not in self.branches:
                return R(422)
            self.branches[b] = self.blobs[self.commits[json["sha"]]]
            return R(200)
        if method == "POST" and path == "/git/refs":
            b = json["ref"].split("refs/heads/", 1)[1]
            self.branches[b] = self.blobs[self.commits[json["sha"]]]
            return R(201)
        if method == "POST" and path.startswith("/actions/workflows/"):
            self.dispatched.append(path)
            return R(204)
        return R(404)


class R:
    def __init__(self, status, data=None, text=None):
        self.status_code = status
        self._data = data
        self.text = text if text is not None else (json.dumps(data) if data is not None else "")

    def json(self):
        return self._data


def test_rest_state_roundtrip(tmp_path, monkeypatch):
    gh = FakeGitHub()
    monkeypatch.setattr(ss.requests, "request", gh)
    monkeypatch.setattr(ss.time, "sleep", lambda s: None)
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    f = tmp_path / "s.json"
    assert ss.load("state-home", str(f)) == 0 and f.read_text() == "{}"          # missing branch -> fresh
    f.write_text('{"runs": 3}')
    assert ss.save("state-home", str(f)) == 0                                    # creates the branch
    f.write_text('{"runs": 4}')
    assert ss.save("state-home", str(f)) == 0                                    # force-updates it
    f.unlink()
    assert ss.load("state-home", str(f)) == 0 and json.loads(f.read_text()) == {"runs": 4}
    gh.fail_branch_read = True
    assert ss.load("state-home", str(f)) == 1 and not f.exists()                 # never start empty on API trouble
    assert ss.next_run("watch-home.yml", 0) == 0 and gh.dispatched
