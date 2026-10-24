"""GitOps 되돌림 PR — base 의 현재 HEAD 위에, 대상 커밋이 바꾼 파일만 되돌린다 (3.0.2).

GitHub API 는 httpx.MockTransport 위의 작은 가짜 저장소로 흉내 낸다(네트워크 없음).
"""
from __future__ import annotations

import asyncio
import hashlib
import json

import httpx
import pytest

from core.agents import rollback_pr_agent as mod
from core.schemas import RollbackPRRequest


class FakeRepo:
    def __init__(self):
        self.trees: dict[str, dict] = {}
        self.commits: dict[str, dict] = {}
        self.refs: dict[str, str] = {}
        self.pulls: list[dict] = []
        self.ref_patches: list[dict] = []

    def tree(self, files: dict[str, str]) -> str:
        entries = {p: ("100644", hashlib.sha1(c.encode()).hexdigest()) for p, c in files.items()}
        sha = hashlib.sha1(json.dumps(sorted(entries.items())).encode()).hexdigest()
        self.trees[sha] = entries
        return sha

    def commit(self, files: dict[str, str], parent: str | None, message: str = "m") -> str:
        tree = self.tree(files)
        sha = hashlib.sha1(f"{tree}{parent}{message}{len(self.commits)}".encode()).hexdigest()
        self.commits[sha] = {"tree": tree, "parents": [parent] if parent else [], "message": message}
        return sha

    def changed(self, sha: str) -> list[dict]:
        c = self.commits[sha]
        new = self.trees[c["tree"]]
        old = self.trees[self.commits[c["parents"][0]]["tree"]] if c["parents"] else {}
        out = []
        for path in sorted(set(new) | set(old)):
            if new.get(path) != old.get(path):
                status = "added" if path not in old else "removed" if path not in new else "modified"
                out.append({"filename": path, "status": status})
        return out

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.replace("/repos/o/r", "")
        if request.method == "GET" and path.startswith("/commits/"):
            sha = path.split("/")[-1]
            c = self.commits[sha]
            return httpx.Response(200, json={"sha": sha, "commit": {"message": c["message"]},
                                             "parents": [{"sha": p} for p in c["parents"]], "files": self.changed(sha)})
        if request.method == "GET" and path.startswith("/git/ref/heads/"):
            return httpx.Response(200, json={"object": {"sha": self.refs[path.split("/heads/", 1)[1]]}})
        if request.method == "GET" and path.startswith("/git/commits/"):
            sha = path.split("/")[-1]
            return httpx.Response(200, json={"sha": sha, "tree": {"sha": self.commits[sha]["tree"]}})
        if request.method == "GET" and path.startswith("/git/trees/"):
            sha = path.split("/")[-1]
            return httpx.Response(200, json={"sha": sha, "truncated": False, "tree": [
                {"path": p, "mode": m, "type": "blob", "sha": b} for p, (m, b) in self.trees[sha].items()]})
        if request.method == "POST" and path == "/git/trees":
            body = json.loads(request.content)
            entries = dict(self.trees[body["base_tree"]])
            for e in body["tree"]:
                if e["sha"] is None:
                    entries.pop(e["path"], None)
                else:
                    entries[e["path"]] = (e["mode"], e["sha"])
            sha = hashlib.sha1(json.dumps(sorted(entries.items())).encode()).hexdigest()
            self.trees[sha] = entries
            return httpx.Response(201, json={"sha": sha})
        if request.method == "POST" and path == "/git/commits":
            body = json.loads(request.content)
            sha = hashlib.sha1(json.dumps(body).encode()).hexdigest()
            self.commits[sha] = {"tree": body["tree"], "parents": body["parents"], "message": body["message"]}
            return httpx.Response(201, json={"sha": sha})
        if request.method == "POST" and path == "/git/refs":
            body = json.loads(request.content)
            name = body["ref"].split("refs/heads/", 1)[1]
            if name in self.refs:
                return httpx.Response(422, json={"message": "Reference already exists"})
            self.refs[name] = body["sha"]
            return httpx.Response(201, json={})
        if request.method == "PATCH" and path.startswith("/git/refs/"):
            self.ref_patches.append(json.loads(request.content))
            return httpx.Response(200, json={})
        if request.method == "POST" and path == "/pulls":
            body = json.loads(request.content)
            self.pulls.append(body)
            return httpx.Response(201, json={"number": len(self.pulls), "html_url": "https://github.com/o/r/pull/1"})
        return httpx.Response(404, json={"message": f"unexpected {request.method} {path}"})


@pytest.fixture
def repo(monkeypatch):
    fake = FakeRepo()
    real = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(fake.handle)
        return real(*args, **kwargs)
    monkeypatch.setattr(mod.httpx, "AsyncClient", factory)
    return fake


def _run(target: str) -> "mod.RollbackPRRecord":
    req = RollbackPRRequest(project_id="p", repo_owner="o", repo_name="r", target_commit_sha=target, github_token="t")
    return asyncio.run(mod.RollbackPRAgent().create_rollback_pr(req))


def test_revert_is_built_on_current_base_head_and_reverts_only_target_files(repo):
    c1 = repo.commit({"app.py": "v1", "README": "r1"}, None)
    bad = repo.commit({"app.py": "BROKEN", "README": "r1", "new.py": "added by bad"}, c1, "break app")
    later = repo.commit({"app.py": "BROKEN", "README": "r2 later edit", "new.py": "added by bad"}, bad, "docs")
    repo.refs["main"] = later

    record = _run(bad)
    assert record.status == "opened", record.error_message
    head = repo.refs[record.revert_branch]
    commit = repo.commits[head]
    assert commit["parents"] == [later], "되돌림 커밋은 base 의 현재 HEAD 위에 있어야 한다"
    files = repo.trees[commit["tree"]]
    assert files["app.py"] == repo.trees[repo.commits[c1]["tree"]]["app.py"]   # 되돌림
    assert "new.py" not in files                                                # 대상이 추가한 파일은 지움
    assert files["README"] == repo.trees[repo.commits[later]["tree"]]["README"]  # 뒤의 변경은 그대로
    assert repo.ref_patches == [], "브랜치를 강제로 옮기지 않는다"
    assert repo.pulls and repo.pulls[0]["head"] == record.revert_branch and repo.pulls[0]["base"] == "main"
    assert "This reverts commit " + bad in commit["message"]


def test_nothing_to_revert_fails_clearly_without_a_pr(repo):
    c1 = repo.commit({"app.py": "v1"}, None)
    bad = repo.commit({"app.py": "BROKEN"}, c1)
    fixed = repo.commit({"app.py": "v1"}, bad, "manual revert")
    repo.refs["main"] = fixed
    record = _run(bad)
    assert record.status == "failed" and record.pr_url is None
    assert "되돌릴 변경이 없습니다" in record.error_message
    assert repo.pulls == []


def test_conflicting_later_change_is_refused(repo):
    c1 = repo.commit({"app.py": "v1"}, None)
    bad = repo.commit({"app.py": "BROKEN"}, c1)
    later = repo.commit({"app.py": "BROKEN + hotfix"}, bad)
    repo.refs["main"] = later
    record = _run(bad)
    assert record.status == "failed" and "app.py" in record.error_message
    assert repo.pulls == [] and len(repo.refs) == 1


def test_already_reverted_tree_gives_nothing_to_revert(repo):
    c1 = repo.commit({"app.py": "v1", "x": "1"}, None)
    bad = repo.commit({"app.py": "v1", "x": "1"}, c1, "empty commit")
    repo.refs["main"] = bad
    record = _run(bad)
    assert record.status == "failed" and "되돌릴" in record.error_message
    assert repo.pulls == []
