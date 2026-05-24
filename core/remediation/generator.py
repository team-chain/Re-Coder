"""
RemediationProposal Generator (§32).

PreflightBlocker → RemediationProposal 변환. 12종 blocker code 각각에 대해
결정론적 proposal 생성 함수가 등록되어 있다. (`_GENERATORS` 디스패치 테이블)

설계 원칙:
  - 같은 (contract + blocker + workspace fingerprint) → 같은 proposal_id
  - LLM 미사용. ``rationale`` 도 자연어 템플릿 치환으로만 생성.
  - 자동 적용 가능한 case 만 ``auto_apply_available=True``. 나머지는 가이드.
  - target_path 는 항상 workspace-relative (절대경로 금지 — 머신 종속 제거)
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Callable, Optional

try:
    from schemas import (
        ApprovalLevel,
        PreflightBlocker,
        PreflightCheckCode,
        PreflightRun,
        PreflightWarning,
        ReleaseContract,
        RemediationApplyMethod,
        RemediationFallback,
        RemediationPreviewCommand,
        RemediationPreviewFile,
        RemediationPreviewGuidance,
        RemediationPreviewType,
        RemediationProposal,
        RemediationTargetType,
        RiskLevel,
    )
except ImportError:  # pragma: no cover
    from core.schemas import (  # type: ignore
        ApprovalLevel,
        PreflightBlocker,
        PreflightCheckCode,
        PreflightRun,
        PreflightWarning,
        ReleaseContract,
        RemediationApplyMethod,
        RemediationFallback,
        RemediationPreviewCommand,
        RemediationPreviewFile,
        RemediationPreviewGuidance,
        RemediationPreviewType,
        RemediationProposal,
        RemediationTargetType,
        RiskLevel,
    )

from .fingerprint import compute_fingerprint, proposal_id_from_fingerprint
from .registry import get_command_template, get_file_template, render


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _file_sha256(path: Path) -> Optional[str]:
    if not path.exists() or not path.is_file():
        return None
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _build_proposal(
    *,
    blocker_code: PreflightCheckCode,
    contract: ReleaseContract,
    summary: str,
    rationale: str,
    target_type: RemediationTargetType,
    target_path: Optional[str],
    apply_method: RemediationApplyMethod,
    template_id: Optional[str],
    template_variables: dict,
    preview_type: RemediationPreviewType,
    preview: Optional[dict],
    auto_apply_available: bool,
    confidence: float = 0.8,
    risk_level: RiskLevel = RiskLevel.LOW,
    approval_level: ApprovalLevel = ApprovalLevel.CONFIRM,
    fallback: Optional[RemediationFallback] = RemediationFallback.MANUAL_GUIDANCE,
    rollback_hint: str = "",
    requires_rerun_preflight: bool = True,
) -> RemediationProposal:
    """결정론적 proposal_id 부여 후 빌드."""
    fp = compute_fingerprint(
        blocker_code=blocker_code.value,
        target_path=target_path,
        template_id=template_id,
        template_variables=template_variables,
        contract_hash=contract.contract_hash,
    )
    return RemediationProposal(
        proposal_id=proposal_id_from_fingerprint(fp),
        source_blocker_code=blocker_code,
        summary=summary,
        rationale=rationale,
        target_type=target_type,
        target_path=target_path,
        approval_level=int(approval_level) if isinstance(approval_level, ApprovalLevel) else approval_level,
        risk_level=risk_level,
        apply_method=apply_method,
        template_id=template_id,
        template_variables=template_variables,
        preview_type=preview_type,
        preview=preview,
        auto_apply_available=auto_apply_available,
        confidence=confidence,
        fallback=fallback,
        rollback_hint=rollback_hint,
        requires_rerun_preflight=requires_rerun_preflight,
    )


# ---------------------------------------------------------------------------
# Per-blocker generators
# ---------------------------------------------------------------------------


def _gen_missing_required_env(
    blocker: PreflightBlocker | PreflightWarning,
    contract: ReleaseContract,
    workspace: Path,
) -> RemediationProposal:
    required = list(contract.preflight.required_env or [])
    env_lines = "\n".join(f"{k}=" for k in required) or "# (recoder.yml preflight.required_env 비어 있음)"
    template_id = "env.example.create"
    template_variables = {"env_lines": env_lines}
    tmpl = get_file_template(template_id)
    rendered = render(tmpl.base_content, template_variables) if tmpl else ""
    preview = RemediationPreviewFile(target_path=".env.example", content=rendered).model_dump()
    return _build_proposal(
        blocker_code=PreflightCheckCode.MISSING_REQUIRED_ENV,
        contract=contract,
        summary="`.env.example` 생성 — 필수 환경 변수 placeholder 채우기",
        rationale=(
            f"recoder.yml 의 preflight.required_env 가 {len(required)}개 정의되어 있지만 "
            ".env / .env.example 에서 발견되지 않았습니다. ReCoder 가 결정론적으로 "
            "placeholder 만 채운 .env.example 을 만들 수 있습니다 (실제 값은 사용자가 입력)."
        ),
        target_type=RemediationTargetType.ENV_FILE,
        target_path=".env.example",
        apply_method=RemediationApplyMethod.FILE_TEMPLATE,
        template_id=template_id,
        template_variables=template_variables,
        preview_type=RemediationPreviewType.FILE_CONTENT,
        preview=preview,
        auto_apply_available=True,
        confidence=0.95,
        risk_level=RiskLevel.LOW,
        approval_level=ApprovalLevel.CONFIRM,
        rollback_hint="생성된 .env.example 파일을 삭제하면 원상복구.",
    )


def _gen_env_file_not_gitignored(
    blocker: PreflightBlocker | PreflightWarning,
    contract: ReleaseContract,
    workspace: Path,
) -> RemediationProposal:
    template_id = "gitignore.env.append"
    tmpl = get_file_template(template_id)
    appended = tmpl.base_content if tmpl else ""
    preview = RemediationPreviewFile(target_path=".gitignore", content=appended).model_dump()
    gi_path = workspace / ".gitignore"
    base_sha = _file_sha256(gi_path) if gi_path.exists() else None
    return _build_proposal(
        blocker_code=PreflightCheckCode.ENV_FILE_NOT_GITIGNORED,
        contract=contract,
        summary="`.gitignore` 에 `.env` 패턴 추가 (보안: secret 유출 방지)",
        rationale=(
            ".env 가 .gitignore 에 포함되어 있지 않습니다 — 커밋 시 secret 이 외부에 노출됩니다. "
            "결정론적 append: .env / .env.local / .env.*.local 추가."
        ),
        target_type=RemediationTargetType.SOURCE_CODE,
        target_path=".gitignore",
        apply_method=RemediationApplyMethod.FILE_TEMPLATE,
        template_id=template_id,
        template_variables={"base_sha256": base_sha or ""},
        preview_type=RemediationPreviewType.FILE_CONTENT,
        preview=preview,
        auto_apply_available=True,
        confidence=0.99,
        risk_level=RiskLevel.LOW,
        approval_level=ApprovalLevel.AUTO,
        rollback_hint=".gitignore 끝의 ReCoder Remediation 블록 제거.",
    )


def _gen_invalid_env_format(
    blocker: PreflightBlocker | PreflightWarning,
    contract: ReleaseContract,
    workspace: Path,
) -> RemediationProposal:
    steps = [
        ".env 파일에서 형식이 잘못된 행을 찾으세요 (KEY=value 형식, 따옴표 균형).",
        "올바른 예: `DATABASE_URL=\"postgres://user:pass@host/db\"`",
        "잘못된 예: `123KEY=...` (숫자로 시작) / `KEY=\"unclosed` (따옴표 안 닫힘)",
        "수정 후 `recoder preflight` 재실행.",
    ]
    preview = RemediationPreviewGuidance(steps=steps, estimated_time="2분").model_dump()
    return _build_proposal(
        blocker_code=PreflightCheckCode.INVALID_ENV_FORMAT,
        contract=contract,
        summary=".env 형식 오류 — 사용자 수동 수정 가이드",
        rationale=(
            ".env 파일에 KEY=value 형식이 아닌 행 또는 따옴표가 닫히지 않은 값이 있습니다. "
            "자동 교정은 secret 값 손상 위험이 있어 가이드만 제공합니다."
        ),
        target_type=RemediationTargetType.ENV_FILE,
        target_path=".env",
        apply_method=RemediationApplyMethod.MANUAL_ONLY,
        template_id=None,
        template_variables={},
        preview_type=RemediationPreviewType.GUIDANCE,
        preview=preview,
        auto_apply_available=False,
        confidence=0.85,
        risk_level=RiskLevel.MEDIUM,
        approval_level=ApprovalLevel.DOUBLE_CONFIRM,
        fallback=RemediationFallback.MANUAL_GUIDANCE,
    )


def _gen_missing_health_endpoint(
    blocker: PreflightBlocker | PreflightWarning,
    contract: ReleaseContract,
    workspace: Path,
) -> RemediationProposal:
    stack = contract.project.stack.value if hasattr(contract.project.stack, "value") else str(contract.project.stack)
    if "fastapi" in stack:
        template_id = "health.fastapi.snippet"
        target = "app/main.py"
    elif "flask" in stack:
        template_id = "health.flask.snippet"
        target = "app.py"
    elif "express" in stack or "node" in stack:
        template_id = "health.express.snippet"
        target = "index.js"
    else:
        template_id = "health.fastapi.snippet"
        target = "app/main.py"
    tmpl = get_file_template(template_id)
    snippet = tmpl.base_content if tmpl else ""
    preview = RemediationPreviewFile(target_path=target, content=snippet).model_dump()
    return _build_proposal(
        blocker_code=PreflightCheckCode.MISSING_HEALTH_ENDPOINT,
        contract=contract,
        summary=f"`/healthz` 엔드포인트 추가 ({stack})",
        rationale=(
            f"recoder.yml 스택이 '{stack}' 이지만 /healthz (또는 동등한) 헬스 엔드포인트가 "
            "탐지되지 않았습니다. Runtime Preflight 가 health probe 로 컨테이너 살아있음을 "
            "확인하므로 반드시 필요합니다."
        ),
        target_type=RemediationTargetType.SOURCE_CODE,
        target_path=target,
        apply_method=RemediationApplyMethod.FILE_TEMPLATE,
        template_id=template_id,
        template_variables={},
        preview_type=RemediationPreviewType.FILE_CONTENT,
        preview=preview,
        auto_apply_available=False,  # 소스 코드 자동 수정은 보수적으로
        confidence=0.85,
        risk_level=RiskLevel.LOW,
        approval_level=ApprovalLevel.CONFIRM,
        rollback_hint="추가된 endpoint 함수 블록을 직접 제거.",
    )


def _gen_app_entrypoint_not_found(
    blocker: PreflightBlocker | PreflightWarning,
    contract: ReleaseContract,
    workspace: Path,
) -> RemediationProposal:
    stack = contract.project.stack.value if hasattr(contract.project.stack, "value") else str(contract.project.stack)
    steps = [
        f"스택 '{stack}' 의 표준 entrypoint 파일을 만드세요.",
        "FastAPI: `app/main.py` 에 `app = FastAPI()` 인스턴스 정의",
        "Flask:   `app.py` 에 `app = Flask(__name__)`",
        "Express: `index.js` 또는 `server.js` 에 `app.listen(port)`",
        "Next.js: `pages/api/healthz.ts` 같은 라우트 또는 `app/` 구조 확인",
        "recoder.yml runtime.entrypoint 와도 일치시키세요.",
    ]
    preview = RemediationPreviewGuidance(steps=steps, estimated_time="10분").model_dump()
    return _build_proposal(
        blocker_code=PreflightCheckCode.APP_ENTRYPOINT_NOT_FOUND,
        contract=contract,
        summary="앱 entrypoint 파일 생성 가이드",
        rationale=(
            f"recoder.yml 스택이 '{stack}' 이지만 표준 entrypoint 파일을 찾지 못했습니다. "
            "사용자가 직접 만들어야 합니다 (코드 구조는 프로젝트마다 달라 결정론적 자동 생성 불가)."
        ),
        target_type=RemediationTargetType.SOURCE_CODE,
        target_path=None,
        apply_method=RemediationApplyMethod.MANUAL_ONLY,
        template_id=None,
        template_variables={},
        preview_type=RemediationPreviewType.GUIDANCE,
        preview=preview,
        auto_apply_available=False,
        confidence=0.7,
        risk_level=RiskLevel.MEDIUM,
        approval_level=ApprovalLevel.DOUBLE_CONFIRM,
        fallback=RemediationFallback.ASK_USER_FOR_PATH,
    )


def _gen_missing_dockerfile(
    blocker: PreflightBlocker | PreflightWarning,
    contract: ReleaseContract,
    workspace: Path,
) -> RemediationProposal:
    stack = contract.project.stack.value if hasattr(contract.project.stack, "value") else str(contract.project.stack)
    app_port = contract.runtime.app_port
    if "fastapi" in stack:
        template_id = "dockerfile.fastapi"
        variables = {
            "python_version": "3.11",
            "app_port": str(app_port),
            "module_path": "app.main:app",
        }
    elif "flask" in stack:
        template_id = "dockerfile.flask"
        variables = {
            "python_version": "3.11",
            "app_port": str(app_port),
            "module_path": "app:app",
        }
    elif "express" in stack or "node" in stack:
        template_id = "dockerfile.node"
        variables = {
            "node_version": "20",
            "app_port": str(app_port),
            "entry_file": "index.js",
        }
    else:
        template_id = "dockerfile.fastapi"
        variables = {
            "python_version": "3.11",
            "app_port": str(app_port),
            "module_path": "app.main:app",
        }
    tmpl = get_file_template(template_id)
    rendered = render(tmpl.base_content, variables) if tmpl else ""
    preview = RemediationPreviewFile(target_path="Dockerfile", content=rendered).model_dump()
    return _build_proposal(
        blocker_code=PreflightCheckCode.MISSING_DOCKERFILE,
        contract=contract,
        summary=f"Dockerfile 생성 (스택: {stack})",
        rationale=(
            f"recoder.yml 스택 '{stack}' 에 맞는 결정론적 Dockerfile 템플릿을 적용합니다. "
            f"FROM 이미지 태그 고정, EXPOSE {app_port}, non-root 권장."
        ),
        target_type=RemediationTargetType.DOCKER_RUNTIME,
        target_path="Dockerfile",
        apply_method=RemediationApplyMethod.FILE_TEMPLATE,
        template_id=template_id,
        template_variables=variables,
        preview_type=RemediationPreviewType.FILE_CONTENT,
        preview=preview,
        auto_apply_available=True,
        confidence=0.9,
        risk_level=RiskLevel.MEDIUM,
        approval_level=ApprovalLevel.CONFIRM,
        rollback_hint="생성된 Dockerfile 삭제 또는 git checkout.",
    )


def _gen_dockerfile_build_risk(
    blocker: PreflightBlocker | PreflightWarning,
    contract: ReleaseContract,
    workspace: Path,
) -> RemediationProposal:
    steps = [
        "Dockerfile 의 안티패턴을 제거하세요:",
        " - FROM 베이스 이미지에 명시적 태그 추가 (예: python:3.11-slim)",
        " - USER root 제거 또는 USER appuser 로 전환",
        " - ADD <URL>, curl | sh 같은 검증 안 된 원격 스크립트 실행 금지",
        " - COPY . / 대신 .dockerignore + 명시적 COPY 사용",
        "수정 후 Hadolint / `recoder preflight` 재실행으로 확인.",
    ]
    preview = RemediationPreviewGuidance(steps=steps, estimated_time="5~15분").model_dump()
    return _build_proposal(
        blocker_code=PreflightCheckCode.DOCKERFILE_BUILD_RISK,
        contract=contract,
        summary="Dockerfile 위험 패턴 수정 가이드 (Hadolint 보조)",
        rationale=(
            "Dockerfile 에 보안/재현성 위험 패턴이 발견되었습니다. 자동 수정은 의도된 동작을 "
            "깨뜨릴 수 있어 가이드만 제공. CRITICAL 발견 시 즉시 차단."
        ),
        target_type=RemediationTargetType.DOCKER_RUNTIME,
        target_path="Dockerfile",
        apply_method=RemediationApplyMethod.MANUAL_ONLY,
        template_id=None,
        template_variables={},
        preview_type=RemediationPreviewType.GUIDANCE,
        preview=preview,
        auto_apply_available=False,
        confidence=0.75,
        risk_level=RiskLevel.MEDIUM,
        approval_level=ApprovalLevel.DOUBLE_CONFIRM,
    )


def _gen_host_port_conflict(
    blocker: PreflightBlocker | PreflightWarning,
    contract: ReleaseContract,
    workspace: Path,
) -> RemediationProposal:
    host_port = contract.runtime.host_port
    suggested_port = host_port + 1 if host_port < 65000 else 8081
    variables = {"host_port": str(suggested_port), "app_port": str(contract.runtime.app_port)}
    preview = RemediationPreviewGuidance(
        steps=[
            f"recoder.yml runtime.host_port: {host_port} → {suggested_port} 로 변경",
            "또는 기존 점유 프로세스를 종료 (CommandTemplate port.kill.* 사용)",
            "변경 후 `recoder preflight` 재실행.",
        ],
        estimated_time="1분",
    ).model_dump()
    return _build_proposal(
        blocker_code=PreflightCheckCode.HOST_PORT_CONFLICT,
        contract=contract,
        summary=f"호스트 포트 {host_port} 충돌 — {suggested_port} 로 변경 제안",
        rationale=(
            f"호스트 포트 {host_port} 가 이미 LISTEN 중입니다. recoder.yml 갱신 또는 점유 "
            "프로세스 종료가 필요합니다. 결정론적 제안: host_port+1."
        ),
        target_type=RemediationTargetType.RELEASE_CONTRACT,
        target_path="recoder.yml",
        apply_method=RemediationApplyMethod.CONTRACT_UPDATE,
        template_id="recoder.yml.port.update",
        template_variables=variables,
        preview_type=RemediationPreviewType.GUIDANCE,
        preview=preview,
        auto_apply_available=False,
        confidence=0.7,
        risk_level=RiskLevel.LOW,
        approval_level=ApprovalLevel.CONFIRM,
        rollback_hint="recoder.yml 의 host_port 를 원래 값으로 되돌리기.",
    )


def _gen_app_port_mismatch(
    blocker: PreflightBlocker | PreflightWarning,
    contract: ReleaseContract,
    workspace: Path,
) -> RemediationProposal:
    app_port = contract.runtime.app_port
    variables = {"app_port": str(app_port)}
    template_id = "dockerfile.expose.append"
    tmpl = get_file_template(template_id)
    rendered = render(tmpl.base_content, variables) if tmpl else ""
    preview = RemediationPreviewFile(target_path="Dockerfile", content=rendered).model_dump()
    return _build_proposal(
        blocker_code=PreflightCheckCode.APP_PORT_MISMATCH,
        contract=contract,
        summary=f"Dockerfile 에 EXPOSE {app_port} 추가",
        rationale=(
            f"recoder.yml app_port={app_port} 와 Dockerfile EXPOSE 가 일치하지 않습니다. "
            "결정론적 append 로 EXPOSE 라인을 추가합니다."
        ),
        target_type=RemediationTargetType.DOCKER_RUNTIME,
        target_path="Dockerfile",
        apply_method=RemediationApplyMethod.FILE_TEMPLATE,
        template_id=template_id,
        template_variables=variables,
        preview_type=RemediationPreviewType.FILE_CONTENT,
        preview=preview,
        auto_apply_available=True,
        confidence=0.9,
        risk_level=RiskLevel.LOW,
        approval_level=ApprovalLevel.CONFIRM,
        rollback_hint="추가된 EXPOSE 라인을 Dockerfile 에서 제거.",
    )


def _gen_unpinned_dependencies(
    blocker: PreflightBlocker | PreflightWarning,
    contract: ReleaseContract,
    workspace: Path,
) -> RemediationProposal:
    stack = contract.project.stack.value if hasattr(contract.project.stack, "value") else str(contract.project.stack)
    if "node" in stack or "express" in stack:
        template_id = "npm.audit.fix"
    else:
        template_id = "pip.compile.requirements"
    tmpl = get_command_template(template_id)
    command = tmpl.command_pattern if tmpl else ""
    preview = RemediationPreviewCommand(
        command=command,
        template_id=template_id,
        requires_consent=True,
    ).model_dump()
    return _build_proposal(
        blocker_code=PreflightCheckCode.UNPINNED_DEPENDENCIES,
        contract=contract,
        summary="의존성 버전 핀 고정 명령 제안",
        rationale=(
            "버전이 고정되지 않은 의존성이 있습니다 (^1.x, ~2.x, >=… 등). 재현성 보장을 "
            "위해 pip-compile / npm ci 흐름으로 lock 파일 생성을 권장합니다."
        ),
        target_type=RemediationTargetType.SOURCE_CODE,
        target_path="requirements.txt" if "node" not in stack else "package.json",
        apply_method=RemediationApplyMethod.COMMAND_TEMPLATE,
        template_id=template_id,
        template_variables={},
        preview_type=RemediationPreviewType.COMMAND,
        preview=preview,
        auto_apply_available=False,
        confidence=0.8,
        risk_level=RiskLevel.MEDIUM,
        approval_level=ApprovalLevel.CONFIRM,
    )


def _gen_critical_vulnerability(
    blocker: PreflightBlocker | PreflightWarning,
    contract: ReleaseContract,
    workspace: Path,
) -> RemediationProposal:
    stack = contract.project.stack.value if hasattr(contract.project.stack, "value") else str(contract.project.stack)
    template_id = "npm.audit.fix" if "node" in stack else "pip.audit.run"
    tmpl = get_command_template(template_id)
    command = tmpl.command_pattern if tmpl else ""
    preview = RemediationPreviewCommand(
        command=command,
        template_id=template_id,
        requires_consent=True,
    ).model_dump()
    return _build_proposal(
        blocker_code=PreflightCheckCode.CRITICAL_VULNERABILITY,
        contract=contract,
        summary="의존성 보안 취약점 스캔 + 자동 수정 명령",
        rationale=(
            "Critical CVE 가 의존성에서 보고되었습니다. 자동 audit/fix 흐름을 실행해 "
            "안전한 버전으로 올리거나 수동 검토가 필요합니다."
        ),
        target_type=RemediationTargetType.SOURCE_CODE,
        target_path=None,
        apply_method=RemediationApplyMethod.COMMAND_TEMPLATE,
        template_id=template_id,
        template_variables={},
        preview_type=RemediationPreviewType.COMMAND,
        preview=preview,
        auto_apply_available=False,
        confidence=0.85,
        risk_level=RiskLevel.HIGH,
        approval_level=ApprovalLevel.DOUBLE_CONFIRM,
    )


def _gen_secret_leak_risk(
    blocker: PreflightBlocker | PreflightWarning,
    contract: ReleaseContract,
    workspace: Path,
) -> RemediationProposal:
    """**보안**: secret 원문을 RemediationProposal 에 절대 포함하지 않음."""
    steps = [
        "발견된 secret 패턴의 파일/라인 위치를 PreflightRun.static_checks.SECRET_LEAK_RISK 에서 확인.",
        "해당 위치에서 실제 secret 값을 제거하거나 환경 변수로 대체.",
        "이미 git 히스토리에 포함됐다면 `git filter-repo` 로 완전 제거.",
        "키 회전 (rotate): 노출된 API 키는 즉시 무효화 + 재발급.",
        "수정 후 `recoder preflight` 재실행으로 확인.",
    ]
    preview = RemediationPreviewGuidance(steps=steps, estimated_time="10~30분").model_dump()
    return _build_proposal(
        blocker_code=PreflightCheckCode.SECRET_LEAK_RISK,
        contract=contract,
        summary="secret 의심 패턴 발견 — 수동 검토 + 키 회전 가이드",
        rationale=(
            "코드/설정 파일에서 secret 의심 패턴이 발견되었습니다. ReCoder 는 보안상 "
            "원문을 메시지에 포함하지 않으며, 자동 수정을 시도하지 않습니다 (잘못된 "
            "패턴 매칭으로 정상 값을 손상시킬 위험)."
        ),
        target_type=RemediationTargetType.GUIDANCE_ONLY,
        target_path=None,
        apply_method=RemediationApplyMethod.MANUAL_ONLY,
        template_id=None,
        template_variables={},
        preview_type=RemediationPreviewType.GUIDANCE,
        preview=preview,
        auto_apply_available=False,
        confidence=0.7,
        risk_level=RiskLevel.HIGH,
        approval_level=ApprovalLevel.DOUBLE_CONFIRM,
    )


# ---------------------------------------------------------------------------
# Dispatch table
# ---------------------------------------------------------------------------


GeneratorFn = Callable[
    [PreflightBlocker | PreflightWarning, ReleaseContract, Path],
    RemediationProposal,
]


_GENERATORS: dict[PreflightCheckCode, GeneratorFn] = {
    PreflightCheckCode.MISSING_REQUIRED_ENV:    _gen_missing_required_env,
    PreflightCheckCode.ENV_FILE_NOT_GITIGNORED: _gen_env_file_not_gitignored,
    PreflightCheckCode.INVALID_ENV_FORMAT:      _gen_invalid_env_format,
    PreflightCheckCode.MISSING_HEALTH_ENDPOINT: _gen_missing_health_endpoint,
    PreflightCheckCode.APP_ENTRYPOINT_NOT_FOUND: _gen_app_entrypoint_not_found,
    PreflightCheckCode.MISSING_DOCKERFILE:      _gen_missing_dockerfile,
    PreflightCheckCode.DOCKERFILE_BUILD_RISK:   _gen_dockerfile_build_risk,
    PreflightCheckCode.HOST_PORT_CONFLICT:      _gen_host_port_conflict,
    PreflightCheckCode.APP_PORT_MISMATCH:       _gen_app_port_mismatch,
    PreflightCheckCode.UNPINNED_DEPENDENCIES:   _gen_unpinned_dependencies,
    PreflightCheckCode.CRITICAL_VULNERABILITY:  _gen_critical_vulnerability,
    PreflightCheckCode.SECRET_LEAK_RISK:        _gen_secret_leak_risk,
}


assert len(_GENERATORS) == 12, "Remediation generator 는 12종 blocker 모두 커버해야 함."


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def generate_proposal_for_blocker(
    blocker: PreflightBlocker | PreflightWarning,
    contract: ReleaseContract,
    workspace: Path,
) -> Optional[RemediationProposal]:
    """단일 blocker/warning 에 대해 RemediationProposal 생성.

    해당 code 에 generator 가 등록 안 되어 있으면 None.
    """
    fn = _GENERATORS.get(blocker.code)
    if fn is None:
        return None
    return fn(blocker, contract, workspace)


def generate_proposals(
    preflight_run: PreflightRun,
    contract: ReleaseContract,
    workspace: Path,
) -> list[RemediationProposal]:
    """PreflightRun 의 모든 blocker + warning 에 대해 RemediationProposal 생성.

    중복 fingerprint 는 제거 (같은 root cause).
    blocker 가 warning 보다 우선 — 같은 code 가 양쪽에 있으면 blocker 가 살아남음.
    """
    proposals: dict[str, RemediationProposal] = {}
    seen_codes: set[str] = set()

    for blocker in preflight_run.blockers:
        p = generate_proposal_for_blocker(blocker, contract, workspace)
        if p is None:
            continue
        proposals[p.proposal_id] = p
        seen_codes.add(blocker.code.value)

    for warning in preflight_run.warnings:
        if warning.code.value in seen_codes:
            continue  # blocker 가 이미 잡았음
        p = generate_proposal_for_blocker(warning, contract, workspace)
        if p is None:
            continue
        proposals[p.proposal_id] = p

    return list(proposals.values())
