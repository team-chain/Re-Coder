"""코드 생성 결과 일관성 — 적용하면 빌드·실행이 깨지는 ops 는 한 번 교정받는다.

실기기: 게시판 생성 결과가 Express + public/ 구조인데 package.json 에 CRA 빌드
설정(react-scripts build)만 남아 Docker 빌드가 실패했다.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

_CORE = Path(__file__).resolve().parents[1]
if str(_CORE) not in sys.path:
    sys.path.insert(0, str(_CORE))

import code_agent as ca  # noqa: E402
PLAN_DECISION = {
    "id": "storage", "question": "데이터를 어디에 저장할까요?", "chosen_key": "file",
    "options": [{"key": "file", "label": "파일"}, {"key": "db", "label": "DB"}],
}

SERVER = ("const express = require('express');\nconst path = require('path');\nconst app = express();\n"
          "app.use(express.static(path.join(__dirname, 'public')));\n"
          "app.get('/health', (q, s) => s.send('ok'));\napp.listen(process.env.PORT || 3000);\n")


def _payload(scripts: dict, deps: dict, extra: list | None = None) -> str:
    ops = [
        {"action": "create", "file": "package.json", "content": json.dumps({"name": "board", "scripts": scripts, "dependencies": deps})},
        {"action": "create", "file": "server.js", "content": SERVER},
        {"action": "create", "file": "public/index.html", "content": "<div id=root></div>"},
    ] + (extra or [])
    return json.dumps({"summary": "게시판", "ops": ops})


class _Router:
    def __init__(self, texts):
        self.texts, self.prompts = list(texts), []

    def call(self, request, agent=None, operation=None):
        self.prompts.append((operation, request.prompt))
        return type("_R", (), {"text": self.texts.pop(0), "model_used": "fake", "provider": "fake"})()


BROKEN = _payload({"start": "node server.js", "build": "react-scripts build"},
                  {"express": "4", "react-scripts": "5"})
FIXED = _payload({"start": "node server.js"}, {"express": "4"})


def test_깨진_생성_결과는_한_번_교정받는다(monkeypatch, tmp_path):
    router = _Router([BROKEN, FIXED])
    monkeypatch.setattr(ca, "get_router", lambda: router)
    result = ca.generate_code("게시판 만들어줘", decisions=[PLAN_DECISION], project_root=str(tmp_path))
    assert [op for op, _ in router.prompts] == ["generate_code", "generate_code_consistency"]
    assert "react-scripts" in router.prompts[1][1] and "src/" in router.prompts[1][1]
    package = json.loads(next(op["content"] for op in result["ops"] if op["file"] == "package.json"))
    assert "build" not in package["scripts"]
    assert result["consistency_issues"] == [] and "확인 필요" not in result["summary"]


def test_교정이_나아지지_않으면_원래_결과에_경고를_남긴다(monkeypatch, tmp_path):
    router = _Router([BROKEN, BROKEN])
    monkeypatch.setattr(ca, "get_router", lambda: router)
    result = ca.generate_code("게시판 만들어줘", decisions=[PLAN_DECISION], project_root=str(tmp_path))
    assert result["consistency_issues"][0]["code"] == "NODE_UNUSED_BUILD_SCRIPT"
    assert "확인 필요" in result["summary"]


def test_음성대조_정상_결과는_추가_호출이_없다(monkeypatch, tmp_path):
    router = _Router([FIXED])
    monkeypatch.setattr(ca, "get_router", lambda: router)
    result = ca.generate_code("게시판 만들어줘", decisions=[PLAN_DECISION], project_root=str(tmp_path))
    assert len(router.prompts) == 1 and result["consistency_issues"] == []


def test_기존_프로젝트의_원래_문제로_범위를_넓히지_않는다(monkeypatch, tmp_path):
    # 이미 선언 안 된 패키지를 쓰는 기존 코드 — 이번 요청과 무관하다.
    (tmp_path / "package.json").write_text(json.dumps({"name": "x", "dependencies": {"express": "4"}}), encoding="utf-8")
    (tmp_path / "legacy.js").write_text("require('lodash')", encoding="utf-8")
    body = json.dumps({"summary": "s", "ops": [{"action": "create", "file": "util.js", "content": "module.exports = 1;"}]})
    router = _Router([body])
    monkeypatch.setattr(ca, "get_router", lambda: router)
    result = ca.generate_code("유틸 추가", decisions=[PLAN_DECISION], project_root=str(tmp_path))
    assert len(router.prompts) == 1 and result["consistency_issues"] == []


def test_새로_import_한_패키지가_선언되지_않으면_잡는다(tmp_path):
    ops = [{"file": "server.js", "content": "const express = require('express'); const dayjs = require('dayjs');"},
           {"file": "package.json", "content": json.dumps({"name": "x", "dependencies": {"express": "4"}})}]
    issues = ca._consistency_issues(tmp_path, "", ops)
    assert [i["code"] for i in issues] == ["NODE_UNDECLARED_DEPENDENCY"] and "dayjs" in issues[0]["message"]


def test_대상_폴더_아래의_프로젝트를_본다(tmp_path):
    ops = [{"file": "package.json", "content": json.dumps({"name": "x", "scripts": {"start": "node app.js"}})}]
    issues = ca._consistency_issues(tmp_path, "board", ops)
    assert [i["code"] for i in issues] == ["NODE_START_ENTRY_MISSING"]


def test_대상_폴더를_붙인_경로는_대상_기준으로_바뀐다():
    import code_agent
    ops = code_agent._relative_to_target([{"file": "web/index.html"}, {"file": "css/a.css"}], "web")
    assert [o["file"] for o in ops] == ["index.html", "css/a.css"]


def test_AI_는_git_hooks_와_vscode_설정을_쓰지_않는다():
    import json
    from code_output import parse_code_output
    raw = json.dumps({"ops": [
        {"file": ".git/hooks/pre-commit", "content": "rm -rf ~"},
        {"file": ".vscode/tasks.json", "content": "{}"},
        {"file": "app.js", "content": "1"},
    ]})
    _, ops = parse_code_output(raw)
    assert [o["file"] for o in ops] == ["app.js"]
