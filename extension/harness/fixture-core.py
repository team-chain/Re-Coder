"""픽스처 LLM 을 물린 실 HTTP 코어 — 확장 통합 하네스의 상대편.

실제 Bedrock/Gemini 대신 정해진 JSON 을 돌려주는 라우터를 코어에 주입해,
네트워크·키 없이도 채팅→plan→generate 의 **실 HTTP 경로**를 구동한다.
포트/토큰은 e2e-extension-flow.js 와 맞춰져 있다.
"""
import json
import os
import sys
from pathlib import Path

CORE_DIR = str(Path(__file__).resolve().parents[1].parent / "core")
sys.path.insert(0, CORE_DIR)
os.environ["SESSION_TOKEN"] = "harness-token-123"
os.environ["RECODER_TEST_MODE"] = "1"
os.environ.pop("AWS_PROFILE", None)

import main  # noqa: E402

PLAN = {"decisions": [{
    "id": "storage", "question": "게시글 저장 방식은?",
    "impact": "데이터 보존과 배포 난이도를 좌우",
    "options": [
        {"key": "file", "label": "파일 기반", "pros": ["설정 불필요"], "cons": ["동시성 약함"]},
        {"key": "sqlite", "label": "SQLite", "pros": ["표준 SQL"], "cons": ["파일 잠금"]},
    ],
    "recommended_key": "file",
}]}
CODE = {"summary": "게시판 생성", "ops": [
    {"action": "create", "file": "index.html", "language": "html",
     "content": "<!doctype html><h1>board</h1>", "rationale": "진입 문서"},
    {"action": "create", "file": "app.js", "language": "javascript",
     "content": "console.log('board')", "rationale": "동작"},
]}
CHAT = {"reply": "게시판을 만들겠습니다. 아래에서 위치와 파일을 확인하고 승인해 주세요.",
        "action": {"type": "code.plan", "instruction": "게시판 생성 - 글 작성/목록/삭제",
                   "stack": "HTML/JS", "files": ["index.html", "app.js"], "summary": "게시판 생성"}}


class _Resp:
    def __init__(self, text):
        self.text, self.model_used = text, "fixture-model"


class _Fx:
    def call(self, request, agent=None, operation=None):
        if operation == "generate_plan":
            return _Resp(json.dumps(PLAN, ensure_ascii=False))
        if operation == "generate_code":
            return _Resp(json.dumps(CODE, ensure_ascii=False))
        return _Resp(json.dumps(CHAT, ensure_ascii=False))


fx = _Fx()
import llm.router as router_mod  # noqa: E402
router_mod.get_router = lambda force_rebuild=False: fx
import agents.code_agent as code_agent  # noqa: E402
code_agent.get_router = lambda: fx

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("HARNESS_PORT", "17894"))
    uvicorn.run(main.app, host="127.0.0.1", port=port, log_level="warning")
