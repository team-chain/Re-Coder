"""큰 요청: 응답이 길이 한도에서 잘리면 파일 목록 → 나눠 생성으로 전환한다(실기기 쇼핑몰)."""
from __future__ import annotations

import json
import threading

import pytest

import code_agent as ca
from llm.base import LLMError, LLMErrorType

DECISION = {"id": "stack", "question": "구성", "chosen_key": "node",
            "options": [{"key": "node", "label": "Node"}, {"key": "py", "label": "Python"}]}
CUT = LLMError("모델 출력이 응답 길이 제한에서 잘렸습니다.", LLMErrorType.STRUCTURED_OUTPUT)

MANIFEST = {"summary": "쇼핑몰", "contracts": "GET /api/products -> [{id,name,price}]",
            "files": [{"file": "package.json", "purpose": "의존성"}, {"file": "server.js", "purpose": "API"},
                      {"file": "public/index.html", "purpose": "화면"}, {"file": "public/app.js", "purpose": "화면 로직"},
                      {"file": "public/style.css", "purpose": "스타일"}]}
CONTENT = {
    "package.json": json.dumps({"name": "shop", "scripts": {"start": "node server.js"}, "dependencies": {"express": "4"}}),
    "server.js": "const express = require('express');\nconst app = express();\napp.use(express.static('public'));\n"
                 "app.get('/health', (q, s) => s.send('ok'));\napp.listen(process.env.PORT || 3000);\n",
    "public/index.html": "<script src=app.js></script>",
    "public/app.js": "const products = fetch('/api/products');\n",
    "public/style.css": "body{}",
}


class Router:
    def __init__(self, cut_files=()):
        self.ops, self.lock, self.cut_files = [], threading.Lock(), set(cut_files)

    def call(self, request, agent=None, operation=None):
        with self.lock:
            self.ops.append(operation)
        if operation == "generate_code":
            raise CUT
        if operation == "generate_code_manifest":
            return type("R", (), {"text": json.dumps(MANIFEST), "model_used": "m", "provider": "p"})()
        wanted = request.prompt.rsplit("**아래 파일만**", 1)[1]
        files = [f for f in CONTENT if f"- {f}\n" in wanted + "\n"]
        if len(files) > 1 and self.cut_files & set(files):
            raise CUT
        ops = [{"action": "create", "file": f, "content": CONTENT[f]} for f in files]
        # 모델이 요청 밖 파일을 끼워 넣어도 무시돼야 한다.
        ops.append({"action": "create", "file": "extra.txt", "content": "x"})
        return type("R", (), {"text": json.dumps({"summary": "part", "ops": ops}), "model_used": "m", "provider": "p"})()


def test_잘리면_파일목록을_받고_나눠서_만든다(monkeypatch, tmp_path):
    router = Router()
    monkeypatch.setattr(ca, "get_router", lambda: router)
    result = ca.generate_code("운영 가능한 쇼핑몰 사이트 하나 만들어줘", decisions=[DECISION], project_root=str(tmp_path))
    files = [op["file"] for op in result["ops"] if not op["file"].startswith("docs/adr/")]
    assert files == ["package.json", "server.js", "public/index.html", "public/app.js", "public/style.css"]
    assert router.ops.count("generate_code") == 1  # 같은 요청을 다시 보내지 않는다
    assert router.ops.count("generate_code_manifest") == 1
    # 의존 순서대로 같은 단계의 작은 파일을 둘씩: 설정·정적 파일(package.json·index.html·style.css) 2묶음 → 실행 코드(server.js·app.js) 1묶음
    assert router.ops.count("generate_code_part") == 3
    assert result["summary"].startswith("쇼핑몰")


def test_두_파일이_합쳐_잘리면_하나씩_다시(monkeypatch, tmp_path):
    router = Router(cut_files={"server.js"})
    monkeypatch.setattr(ca, "get_router", lambda: router)
    result = ca.generate_code("쇼핑몰", decisions=[DECISION], project_root=str(tmp_path))
    assert {op["file"] for op in result["ops"]} >= set(CONTENT)


def test_분할도_실패하면_파일을_바꾸지_않고_알린다(monkeypatch, tmp_path):
    class Broken(Router):
        def call(self, request, agent=None, operation=None):
            if operation == "generate_code_manifest":
                return type("R", (), {"text": json.dumps({"summary": "x", "contracts": "", "files": [], "more": False}),
                                      "model_used": "m", "provider": "p"})()
            return super().call(request, agent, operation)
    monkeypatch.setattr(ca, "get_router", lambda: Broken())
    with pytest.raises(RuntimeError, match="나눠서 만들기도 실패"):
        ca.generate_code("쇼핑몰", decisions=[DECISION], project_root=str(tmp_path))


def test_교정은_바꿀_파일만_받아_합친다():
    base = [{"file": "a.js", "content": "1"}, {"file": "package.json", "content": "{}"}]
    merged = ca._merge_ops(base, [{"file": "package.json", "content": "{\"x\":1}"}, {"file": "b.js", "content": "2"}])
    assert [(o["file"], o["content"]) for o in merged] == [("a.js", "1"), ("package.json", "{\"x\":1}"), ("b.js", "2")]


def test_later_batches_use_actual_generated_source(monkeypatch):
    class Checking(Router):
        def call(self, request, agent=None, operation=None):
            if operation == "generate_code_part" and "- public/index.html\n" in request.prompt.rsplit("**아래 파일만**", 1)[1]:
                assert CONTENT["server.js"] in request.prompt
                assert "[이미 생성한 파일" in request.prompt
            return super().call(request, agent, operation)
    monkeypatch.setattr(ca, "get_router", lambda: Checking())
    _, ops, _ = ca._generate_split("build a shop")
    assert {o["file"] for o in ops} == set(CONTENT)


def test_completed_context_does_not_pass_partial_source():
    text = ca._completed_files_context([
        {"file": "large.js", "content": "a" * 500},
        {"file": "small.js", "content": "module.exports = { auth };"},
    ], limit=200)
    assert "large.js" not in text
    assert "module.exports = { auth };" in text


def test_fresh_fullstack_manifest_cannot_omit_the_deployment_root():
    files = [{"file": p, "purpose": ""} for p in ("backend/package.json", "frontend/package.json", "README.md")]
    completed = ca._complete_fullstack_manifest(files)
    assert {f["file"] for f in completed} >= {"package.json", "Dockerfile", ".dockerignore", "README.md"}
    assert len([f for f in completed if f["file"] == "README.md"]) == 1
    assert ca._complete_fullstack_manifest([files[0]]) == [files[0]]


def test_대상_폴더가_목록과_응답에_붙어_와도_걸러지지_않는다(monkeypatch, tmp_path):
    """모델이 목록·묶음 응답 모두에 대상 폴더(web/)를 앞에 붙여 돌려주는 경우 — 예전엔 전부 걸러져 실패했다."""
    prefixed = {"summary": "쇼핑몰", "contracts": "", "files": [{"file": f"web/{f['file']}", "purpose": f["purpose"]} for f in MANIFEST["files"]]}

    class Prefixing(Router):
        def call(self, request, agent=None, operation=None):
            if operation == "generate_code_manifest":
                with self.lock:
                    self.ops.append(operation)
                return type("R", (), {"text": json.dumps(prefixed), "model_used": "m", "provider": "p"})()
            resp = super().call(request, agent, operation)
            if operation == "generate_code_part":
                data = json.loads(resp.text)
                for op in data["ops"]:
                    op["file"] = "web/" + op["file"]
                resp = type("R", (), {"text": json.dumps(data), "model_used": "m", "provider": "p"})()
            return resp

    monkeypatch.setattr(ca, "get_router", lambda: Prefixing())
    result = ca.generate_code("쇼핑몰", decisions=[DECISION], project_root=str(tmp_path), target_folder="web")
    files = [op["file"] for op in result["ops"] if not op["file"].startswith(("docs/", "web/docs/"))]
    assert files == ["package.json", "server.js", "public/index.html", "public/app.js", "public/style.css"]


class BigFile(Router):
    """결제 로직이 든 server.js 는 혼자서도 출력 한도를 넘는다(게이트웨이 4096 토큰)."""
    def __init__(self, parts, **kw):
        super().__init__(**kw)
        self.parts, self.part_prompts = list(parts), []

    def call(self, request, agent=None, operation=None):
        if operation == "generate_code_file_part":
            with self.lock:
                self.ops.append(operation)
            self.part_prompts.append(request.prompt)
            content, done = self.parts.pop(0)
            return type("R", (), {"text": json.dumps({"content": content, "done": done}), "model_used": "m", "provider": "p"})()
        if operation == "generate_code_part":
            wanted = request.prompt.rsplit("**아래 파일만**", 1)[1]
            if "- server.js\n" in wanted + "\n":
                with self.lock:
                    self.ops.append(operation)
                raise CUT
        return super().call(request, agent, operation)


def test_파일_하나가_한도를_넘으면_나눠서_이어_쓴다(monkeypatch, tmp_path):
    router = BigFile([("const a = 1;\nconst b = 2;", False),
                      ("const b = 2;\nfunction pay() {\n  return a + b;\n}", False),  # 직전 줄 반복은 걷어낸다
                      ("module.exports = { pay };", True)])
    monkeypatch.setattr(ca, "get_router", lambda: router)
    result = ca.generate_code("결제까지 되는 쇼핑몰 만들어줘", decisions=[DECISION], project_root=str(tmp_path))
    by_file = {op["file"]: op["content"] for op in result["ops"]}
    assert by_file["server.js"] == "const a = 1;\nconst b = 2;\nfunction pay() {\n  return a + b;\n}\nmodule.exports = { pay };\n"
    assert set(CONTENT) <= set(by_file)
    assert router.ops.count("generate_code_file_part") == 3
    # 이어 쓸 때는 지금까지 쓴 내용과 파일 사이의 약속을 함께 보낸다.
    assert "const b = 2;" in router.part_prompts[1] and MANIFEST["contracts"] in router.part_prompts[1]
    assert "지금까지 쓴 내용" not in router.part_prompts[0] and "const a = 1;" not in router.part_prompts[0]  # 첫 요청에는 쓴 내용이 없다


def test_이어_쓰기가_진행되지_않으면_멈추고_이어서_만들_수_있게_남긴다(monkeypatch, tmp_path):
    router = BigFile([("", False)] * 30)
    monkeypatch.setattr(ca, "get_router", lambda: router)
    with pytest.raises(RuntimeError) as err:
        ca.generate_code("쇼핑몰", decisions=[DECISION], project_root=str(tmp_path))
    assert getattr(err.value, "job_id", "")  # GenerationPaused — 다 만든 파일은 체크포인트에
    #: 같은 자리를 끝없이 반복하지 않는다 — 방법을 바꿔 정해진 횟수(3번×빈 조각 3번)만 시도하고, 그 파일만 실패로 남긴다.
    assert router.ops.count("generate_code_file_part") == 9
    assert [f["file"] for f in err.value.failed] == ["server.js"]
    assert err.value.done == len(MANIFEST["files"]) - 1, "나머지 파일은 끝까지 만들었다"
    assert "이 파일 다시 쓰기" in str(err.value) and "빼고 결과 받기" in str(err.value)


def test_파일_목록이_잘리면_약속은_조각으로_목록은_페이지로_받는다(monkeypatch, tmp_path):
    class CutManifest(Router):
        calls = []
        def call(self, request, agent=None, operation=None):
            if operation == "generate_code_manifest":
                CutManifest.calls.append(request.prompt)
                if len(CutManifest.calls) == 1:
                    raise CUT
                if len(CutManifest.calls) == 2:  # 요약 한 줄
                    return type("R", (), {"text": json.dumps({"summary": "쇼핑몰"}), "model_used": "m", "provider": "p"})()
                return type("R", (), {"text": json.dumps({"files": MANIFEST["files"], "more": False}), "model_used": "m", "provider": "p"})()
            if operation == "generate_code_contracts":
                return type("R", (), {"text": json.dumps({"content": "GET /api/products", "done": True}), "model_used": "m", "provider": "p"})()
            return super().call(request, agent, operation)
    monkeypatch.setattr(ca, "get_router", lambda r=CutManifest(): r)
    result = ca.generate_code("쇼핑몰", decisions=[DECISION], project_root=str(tmp_path))
    assert set(CONTENT) <= {op["file"] for op in result["ops"]}
    assert "GET /api/products" in CutManifest.calls[-1]  # 이어 받은 약속을 목록 요청에 넣었다
