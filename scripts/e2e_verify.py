#!/usr/bin/env python3
"""
E2E 통합 검증 — 개발 → 검사 → 배포 → 감시 → 롤백 (회차4 · 공통)

## 왜 이게 있나

골든패스 스모크(`golden_path_smoke.py`)는 **생성 → 배포**까지만 본다. 그 뒤의
검사·감시·롤백은 각자 단위 테스트가 있을 뿐, **이어서 돌려본 적이 없었다.**

그 결과 실제로 이런 구멍이 있었다: `DeployAgent.create_plan()` 이
`rollback_image=None` 을 하드코딩하고 아무도 채우지 않아, **모든 로컬 Docker
배포가 되돌릴 수 없는 상태**였다(`/api/deploy/rollback` 이 항상 422). 배포
라우트도 롤백 라우트도 각자의 규칙은 정확히 지켰기 때문에 단위 테스트는
전부 초록이었다. 사이가 비어 있다는 건 이어 돌려야만 보인다.

이 스크립트는 그 "사이"를 본다. 각 모듈의 세부 동작은 `core/tests/` 가 맡는다.

## 무엇을 실제로 돌리는가

로컬 Docker 를 쓴다. 감시(watchdog)와 롤백이 **실제로 구현된 경로**가 여기라서다.
컨테이너를 띄우고, 일부러 깨진 버전을 덮어 배포하고, 되돌린다. AWS 비용은 0.

    python scripts/e2e_verify.py            # 전 구간
    python scripts/e2e_verify.py --keep     # 끝나고 컨테이너·이미지를 지우지 않음

Docker Desktop 이 켜져 있어야 한다.

## 단계

    1  코어 기동
    2  개발      plan → 추천 승인 → generate → 파일 적용
    3  인프라    /api/deploy/dockerfile → approve (제품이 만든 Dockerfile 을 그대로 쓴다)
    4  검사      /api/deploy/preflight (차단 여부) + /api/deploy/scan
    5  배포 v1   docker build → /api/deploy/plan → /api/deploy/execute → 응답 확인
    6  감시      continuous verification 상태 + 실제 HTTP 헬스
    7  재배포 v2 깨진 버전 배포. **이때 롤백 대상이 v1 로 잡혀야 한다**
    8  롤백      /api/deploy/rollback → v1 으로 복귀 + HTTP 200 회복

7단계가 이 카드의 핵심이다. 롤백은 "되돌릴 곳을 알고 있을 때"만 의미가 있고,
그 값이 비어 있던 것이 위에서 말한 구멍이다.

종료 코드: 성공 0, 실패 1.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
CORE_DIR = REPO_ROOT / "core"

if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))


# ---------------------------------------------------------------------------
# 고정값 — 흔들리면 매번 다른 것을 검사하게 된다.
# ---------------------------------------------------------------------------

INSTRUCTION = (
    "파이썬 표준 라이브러리만 써서 아주 작은 HTTP 서버를 만들어줘. "
    "8080 포트에서 듣고, /health 는 200 과 함께 ok 를 돌려주고, / 는 인사말을 보여준다. "
    "외부 패키지는 쓰지 않는다."
)

CONTAINER = "recoder-e2e"
IMAGE_V1 = "recoder-e2e:v1"
IMAGE_V2 = "recoder-e2e:v2"
HOST_PORT = 18080
CONTAINER_PORT = 8080
HEALTH_URL = f"http://127.0.0.1:{HOST_PORT}/health"

TOTAL_STEPS = 8


# ---------------------------------------------------------------------------
# 출력
# ---------------------------------------------------------------------------


class StepFailure(Exception):
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


def _note(msg: str) -> None:
    print(f"      ·   {msg}", flush=True)


def _report(exc: StepFailure) -> None:
    print("", flush=True)
    print("=" * 68, flush=True)
    print(f"E2E 통합 검증 실패 — [{exc.step}/{TOTAL_STEPS}] {exc.title}", flush=True)
    print("=" * 68, flush=True)
    print(f"무엇이: {exc.detail}", flush=True)
    if exc.hint:
        print("", flush=True)
        print("어디를 볼 것:", flush=True)
        for line in exc.hint.strip().splitlines():
            print(f"  - {line.strip()}", flush=True)
    print("", flush=True)


# ---------------------------------------------------------------------------
# docker 얇은 래퍼
# ---------------------------------------------------------------------------


def _docker(*args: str, timeout: int = 180) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _running_image(container: str) -> str:
    """지금 그 컨테이너가 실제로 돌리고 있는 이미지 태그. 없으면 빈 문자열.

    롤백이 정말 됐는지는 API 응답이 아니라 **docker 가 뭘 돌리고 있는지**로
    확인해야 한다. 응답만 보면 "롤백했다"고 말해 놓고 컨테이너는 그대로인
    경우를 못 잡는다.
    """
    res = _docker("inspect", "-f", "{{.Config.Image}}", container, timeout=30)
    return res.stdout.strip() if res.returncode == 0 else ""


def _http(url: str, timeout: int = 5) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:  # noqa: S310
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, ""
    except Exception:
        return 0, ""


def _container_diagnosis() -> str:
    """컨테이너가 왜 안 뜨는지를 실패 메시지에 **그 자리에서** 담는다.

    "docker logs 를 보라"고 안내해 봐야 정리 단계가 컨테이너를 이미 지운 뒤다.
    실패한 순간에 찍어야 사람이 볼 수 있다.
    """
    parts: list[str] = []
    ps = _docker("ps", "-a", "--filter", f"name={CONTAINER}",
                 "--format", "{{.Status}} / {{.Image}} / {{.Ports}}", timeout=30)
    parts.append(f"컨테이너 상태: {ps.stdout.strip() or '(없음)'}")
    logs = _docker("logs", "--tail", "30", CONTAINER, timeout=30)
    tail = ((logs.stdout or "") + (logs.stderr or "")).strip()
    parts.append("컨테이너 로그(마지막 30줄):\n" + (tail or "(비어 있음)"))
    return "\n".join(parts)


def _wait_healthy(url: str, seconds: int = 30) -> tuple[int, str]:
    deadline = time.time() + seconds
    last = (0, "")
    while time.time() < deadline:
        last = _http(url)
        if last[0] == 200:
            return last
        time.sleep(1)
    return last


# ---------------------------------------------------------------------------
# 픽스처 LLM — 개발 단계를 결정적으로 만든다
# ---------------------------------------------------------------------------


class _FixtureResponse:
    def __init__(self, text: str):
        self.text = text
        self.model_used = "fixture-model"


class _FixtureRouter:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def call(self, request, agent=None, operation=None):  # noqa: ANN001
        self.calls.append(operation or "")
        if operation == "generate_plan":
            return _FixtureResponse(json.dumps(_PLAN_FIXTURE, ensure_ascii=False))
        if operation == "generate_code":
            return _FixtureResponse(json.dumps(_CODE_FIXTURE, ensure_ascii=False))
        raise AssertionError(
            f"예상하지 못한 LLM 호출: agent={agent!r} operation={operation!r}. "
            "이 경로에 새 LLM 단계가 생겼다면 픽스처를 추가해야 한다."
        )


_PLAN_FIXTURE: dict[str, Any] = {
    "decisions": [
        {
            "id": "server",
            "question": "서버를 무엇으로 띄울까요?",
            "options": [
                {
                    "key": "stdlib",
                    "label": "표준 라이브러리 http.server",
                    "summary": "의존성 없이 바로 실행",
                    "pros": ["설치 불필요", "이미지가 작다"],
                    "cons": ["기능이 최소"],
                    "recommended": True,
                },
                {
                    "key": "framework",
                    "label": "웹 프레임워크",
                    "summary": "기능은 많지만 의존성이 붙는다",
                    "pros": ["확장 용이"],
                    "cons": ["설치 필요"],
                    "recommended": False,
                },
            ],
            "impact": "이미지 크기와 빌드 시간에 영향",
        },
    ],
}

_APP_PY = '''"""E2E 검증용 최소 FastAPI 앱.

제품이 가진 Dockerfile 템플릿은 node-express / node-next / python-fastapi /
python-flask 넷뿐이다. 순수 파이썬 스크립트용 템플릿은 없고, 스택 감지는 그런
프로젝트도 python-fastapi 로 떨어뜨린다(별도 결함으로 기록). E2E 는 제품이
**지원하는 경로**를 검증해야 하므로 FastAPI 앱을 쓴다.
"""
import os

from fastapi import FastAPI

VERSION = "v1"
#: 릴리스 계약(recoder.yml)의 required_env 기본값이 PORT 다.
PORT = int(os.environ.get("PORT", "8080"))

app = FastAPI()


@app.get("/health")
def health():
    return "ok"


@app.get("/")
def root():
    return {"message": f"hello from {VERSION}"}
'''

_REQUIREMENTS = "fastapi>=0.111.0\nuvicorn[standard]>=0.29.0\n"

_CODE_FIXTURE: dict[str, Any] = {
    "summary": "표준 라이브러리 HTTP 서버를 만들었습니다.",
    "ops": [
        {
            "action": "create",
            "file": "main.py",
            "language": "python",
            "content": _APP_PY,
            "rationale": "진입점",
        },
        {
            "action": "create",
            "file": "requirements.txt",
            "language": "text",
            "content": _REQUIREMENTS,
            "rationale": "런타임 의존성",
        },
    ],
}

#: v2 는 일부러 깨뜨린다 — 시작하자마자 죽는다. 롤백이 실제로 필요한 상황을 만든다.
_BROKEN_APP_PY = '''"""E2E 검증용 — 일부러 깨진 버전 (v2). 임포트 시점에 죽는다."""

raise RuntimeError("v2: 기동 실패를 흉내낸다")
'''


# ---------------------------------------------------------------------------
# 단계
# ---------------------------------------------------------------------------


def _headers(token: str) -> dict[str, str]:
    return {"X-Session-Token": token}


def step1_boot():
    _step(1, "코어 기동")
    if shutil.which("docker") is None:
        raise StepFailure(
            1, "docker 명령을 찾을 수 없음", "PATH 에 docker 가 없다",
            "Docker Desktop 이 설치·실행 중인지",
        )
    res = _docker("info", timeout=30)
    if res.returncode != 0:
        raise StepFailure(
            1, "Docker 데몬에 연결할 수 없음", (res.stderr or res.stdout).strip()[:300],
            "Docker Desktop 을 켤 것. 이 검증은 실제로 컨테이너를 띄운다",
        )

    try:
        import main  # type: ignore
        from fastapi.testclient import TestClient
    except Exception as exc:  # noqa: BLE001
        raise StepFailure(
            1, "코어 임포트 실패", str(exc),
            "pip install -r core/requirements.txt",
        ) from exc

    app = main.create_app()
    token = "e" * 32
    app.state.session_token = token
    client = TestClient(app, raise_server_exceptions=False, client=("127.0.0.1", 5555))

    resp = client.get("/api/health")
    if resp.status_code != 200:
        raise StepFailure(
            1, "코어가 응답하지 않음", f"GET /api/health → {resp.status_code}",
            "core/api/routes/health.py 와 create_app 의 미들웨어",
        )
    _ok("Docker 연결됨, 코어 /api/health 200")
    return client, token


def step2_develop(client, token: str, ws: Path) -> None:
    _step(2, "개발 (plan → 승인 → generate → 파일 적용)")

    resp = client.post(
        "/api/code/plan",
        json={"instruction": INSTRUCTION, "workspace_path": str(ws)},
        headers=_headers(token),
    )
    if resp.status_code != 200:
        raise StepFailure(
            2, "plan 응답이 200 이 아님", f"HTTP {resp.status_code} — {resp.text[:300]}",
            "core/api/routes/analyze.py 의 code_plan_route",
        )
    decisions = (resp.json() or {}).get("decisions") or []
    if not decisions or any(str(d.get("id", "")).startswith("__") for d in decisions):
        raise StepFailure(
            2, "설계 결정이 오지 않음", f"decisions={[d.get('id') for d in decisions]}",
            "확인 카드(__confirm__)로 대체됐다면 결정이 전부 형식 검사에서 걸러진 것",
        )

    approved = []
    for d in decisions:
        opts = d.get("options") or []
        chosen = next((o for o in opts if o.get("recommended")), opts[0])
        approved.append({**d, "chosen_key": chosen.get("key")})

    resp = client.post(
        "/api/code/generate",
        json={
            "instruction": INSTRUCTION,
            "workspace_path": str(ws),
            "decisions": approved,
        },
        headers=_headers(token),
    )
    if resp.status_code != 200:
        raise StepFailure(
            2, "generate 응답이 200 이 아님", f"HTTP {resp.status_code} — {resp.text[:300]}",
            "400 이고 '승인 내용이 온전하지 않아' 라면 plan↔generate 계약이 어긋난 것 —\n"
            "  code_agent._approval_state / _decision_is_valid",
        )

    ops = (resp.json() or {}).get("ops") or []
    for op in ops:
        rel = (op.get("file") or "").strip().lstrip("/")
        if not rel:
            continue
        target = (ws / rel).resolve()
        if not str(target).startswith(str(ws.resolve())):
            raise StepFailure(5, "워크스페이스 밖으로 쓰려 함", rel, "code_agent 경로 정규화")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(str(op.get("content") or ""), encoding="utf-8")

    if not (ws / "main.py").exists():
        raise StepFailure(
            2, "진입점(main.py)이 생성되지 않음", f"생성된 파일: {[o.get('file') for o in ops]}",
            "고정 요청은 표준 라이브러리 HTTP 서버를 명시한다",
        )
    _ok(f"ops {len(ops)}건 적용, main.py 확인")


def step3_infra(client, token: str, ws: Path) -> str:
    """제품이 만든 Dockerfile 을 그대로 쓴다. 못 쓰면 그게 결함이다."""
    _step(3, "인프라 파일 생성 (/api/deploy/dockerfile)")

    resp = client.post(
        "/api/deploy/dockerfile",
        json={"workspace_path": str(ws)},
        headers=_headers(token),
    )
    if resp.status_code != 200:
        raise StepFailure(
            3, "Dockerfile 생성이 200 이 아님", f"HTTP {resp.status_code} — {resp.text[:300]}",
            "core/api/routes/deploy.py 의 generate_dockerfile\n"
            "AI 실패는 템플릿 폴백으로 처리돼야 하고 500 이 나오면 안 된다",
        )
    proposal = resp.json() or {}
    pid = proposal.get("proposal_id")
    content = proposal.get("content") or ""
    if not pid or not content.strip():
        raise StepFailure(
            3, "제안이 비어 있음", json.dumps(proposal, ensure_ascii=False)[:300],
            "InfraFileProposal.content 가 렌더되지 않았다 (템플릿 폴백 확인)",
        )

    resp = client.post(
        "/api/deploy/dockerfile/approve",
        params={"proposal_id": pid, "approved": True, "workspace_path": str(ws)},
        headers=_headers(token),
    )
    if resp.status_code != 200:
        raise StepFailure(
            3, "승인이 200 이 아님", f"HTTP {resp.status_code} — {resp.text[:300]}",
            "approve_dockerfile — workspace_path 를 안 넘기면 core/ 아래에 쓰는 버그가 있었다",
        )

    dockerfile = ws / "Dockerfile"
    if not dockerfile.exists():
        raise StepFailure(
            3, "Dockerfile 이 워크스페이스에 없음", f"기대 경로: {dockerfile}",
            "승인은 성공했는데 파일이 없다 — target_path 해석이 워크스페이스 기준인지",
        )
    text = dockerfile.read_text(encoding="utf-8")
    # 컨테이너가 실제로 듣는 포트는 **제품이 만든 Dockerfile**이 정한다.
    # 우리가 8080 이라고 가정하고 포트를 매핑하면, 템플릿이 다른 포트를 쓸 때
    # "배포는 성공인데 접속이 안 됨" 이 되고 원인이 포트라는 걸 알기 어렵다.
    global CONTAINER_PORT
    m = re.search(r"(?m)^EXPOSE\s+(\d+)", text)
    if m:
        CONTAINER_PORT = int(m.group(1))
    _ok(f"Dockerfile {len(text)}B 작성 (EXPOSE {CONTAINER_PORT})")
    return text


def _run_preflight(client, token: str, ws: Path) -> dict:
    resp = client.post(
        "/api/deploy/preflight",
        json={"workspace_path": str(ws)},
        headers=_headers(token),
    )
    if resp.status_code != 200:
        raise StepFailure(
            4, "preflight 가 200 이 아님", f"HTTP {resp.status_code} — {resp.text[:300]}",
            "core/api/routes/deploy.py 의 deploy_preflight",
        )
    return resp.json() or {}


def _resolve_blockers(client, token: str, ws: Path, reasons: list[dict]) -> list[str]:
    """차단 사유를 사용자가 하듯 해소한다. 무엇을 했는지 목록으로 돌려준다.

    제품은 차단만 하고 끝내지 않는다. 자동 적용 가능한 수정안은 API 로 적용하고,
    그렇지 않은 것은 `fix` 안내를 사람이 따른다. E2E 는 그 흐름을 그대로 밟아야
    한다 — "차단됐으니 실패" 로 끝내면 제품이 실제로 어떻게 쓰이는지를 검증하지
    못한다.

    다만 **아무거나 만들어 넘기지는 않는다.** 여기서 손으로 처리하는 것은
    제품이 `fix` 로 명시한 조치뿐이고, 그 사실을 출력에 남긴다.
    """
    done: list[str] = []
    for reason in reasons:
        code = reason.get("code")
        pid = reason.get("proposal_id")

        if reason.get("remediation_available") and pid:
            resp = client.post(
                f"/api/deploy/remediations/{pid}/apply",
                json={"workspace_path": str(ws)},
                headers=_headers(token),
            )
            if resp.status_code != 200:
                raise StepFailure(
                    4, f"수정안 적용 실패 ({code})",
                    f"HTTP {resp.status_code} — {resp.text[:300]}",
                    "제품이 '자동 수정 가능'이라고 표시해 놓고 적용에 실패하면,\n"
                    "  사용자는 버튼을 눌러도 배포로 넘어가지 못한 채 갇힌다.\n"
                    "  core/remediation.py 의 apply_proposal",
                )
            done.append(f"{code}: 자동 수정 적용")
            continue

        # 자동 수정이 없는 것 — 제품이 안내한 조치를 사람이 하는 자리.
        if code == "MISSING_REQUIRED_ENV":
            # 기본 릴리스 계약은 required_env=["PORT"], env_file=".env" 다.
            # 수정안은 .env.example 만 만들어 검사를 해소하지 못하므로(코드에 명시)
            # 실제 .env 는 사용자가 만든다.
            (ws / ".env").write_text(f"PORT={CONTAINER_PORT}\n", encoding="utf-8")
            done.append("MISSING_REQUIRED_ENV: .env 에 PORT 작성 (안내대로)")
        elif code == "ENV_FILE_NOT_GITIGNORED":
            gi = ws / ".gitignore"
            existing = gi.read_text(encoding="utf-8") if gi.exists() else ""
            if ".env" not in existing:
                gi.write_text(existing + ".env\n", encoding="utf-8")
            done.append("ENV_FILE_NOT_GITIGNORED: .gitignore 에 .env 추가 (안내대로)")
        else:
            raise StepFailure(
                4, f"해소 방법을 모르는 차단 사유 ({code})",
                f"{reason.get('message')} / fix: {reason.get('fix')}",
                "자동 수정도 없고 이 스크립트도 처리 규칙이 없는 차단이다.\n"
                "  규칙이 타당하면 스크립트에 처리를 추가하고,\n"
                "  최소 앱을 막는 오탐이면 그 검사를 고쳐야 한다",
            )
    return done


def step4_inspect(client, token: str, ws: Path) -> None:
    _step(4, "배포 전 검사 (preflight → 수정안 → 재검사 → scan)")

    body = _run_preflight(client, token, ws)
    if body.get("blocked"):
        reasons = body.get("reasons") or []
        _note(f"차단 {len(reasons)}건 — 수정안을 적용한다")
        for line in _resolve_blockers(client, token, ws, reasons):
            _note(f"  {line}")

        body = _run_preflight(client, token, ws)
        if body.get("blocked"):
            still = [r.get("code") for r in (body.get("reasons") or [])]
            raise StepFailure(
                4, "수정안을 적용했는데도 여전히 차단됨", f"남은 사유: {still}",
                "적용은 성공했다는데 재검사가 같은 것을 다시 막는다 —\n"
                "  수정안이 검사가 보는 것과 다른 파일을 건드리고 있을 수 있다.\n"
                "  (예: 검사는 .env 를 보는데 수정안은 .env.example 만 만든다)",
            )
        _note("재검사 통과")

    _note(f"preflight 통과 (감지: {(body.get('detected') or {}).get('target', '?')})")

    # 스캔 도구(trivy/hadolint)는 없을 수 있다. 도구 부재는 결함이 아니므로
    # 실패로 만들지 않되, **조용히 넘기지도 않는다** — 검사가 실제로 돌았는지
    # 아닌지를 사람이 알아야 한다.
    for scan_type in ("hadolint", "trivy"):
        resp = client.post(
            "/api/deploy/scan",
            json={"workspace_path": str(ws), "scan_type": scan_type},
            headers=_headers(token),
        )
        if resp.status_code != 200:
            raise StepFailure(
                4, f"{scan_type} 스캔이 200 이 아님", f"HTTP {resp.status_code} — {resp.text[:300]}",
                "도구가 없을 때는 status=error 로 돌려줘야 하고 HTTP 오류를 내면 안 된다",
            )
        sb = resp.json() or {}
        if sb.get("status") == "ok":
            _note(
                f"{scan_type}: critical {sb.get('critical_count', 0)} / "
                f"high {sb.get('high_count', 0)}"
            )
        else:
            _note(f"{scan_type}: 실행 안 됨 — {str(sb.get('message', ''))[:80]}")
    _ok("검사 단계 통과")


def _build(ws: Path, tag: str) -> None:
    res = _docker("build", "-t", tag, str(ws), timeout=600)
    if res.returncode != 0:
        raise StepFailure(
            5, f"docker build 실패 ({tag})", (res.stderr or res.stdout).strip()[-600:],
            "제품이 만든 Dockerfile 로 이미지가 안 만들어진다 —\n"
            "  core/agents/infra_agent.py 의 템플릿과 스택 감지를 볼 것.\n"
            "  '설치는 되는데 안 돌아가는 Dockerfile' 은 사용자에게 가장 나쁜 결과다",
        )


def _deploy(client, token: str, ws: Path, image: str, step: int) -> dict:
    """plan → execute. 응답(플랜, 배포 기록)을 돌려준다."""
    resp = client.post(
        "/api/deploy/plan",
        json={
            "workspace_path": str(ws),
            "method": "local_docker",
            "image": image,
            "container_name": CONTAINER,
            "host_port": HOST_PORT,
            "container_port": CONTAINER_PORT,
            "skip_security_scan": True,
            "enable_continuous_verification": True,
        },
        headers=_headers(token),
    )
    if resp.status_code != 200:
        raise StepFailure(
            step, "배포 플랜이 200 이 아님", f"HTTP {resp.status_code} — {resp.text[:300]}",
            "core/api/routes/deploy.py 의 create_deployment_plan",
        )
    plan = resp.json() or {}

    resp = client.post(
        "/api/deploy/execute",
        json={"plan_id": plan.get("plan_id"), "approved": True},
        headers=_headers(token),
    )
    if resp.status_code != 200:
        raise StepFailure(
            step, "배포 실행이 200 이 아님", f"HTTP {resp.status_code} — {resp.text[:300]}",
            "execute_deployment — docker run 명령이 CommandRegistry 검증을 통과하는지",
        )
    return {"plan": plan, "execute": resp.json() or {}}


def step5_deploy_v1(client, token: str, ws: Path) -> str:
    _step(5, "배포 v1 (build → plan → execute)")
    _docker("rm", "-f", CONTAINER, timeout=60)
    _build(ws, IMAGE_V1)
    _note(f"{IMAGE_V1} 빌드 완료")

    result = _deploy(client, token, ws, IMAGE_V1, step=5)
    plan = result["plan"]
    execute = result["execute"]

    if plan.get("rollback_image"):
        raise StepFailure(
            5, "첫 배포인데 롤백 대상이 잡혔다", str(plan.get("rollback_image")),
            "되돌릴 곳이 없는데 있다고 표시하면, 정작 롤백을 눌렀을 때 실패한다.\n"
            "  api/routes/deploy.py 의 _previous_image_for",
        )

    deployment_id = (
        execute.get("deployment_id")
        or (execute.get("record") or {}).get("deployment_id")
        or ""
    )
    if not deployment_id:
        raise StepFailure(
            5, "배포 기록 id 를 못 받음", json.dumps(execute, ensure_ascii=False)[:300],
            "execute 응답에 deployment_id 가 있어야 감시·롤백을 걸 수 있다",
        )

    running = _running_image(CONTAINER)
    if running != IMAGE_V1:
        raise StepFailure(
            5, "컨테이너가 v1 을 돌리고 있지 않음", f"현재 이미지: {running or '(없음)'}",
            "배포 응답은 성공인데 docker 에 반영되지 않았다",
        )
    _ok(f"{CONTAINER} 가 {IMAGE_V1} 로 기동, deployment_id={deployment_id[:8]}")
    return deployment_id


def step6_watch(client, token: str, deployment_id: str) -> None:
    _step(6, "감시 (continuous verification + 실제 헬스)")

    status, body = _wait_healthy(HEALTH_URL, seconds=45)
    if status != 200:
        raise StepFailure(
            6, "배포한 앱이 응답하지 않음",
            f"{HEALTH_URL} → {status or '연결 실패'}\n\n{_container_diagnosis()}",
            f"포트 매핑(호스트 {HOST_PORT} → 컨테이너 {CONTAINER_PORT})이 플랜대로 걸렸는지\n"
            "컨테이너가 즉시 종료됐다면 Dockerfile 의 CMD 가 이 앱에 맞지 않는 것 —\n"
            "  스택 감지가 엉뚱한 템플릿을 골랐을 수 있다 (registry/file_templates/)",
        )
    _note(f"HTTP 200 · {body.strip()[:40]!r}")

    resp = client.get(
        f"/api/deploy/verification/{deployment_id}/status",
        headers=_headers(token),
    )
    if resp.status_code == 404:
        raise StepFailure(
            6, "지속 검증이 시작되지 않음", "verification status 가 404",
            "enable_continuous_verification=True 로 배포했는데 감시가 안 붙었다 —\n"
            "  execute_deployment 의 get_continuous_verifier 임포트가 조용히 실패했을 수 있다.\n"
            "  감시가 없으면 배포 후 이상을 아무도 못 잡는다",
        )
    if resp.status_code != 200:
        raise StepFailure(
            6, "verification status 가 200 이 아님", f"HTTP {resp.status_code} — {resp.text[:300]}",
            "core/preflight/continuous_verification.py",
        )
    snapshot = resp.json() or {}
    _ok(f"감시 동작 중 (상태: {snapshot.get('status', snapshot)})")

    client.post(
        f"/api/deploy/verification/{deployment_id}/stop",
        headers=_headers(token),
    )


def step7_redeploy_broken(client, token: str, ws: Path) -> str:
    """깨진 v2 를 배포한다. **이 단계의 목적은 롤백 대상 확인이다.**"""
    _step(7, "재배포 v2 (깨진 버전) — 롤백 대상이 잡히는지")

    (ws / "main.py").write_text(_BROKEN_APP_PY, encoding="utf-8")
    _build(ws, IMAGE_V2)
    _docker("rm", "-f", CONTAINER, timeout=60)

    result = _deploy(client, token, ws, IMAGE_V2, step=7)
    plan = result["plan"]

    rollback_image = plan.get("rollback_image")
    if rollback_image != IMAGE_V1:
        reasons = plan.get("risk_reasons") or []
        raise StepFailure(
            7, "롤백 대상이 v1 이 아님", f"rollback_image={rollback_image!r}, 사유={reasons}",
            "이 카드가 존재하는 이유가 이 지점이다. 값이 None 이면 되돌릴 곳을\n"
            "  모르는 상태로 배포된 것이고, /api/deploy/rollback 은 422 를 낸다.\n"
            "  api/routes/deploy.py 의 _previous_image_for 와\n"
            "  create_deployment_plan 의 rollback_image 채우기를 볼 것",
        )

    deployment_id = (
        result["execute"].get("deployment_id")
        or (result["execute"].get("record") or {}).get("deployment_id")
        or ""
    )
    if not deployment_id:
        raise StepFailure(7, "배포 기록 id 를 못 받음", "execute 응답에 deployment_id 없음")

    execute = result["execute"]
    if execute.get("status") != "success":
        raise StepFailure(
            7, "깨진 v2 컨테이너를 실제로 시작하지 못함", json.dumps(execute, ensure_ascii=False)[:300],
            "docker run 이 실패했는데 롤백만 성공하면 E2E 가 거짓 양성이 된다.\n"
            "  execute 응답 status 와 Docker 실행 로그를 확인하세요.",
        )
    running_image = _running_image(CONTAINER)
    if running_image != IMAGE_V2:
        raise StepFailure(
            7, "v2 이미지가 실제로 실행되지 않음", f"현재 이미지: {running_image or '(없음)'}",
            "재배포가 실제로 일어나야 롤백 검증이 의미가 있습니다.",
        )

    actual_rollback_target = execute.get("rollback_target")
    if actual_rollback_target != IMAGE_V1:
        raise StepFailure(
            7, "실행 시점의 롤백 대상이 v1 이 아님",
            f"rollback_target={actual_rollback_target!r}",
            "승인 대기 플랜의 대상이 오래됐을 수 있으므로 execute 직전에 대상이 다시 계산되어야 합니다.",
        )

    _ok(f"v2 배포됨, 롤백 대상 = {rollback_image}")

    status, _ = _http(HEALTH_URL)
    if status == 200:
        _note("경고: 깨진 v2 인데 헬스가 200 이다. 이미지가 실제로 바뀌었는지 확인할 것")
    else:
        _note(f"예상대로 v2 는 응답하지 않음 (status={status or '연결 실패'})")
    return deployment_id


def step8_rollback(client, token: str, deployment_id: str) -> None:
    _step(8, "롤백 (/api/deploy/rollback)")

    resp = client.post(
        "/api/deploy/rollback",
        json={"deployment_id": deployment_id},
        headers=_headers(token),
    )
    if resp.status_code == 422:
        raise StepFailure(
            8, "롤백 대상이 없다며 거절됨", resp.text[:300],
            "7단계에서는 대상이 잡혔는데 여기서 없다면, execute 가 record 에\n"
            "  rollback_target 을 옮겨 담지 않은 것이다 (plan.rollback_image → record.rollback_target)",
        )
    if resp.status_code != 200:
        raise StepFailure(
            8, "롤백이 200 이 아님", f"HTTP {resp.status_code} — {resp.text[:300]}",
            "core/api/routes/deploy.py 의 rollback",
        )

    running = _running_image(CONTAINER)
    if running != IMAGE_V1:
        raise StepFailure(
            8, "롤백했다는데 컨테이너는 v1 이 아님", f"현재 이미지: {running or '(없음)'}",
            "응답만 성공이고 docker 에는 반영되지 않았다 — 롤백의 가장 위험한 실패 형태다.\n"
            "  사용자는 복구됐다고 믿고 장애는 계속된다",
        )

    status, body = _wait_healthy(HEALTH_URL, seconds=45)
    if status != 200:
        raise StepFailure(
            8, "롤백 후에도 앱이 응답하지 않음",
            f"{HEALTH_URL} → {status or '연결 실패'}\n\n{_container_diagnosis()}",
            "롤백은 이미지를 되돌렸지만 컨테이너가 뜨지 않았다",
        )
    _ok(f"{IMAGE_V1} 로 복귀, HTTP 200 · {body.strip()[:40]!r}")


# ---------------------------------------------------------------------------
# 실행
# ---------------------------------------------------------------------------


def _cleanup(keep: bool) -> None:
    if keep:
        print("", flush=True)
        print(f"--keep: {CONTAINER} 와 {IMAGE_V1}/{IMAGE_V2} 를 남겨 둔다.", flush=True)
        print(f"  정리: docker rm -f {CONTAINER}; docker rmi {IMAGE_V1} {IMAGE_V2}", flush=True)
        return
    _docker("rm", "-f", CONTAINER, timeout=60)
    _docker("rmi", "-f", IMAGE_V1, IMAGE_V2, timeout=120)


def run(keep: bool) -> int:
    ws = Path(tempfile.mkdtemp(prefix="recoder-e2e-ws-"))
    stores = Path(tempfile.mkdtemp(prefix="recoder-e2e-store-"))

    import os

    # 개발자의 진짜 ADR 번호 장부·배포 기록을 건드리지 않는다.
    os.environ["RECODER_ADR_STORE"] = str(stores / "adr_reservations.json")
    os.environ["RECODER_ECS_STORE"] = str(stores / "ecs_deployments.json")

    print("E2E 통합 검증 — 개발 → 검사 → 배포 → 감시 → 롤백")
    print(f"워크스페이스: {ws}")
    print("")

    import code_agent  # type: ignore

    router = _FixtureRouter()
    original = code_agent.get_router
    code_agent.get_router = lambda: router  # type: ignore[assignment]

    try:
        client, token = step1_boot()
        step2_develop(client, token, ws)
        step3_infra(client, token, ws)
        step4_inspect(client, token, ws)
        dep_v1 = step5_deploy_v1(client, token, ws)
        step6_watch(client, token, dep_v1)
        dep_v2 = step7_redeploy_broken(client, token, ws)
        step8_rollback(client, token, dep_v2)
    except StepFailure as exc:
        _report(exc)
        return 1
    except Exception as exc:  # noqa: BLE001
        print("", flush=True)
        print("=" * 68, flush=True)
        print("E2E 통합 검증 실패 — 스크립트 자체에서 예외", flush=True)
        print("=" * 68, flush=True)
        traceback.print_exc()
        print(f"\n예외: {exc}", flush=True)
        return 1
    finally:
        code_agent.get_router = original  # type: ignore[assignment]
        _cleanup(keep)
        shutil.rmtree(ws, ignore_errors=True)
        shutil.rmtree(stores, ignore_errors=True)

    if router.calls != ["generate_plan", "generate_code"]:
        print(
            f"\n실패 — 예상한 LLM 단계가 실행되지 않았다: {router.calls}",
            flush=True,
        )
        return 1

    print("")
    print("전 구간 통과 — 개발부터 롤백까지 이어져 있다.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="E2E 통합 검증 (개발→검사→배포→감시→롤백)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "예:\n"
            "  python scripts/e2e_verify.py           # 전 구간, 끝나고 정리\n"
            "  python scripts/e2e_verify.py --keep    # 컨테이너를 남겨 두고 직접 확인\n"
        ),
    )
    parser.add_argument(
        "--keep", action="store_true",
        help="끝나고 컨테이너·이미지를 지우지 않는다 (실패 원인을 직접 볼 때)",
    )
    args = parser.parse_args()
    return run(keep=args.keep)


if __name__ == "__main__":
    sys.exit(main())
