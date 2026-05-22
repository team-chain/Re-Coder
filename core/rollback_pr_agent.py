"""
core/rollback_pr_agent.py — **Helm-values flow** rollback PR 생성기 (ADR-005)

ReCoder 에는 두 개의 rollback PR 변종이 존재한다. 호출자는 시나리오에 맞는 쪽을
선택해야 한다.

  ┌─────────────────────────────────┬─────────────────────────────────────┐
  │ 파일                             │ 시나리오                             │
  ├─────────────────────────────────┼─────────────────────────────────────┤
  │ core/rollback_pr_agent.py       │ Helm values.yaml image.tag 를         │
  │ (이 파일)                        │ last_healthy_image_tag 로 되돌리는    │
  │                                  │ Helm-managed GitOps 환경 전용.        │
  │                                  │ ArgoCD Application 이 Helm chart      │
  │                                  │ values 를 watch 한다고 가정한다.      │
  ├─────────────────────────────────┼─────────────────────────────────────┤
  │ core/agents/rollback_pr_agent.py │ revert-commit flow. 임의 commit SHA  │
  │                                  │ 를 GitHub API 로 revert 하고 PR 을    │
  │                                  │ 연다. Helm 비사용 또는 일반 Git 기반   │
  │                                  │ GitOps 환경에서 사용.                 │
  └─────────────────────────────────┴─────────────────────────────────────┘

두 변종 모두 ADR-005 의 production rollback 정책을 충족한다.

ADR-005 rollback 정책:
  - staging/dev : ArgoCD API rollback (gitops_agent.rollback_app, env="staging")
  - production  : Git revert PR 생성 (이 파일 또는 agents/rollback_pr_agent)
  - Severity 1  : emergency rollback 허용 + 30분 이내 Git reconciliation PR 필수

입력:
  failed_image_tag       — 실패한 이미지 태그
  last_healthy_image_tag — 마지막 정상 이미지 태그
  helm_values_path       — Helm values.yaml 경로 (Git 저장소 내 상대 경로)
  argocd_app_name        — ArgoCD 애플리케이션 이름
  deployment_record      — 실패한 배포 정보 (DeploymentRecord)
  incident_id            — 인시던트 ID

출력:
  values.yaml image.tag → last_healthy_image_tag 로 변경
  PR title: "rollback: restore {app} to {previous_image_tag}"
  PR body: incident summary, RCA candidate, approval link, rollback risk
  AuditLog rollback_pr_created 기록 (~/.recoder/audit/rollback_pr_{incident}.json)

환경변수:
  GITHUB_TOKEN  — GitHub PAT (repo 권한)
  GITHUB_REPO   — "owner/repo" 형식
"""

from __future__ import annotations

import logging
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ── 상수 ─────────────────────────────────────────────────────────────────
_ROLLBACK_BRANCH_PREFIX = "rollback"
_GITHUB_API = "https://api.github.com"
_PR_LABEL = "rollback"
_AUDIT_LOG_DIR = Path.home() / ".recoder" / "audit"


# ── 데이터 타입 ───────────────────────────────────────────────────────────

@dataclass
class DeploymentRecord:
    """실패한 배포 정보."""
    app_name:        str
    environment:     str           # "production" | "staging" | "dev"
    failed_at:       str = ""      # ISO 8601
    deployed_by:     str = ""
    deploy_duration_s: float = 0.0
    error_summary:   str = ""
    cluster:         str = ""
    namespace:       str = "default"
    incident_severity: int = 2     # 1~4, 1=최고 심각


@dataclass
class RollbackPRConfig:
    """rollback PR 생성 설정."""
    failed_image_tag:       str
    last_healthy_image_tag: str
    helm_values_path:       str       # Git repo 내 상대 경로 (예: "helm/values.yaml")
    argocd_app_name:        str
    deployment_record:      DeploymentRecord
    incident_id:            str = field(default_factory=lambda: f"INC-{uuid.uuid4().hex[:8].upper()}")
    github_token:           str = field(default_factory=lambda: os.environ.get("GITHUB_TOKEN", ""))
    github_repo:            str = field(default_factory=lambda: os.environ.get("GITHUB_REPO", ""))
    base_branch:            str = "main"
    emergency:              bool = False   # Severity 1 emergency rollback


@dataclass
class RollbackPRResult:
    """rollback PR 생성 결과."""
    success:        bool
    pr_url:         str = ""
    pr_number:      int = 0
    branch:         str = ""
    incident_id:    str = ""
    audit_log_path: str = ""
    error:          str = ""
    logs:           list[str] = field(default_factory=list)

    def to_summary(self) -> dict:
        return {
            "success":        self.success,
            "pr_url":         self.pr_url,
            "pr_number":      self.pr_number,
            "branch":         self.branch,
            "incident_id":    self.incident_id,
            "error":          self.error,
        }


# ── GitHub API 헬퍼 ───────────────────────────────────────────────────────

class GitHubClient:
    """최소한의 GitHub REST API 클라이언트."""

    def __init__(self, token: str, repo: str):
        self._token = token
        self._repo  = repo          # "owner/repo"

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept":        "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _url(self, path: str) -> str:
        return f"{_GITHUB_API}/repos/{self._repo}{path}"

    # ── 파일 조회 ─────────────────────────────────────────────────────
    def get_file(self, path: str, ref: str = "main") -> dict:
        """파일 내용 + sha 반환."""
        import urllib.request, urllib.error, json
        url = self._url(f"/contents/{path}?ref={ref}")
        req = urllib.request.Request(url, headers=self._headers())
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"GitHub get_file failed [{e.code}]: {e.read().decode()}") from e

    # ── 파일 생성/수정 ────────────────────────────────────────────────
    def put_file(self, path: str, message: str, content_b64: str,
                 sha: Optional[str], branch: str) -> dict:
        import urllib.request, urllib.error, json
        body = {
            "message": message,
            "content": content_b64,
            "branch":  branch,
        }
        if sha:
            body["sha"] = sha
        data = json.dumps(body).encode()
        url = self._url(f"/contents/{path}")
        req = urllib.request.Request(url, data=data, headers=self._headers(), method="PUT")
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"GitHub put_file failed [{e.code}]: {e.read().decode()}") from e

    # ── 브랜치 생성 ───────────────────────────────────────────────────
    def create_branch(self, branch: str, from_ref: str = "main") -> str:
        """브랜치 생성 후 sha 반환."""
        import urllib.request, urllib.error, json
        # 1) base sha 조회
        url = self._url(f"/git/ref/heads/{from_ref}")
        req = urllib.request.Request(url, headers=self._headers())
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                base_sha = json.loads(resp.read())["object"]["sha"]
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"GitHub get_ref failed [{e.code}]: {e.read().decode()}") from e

        # 2) 브랜치 생성
        body = json.dumps({"ref": f"refs/heads/{branch}", "sha": base_sha}).encode()
        url2 = self._url("/git/refs")
        req2 = urllib.request.Request(url2, data=body, headers=self._headers(), method="POST")
        try:
            with urllib.request.urlopen(req2, timeout=15) as resp:
                return base_sha
        except urllib.error.HTTPError as e:
            # 422 = 이미 존재 → 무시
            if e.code == 422:
                return base_sha
            raise RuntimeError(f"GitHub create_branch failed [{e.code}]: {e.read().decode()}") from e

    # ── PR 생성 ───────────────────────────────────────────────────────
    def create_pr(self, title: str, body: str, head: str, base: str,
                  labels: list[str] | None = None) -> dict:
        import urllib.request, urllib.error, json
        payload: dict = {"title": title, "body": body, "head": head, "base": base}
        data = json.dumps(payload).encode()
        url = self._url("/pulls")
        req = urllib.request.Request(url, data=data, headers=self._headers(), method="POST")
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                pr = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"GitHub create_pr failed [{e.code}]: {e.read().decode()}") from e

        # 라벨 추가 (선택)
        if labels and pr.get("number"):
            self._add_labels(pr["number"], labels)

        return pr

    def _add_labels(self, pr_number: int, labels: list[str]) -> None:
        import urllib.request, urllib.error, json
        url = self._url(f"/issues/{pr_number}/labels")
        data = json.dumps({"labels": labels}).encode()
        req = urllib.request.Request(url, data=data, headers=self._headers(), method="POST")
        try:
            urllib.request.urlopen(req, timeout=10)
        except urllib.error.HTTPError:
            pass   # 라벨 추가 실패 → 무시


# ── values.yaml 패치 ─────────────────────────────────────────────────────

def _patch_image_tag(yaml_text: str, new_tag: str) -> str:
    """
    values.yaml 에서 image.tag 값을 new_tag 로 교체.
    YAML 파서 없이 정규식 사용 (설계서 §템플릿 정책: 최소 의존성).

    지원 형식:
      image:
        tag: "old"     # 인용 포함
        tag: old       # 인용 없음
    """
    # 인용부호 있는 경우
    patched, n = re.subn(
        r'(^\s*tag\s*:\s*)["\']?([^"\'#\n]+?)["\']?(\s*(?:#.*)?)$',
        lambda m: f'{m.group(1)}"{new_tag}"{m.group(3)}',
        yaml_text,
        flags=re.MULTILINE,
    )
    if n == 0:
        # tag: 키가 없으면 image: 섹션 아래에 추가 시도
        patched = re.sub(
            r'(^\s*image\s*:)',
            f'\\1\n  tag: "{new_tag}"',
            yaml_text,
            count=1,
            flags=re.MULTILINE,
        )
    return patched


# ── PR body 생성 ─────────────────────────────────────────────────────────

def _build_pr_body(cfg: RollbackPRConfig) -> str:
    rec = cfg.deployment_record
    severity_emoji = {1: "🔴", 2: "🟠", 3: "🟡", 4: "🟢"}.get(rec.incident_severity, "⚪")

    rollback_risk_lines = [
        "- 이 PR 머지 시 ArgoCD 자동 sync → 이전 이미지로 즉시 재배포됩니다.",
        "- values.yaml 외 다른 변경(DB 마이그레이션 등)이 있었다면 수동 검증 필요.",
        "- emergency=True 로 실행된 경우 30분 이내 Git reconciliation 필수 (ADR-005).",
    ] if cfg.emergency else [
        "- 이 PR 머지 시 ArgoCD 자동 sync → 이전 이미지로 재배포됩니다.",
        "- 머지 전 staging 에서 `last_healthy_image_tag` 동작 확인 권장.",
    ]
    # f-string 식 부분에 backslash 가 들어가지 못하므로 외부에서 문자열을 미리 구성한다.
    rollback_risk_block = "\n".join(rollback_risk_lines)
    emergency_checkbox = (
        "- [ ] Emergency: ADR-005 §Sev-1 30분 이내 Git reconciliation 진행 예정"
        if cfg.emergency else ""
    )

    return f"""## 🔄 Rollback PR — {cfg.incident_id}

{severity_emoji} **Severity {rec.incident_severity}** | App: `{rec.app_name}` | Env: `{rec.environment}`

---

### Incident Summary

| 항목 | 값 |
|------|-----|
| 인시던트 ID | `{cfg.incident_id}` |
| 실패 이미지 | `{cfg.failed_image_tag}` |
| 복구 대상 이미지 | `{cfg.last_healthy_image_tag}` |
| 실패 시각 | `{rec.failed_at or datetime.now(timezone.utc).isoformat()}` |
| 배포자 | `{rec.deployed_by or "unknown"}` |
| 소요 시간 | `{rec.deploy_duration_s:.1f}s` |
| 클러스터 | `{rec.cluster or "N/A"}` |
| 네임스페이스 | `{rec.namespace}` |

**오류 요약:**
```
{rec.error_summary or "(오류 정보 없음)"}
```

---

### 변경 내용

- **파일**: `{cfg.helm_values_path}`
- `image.tag`: `{cfg.failed_image_tag}` → `{cfg.last_healthy_image_tag}`
- ArgoCD App: `{cfg.argocd_app_name}`

---

### RCA Candidate (초안)

> ⚠️ 이 섹션은 자동 생성된 초안입니다. Postmortem 작성 시 업데이트하세요.

1. **무엇이 실패했는가?** — 이미지 `{cfg.failed_image_tag}` 배포 후 서비스 이상 감지
2. **왜 실패했는가?** — (조사 필요: 배포 로그, OTel trace, CloudWatch 로그 확인)
3. **타임라인** — {rec.failed_at or "N/A"} 배포 시작 → 장애 감지 → 이 PR 생성
4. **재발 방지** — (조사 후 작성)

---

### Rollback Risk

{rollback_risk_block}

---

### Approval

- [ ] SRE / Engineering Lead 승인
{emergency_checkbox}

---

*이 PR 은 ReCoder `rollback_pr_agent` 에 의해 자동 생성되었습니다.*
*ADR-005 rollback 정책을 준수합니다.*
"""


# ── AuditLog ─────────────────────────────────────────────────────────────

def _write_audit_log(cfg: RollbackPRConfig, result: RollbackPRResult) -> str:
    """~/.recoder/audit/rollback_pr_{incident_id}.json 에 AuditLog 기록."""
    import json
    _AUDIT_LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = _AUDIT_LOG_DIR / f"rollback_pr_{cfg.incident_id}.json"
    rec = cfg.deployment_record
    payload = {
        "event":                "rollback_pr_created",
        "timestamp":            datetime.now(timezone.utc).isoformat(),
        "incident_id":          cfg.incident_id,
        "app_name":             rec.app_name,
        "environment":          rec.environment,
        "severity":             rec.incident_severity,
        "failed_image_tag":     cfg.failed_image_tag,
        "healthy_image_tag":    cfg.last_healthy_image_tag,
        "helm_values_path":     cfg.helm_values_path,
        "argocd_app_name":      cfg.argocd_app_name,
        "pr_url":               result.pr_url,
        "pr_number":            result.pr_number,
        "branch":               result.branch,
        "emergency":            cfg.emergency,
        "success":              result.success,
        "error":                result.error,
    }
    log_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    return str(log_path)


# ── RollbackPRAgent ───────────────────────────────────────────────────────

class RollbackPRAgent:
    """
    ADR-005 production rollback PR 생성 에이전트.

    호출 예::

        agent = RollbackPRAgent()
        result = agent.create_rollback_pr(cfg, log_fn=print)
    """

    def create_rollback_pr(
        self,
        cfg: RollbackPRConfig,
        log_fn=None,
    ) -> RollbackPRResult:
        logs: list[str] = []

        def _log(msg: str) -> None:
            logs.append(msg)
            logger.info(msg)
            if log_fn:
                log_fn(msg)

        result = RollbackPRResult(success=False, incident_id=cfg.incident_id, logs=logs)

        # ── 입력 검증 ─────────────────────────────────────────────────
        if not cfg.github_token:
            result.error = "GITHUB_TOKEN 환경변수가 설정되지 않았습니다."
            _log(f"[ERROR] {result.error}")
            return result
        if not cfg.github_repo:
            result.error = "GITHUB_REPO 환경변수가 설정되지 않았습니다. (예: owner/repo)"
            _log(f"[ERROR] {result.error}")
            return result
        if not cfg.failed_image_tag or not cfg.last_healthy_image_tag:
            result.error = "failed_image_tag / last_healthy_image_tag 가 비어 있습니다."
            _log(f"[ERROR] {result.error}")
            return result

        gh = GitHubClient(cfg.github_token, cfg.github_repo)
        rec = cfg.deployment_record

        try:
            # ── Step 1: rollback 브랜치 생성 ──────────────────────────
            ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
            safe_tag = re.sub(r"[^a-zA-Z0-9._-]", "-", cfg.last_healthy_image_tag)
            branch = f"{_ROLLBACK_BRANCH_PREFIX}/{cfg.incident_id}-{safe_tag}-{ts}"
            result.branch = branch
            _log(f"[rollback] 브랜치 생성: {branch}")
            gh.create_branch(branch, from_ref=cfg.base_branch)

            # ── Step 2: values.yaml 조회 및 패치 ─────────────────────
            _log(f"[rollback] {cfg.helm_values_path} 조회 중...")
            file_info = gh.get_file(cfg.helm_values_path, ref=cfg.base_branch)
            import base64
            original_b64: str = file_info.get("content", "")
            original_text = base64.b64decode(original_b64.replace("\n", "")).decode()
            file_sha: str = file_info.get("sha", "")

            _log(f"[rollback] image.tag 패치: {cfg.failed_image_tag} → {cfg.last_healthy_image_tag}")
            patched_text = _patch_image_tag(original_text, cfg.last_healthy_image_tag)
            patched_b64 = base64.b64encode(patched_text.encode()).decode()

            # ── Step 3: 패치된 파일 커밋 ─────────────────────────────
            commit_msg = (
                f"rollback({rec.app_name}): restore image.tag to {cfg.last_healthy_image_tag}\n\n"
                f"Incident: {cfg.incident_id}\n"
                f"Failed tag: {cfg.failed_image_tag}\n"
                f"Healthy tag: {cfg.last_healthy_image_tag}"
            )
            _log(f"[rollback] 파일 커밋: {cfg.helm_values_path}")
            gh.put_file(
                path=cfg.helm_values_path,
                message=commit_msg,
                content_b64=patched_b64,
                sha=file_sha,
                branch=branch,
            )

            # ── Step 4: PR 생성 ───────────────────────────────────────
            pr_title = (
                f"🔴 [EMERGENCY] rollback: restore {rec.app_name} to {cfg.last_healthy_image_tag}"
                if cfg.emergency else
                f"rollback: restore {rec.app_name} to {cfg.last_healthy_image_tag}"
            )
            pr_body = _build_pr_body(cfg)
            labels = [_PR_LABEL]
            if cfg.emergency:
                labels.append("emergency")
            if rec.incident_severity == 1:
                labels.append("severity-1")

            _log(f"[rollback] PR 생성 중: {pr_title}")
            pr = gh.create_pr(
                title=pr_title,
                body=pr_body,
                head=branch,
                base=cfg.base_branch,
                labels=labels,
            )
            result.pr_url    = pr.get("html_url", "")
            result.pr_number = pr.get("number", 0)
            _log(f"[rollback] PR 생성 완료: {result.pr_url}")

            # ── Step 5: AuditLog 기록 ─────────────────────────────────
            result.success = True
            audit_path = _write_audit_log(cfg, result)
            result.audit_log_path = audit_path
            _log(f"[rollback] AuditLog 기록: {audit_path}")

            # ── Severity 1 emergency 경고 ─────────────────────────────
            if cfg.emergency or rec.incident_severity == 1:
                _log(
                    "[rollback] ⚠️  Severity 1 / Emergency 감지: "
                    "이 PR 머지 후 30분 이내 Git reconciliation 필수 (ADR-005)"
                )

        except Exception as exc:
            result.error = str(exc)
            result.success = False
            _log(f"[ERROR] rollback PR 생성 실패: {exc}")
            # 실패해도 AuditLog 기록
            try:
                audit_path = _write_audit_log(cfg, result)
                result.audit_log_path = audit_path
            except Exception:
                pass

        return result


# ── 싱글턴 ───────────────────────────────────────────────────────────────

_rollback_pr_agent: Optional[RollbackPRAgent] = None


def get_rollback_pr_agent() -> RollbackPRAgent:
    global _rollback_pr_agent
    if _rollback_pr_agent is None:
        _rollback_pr_agent = RollbackPRAgent()
    return _rollback_pr_agent
