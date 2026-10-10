"""대규모 생성이 같은 자리에서 끝없이 멈추지 않는다(실기기 2026-10-10: 45/51 에서 [이어서 만들기] 무한 반복).

원인: pages.css 첫 조각 자리에 AI 가 응답 형식(ops JSON)을 통째로 넣었고, 엔진이 그대로 저장한 뒤
이어 만들 때마다 그 망가진 조각을 다시 이어 쓰려다 실패했다. 같은 묶음의 App.css 는 매번 만들어졌다가 버려졌다.
"""
from __future__ import annotations

import json
import threading

import pytest

import gen_engine as ge
import generation_jobs as jobs
from code_output import CodeOutputError
from llm.base import LLMError, LLMErrorType

CUT = LLMError("모델 출력이 응답 길이 제한에서 잘렸습니다.", LLMErrorType.STRUCTURED_OUTPUT)
FORMAT_TAIL = '\n\n아래 JSON 형식으로만 응답하세요(설명 문장 금지):\n{\n  "summary": "...",\n  "ops": []\n}'
PROMPT = "쇼핑몰을 만들어 주세요." + FORMAT_TAIL
CSS1 = ".home { color: #333; }\n.cart-actions .btn-primary {\n  background-color: #007bff;\n}\n"
CSS2 = ".login { margin: 0 auto; }\n"
WRAPPED = json.dumps({"summary": "스타일", "ops": [{"action": "create", "file": "client/src/styles/pages.css",
                                                    "content": CSS1, "rationale": "첫 번째 부분"}]}, ensure_ascii=False, indent=2)


def R(obj):
    return type("R", (), {"text": json.dumps(obj, ensure_ascii=False) if not isinstance(obj, str) else obj,
                          "model_used": "m", "provider": "p"})()


class Script:
    """operation·파일별로 정해 둔 응답을 차례로 돌려주는 가짜 AI."""

    def __init__(self, parts=None, batch=None):
        self.parts = {k: list(v) for k, v in (parts or {}).items()}
        self.batch = batch or {}
        self.calls: list[tuple[str, str]] = []
        self.prompts: list[tuple[str, str]] = []
        self.lock = threading.Lock()

    def call(self, request, agent=None, operation=None):
        prompt = request.prompt
        with self.lock:
            self.prompts.append((operation, prompt))
        if operation == "generate_code_file_part":
            target = prompt.split("지금은 ", 1)[1].split(" 파일 하나만", 1)[0]
            with self.lock:
                self.calls.append((operation, target))
                queue = self.parts.get(target) or []
                item = queue.pop(0) if queue else ("", True)
            if isinstance(item, Exception):
                raise item
            return R(item if isinstance(item, (dict, str)) else {"content": item[0], "done": item[1]})
        if operation == "generate_code_part":
            wanted = prompt.rsplit("**아래 파일만**", 1)[1]
            files = [f.strip()[2:] for f in wanted.split("\n") if f.strip().startswith("- ")]
            with self.lock:
                self.calls.append((operation, ",".join(files)))
            out = []
            for f in files:
                v = self.batch.get(f, CUT)
                if isinstance(v, Exception):
                    raise v
                out.append({"action": "create", "file": f, "content": v})
            return R({"summary": "x", "ops": out})
        if operation == "generate_code_fix":
            return R({"edits": []})
        raise AssertionError(f"예상하지 못한 호출 {operation}")


FILES = [{"file": "package.json", "purpose": "", "layer": 0},
         {"file": "client/src/styles/App.css", "purpose": "전역", "layer": 1},
         {"file": "client/src/styles/pages.css", "purpose": "페이지", "layer": 1}]


def engine(monkeypatch, router, *, saved=None, skip_failed=False, events=None, concurrency=2):
    import code_agent as ca
    monkeypatch.setattr(ca, "get_router", lambda: router)
    e = ge.LargeGeneration(PROMPT, job_id="3f89c434c07345af", fingerprint="fp", concurrency=concurrency,
                           emit=(events.append if events is not None else None), limiter=ge.RateLimiter(0),
                           skip_failed=skip_failed)
    state = saved or {"fingerprint": "fp", "manifest": {"summary": "쇼핑몰", "contracts": "약속"}, "files": FILES,
                      "ops": {"package.json": {"action": "create", "file": "package.json", "content": "{}\n"}}, "partial": {}}
    e.resume_from(state)
    return e


# ── 받은 내용 검사 ─────────────────────────────────────────────────────

def test_응답_형식이_섞인_내용에서_실제_내용만_꺼낸다():
    path = "client/src/styles/pages.css"
    assert ge.clean_content(path, WRAPPED) == CSS1
    #: 길이 한도에서 잘려 닫히지 않은 JSON 이어도 content 를 끝까지 푼다
    cut = WRAPPED[: WRAPPED.index("#007bff") + 7]
    assert ge.clean_content(path, cut) == CSS1[: CSS1.index("#007bff") + 7]
    #: 조각 형식이 한 번 더 감싸인 경우
    assert ge.clean_content(path, json.dumps({"content": CSS2, "done": False})) == CSS2
    #: 줄바꿈이 \n 글자로 들어온 한 줄
    flat = CSS1.replace("\n", "\\n")
    assert ge.clean_content(path, flat) == CSS1
    #: 정상 내용·정상 JSON 파일은 그대로
    assert ge.clean_content(path, CSS1) == CSS1
    pkg = json.dumps({"name": "shop", "scripts": {"start": "node s.js"}})
    assert ge.clean_content("package.json", pkg) == pkg
    #: 다른 파일 것만 든 묶음에서는 추측하지 않는다
    other = json.dumps({"ops": [{"file": "a.css", "content": "a{}"}, {"file": "b.css", "content": "b{}"}]})
    assert ge.clean_content(path, other) is None
    assert ge.is_wrapped(path, '.a{}\n",\n      "rationale": "첫 번째 부분"\n    }\n  ]\n}\n'), "흔적만 남은 것도 잡는다"


def test_조각_지시에는_ops_응답_형식을_넣지_않는다(monkeypatch):
    router = Script(parts={"client/src/styles/pages.css": [(CSS1, True)], "client/src/styles/App.css": [(CSS2, True)]})
    e = engine(monkeypatch, router)
    e.run()
    part_prompts = [p for op, p in router.prompts if op == "generate_code_file_part"]
    assert part_prompts and all("아래 JSON 형식으로만 응답하세요" not in p for p in part_prompts)
    assert all('{"content": "...", "done": true|false}' in p and "다시 JSON(summary·ops·file·rationale 등)으로 감싸지 마세요" in p
               for p in part_prompts)
    batch_prompts = [p for op, p in router.prompts if op == "generate_code_part"]
    assert all("아래 JSON 형식으로만 응답하세요" in p for p in batch_prompts), "한 번에 받을 때는 ops 형식이 맞다"


def test_조각에_응답_형식이_섞여_와도_실제_내용만_이어_쓴다(monkeypatch):
    router = Script(parts={"client/src/styles/pages.css": [{"content": WRAPPED, "done": False}, (CSS2, True)],
                           "client/src/styles/App.css": [(CSS2, True)]})
    e = engine(monkeypatch, router)
    _, ops, _ = e.run()
    pages = next(o for o in ops if o["file"].endswith("pages.css"))["content"]
    assert pages == CSS1 + CSS2 and "rationale" not in pages


def test_형식이_계속_틀린_조각은_버리고_다시_받는다(monkeypatch):
    junk = {"content": json.dumps({"ops": [{"file": "x.css", "content": "x"}, {"file": "y.css", "content": "y"}]}), "done": True}
    router = Script(parts={"client/src/styles/pages.css": ["not json", junk, (CSS1, True)],
                           "client/src/styles/App.css": [(CSS2, True)]})
    _, ops, _ = engine(monkeypatch, router).run()
    assert next(o for o in ops if o["file"].endswith("pages.css"))["content"] == CSS1


def test_조각_응답이_잘리면_조각을_줄여_같은_조각을_다시_받는다(monkeypatch):
    events: list = []
    router = Script(parts={"client/src/styles/pages.css": [CUT, CUT, (CSS1, True)], "client/src/styles/App.css": [(CSS2, True)]})
    _, ops, _ = engine(monkeypatch, router, events=events).run()
    prompts = [p for op, p in router.prompts if op == "generate_code_file_part" and "pages.css 파일 하나만" in p]
    assert ["최대 150줄" in prompts[0], "최대 80줄" in prompts[1], "최대 40줄" in prompts[2]] == [True, True, True]
    assert next(o for o in ops if o["file"].endswith("pages.css"))["content"] == CSS1
    assert [e["lines"] for e in events if e["step"] == "part_retry"] == [80, 40]


# ── 실기기 재현: 망가진 조각이 저장된 체크포인트에서 이어 만들기 ─────────────────

def _user_checkpoint(partial_written):
    return {"fingerprint": "fp", "manifest": {"summary": "쇼핑몰", "contracts": "약속"}, "files": FILES,
            "ops": {"package.json": {"action": "create", "file": "package.json", "content": "{}\n"}},
            "partial": {"client/src/styles/pages.css": {"written": partial_written, "parts": 1}}}


def test_망가진_조각이_저장된_작업을_이어_만들면_실제_내용만_이어_쓰고_끝까지_간다(monkeypatch):
    events: list = []
    router = Script(parts={"client/src/styles/pages.css": [(CSS2, True)], "client/src/styles/App.css": [(CSS2, True)]})
    e = engine(monkeypatch, router, saved=_user_checkpoint(WRAPPED + "\n"), events=events)
    _, ops, _ = e.run()
    pages = next(o for o in ops if o["file"].endswith("pages.css"))["content"]
    assert pages == CSS1 + CSS2, "1번째 조각은 응답 형식을 벗겨 그대로 쓰고 2번째부터 이어 쓴다"
    cont = next(p for op, p in router.prompts if op == "generate_code_file_part" and "pages.css 파일 하나만" in p)
    assert "rationale" not in cont.split("지금까지 쓴", 1)[1], "이어 쓰기 지시에 망가진 내용을 보여 주지 않는다"
    assert "망가진 조각 1개 정리" in next(ev["message"] for ev in events if ev["step"] == "resumed")
    assert len(ops) == 3


def test_꺼낼_수_없는_조각은_버리고_그_파일만_처음부터_쓴다(monkeypatch):
    garbage = '{\n  "summary": "x", "ops": [{"file": "other.css", "content": "a"}, {"file": "b.css", "content": "b"}]}'
    router = Script(parts={"client/src/styles/pages.css": [(CSS1, True)], "client/src/styles/App.css": [(CSS2, True)]})
    e = engine(monkeypatch, router, saved=_user_checkpoint(garbage))
    assert "client/src/styles/pages.css" not in e.state["partial"]
    _, ops, _ = e.run()
    assert next(o for o in ops if o["file"].endswith("pages.css"))["content"] == CSS1


# ── 반복 끊기 · 성공한 것은 남기기 ──────────────────────────────────────

def test_같은_파일이_계속_실패하면_방법을_바꾸고_그래도_안되면_그_파일만_실패로_남긴다(monkeypatch):
    events: list = []
    #: 매 조각이 형식 오류 — 시도마다 3번씩, 3번의 시도(150 → 80 → 40줄)
    router = Script(parts={"client/src/styles/pages.css": ["bad"] * 30}, batch={"client/src/styles/App.css": CSS2})
    e = engine(monkeypatch, router, events=events)
    with pytest.raises(jobs.GenerationPaused) as err:
        e.run()
    tries = [p for op, p in router.prompts if op == "generate_code_file_part"]
    assert len(tries) == 9
    assert ["최대 80줄" in tries[3], "최대 40줄" in tries[6]] == [True, True]
    assert [f["file"] for f in err.value.failed] == ["client/src/styles/pages.css"]
    assert err.value.failed[0]["reason"] == "응답 형식이 맞지 않음"
    #: 같이 맡은 App.css 는 실패와 상관없이 저장됐다(예전에는 매번 버려지고 다시 만들어졌다)
    saved = jobs.load("3f89c434c07345af", "fp")
    assert "client/src/styles/app.css" in saved["ops"] and err.value.done == 2
    steps = [ev["step"] for ev in events]
    assert steps.count("file_retry") == 2 and "file_failed" in steps
    paused = [ev for ev in events if ev["step"] == "paused"][-1]
    assert "pages.css" in paused["message"] and "응답 형식이 맞지 않음" in paused["message"]


def test_실패한_파일은_다시_쓰기를_고르면_가장_작은_조각으로_처음부터_다시_한다(monkeypatch):
    router = Script(parts={"client/src/styles/pages.css": ["bad"] * 9}, batch={"client/src/styles/App.css": CSS2})
    with pytest.raises(jobs.GenerationPaused):
        engine(monkeypatch, router).run()
    saved = jobs.load("3f89c434c07345af", "fp")
    router2 = Script(parts={"client/src/styles/pages.css": [(CSS1, True)]})
    e = engine(monkeypatch, router2, saved=saved)
    _, ops, _ = e.run()
    prompt = next(p for op, p in router2.prompts if op == "generate_code_file_part")
    assert "최대 40줄" in prompt and "1번째 조각" in prompt
    assert {o["file"] for o in ops} == {f["file"] for f in FILES}
    assert [c for c in router2.calls if c[0] == "generate_code_part"] == [], "App.css 는 다시 만들지 않는다"


def test_실패한_파일을_빼고_결과를_받을_수_있다(monkeypatch):
    router = Script(parts={"client/src/styles/pages.css": ["bad"] * 9}, batch={"client/src/styles/App.css": CSS2})
    with pytest.raises(jobs.GenerationPaused):
        engine(monkeypatch, router).run()
    saved = jobs.load("3f89c434c07345af", "fp")
    router2 = Script()
    e = engine(monkeypatch, router2, saved=saved, skip_failed=True)
    data, ops, _ = e.run()
    assert router2.calls == [], "빼고 받을 때는 AI 를 다시 부르지 않는다"
    assert {o["file"] for o in ops} == {"package.json", "client/src/styles/App.css"}
    assert [f["file"] for f in data["skipped"]] == ["client/src/styles/pages.css"]


def test_이어_만들기를_몇_번_눌러도_정해진_횟수_안에_끝난다(monkeypatch):
    """무진행 재개가 끝없이 반복되던 실기기 상황 — 매 실행은 정해진 호출 수 안에서 '실패 파일' 로 끝난다."""
    saved = None
    for _ in range(4):
        router = Script(parts={"client/src/styles/pages.css": ["bad"] * 50}, batch={"client/src/styles/App.css": CSS2})
        with pytest.raises(jobs.GenerationPaused) as err:
            engine(monkeypatch, router, saved=saved).run()
        assert len([c for c in router.calls if c[0] == "generate_code_file_part"]) <= 9
        assert [f["file"] for f in err.value.failed] == ["client/src/styles/pages.css"]
        saved = jobs.load("3f89c434c07345af", "fp")


def test_동시에_만들_때_한_파일이_실패해도_나머지는_끝까지_만든다(monkeypatch):
    files = FILES[:1] + [{"file": f"client/src/pages/P{i}.jsx", "purpose": "", "layer": 2} for i in range(5)]
    state = {"fingerprint": "fp", "manifest": {"summary": "s", "contracts": ""}, "files": files,
             "ops": {"package.json": {"action": "create", "file": "package.json", "content": "{}\n"}}, "partial": {}}
    batch = {f"client/src/pages/P{i}.jsx": f"export default function P{i}() {{ return null; }}\n" for i in range(5) if i != 2}
    router = Script(parts={"client/src/pages/P2.jsx": ["bad"] * 30}, batch=batch)
    with pytest.raises(jobs.GenerationPaused) as err:
        engine(monkeypatch, router, saved=state, concurrency=3).run()
    assert err.value.done == 5 and [f["file"] for f in err.value.failed] == ["client/src/pages/P2.jsx"]


def test_인증_오류처럼_방법을_바꿔도_안_되는_실패는_바로_멈춘다(monkeypatch):
    denied = LLMError("권한 없음", LLMErrorType.ACCESS_DENIED)
    router = Script(parts={"client/src/styles/pages.css": [denied]}, batch={"client/src/styles/App.css": CSS2})
    with pytest.raises(jobs.GenerationPaused) as err:
        engine(monkeypatch, router, concurrency=1).run()
    assert not err.value.failed and len([c for c in router.calls if c[0] == "generate_code_file_part"]) == 1


def test_받은_파일_내용에_응답_형식이_섞이면_버리고_다시_받는다(monkeypatch):
    router = Script(parts={"client/src/styles/pages.css": [(CSS1, True)]},
                    batch={"client/src/styles/App.css": CSS2,
                           "client/src/styles/pages.css": json.dumps({"ops": [{"file": "a"}, {"file": "b"}]})})
    _, ops, _ = engine(monkeypatch, router).run()
    assert next(o for o in ops if o["file"].endswith("pages.css"))["content"] == CSS1


def test_검사_결과_멈춤_payload_에_실패_파일이_실린다():
    exc = jobs.GenerationPaused("m", "3f89c434c07345af", 50, 51, reason="r",
                                failed=[{"file": "a.css", "kind": "format", "reason": "응답 형식이 맞지 않음", "detail": "x"}])
    assert exc.progress_payload()["failed"] == [{"file": "a.css", "kind": "format", "reason": "응답 형식이 맞지 않음"}]
    assert isinstance(CodeOutputError("x"), Exception)
