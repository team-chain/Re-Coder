"""대규모 생성 엔진: 상한 없이 끝까지, 동시에, 멈추면 이어서, 만들 때마다 확인."""
from __future__ import annotations

import json
import threading
import time

import pytest

import code_agent as ca
import gen_engine
import generation_jobs
from llm.base import LLMError, LLMErrorType

#: 테스트용 가짜 결제 키 — 저장소 비밀값 검사(push protection)에 걸리지 않게 실행 중에 조립한다.
FAKE_KEY = "sk_" + "live_" + "51HxQ" + "abcdefghijklmnopqrstuv"

DECISION = {"id": "stack", "question": "구성", "chosen_key": "node",
            "options": [{"key": "node", "label": "Node"}, {"key": "py", "label": "Python"}]}


def R(obj):
    return type("R", (), {"text": json.dumps(obj, ensure_ascii=False), "model_used": "m", "provider": "p"})()


def files_for(n):
    out = [{"file": "package.json", "purpose": "의존성", "layer": 0}, {"file": "src/db.js", "purpose": "DB", "layer": 0}]
    out += [{"file": f"src/routes/r{i}.js", "purpose": f"API {i}", "layer": 1} for i in range(n)]
    out += [{"file": f"public/p{i}.html", "purpose": f"화면 {i}", "layer": 2} for i in range(n // 2)]
    return out


class Fake:
    def __init__(self, files, *, delay=0.0, fail_after=None, fail_exc=None, bad=None):
        self.files, self.delay = files, delay
        self.lock = threading.Lock()
        self.ops, self.active, self.max_active = [], 0, 0
        self.generated: list[str] = []
        self.fail_after, self.fail_exc = fail_after, fail_exc
        self.bad = bad or {}

    def call(self, request, agent=None, operation=None):
        with self.lock:
            self.ops.append(operation)
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            if self.delay:
                time.sleep(self.delay)
            return self._answer(request.prompt, operation)
        finally:
            with self.lock:
                self.active -= 1

    def _answer(self, prompt, operation):
        if operation == "generate_code_manifest":
            listed = prompt.rsplit("이미 받은 파일", 1)[1] if "이미 받은 파일" in prompt else ""
            remaining = [f for f in self.files if f"- {f['file']}\n" not in listed + "\n"]
            page = remaining[:gen_engine.PAGE_FILES]
            more = len(remaining) > len(page)
            if "이미 받은 파일" not in prompt:
                return R({"summary": "대형 쇼핑몰", "contracts": "GET /api/items", "files": page, "more": more})
            return R({"files": page, "more": more})
        if operation == "generate_code_part":
            wanted = prompt.rsplit("**아래 파일만**", 1)[1]
            names = [f["file"] for f in self.files if f"- {f['file']}\n" in wanted + "\n"]
            with self.lock:
                if self.fail_after is not None and len(self.generated) >= self.fail_after:
                    raise self.fail_exc
                self.generated.extend(names)
            ops = [{"action": "create", "file": n, "content": self.bad.get(n, self._content(n))} for n in names]
            return R({"summary": "part", "ops": ops})
        if operation == "generate_code_fix":
            return R({"edits": [{"find": '{"name": "shop",}', "replace": '{"name": "shop"}'},
                                {"find": f'const apiKey = "{FAKE_KEY}";', "replace": "const apiKey = process.env.PAY_KEY;"}]})
        raise AssertionError(operation)

    @staticmethod
    def _content(name):
        if name.endswith(".json"):
            return json.dumps({"name": "shop", "scripts": {"start": "node src/server.js"}})
        if name.endswith(".html"):
            return "<h1>shop</h1>\n"
        return f"module.exports = function () {{ return {json.dumps(name)}; }};\n"


def run(monkeypatch, tmp_path, fake, **kw):
    monkeypatch.setattr(ca, "get_router", lambda: fake)
    return ca.generate_code("결제까지 되는 대형 쇼핑몰 만들어줘", decisions=[DECISION], project_root=str(tmp_path),
                            mode="team", **kw)


def test_파일이_36개를_넘어도_자르지_않고_모두_만든다(monkeypatch, tmp_path):
    files = files_for(40)  # 2 + 40 + 20 = 62개 → 목록 3페이지
    fake = Fake(files)
    result = run(monkeypatch, tmp_path, fake)
    got = [op["file"] for op in result["ops"] if not op["file"].startswith("docs/adr/")]
    assert got == [f["file"] for f in files]
    assert fake.ops.count("generate_code_manifest") == 3
    assert "generate_code" not in fake.ops  # 팀 모드는 한 번에 만들어 보는 호출을 건너뛴다


def test_기반은_순서대로_기능과_화면은_동시에(monkeypatch, tmp_path):
    fake = Fake(files_for(8), delay=0.15)
    run(monkeypatch, tmp_path, fake)
    assert fake.max_active >= 2
    # 기반 파일은 기능 파일보다 먼저 끝난다(기능 파일이 기반 코드를 보고 쓴다).
    assert fake.generated.index("src/db.js") < min(fake.generated.index(f"src/routes/r{i}.js") for i in range(8))


def test_기능_파일은_기반_파일의_실제_코드를_보고_쓴다(monkeypatch, tmp_path):
    seen = []

    class Spy(Fake):
        def _answer(self, prompt, operation):
            if operation == "generate_code_part" and "src/routes/r0.js" in prompt.rsplit("**아래 파일만**", 1)[1]:
                seen.append(prompt)
            return super()._answer(prompt, operation)

    run(monkeypatch, tmp_path, Spy(files_for(2)))
    assert seen and "[이미 생성한 파일" in seen[0] and "src/db.js" in seen[0] and "GET /api/items" in seen[0]


def test_일일_한도에_걸리면_멈추고_같은_작업_ID로_이어서_만든다(monkeypatch, tmp_path):
    files = files_for(10)
    quota = LLMError("gateway 429: 일일 토큰 한도를 초과했습니다.", LLMErrorType.QUOTA_EXCEEDED)
    first = Fake(files, fail_after=6, fail_exc=quota)
    with pytest.raises(generation_jobs.GenerationPaused) as err:
        run(monkeypatch, tmp_path, first)
    paused = err.value
    assert paused.job_id and paused.total == len(files) and 0 < paused.done < len(files)
    second = Fake(files)
    result = run(monkeypatch, tmp_path, second, job_id=paused.job_id)
    assert {op["file"] for op in result["ops"]} >= {f["file"] for f in files}
    assert set(first.generated).isdisjoint(second.generated) or len(second.generated) < len(files)
    assert "generate_code_manifest" not in second.ops  # 설계도 다시 하지 않는다
    assert len(second.generated) == len(files) - paused.done


def test_다른_요청에는_남의_체크포인트를_쓰지_않는다(monkeypatch, tmp_path):
    files = files_for(4)
    quota = LLMError("gateway 429: 일일 토큰 한도", LLMErrorType.QUOTA_EXCEEDED)
    with pytest.raises(generation_jobs.GenerationPaused) as err:
        run(monkeypatch, tmp_path, Fake(files, fail_after=2, fail_exc=quota))
    fresh = Fake(files)
    monkeypatch.setattr(ca, "get_router", lambda: fresh)
    ca.generate_code("완전히 다른 요청", decisions=[DECISION], project_root=str(tmp_path), mode="team",
                     job_id=err.value.job_id)
    assert "generate_code_manifest" in fresh.ops


def test_일시적_오류는_기다렸다_다시_한다(monkeypatch, tmp_path):
    monkeypatch.setattr(gen_engine.time, "sleep", lambda s: None)
    files = files_for(2)
    flaky = LLMError("gateway 503: busy", LLMErrorType.SERVICE_ERROR, retryable=True)

    class Flaky(Fake):
        failed = 0
        def _answer(self, prompt, operation):
            if operation == "generate_code_part" and Flaky.failed < 2:
                Flaky.failed += 1
                raise flaky
            return super()._answer(prompt, operation)

    events = []
    import generation_progress
    token = generation_progress.bind(events.append)
    try:
        result = run(monkeypatch, tmp_path, Flaky(files))
    finally:
        generation_progress._reporter.reset(token)
    assert {f["file"] for f in files} <= {op["file"] for op in result["ops"]}
    assert any(e["step"] == "retry" for e in events)
    assert any(e["step"] == "file_done" and e.get("agent") for e in events)


def test_만들_때마다_문법과_비밀값을_확인하고_바뀔_부분만_고친다(monkeypatch, tmp_path):
    files = [{"file": "package.json", "purpose": "의존성", "layer": 0},
             {"file": "src/pay.js", "purpose": "결제", "layer": 1}]
    bad = {"package.json": '{"name": "shop",}',
           "src/pay.js": f'const apiKey = "{FAKE_KEY}";\nmodule.exports = apiKey;\n'}
    fake = Fake(files, bad=bad)
    result = run(monkeypatch, tmp_path, fake)
    by = {op["file"]: op["content"] for op in result["ops"]}
    assert json.loads(by["package.json"]) == {"name": "shop"}
    assert "process.env.PAY_KEY" in by["src/pay.js"] and "sk_live" not in by["src/pay.js"]
    assert fake.ops.count("generate_code_fix") >= 1


def test_속도_제한은_분당_호출_수를_넘지_않는다(monkeypatch):
    clock = {"t": 0.0}
    monkeypatch.setattr(gen_engine.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(gen_engine.time, "sleep", lambda s: clock.__setitem__("t", clock["t"] + s))
    limiter = gen_engine.RateLimiter(3)
    stamps = []
    for _ in range(7):
        limiter.acquire()
        stamps.append(clock["t"])
    for i in range(3, 7):
        assert stamps[i] - stamps[i - 3] >= 60


def test_분당_한도는_기다리고_일일_한도는_멈춘다():
    rate = LLMError("gateway 429: 분당 요청 한도(10)를 초과했습니다.", LLMErrorType.QUOTA_EXCEEDED)
    daily = LLMError("gateway 429: 일일 토큰 한도를 초과했습니다.", LLMErrorType.QUOTA_EXCEEDED)
    assert gen_engine._transient(rate) == (True, 61.0)
    assert gen_engine._transient(daily)[0] is False


def test_끝난_작업을_다시_요청하면_만들지_않고_결과를_돌려준다(monkeypatch, tmp_path):
    files = files_for(3)
    first = run(monkeypatch, tmp_path, Fake(files))
    again = Fake(files)
    second = run(monkeypatch, tmp_path, again, job_id=first["job_id"])
    assert again.ops == [] and [op["file"] for op in second["ops"]] == [op["file"] for op in first["ops"]]


def test_이어_쓰기가_빈_내용으로_끝나면_빈_파일을_만들지_않는다(monkeypatch, tmp_path):
    from tests.test_code_split_generation import BigFile, DECISION as D
    router = BigFile([("", True)] * 3)
    monkeypatch.setattr(ca, "get_router", lambda: router)
    with pytest.raises(RuntimeError) as err:
        ca.generate_code("쇼핑몰", decisions=[D], project_root=str(tmp_path))
    assert getattr(err.value, "job_id", "") and [f["file"] for f in err.value.failed] == ["server.js"]
    assert "이 파일 다시 쓰기" in str(err.value)


def test_전체_점검_문제는_지목된_파일의_바뀔_부분만_고친다(monkeypatch):
    class Edits:
        def call(self, request, agent=None, operation=None):
            assert operation == "generate_code_consistency" and "src/a.js" in request.prompt
            return R({"edits": [{"find": "require('./missing')", "replace": "require('./db')"}]})
    monkeypatch.setattr(ca, "get_router", lambda: Edits())
    ops = [{"file": "src/a.js", "content": "const db = require('./missing');\nmodule.exports = db;\n"},
           {"file": "src/db.js", "content": "module.exports = {};\n"}]
    issues = [{"severity": "error", "file": "src/a.js", "message": "없는 모듈 ./missing", "fix": "./db 사용"},
              {"severity": "warning", "file": "src/db.js", "message": "경고", "fix": ""}]
    out = gen_engine.edit_fix_round("요청", ops, issues)
    assert out[0]["content"].startswith("const db = require('./db')") and out[1] == ops[1]
    assert gen_engine.edit_fix_round("요청", ops, [{"severity": "error", "file": "", "message": "x"}]) is None
