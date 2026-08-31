#!/usr/bin/env python3
"""
골든패스 E2E 스모크 — 생성 → 배포가 아직 되는지 한 번에 확인한다. (회차4 통합)

## 왜 이게 있나

회차4쯤 되면 코드 수정이 잦아져서, 어제 되던 게 오늘 깨지는(회귀) 일이 생긴다.
그런데 이 제품의 핵심 흐름은 **여러 모듈에 걸쳐 있다**:

    채팅 요청 → /api/code/plan(설계 결정 카드) → 사람이 선택
             → /api/code/generate(코드 + ADR) → 파일 적용
             → /api/deploy/s3(정적 배포) → 공개 URL

단위 테스트는 각 조각이 혼자 잘 도는 것만 본다. 조각 사이의 **배선**이 끊기면
(예: plan 이 준 decisions 모양을 generate 가 안 받아들이면) 단위 테스트는 전부
초록인데 제품은 죽어 있다. 실제로 회차1 데모 당일까지 아무도 몰랐던 버그가
그런 종류였다. 이 스크립트는 그 배선만 본다.

"연기가 나는지만 빨리 확인"하는 수준의 가벼운 시험이다. 각 모듈의 세부 동작은
core/tests/ 의 단위 테스트가 담당한다. 여기서 겹쳐서 검사하지 않는다.

## 두 가지 모드

    python scripts/golden_path_smoke.py            # 기본 — 픽스처 LLM + moto S3
    python scripts/golden_path_smoke.py --live     # 실제 Bedrock + 실제 S3 버킷

**기본(모킹)** 은 비용 0, 몇 초면 끝난다. LLM 응답은 고정 픽스처로, S3 는 moto 로
대체한다. moto 는 stub 이 아니라 S3 API 를 실제로 구현하므로, PutBucketPolicy 나
PublicAccessBlock 순서가 틀리면 여기서도 걸린다. **PR 머지 전마다** 돌린다.

**--live** 는 진짜 모델을 부르고 진짜 버킷에 올린 뒤 공개 URL 에 HTTP 200 이
뜨는지까지 본다. 모킹이 절대 못 잡는 것(모델 접근 권한, IAM 권한, 리전 불일치,
버킷 공개 설정)을 잡는다. 대신 Bedrock 토큰 비용이 들고 AWS 자격증명이 필요하다.
**하루 1회 또는 발표 리허설 전**에만 수동으로 돌린다.

## 실패했을 때

각 단계에 번호가 붙어 있고, 깨진 단계에서 멈추면서 **무엇을 확인해야 하는지**를
같이 찍는다. "스모크 실패" 한 줄만 남기면 아무도 안 고치기 때문이다.

종료 코드: 성공 0, 실패 1.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
import traceback
from contextlib import contextmanager
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
CORE_DIR = REPO_ROOT / "core"

# core 는 자기 디렉터리를 루트로 임포트한다(schemas, main, code_agent ...).
# core/tests/conftest.py 와 같은 규칙.
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))


# ---------------------------------------------------------------------------
# 고정 요청 — 이 값이 흔들리면 스모크가 매번 다른 걸 검사하게 된다.
# ---------------------------------------------------------------------------

#: 정적 사이트로 못을 박는다. "할 일 앱"만 주면 모델이 Express 서버를 뱉을 수
#: 있는데, 그러면 S3 정적 배포 단계가 제품 결함이 아니라 요청 탓으로 깨진다.
INSTRUCTION = (
    "브라우저에서 바로 열리는 정적 할 일(To-do) 웹앱을 만들어줘. "
    "빌드 도구 없이 index.html 하나만 열면 동작해야 하고, 서버는 쓰지 않는다."
)

PROJECT_NAME = "recoder-golden-path-smoke"
REGION = "us-east-1"

#: S3 에 올릴 확장자. 문서(.md)·설정은 사이트가 아니므로 뺀다.
STATIC_SUFFIXES = {
    ".html", ".htm", ".css", ".js", ".mjs", ".json",
    ".svg", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".txt",
}


# ---------------------------------------------------------------------------
# 출력 — 어디서 깨졌는지가 이 스크립트의 전부다.
# ---------------------------------------------------------------------------

TOTAL_STEPS = 7


class SmokeFailure(Exception):
    """단계 실패. hint 는 '다음에 무엇을 볼지'를 담는다."""

    def __init__(self, step: int, title: str, detail: str, hint: str = ""):
        self.step = step
        self.title = title
        self.detail = detail
        self.hint = hint
        super().__init__(f"[{step}/{TOTAL_STEPS}] {title}: {detail}")


def _step(n: int, title: str) -> None:
    print(f"[{n}/{TOTAL_STEPS}] {title} ...", flush=True)


def _ok(msg: str) -> None:
    print(f"      OK  {msg}", flush=True)


def _fail(exc: SmokeFailure) -> None:
    print("", flush=True)
    print("=" * 68, flush=True)
    print(f"골든패스 스모크 실패 — [{exc.step}/{TOTAL_STEPS}] {exc.title}", flush=True)
    print("=" * 68, flush=True)
    print(f"무엇이: {exc.detail}", flush=True)
    if exc.hint:
        print("", flush=True)
        print("어디를 볼 것:", flush=True)
        for line in exc.hint.strip().splitlines():
            print(f"  - {line.strip()}", flush=True)
    print("", flush=True)


# ---------------------------------------------------------------------------
# 픽스처 LLM — 기본 모드에서 Bedrock 을 대신한다.
# ---------------------------------------------------------------------------


class _FixtureResponse:
    def __init__(self, text: str, model_used: str = "fixture-model"):
        self.text = text
        self.model_used = model_used


class _FixtureRouter:
    """operation 에 따라 정해둔 JSON 을 돌려주는 가짜 라우터.

    core/tests 의 _FakeRouter 는 호출 순서에 의존하는데, 여기서는 plan 과
    generate 가 서로 다른 시점에 불리므로 operation 으로 고른다. 순서가
    바뀌어도 스모크가 엉뚱하게 깨지지 않는다.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []

    def call(self, request, agent=None, operation=None):  # noqa: ANN001
        self.calls.append(operation or "")
        if operation == "generate_plan":
            return _FixtureResponse(json.dumps(_PLAN_FIXTURE, ensure_ascii=False))
        if operation == "generate_code":
            return _FixtureResponse(json.dumps(_CODE_FIXTURE, ensure_ascii=False))
        raise AssertionError(
            f"스모크가 예상하지 못한 LLM 호출: agent={agent!r} operation={operation!r}. "
            "골든패스에 새 LLM 단계가 생겼다면 이 스크립트에도 픽스처를 추가해야 한다."
        )


_PLAN_FIXTURE: dict[str, Any] = {
    "decisions": [
        {
            "id": "storage",
            "question": "할 일 데이터를 어디에 저장할까요?",
            "options": [
                {
                    "key": "local",
                    "label": "브라우저 localStorage",
                    "summary": "서버 없이 바로 동작",
                    "pros": ["정적 배포 가능", "설정 없음"],
                    "cons": ["기기 간 공유 불가"],
                    "recommended": True,
                },
                {
                    "key": "memory",
                    "label": "메모리만 (새로고침 시 사라짐)",
                    "summary": "가장 단순",
                    "pros": ["구현 최소"],
                    "cons": ["데이터 유실"],
                    "recommended": False,
                },
            ],
            "impact": "저장 방식이 앱 구조와 배포 형태를 정한다",
        },
    ],
}

_INDEX_HTML = """<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8" />
  <title>할 일</title>
  <link rel="stylesheet" href="style.css" />
</head>
<body>
  <h1>할 일</h1>
  <form id="add"><input id="text" placeholder="할 일" /><button>추가</button></form>
  <ul id="list"></ul>
  <script src="app.js"></script>
</body>
</html>
"""

_CODE_FIXTURE: dict[str, Any] = {
    "summary": "localStorage 기반 정적 할 일 앱을 만들었습니다.",
    "ops": [
        {
            "action": "create",
            "file": "index.html",
            "language": "html",
            "content": _INDEX_HTML,
            "rationale": "진입 문서",
        },
        {
            "action": "create",
            "file": "style.css",
            "language": "css",
            "content": "body{font-family:sans-serif;max-width:32rem;margin:2rem auto}\n",
            "rationale": "최소 스타일",
        },
        {
            "action": "create",
            "file": "app.js",
            "language": "javascript",
            "content": (
                "const KEY='todos';\n"
                "const load=()=>JSON.parse(localStorage.getItem(KEY)||'[]');\n"
                "const save=(v)=>localStorage.setItem(KEY,JSON.stringify(v));\n"
                "function render(){const ul=document.getElementById('list');"
                "ul.innerHTML='';load().forEach(t=>{const li=document.createElement('li');"
                "li.textContent=t;ul.appendChild(li);});}\n"
                "document.getElementById('add').addEventListener('submit',e=>{"
                "e.preventDefault();const i=document.getElementById('text');"
                "if(!i.value.trim())return;save([...load(),i.value.trim()]);"
                "i.value='';render();});\n"
                "render();\n"
            ),
            "rationale": "선택한 localStorage 저장 반영",
        },
    ],
}


# ---------------------------------------------------------------------------
# 단계
# ---------------------------------------------------------------------------


def _headers(token: str) -> dict[str, str]:
    return {"X-Session-Token": token}


def _route_hint(path: str) -> str:
    """404 는 '경로가 사라졌다'는 뜻이므로 볼 곳이 다르다."""
    return (
        f"404 라면 {path} 가 앱에 등록되지 않은 것이다 —\n"
        "  core/main.py 의 include_router 목록과 해당 라우트 모듈의 데코레이터 경로"
    )


def step1_boot_core():
    """[1/7] 앱을 만들고, 실행부가 lifespan 안에서 실제로 기동하게 한다."""
    _step(1, "코어 기동 및 라우트 등록 확인")
    try:
        import main  # type: ignore
    except Exception as exc:  # noqa: BLE001
        raise SmokeFailure(
            1, "코어 임포트 실패", str(exc),
            "core/requirements.txt 가 설치돼 있는지 (pip install -r core/requirements.txt)\n"
            "core/main.py 가 임포트 시점에 예외를 던지지 않는지",
        ) from exc

    try:
        app = main.create_app()
    except Exception as exc:  # noqa: BLE001
        raise SmokeFailure(
            1, "create_app() 실패", str(exc),
            "core/main.py 의 create_app — 라우터 하나가 임포트에 실패하면 여기서 죽는다",
        ) from exc

    return app


def step1_confirm_core(client) -> str:  # noqa: ANN001
    """lifespan 실행 뒤 health와 세션 토큰을 확인한다."""
    token = str(getattr(client.app.state, "session_token", "") or "")
    if not token:
        raise SmokeFailure(
            1, "코어 세션 토큰이 초기화되지 않음", "lifespan 뒤 app.state.session_token 이 비어 있음",
            "core/main.py 의 lifespan — 런타임 초기화와 세션 토큰 설정을 확인하세요",
        )

    # 라우트 등록 여부를 `app.routes` 를 뒤져서 확인하지 않는다. 그건 제품이 아니라
    # 프레임워크 내부 구조를 검사하는 것이라, Starlette/FastAPI 가 올라가면 제품은
    # 멀쩡한데 스모크만 깨진다. 경로가 사라졌는지는 뒤 단계에서 그 경로를 실제로
    # 불러 보면 404 로 드러나고, 그쪽이 진짜 증상에 가깝다.
    resp = client.get("/api/health")
    if resp.status_code != 200:
        raise SmokeFailure(
            1, "코어가 응답하지 않음", f"GET /api/health → HTTP {resp.status_code} — {resp.text[:200]}",
            "core/api/routes/health.py 가 앱에 붙어 있는지\n"
            "create_app 의 미들웨어(SessionTokenMiddleware, CSRF)가 면제 경로까지 막고 있지 않은지",
        )

    _ok("코어 기동 확인 (/api/health 200)")
    return token


@contextmanager
def _require_bedrock_in_live_mode(live: bool):
    """라이브 스모크에서는 Gemini 폴백을 끄고 Bedrock만 검증한다.

    제품의 일반 요청은 Bedrock 장애 시 Gemini로 넘어갈 수 있다. 하지만 이
    스크립트의 --live 표시는 Bedrock 접근을 검증한다는 약속이므로, 그 순간만
    Gemini 키를 분리하고 라우터 싱글턴도 다시 만든다.
    """
    if not live:
        yield
        return

    original_gemini_key = os.environ.pop("GEMINI_API_KEY", None)
    try:
        from llm.router import get_router
        get_router(force_rebuild=True)
        yield
    finally:
        if original_gemini_key is None:
            os.environ.pop("GEMINI_API_KEY", None)
        else:
            os.environ["GEMINI_API_KEY"] = original_gemini_key
        # 이후 같은 프로세스에서 실행되는 다른 작업이 Gemini 폴백을 잃지 않게 한다.
        try:
            from llm.router import get_router
            get_router(force_rebuild=True)
        except Exception:
            pass


def _require_bedrock_response(body: dict, step: int) -> None:
    """응답 메타데이터로도 실제 공급자가 Bedrock인지 명시적으로 확인한다."""
    provider = str(body.get("provider") or "").lower()
    if provider != "bedrock":
        raise SmokeFailure(
            step,
            "Bedrock 응답을 확인하지 못함",
            f"실제 provider={provider or '(없음)'!r}, model={body.get('model') or '(없음)'!r}",
            "--live 는 Bedrock 모델 접근 검증용이다. Gemini 폴백이나 다른 공급자 응답은 통과가 아니다.\n"
            "core/llm/router.py 의 provider 체인과 Bedrock 자격증명·모델 권한을 확인하세요.",
        )


def step2_plan(client, token: str, workspace: Path, *, require_bedrock: bool = False) -> list[dict]:
    """[2/7] 코드가 아니라 '설계 결정'이 오는지 본다 — AI-DLC 의 전제."""
    _step(2, "설계 결정 요청 (/api/code/plan)")
    resp = client.post(
        "/api/code/plan",
        json={"instruction": INSTRUCTION, "workspace_path": str(workspace)},
        headers=_headers(token),
    )
    if resp.status_code != 200:
        raise SmokeFailure(
            2, "plan 응답이 200 이 아님", f"HTTP {resp.status_code} — {resp.text[:400]}",
            f"{_route_hint('/api/code/plan')}\n"
            "core/api/routes/analyze.py 의 code_plan_route\n"
            "--live 라면 Bedrock 모델 접근 권한과 리전 (AccessDeniedException 여부)",
        )

    body = resp.json() or {}
    if require_bedrock:
        _require_bedrock_response(body, 2)
    decisions = body.get("decisions") or []
    if not decisions:
        raise SmokeFailure(
            2, "설계 결정이 비어 있음", "decisions 가 빈 목록",
            "code_agent.generate_plan 의 _extract_json — 모델이 JSON 이 아닌 산문을 뱉으면 여기가 빈다\n"
            "확장에서는 '결정 카드가 안 뜬다'로 보이는 증상과 같은 원인",
        )

    # 설계 결정이 하나도 안 나오면 generate_plan 이 '확인 카드'(__confirm__)로
    # 대체한다. 그건 오타 수정처럼 갈림길이 없는 요청을 위한 폴백이라 정상 동작이지만,
    # 골든패스에서는 다르다 — 이 스모크의 고정 요청은 저장 방식이라는 명백한 갈림길을
    # 담고 있다. 여기서 확인 카드가 나왔다면 AI-DLC 1단계가 사실상 작동하지 않은
    # 것이고, 그대로 두면 4단계에서 "ADR 이 없다"는 더 먼 곳의 증상으로만 드러난다.
    if any(str(d.get("id") or "").startswith("__") for d in decisions):
        raise SmokeFailure(
            2, "설계 결정 대신 확인 카드가 왔음",
            f"id={[d.get('id') for d in decisions]}",
            "모델이 결정을 못 뽑았거나, 뽑은 결정이 전부 형식 검사에서 걸러졌다\n"
            "code_agent.generate_plan 의 정규화 루프 — 선택지 2개 미만이면 결정이 통째로 빠진다\n"
            "code_agent._build_plan_prompt 가 요청의 갈림길을 짚어주는지",
        )

    for d in decisions:
        opts = d.get("options") or []
        if len(opts) < 2:
            raise SmokeFailure(
                2, "선택지가 부족한 결정 카드",
                f"id={d.get('id')!r} 의 options 가 {len(opts)}개",
                "선택지가 하나면 '사람이 고른다'는 AI-DLC 전제가 깨진다\n"
                "code_agent._build_plan_prompt 가 최소 2개를 요구하는지",
            )

    _ok(f"결정 {len(decisions)}건 수신 (선택지 {sum(len(d.get('options') or []) for d in decisions)}개)")
    return decisions


def step3_choose(decisions: list[dict]) -> list[dict]:
    """[3/7] 사람이 추천안을 승인한 상황을 만든다."""
    _step(3, "추천 선택지로 승인")
    approved: list[dict] = []
    for d in decisions:
        options = d.get("options") or []
        chosen = next((o for o in options if o.get("recommended")), options[0])
        key = (chosen.get("key") or "").strip()
        if not key:
            raise SmokeFailure(
                3, "선택지에 key 가 없음", f"id={d.get('id')!r}",
                "key 가 없으면 확장이 무엇을 골랐는지 generate 에 전달할 수 없다\n"
                "code_agent 의 결정 정규화(_clean_str_list / _offered_key_list)",
            )
        # 확장이 보내는 모양 그대로 — 원본 결정(options 포함) + chosen_key.
        # options 를 빼면 서버가 '제시된 목록에 있는 key 인지' 검증할 수 없어
        # APPROVAL_INVALID 로 거절된다.
        approved.append({**d, "chosen_key": key})

    _ok(", ".join(f"{d.get('id')}={d['chosen_key']}" for d in approved))
    return approved


def step4_generate(
    client, token: str, workspace: Path, decisions: list[dict], *, require_bedrock: bool = False,
) -> dict:
    """[4/7] 승인된 결정이 실제로 코드와 ADR 로 이어지는지 본다."""
    _step(4, "코드 생성 (/api/code/generate)")
    resp = client.post(
        "/api/code/generate",
        json={
            "instruction": INSTRUCTION,
            "workspace_path": str(workspace),
            "decisions": decisions,
        },
        headers=_headers(token),
    )
    if resp.status_code != 200:
        raise SmokeFailure(
            4, "generate 응답이 200 이 아님", f"HTTP {resp.status_code} — {resp.text[:400]}",
            f"{_route_hint('/api/code/generate')}\n"
            "400 이고 '승인 내용이 온전하지 않아' 라면 plan 과 generate 의 decisions 계약이 어긋난 것 —\n"
            "  code_agent._approval_state / _decision_is_valid 를 볼 것 (배선 끊김의 대표 증상)\n"
            "500 이면 code_agent.generate_code 내부 예외",
        )

    result = resp.json() or {}
    if require_bedrock:
        _require_bedrock_response(result, 4)
    ops = result.get("ops") or []
    if not ops:
        raise SmokeFailure(
            4, "생성된 파일 작업이 없음", "ops 가 빈 목록",
            "code_agent.generate_code 의 ops 파싱 — action/file/content 중 하나라도 비면 걸러진다",
        )

    adr = result.get("adr") or []
    if not adr:
        raise SmokeFailure(
            4, "ADR 이 생성되지 않음", "응답에 adr 항목이 없음",
            "승인된 결정은 docs/adr/ADR-NNN-*.md 로 남아야 한다 (AI-DLC 의 '기록' 축)\n"
            "code_agent.build_adr_ops 가 norm_decisions 를 못 받았을 가능성",
        )

    _ok(f"ops {len(ops)}건, ADR {len(adr)}건 ({', '.join(adr)})")
    return result


def step5_apply(workspace: Path, result: dict) -> list[dict]:
    """[5/7] ops 를 실제 파일로 적용한다 — 확장이 하는 일을 대신한다."""
    _step(5, "파일 적용 및 정적 산출물 확인")
    written: list[str] = []
    workspace_root = workspace.resolve()
    for op in result.get("ops") or []:
        rel = (op.get("file") or "").strip().lstrip("/")
        if not rel:
            continue
        target = (workspace_root / rel).resolve()
        # 워크스페이스 밖으로 쓰려는 op 는 제품 결함이다. 조용히 넘기지 않는다.
        # 문자열 접두사 비교는 `workspace-escaped` 같은 형제 폴더까지 통과시킨다.
        # resolve한 경로의 실제 조상 중에 root가 있는지 확인해야 한다.
        if target != workspace_root and workspace_root not in target.parents:
            raise SmokeFailure(
                5, "워크스페이스 밖으로 파일을 쓰려 함", rel,
                "code_agent 의 경로 정규화 — 사용자 프로젝트 밖을 건드리면 안 된다",
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(str(op.get("content") or ""), encoding="utf-8")
        written.append(rel)

    index = workspace / "index.html"
    if not index.exists():
        raise SmokeFailure(
            5, "index.html 이 없음", f"생성된 파일: {written}",
            "고정 요청은 '정적 웹앱'을 명시한다. 서버형 산출물이 나왔다면\n"
            "  code_agent._build_code_prompt 가 결정(정적 저장)을 반영하지 못한 것",
        )

    adr_files = sorted(p.relative_to(workspace).as_posix()
                       for p in (workspace / "docs" / "adr").glob("ADR-*.md")) \
        if (workspace / "docs" / "adr").exists() else []
    if not adr_files:
        raise SmokeFailure(
            5, "ADR 파일이 디스크에 없음", "docs/adr/ADR-*.md 없음",
            "generate 응답에는 adr 이 있었는데 파일이 없다면 ops 에 ADR op 가 빠진 것",
        )

    static_files = [
        {"path": p.relative_to(workspace).as_posix(),
         "content": p.read_text(encoding="utf-8")}
        for p in sorted(workspace.rglob("*"))
        if p.is_file() and p.suffix.lower() in STATIC_SUFFIXES
    ]

    _ok(f"파일 {len(written)}건 적용, 정적 자산 {len(static_files)}건, ADR {len(adr_files)}건")
    return static_files


def step6_deploy(client, token: str, static_files: list[dict]) -> dict:
    """[6/7] 정적 산출물을 S3 에 올린다."""
    _step(6, "S3 정적 배포 (/api/deploy/s3)")
    resp = client.post(
        "/api/deploy/s3",
        json={"project": PROJECT_NAME, "region": REGION, "files": static_files},
        headers=_headers(token),
    )
    if resp.status_code != 200:
        raise SmokeFailure(
            6, "배포 응답이 200 이 아님", f"HTTP {resp.status_code} — {resp.text[:400]}",
            f"{_route_hint('/api/deploy/s3')}\n"
            "400 이면 요청(빈 project, 리전 미확정) — core/s3_byo.py 의 검사\n"
            "502 면 AWS 호출 거절 — --live 에서는 IAM 권한(s3:CreateBucket, PutBucketPolicy,\n"
            "  PutPublicAccessBlock, PutObject)과 계정 차원 공개 차단 설정을 볼 것\n"
            "core/api/routes/deploy_s3.py",
        )

    body = resp.json() or {}
    url = (body.get("url") or "").strip()
    if not url or body.get("status") != "deployed":
        raise SmokeFailure(
            6, "배포 결과가 온전하지 않음", json.dumps(body, ensure_ascii=False)[:400],
            "S3DeployResponse 의 status/url — url 이 비면 확장이 링크를 못 보여준다",
        )

    _ok(f"{body.get('bucket')} ({body.get('region')}) — {url}")
    return body


def step7_verify(body: dict, static_files: list[dict], live: bool) -> None:
    """[7/7] 올라간 것이 실제로 열리는지(또는 그대로 있는지) 본다."""
    _step(7, "공개 URL 확인" if live else "업로드 결과 확인 (moto)")

    expected = next((f["content"] for f in static_files if f["path"] == "index.html"), "")

    if live:
        import urllib.error
        import urllib.request

        url = body["url"]
        # S3 웹사이트 엔드포인트는 버킷 생성 직후 DNS 전파에 몇 초 걸린다.
        last = ""
        for attempt in range(1, 13):
            try:
                with urllib.request.urlopen(url, timeout=10) as r:  # noqa: S310
                    if r.status == 200:
                        got = r.read().decode("utf-8", "replace")
                        if "<html" not in got.lower():
                            raise SmokeFailure(
                                7, "200 인데 내용이 HTML 이 아님", got[:200],
                                "index.html 이 text/html 로 올라갔는지 (ContentType)",
                            )
                        _ok(f"HTTP 200 · {len(got)} bytes · {url}")
                        return
                    last = f"HTTP {r.status}"
            except urllib.error.HTTPError as exc:
                last = f"HTTP {exc.code}"
                if exc.code == 403:
                    raise SmokeFailure(
                        7, "공개 URL 이 403", f"{url} — 업로드는 됐는데 링크가 막혔다",
                        "PublicAccessBlock 을 풀기 전에 PutBucketPolicy 를 부르면 이 상태가 된다\n"
                        "core/api/routes/deploy_s3.py 의 호출 순서\n"
                        "계정 차원 S3 공개 차단(Block Public Access)이 켜져 있는지도 확인",
                    ) from exc
            except Exception as exc:  # noqa: BLE001
                last = str(exc)
            time.sleep(5)

        raise SmokeFailure(
            7, "공개 URL 에서 200 을 못 받음", f"{url} — 마지막 응답: {last} (60초 대기)",
            "정적 웹사이트 호스팅(PutBucketWebsite)이 설정됐는지\n"
            "s3_byo.website_url 의 리전별 구분자(-/.)가 맞는지",
        )

    # 모킹 모드 — moto 는 웹사이트 엔드포인트를 서빙하지 않는다. HTTP 200 대신
    # "올라간 객체가 우리가 만든 그 파일인지"를 S3 API 로 되받아 확인한다.
    # 여기까지 통과하면 남은 실패 경로는 실제 AWS 권한·공개 설정뿐이고,
    # 그건 --live 가 본다.
    import boto3  # type: ignore

    s3 = boto3.client("s3", region_name=body["region"])
    try:
        got = s3.get_object(Bucket=body["bucket"], Key="index.html")
    except Exception as exc:  # noqa: BLE001
        raise SmokeFailure(
            7, "업로드된 index.html 을 되읽지 못함", str(exc),
            "배포 응답은 성공인데 객체가 없다 — deploy_s3 의 put_object 키 정규화",
        ) from exc

    payload = got["Body"].read().decode("utf-8", "replace")
    if payload != expected:
        raise SmokeFailure(
            7, "올라간 내용이 생성한 내용과 다름",
            f"업로드 {len(payload)}B vs 생성 {len(expected)}B",
            "s3_byo 의 파일 인코딩 처리 (텍스트/base64)",
        )
    ctype = got.get("ContentType", "")
    if not ctype.startswith("text/html"):
        raise SmokeFailure(
            7, "index.html 의 ContentType 이 text/html 이 아님", ctype or "(없음)",
            "브라우저가 렌더 대신 다운로드한다 — s3_byo 의 content_type 추론",
        )

    url = body["url"]
    if not url.startswith("http://") or body["bucket"] not in url or "s3-website" not in url:
        raise SmokeFailure(
            7, "공개 URL 형식이 이상함", url,
            "s3_byo.website_url — 리전별 구분자(-/.)와 DNS 접미사",
        )

    _ok(f"index.html {len(payload)}B · {ctype} · URL 형식 정상")


# ---------------------------------------------------------------------------
# 정리 — --live 가 남긴 버킷은 반드시 지운다. 공개 버킷이 쌓이면 보안 문제다.
# ---------------------------------------------------------------------------


def _cleanup_bucket(bucket: str, region: str) -> None:
    try:
        import boto3  # type: ignore

        s3 = boto3.client("s3", region_name=region)
        try:
            s3.head_bucket(Bucket=bucket)
        except Exception as exc:  # noqa: BLE001
            # 배포 API가 버킷을 만들기 전 실패한 경우에는 정리할 것이 없다.
            code = str(getattr(exc, "response", {}).get("Error", {}).get("Code", ""))
            status = getattr(exc, "response", {}).get("ResponseMetadata", {}).get("HTTPStatusCode")
            if code in {"NoSuchBucket", "NotFound"} or status == 404:
                return
            raise
        while True:
            listed = s3.list_objects_v2(Bucket=bucket)
            keys = [{"Key": o["Key"]} for o in listed.get("Contents", [])]
            if not keys:
                break
            s3.delete_objects(Bucket=bucket, Delete={"Objects": keys})
            if not listed.get("IsTruncated"):
                break
        s3.delete_bucket(Bucket=bucket)
        print(f"      정리  버킷 삭제: {bucket}", flush=True)
    except Exception as exc:  # noqa: BLE001
        # 정리 실패가 스모크 결과를 뒤집으면 안 된다. 대신 반드시 알린다.
        print(
            f"      경고  버킷 정리 실패: {bucket} ({exc})\n"
            f"            공개 버킷이 남았다. 손으로 지울 것: "
            f"aws s3 rb s3://{bucket} --force",
            flush=True,
        )


def _live_cleanup_target() -> tuple[str, str]:
    """라이브 스모크가 만들 버킷을 배포 **전에** 확정한다.

    /api/deploy/s3는 버킷을 만든 뒤 공개 설정·업로드 중에 실패할 수 있다.
    성공 응답을 받은 뒤에만 버킷 이름을 저장하면 그 실패 경로에서는 공개
    버킷이 남는다. 같은 입력으로 정해지는 이름을 먼저 계산하고, 기존 버킷이
    없다는 것도 확인한 뒤 finally에서 항상 정리한다.
    """
    import boto3  # type: ignore
    from botocore.exceptions import BotoCoreError, ClientError  # type: ignore

    try:
        session = boto3.session.Session(region_name=REGION)
        account_id = session.client("sts").get_caller_identity()["Account"]
        # core와 같은 모듈을 사용해 버킷 이름 규칙이 어긋나지 않게 한다.
        import s3_byo  # type: ignore

        bucket = s3_byo.bucket_name(PROJECT_NAME, account_id)
        s3 = session.client("s3", region_name=REGION)
        try:
            s3.head_bucket(Bucket=bucket)
        except ClientError as exc:
            status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if status == 404:
                return bucket, REGION
            raise
    except (ClientError, BotoCoreError, KeyError) as exc:
        raise SmokeFailure(
            6,
            "라이브 스모크 버킷 사전 확인 실패",
            str(exc),
            "S3 배포 전에 정리 대상을 확인하지 못해 실행하지 않았다. AWS 자격증명과 s3:ListBucket 권한을 확인하세요.",
        ) from exc

    # 기존 버킷은 이 실행이 만든 자원이 아니므로 삭제하면 안 된다. 남은 이전
    # 라이브 스모크 버킷도 여기서 발견되어, 먼저 수동 정리하도록 멈춘다.
    raise SmokeFailure(
        6,
        "기존 라이브 스모크 버킷이 남아 있음",
        bucket,
        "이 실행은 기존 버킷을 삭제하지 않는다. 남은 버킷을 확인·정리한 뒤 다시 실행하세요.",
    )


# ---------------------------------------------------------------------------
# 실행
# ---------------------------------------------------------------------------


def _isolate_stores() -> Path:
    """기록 저장소를 임시 경로로 돌린다 — 두 모드 모두.

    기본 경로가 `~/.recoder/adr_reservations.json` 이라, 막지 않으면 스모크가
    돌 때마다 개발자의 **진짜** ADR 번호 장부에 번호를 예약한다. 스모크가
    만드는 ADR 은 버려지는 것이므로 실제 번호를 태우면 안 된다.
    core/tests/conftest.py 가 같은 이유로 같은 일을 한다.
    """
    tmp = Path(tempfile.mkdtemp(prefix="recoder-smoke-store-"))
    os.environ["RECODER_ADR_STORE"] = str(tmp / "adr_reservations.json")
    os.environ["RECODER_ECS_STORE"] = str(tmp / "ecs_deployments.json")
    return tmp


def run(live: bool) -> int:
    workspace = Path(tempfile.mkdtemp(prefix="recoder-smoke-ws-"))
    cleanup_target: tuple[str, str] | None = None

    print(f"골든패스 스모크 — 모드: {'LIVE (실제 Bedrock + 실제 S3)' if live else '기본 (픽스처 LLM + moto S3)'}")
    print(f"워크스페이스: {workspace}")
    print("")

    try:
        app = step1_boot_core()
        # TestClient는 context manager로 써야 FastAPI lifespan을 실행한다.
        # 그래야 런타임 초기화·싱글턴 획득이 깨져도 [1/7]에서 잡힌다.
        from fastapi.testclient import TestClient
        with _require_bedrock_in_live_mode(live):
            with TestClient(app, raise_server_exceptions=False, client=("127.0.0.1", 5555)) as client:
                token = step1_confirm_core(client)
                decisions = step2_plan(client, token, workspace, require_bedrock=live)
                approved = step3_choose(decisions)
                result = step4_generate(client, token, workspace, approved, require_bedrock=live)
                static_files = step5_apply(workspace, result)
                # 실패 응답에도 cleanup_target은 남아 finally에서 실제 버킷을 지운다.
                if live:
                    cleanup_target = _live_cleanup_target()
                deployed = step6_deploy(client, token, static_files)
                step7_verify(deployed, static_files, live)
    except SmokeFailure as exc:
        _fail(exc)
        return 1
    except Exception as exc:  # noqa: BLE001
        print("", flush=True)
        print("=" * 68, flush=True)
        print("골든패스 스모크 실패 — 스모크 스크립트 자체에서 예외", flush=True)
        print("=" * 68, flush=True)
        traceback.print_exc()
        print("", flush=True)
        print(f"예외: {exc}", flush=True)
        return 1
    finally:
        if cleanup_target:
            _cleanup_bucket(*cleanup_target)
        shutil.rmtree(workspace, ignore_errors=True)

    print("")
    print("골든패스 통과 — 생성부터 배포까지 배선이 살아 있다.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="골든패스 E2E 스모크 (생성 → 배포)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "예:\n"
            "  python scripts/golden_path_smoke.py          # PR 머지 전. 비용 0\n"
            "  python scripts/golden_path_smoke.py --live   # 하루 1회 / 리허설 전\n"
        ),
    )
    parser.add_argument(
        "--live", action="store_true",
        help="실제 Bedrock 과 실제 S3 버킷을 사용한다 (AWS 자격증명 필요, 토큰 비용 발생)",
    )
    args = parser.parse_args()

    tmp_store = _isolate_stores()

    if args.live:
        has_env_keys = all(
            os.environ.get(k) for k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY")
        )
        # 프로필/SSO 로도 자격증명이 잡히므로 없다고 막지는 않는다. 경고만 한다.
        if not has_env_keys and not os.environ.get("AWS_PROFILE"):
            print(
                "경고: AWS 자격증명 환경변수도 AWS_PROFILE 도 없다. "
                "boto3 기본 체인에 의존한다.\n",
                flush=True,
            )
        try:
            return run(live=True)
        finally:
            shutil.rmtree(tmp_store, ignore_errors=True)

    # ── 기본(모킹) 모드 ────────────────────────────────────────────────
    for key, value in {
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SESSION_TOKEN": "testing",
        "AWS_DEFAULT_REGION": REGION,
        "AWS_REGION": REGION,
    }.items():
        os.environ[key] = value
    os.environ.pop("AWS_PROFILE", None)

    try:
        from moto import mock_aws  # type: ignore
    except ImportError:
        print(
            "moto 가 없어 기본 모드를 돌릴 수 없다. 조용히 통과시키지 않는다.\n"
            "  pip install -r core/requirements-dev.txt\n"
            "실제 AWS 로 돌리려면 --live 를 쓸 것.",
            flush=True,
        )
        return 1

    import code_agent  # type: ignore

    router = _FixtureRouter()
    original = code_agent.get_router
    code_agent.get_router = lambda: router  # type: ignore[assignment]
    try:
        with mock_aws():
            code = run(live=False)
    finally:
        code_agent.get_router = original  # type: ignore[assignment]
        shutil.rmtree(tmp_store, ignore_errors=True)

    if code == 0 and router.calls != ["generate_plan", "generate_code"]:
        # 픽스처가 안 불렸다면 골든패스가 LLM 을 안 거친 것이다 — 통과로 보면 안 된다.
        print(
            f"\n골든패스 스모크 실패 — 예상한 LLM 단계가 실행되지 않았다: {router.calls}\n"
            "  plan 과 generate 가 각각 한 번씩 불려야 한다.",
            flush=True,
        )
        return 1
    return code


if __name__ == "__main__":
    sys.exit(main())
