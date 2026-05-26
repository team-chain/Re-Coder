"""
core/visual_diff/infra_differ.py — Visual Diff for Infrastructure (설계서 §42)

§42 Visual Diff for Infrastructure:
  - Terraform(.tf), Kubernetes YAML(.yaml/.yml), ECS Task Definition(.json) 지원
  - 변경 유형 분류: ADD / REMOVE / MODIFY / RENAME / SECURITY_RISK
  - 보안 민감 변경 자동 감지 (포트 개방, IAM 권한 확대, 0.0.0.0/0, root 컨테이너)
  - 변경 영향도 점수 (0~100)
  - Haiku로 자연어 변경 요약 생성
  - VSCode WebView 및 Discord Embed 양쪽에서 소비

출력: InfraDiffReport — 변경 항목 목록 + 요약 + 보안 경고.
"""

from __future__ import annotations

import difflib
import json
import logging
import re
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

# ── 위험 패턴 (보안 민감 변경 감지) ─────────────────────────────────────
_SECURITY_PATTERNS = [
    (re.compile(r"0\.0\.0\.0/0"), "전체 IP 개방 (0.0.0.0/0) — 보안 위험"),
    (re.compile(r"::/0"), "전체 IPv6 개방 (::/0) — 보안 위험"),
    (re.compile(r'"privileged"\s*:\s*true'), "컨테이너 privileged 모드 활성화"),
    (re.compile(r"runAsUser\s*:\s*0"), "root 사용자로 컨테이너 실행"),
    (re.compile(r"(AdministratorAccess|FullAccess)", re.I), "광범위 IAM 권한 부여"),
    (re.compile(r"allow_public_access"), "퍼블릭 접근 허용"),
    (re.compile(r"enable_deletion_protection\s*=\s*false"), "삭제 보호 비활성화"),
    (re.compile(r"(password|secret|key)\s*=\s*[\"'][^\"']{8,}", re.I), "하드코딩된 시크릿 의심"),
    (re.compile(r"ssl_policy\s*=\s*[\"']ELBSecurityPolicy-2016", re.I), "구버전 TLS 정책 사용"),
]


# ── 데이터 모델 ────────────────────────────────────────────────────────────

@dataclass
class DiffHunk:
    """단일 변경 덩어리 (파일 내 연속된 변경 라인 그룹)."""
    file_path: str
    change_type: str        # ADD / REMOVE / MODIFY / RENAME / SECURITY_RISK
    old_start: int
    new_start: int
    context: str            # 변경 전후 컨텍스트 (unified diff 형식)
    severity: str = "INFO"  # INFO / WARN / ERROR
    security_warnings: List[str] = field(default_factory=list)
    impact_score: int = 0   # 0~100 (영향도)
    resource_type: str = "" # aws_ecs_service / kubernetes_deployment / etc.


@dataclass
class InfraDiffReport:
    """전체 인프라 변경 리포트 (§42)."""
    generated_at: str
    base_ref: str           # git 기준 커밋/브랜치
    head_ref: str           # git 비교 커밋/브랜치
    summary: str            # 자연어 요약
    total_changes: int = 0
    files_changed: int = 0
    security_risks: int = 0
    overall_impact_score: int = 0   # 0~100
    hunks: List[DiffHunk] = field(default_factory=list)
    security_warnings: List[str] = field(default_factory=list)
    recommended_actions: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)

    @property
    def risk_color(self) -> str:
        if self.security_risks > 0:
            return "#ef4444"
        if self.overall_impact_score >= 70:
            return "#f97316"
        if self.overall_impact_score >= 40:
            return "#f59e0b"
        return "#10b981"

    # ── §42 Visual 출력 ──────────────────────────────────────────────────
    #
    # 스펙은 "Mermaid 또는 D3로 인프라 시각화 diff"를 요구한다. unified diff
    # 텍스트만으로는 시각화가 아니므로, 변경된 리소스 그래프를
    # Mermaid `graph TD` 문법으로 직렬화한다.
    #
    # 표기 규약:
    #   - 노드 색상: 보안 위험 hunk = 빨강, 영향도 ≥70 = 주황, 그 외 = 파랑
    #   - 노드 라벨: "<filename>\n<resource_type>"
    #   - 엣지 라벨: change_type (ADD/REMOVE/MODIFY/SECURITY_RISK 등)
    #   - 보안 경고는 별도 클러스터로 묶어 한 눈에 보이게 한다.
    #
    # 출력은 ```mermaid 코드 블록 안에 그대로 넣을 수 있는 문자열이다.
    def to_mermaid(self) -> str:
        """변경된 인프라 리소스를 Mermaid graph TD 다이어그램으로 직렬화."""
        lines: List[str] = ["graph TD"]
        lines.append(f'    BASE["{self.base_ref}"]:::baseRef')
        lines.append(f'    HEAD["{self.head_ref}"]:::headRef')
        lines.append("    BASE --> HEAD")

        if not self.hunks:
            lines.append('    HEAD --> NONE["변경된 인프라 파일 없음"]')
        else:
            # 파일별로 그룹화 — 한 파일에 여러 hunk가 있을 수 있으므로
            # 가장 위험한 hunk(보안 > impact 점수)를 대표 노드 스타일로 사용.
            file_repr: Dict[str, DiffHunk] = {}
            for h in self.hunks:
                cur = file_repr.get(h.file_path)
                if cur is None:
                    file_repr[h.file_path] = h
                    continue
                cur_is_risk = bool(cur.security_warnings) or cur.change_type == "SECURITY_RISK"
                new_is_risk = bool(h.security_warnings) or h.change_type == "SECURITY_RISK"
                if (new_is_risk and not cur_is_risk) or h.impact_score > cur.impact_score:
                    file_repr[h.file_path] = h

            for idx, (fp, hunk) in enumerate(file_repr.items()):
                node_id = f"F{idx}"
                label_file = fp.replace('"', "'")
                label_res = hunk.resource_type or "infra"
                # mermaid label 내 줄바꿈은 <br/>
                label = f"{label_file}<br/>{label_res}"
                style_cls = "risk" if (hunk.security_warnings or hunk.change_type == "SECURITY_RISK") \
                    else ("warn" if hunk.impact_score >= 70 else "info")
                lines.append(f'    HEAD -->|{hunk.change_type}| {node_id}["{label}"]:::{style_cls}')

                # 보안 경고는 sub-node로 노출
                for w_idx, warning in enumerate(hunk.security_warnings[:3]):
                    w_id = f"{node_id}_W{w_idx}"
                    w_label = warning.replace('"', "'")
                    lines.append(f'    {node_id} -.->|risk| {w_id}["{w_label}"]:::risk')

        # 클래스 정의
        lines.extend([
            "    classDef baseRef fill:#1f2937,stroke:#9ca3af,color:#e5e7eb",
            "    classDef headRef fill:#1e3a8a,stroke:#60a5fa,color:#e0f2fe",
            "    classDef info fill:#0f172a,stroke:#3b82f6,color:#bfdbfe",
            "    classDef warn fill:#78350f,stroke:#f97316,color:#fed7aa",
            "    classDef risk fill:#7f1d1d,stroke:#ef4444,color:#fecaca",
        ])
        return "\n".join(lines)


# ── 지원 파일 타입 판별 ────────────────────────────────────────────────────

def _detect_resource_type(content: str, file_path: str) -> str:
    """파일 내용/경로에서 인프라 리소스 타입을 판별한다."""
    fp = file_path.lower()
    if fp.endswith(".tf"):
        for line in content.splitlines()[:20]:
            m = re.search(r'resource\s+"([\w_]+)"', line)
            if m:
                return m.group(1)
        return "terraform"
    if fp.endswith((".yaml", ".yml")):
        for line in content.splitlines()[:20]:
            m = re.search(r"^kind:\s+(\w+)", line)
            if m:
                return f"k8s_{m.group(1).lower()}"
        return "kubernetes"
    if fp.endswith(".json") and "taskDefinition" in content[:500]:
        return "ecs_task_definition"
    return "unknown"


def _calc_impact_score(hunk: DiffHunk, added: int, removed: int) -> int:
    """변경 영향도를 0~100으로 계산한다."""
    score = 0
    # 보안 리스크
    score += len(hunk.security_warnings) * 25
    # 변경 라인 수 (최대 30점)
    score += min((added + removed) * 2, 30)
    # 리소스 타입별 가중치
    high_impact = ("iam", "security_group", "alb", "ecs_service", "k8s_deployment", "k8s_service")
    if any(t in hunk.resource_type for t in high_impact):
        score += 20
    # REMOVE는 ADD보다 위험
    score += removed * 1
    return min(score, 100)


# ── 메인 Differ ────────────────────────────────────────────────────────────

class InfraDiffer:
    """
    인프라 파일의 시각적 diff를 생성한다 (§42).

    사용법:
        differ = InfraDiffer()
        report = await differ.diff(base="main", head="feature/update-ecs")
    """

    def __init__(self) -> None:
        self._haiku: Optional[Any] = None
        self._try_init_haiku()

    def _try_init_haiku(self) -> None:
        try:
            import anthropic
            import os
            key = os.getenv("ANTHROPIC_API_KEY", "")
            if key:
                self._haiku = anthropic.AsyncAnthropic(api_key=key)
        except ImportError:
            pass

    async def diff(
        self,
        base: str = "HEAD~1",
        head: str = "HEAD",
        paths: Optional[List[str]] = None,
        repo_path: str = ".",
    ) -> InfraDiffReport:
        """
        git diff를 실행하여 인프라 파일 변경을 분석한다.

        paths: 특정 파일/디렉토리만 분석 (None이면 전체 인프라 파일)
        """
        now = datetime.now(tz=timezone.utc)

        # git diff 실행
        raw_diffs = self._run_git_diff(base, head, paths, repo_path)

        hunks: List[DiffHunk] = []
        all_warnings: List[str] = []

        for file_path, old_content, new_content in raw_diffs:
            file_hunks = self._analyze_file_diff(file_path, old_content, new_content)
            hunks.extend(file_hunks)
            for h in file_hunks:
                all_warnings.extend(h.security_warnings)

        # 파일별 그룹화
        files_changed = len(set(h.file_path for h in hunks))
        total_changes = len(hunks)
        security_risks = sum(1 for h in hunks if h.security_warnings)
        overall_impact = min(sum(h.impact_score for h in hunks[:10]), 100)

        # 권고 액션
        recommended = self._build_recommendations(hunks, all_warnings)

        # 자연어 요약
        summary = await self._generate_summary(
            base=base,
            head=head,
            hunks=hunks,
            security_risks=security_risks,
            overall_impact=overall_impact,
        )

        return InfraDiffReport(
            generated_at=now.isoformat(),
            base_ref=base,
            head_ref=head,
            summary=summary,
            total_changes=total_changes,
            files_changed=files_changed,
            security_risks=security_risks,
            overall_impact_score=overall_impact,
            hunks=hunks,
            security_warnings=list(set(all_warnings)),
            recommended_actions=recommended,
        )

    def diff_strings(
        self,
        old_content: str,
        new_content: str,
        file_path: str = "infra.tf",
    ) -> List[DiffHunk]:
        """
        git 없이 두 문자열을 직접 비교한다.
        단위 테스트 / API 엔드포인트에서 사용.
        """
        return self._analyze_file_diff(file_path, old_content, new_content)

    # ── git diff 실행 ──────────────────────────────────────────────────────

    def _run_git_diff(
        self,
        base: str,
        head: str,
        paths: Optional[List[str]],
        repo_path: str,
    ) -> List[Tuple[str, str, str]]:
        """
        git diff --name-only → 각 파일 content 조회.
        반환: [(file_path, old_content, new_content), ...]
        """
        # 인프라 파일 필터
        extensions = ("*.tf", "*.yaml", "*.yml", "*.json")
        ext_filter = [f"**/{ext}" for ext in extensions]

        try:
            # 변경된 파일 목록
            cmd = ["git", "diff", "--name-only", base, head]
            if paths:
                cmd.extend(["--"] + paths)
            result = subprocess.run(
                cmd, capture_output=True, text=True, cwd=repo_path, timeout=10
            )
            if result.returncode != 0:
                log.warning("git diff 실패: %s", result.stderr)
                return []

            changed_files = [
                f for f in result.stdout.strip().splitlines()
                if any(f.endswith(ext.lstrip("*")) for ext in (".tf", ".yaml", ".yml", ".json"))
            ]

            results = []
            for fp in changed_files[:20]:  # 최대 20개
                old = self._git_show(f"{base}:{fp}", repo_path)
                new = self._git_show(f"{head}:{fp}", repo_path)
                results.append((fp, old, new))
            return results

        except Exception as exc:
            log.warning("git diff 실행 실패: %s", exc)
            return []

    def _git_show(self, ref: str, repo_path: str) -> str:
        """git show <ref>로 파일 내용을 가져온다."""
        try:
            result = subprocess.run(
                ["git", "show", ref],
                capture_output=True, text=True, cwd=repo_path, timeout=5,
            )
            return result.stdout if result.returncode == 0 else ""
        except Exception:
            return ""

    # ── 파일 분석 ──────────────────────────────────────────────────────────

    def _analyze_file_diff(
        self,
        file_path: str,
        old_content: str,
        new_content: str,
    ) -> List[DiffHunk]:
        """단일 파일의 변경 사항을 분석한다."""
        hunks: List[DiffHunk] = []

        if not old_content and not new_content:
            return hunks

        # 변경 타입 결정
        if not old_content:
            change_type = "ADD"
        elif not new_content:
            change_type = "REMOVE"
        else:
            change_type = "MODIFY"

        # unified diff 생성
        old_lines = old_content.splitlines(keepends=True)
        new_lines = new_content.splitlines(keepends=True)
        diff = list(difflib.unified_diff(
            old_lines, new_lines,
            fromfile=f"a/{file_path}",
            tofile=f"b/{file_path}",
            n=3,
        ))

        if not diff and change_type == "MODIFY":
            return hunks  # 변경 없음

        # 보안 위험 감지
        security_warnings = self._detect_security_risks(new_content, old_content)
        if security_warnings:
            change_type = "SECURITY_RISK"

        # 리소스 타입 감지
        resource_type = _detect_resource_type(new_content or old_content, file_path)

        # unified diff를 hunk 단위로 분할
        context_str = "".join(diff[:80])  # 최대 80라인

        added = sum(1 for l in diff if l.startswith("+") and not l.startswith("+++"))
        removed = sum(1 for l in diff if l.startswith("-") and not l.startswith("---"))

        severity = "ERROR" if security_warnings else \
                   "WARN" if (added + removed) > 20 else "INFO"

        hunk = DiffHunk(
            file_path=file_path,
            change_type=change_type,
            old_start=1,
            new_start=1,
            context=context_str,
            severity=severity,
            security_warnings=security_warnings,
            resource_type=resource_type,
        )
        hunk.impact_score = _calc_impact_score(hunk, added, removed)
        hunks.append(hunk)

        return hunks

    def _detect_security_risks(
        self, new_content: str, old_content: str
    ) -> List[str]:
        """
        새 내용에만 나타난 보안 민감 패턴을 감지한다.
        기존에 이미 있던 패턴은 무시한다.
        """
        warnings: List[str] = []
        for pattern, message in _SECURITY_PATTERNS:
            new_matches = pattern.findall(new_content)
            old_matches = pattern.findall(old_content)
            # 새로 추가된 패턴만 경고
            if len(new_matches) > len(old_matches):
                warnings.append(message)
        return warnings

    def _build_recommendations(
        self, hunks: List[DiffHunk], warnings: List[str]
    ) -> List[str]:
        actions: List[str] = []
        if any("0.0.0.0/0" in w for w in warnings):
            actions.append("보안 그룹 인바운드 규칙에서 0.0.0.0/0 제거를 검토하세요.")
        if any("IAM" in w or "FullAccess" in w for w in warnings):
            actions.append("IAM 권한을 최소 권한 원칙(Least Privilege)에 맞게 제한하세요.")
        if any("하드코딩된 시크릿" in w for w in warnings):
            actions.append("시크릿/패스워드는 AWS Secrets Manager 또는 환경변수로 관리하세요.")
        if any("privileged" in w for w in warnings):
            actions.append("컨테이너 privileged 모드 사용 이유를 검토하세요.")
        if not actions and hunks:
            actions.append("변경 사항 검토 후 배포를 진행하세요.")
        return actions

    # ── 자연어 요약 ────────────────────────────────────────────────────────

    async def _generate_summary(
        self,
        base: str,
        head: str,
        hunks: List[DiffHunk],
        security_risks: int,
        overall_impact: int,
    ) -> str:
        """Haiku로 인프라 변경 자연어 요약을 생성한다."""
        if not self._haiku or not hunks:
            return self._fallback_summary(hunks, security_risks, overall_impact)

        changed_files = list(set(h.file_path for h in hunks[:5]))
        warnings = list(set(w for h in hunks for w in h.security_warnings))

        prompt = (
            f"인프라 변경 요약을 2~3문장 한국어로 작성하세요.\n\n"
            f"기준: {base} → {head}\n"
            f"변경 파일: {', '.join(changed_files)}\n"
            f"보안 경고: {'; '.join(warnings) or '없음'}\n"
            f"전체 영향도: {overall_impact}/100\n\n"
            "요약 (2~3문장, 이모지 1개):"
        )
        try:
            resp = await self._haiku.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=180,
                messages=[{"role": "user", "content": prompt}],
            )
            return resp.content[0].text.strip()
        except Exception as exc:
            log.warning("Haiku 요약 실패: %s", exc)
            return self._fallback_summary(hunks, security_risks, overall_impact)

    def _fallback_summary(
        self,
        hunks: List[DiffHunk],
        security_risks: int,
        impact: int,
    ) -> str:
        if not hunks:
            return "인프라 변경 사항이 없습니다."
        files = list(set(h.file_path for h in hunks))[:3]
        summary = f"📋 {len(files)}개 인프라 파일이 변경되었습니다 ({', '.join(files)})."
        if security_risks:
            summary += f" ⚠️ 보안 리스크 {security_risks}건이 감지되었습니다."
        return summary
