# ReCoder Enterprise — 구현 진척률 트래커

> **설계서 버전**: Enterprise Final v5.0 (1년 로드맵)
> **마지막 업데이트**: 2026-05-16
> **담당**: 이동규(백엔드·인프라), 윤세빈(Extension·코드 분석)
> **AI 구현 진행**: Claude (Cowork)

---

## 범례

| 아이콘 | 상태 |
|---|---|
| 🔲 | 미시작 |
| 🔄 | 진행 중 |
| ✅ | 완료 |
| ⚠ | 부분 완료 / 검증 필요 |
| ❌ | 블로킹 |

---

## 전체 진척률

| 분기 | 범위 | 진척률 | 비고 |
|---|---|---|---|
| Q1 | AI 품질 기반 (AST 청킹, PEV, Eval) | 🔄 **60%** | Must-Core 1차 완료. DoD 검증 필요 |
| Q2-A | Control Plane Core (Identity/Org/Audit) | 🔲 0% | Q1 완료 후 시작 |
| Q2-B | Governance (OPA, 2인 승인) | 🔲 0% | Q2-A 안정화 후 시작 |
| Q3 | Cloud Execution (ECS, SBOM) | 🔲 0% | Q2-B 완료 후 시작 |
| Q4 | GitOps + Observability + MCP | 🔲 0% | Q3 완료 후 시작 |

---

## Q1 — AI 품질 기반

**DoD 기준 (설계서 §Q1)**
- [ ] 카테고리별 pass_rate 60% 이상
- [ ] Safety violation 0건
- [ ] Windows x64, Linux x64 빌드 자동화 완료
- [ ] 다중 파일 PatchProposal 안정 동작
- [ ] 청킹 인덱스 업데이트 지연 3초 이내
- [ ] Eval 실행 평균 소요 시간 5분 이내

### Must-Core 1차: AI 품질 기반

| # | 항목 | 파일 | 상태 | 메모 |
|---|---|---|---|---|
| Q1-1 | **AST Chunker 독립 모듈** | `core/chunker/` | ✅ | Python AST FunctionDef/ClassDef, JS line-based fallback, 1500토큰 상한, source text 미포함 (ADR-004), 민감파일 제외 |
| Q1-2 | **Plan-Execute-Verify 체인** | `core/planner.py`, `executor.py`, `verifier.py`, `plan_execute_verify.py` | ✅ | PlannerAgent(LLM→최대 5단계), Executor(결정론적), VerifierAgent(LLM없음), 재시도 2회 |
| Q1-3 | **Eval Harness** | `core/eval/` | ✅ | 6카테고리×19케이스, SafetyChecker, CI gate(violation=0 AND pass_rate≥60%) |
| Q1-4 | **Q1 스키마 추가** | `core/schemas.py` | ✅ | ChunkMetadata, ExecutionPlan, VerificationResult, EvalCase/Result/Report |
| Q1-5 | **Eval 실제 실행 + DoD 달성** | `core/eval/` | 🔲 | LLM 연동 후 pass_rate 측정 필요 |
| Q1-6 | **PyInstaller 빌드 자동화** | `core/recoder.spec` | 🔲 | Windows x64, Linux x64 |
| Q1-7 | **Local Core 안정화 검증** | `core/` 전체 | ⚠ | extension-terminal-api 브랜치에서 기존 기능 수정 완료. 실 LLM 연동 E2E 필요 |

### 기존 Local Core (이전 v6.4 설계 기반 — 재사용)

| 항목 | 상태 | 비고 |
|---|---|---|
| FastAPI 서버 + 세션토큰 인증 | ✅ | `core/main.py`, `core/api/` |
| ContextGate (16종 마스킹) | ✅ | `core/context_gate.py` |
| LLM Router (Bedrock/Gemini) | ✅ | `core/llm/` |
| CodeAgent (에러 분석 + 패치) | ✅ | `core/agents/code_agent.py` |
| InfraAgent (Dockerfile 생성) | ✅ | `core/agents/infra_agent.py` |
| DeployAgent (Docker/EC2) | ✅ | `core/agents/deploy_agent.py` |
| OpsAgent | ✅ | `core/agents/ops_agent.py` |
| Orchestrator FSM | ✅ | `core/orchestrator.py` |
| RiskValidator | ✅ | `core/risk_validator.py` |
| Registry (Command/File Templates) | ✅ | `core/registry/` |
| VSCode Extension (기본 기능) | ✅ | `extension/src/` |
| Shell Integration 터미널 수집 | ✅ | `extension/src/terminal/` |
| Sidebar Webview | ✅ | `extension/src/sidebar/` |
| terminalDataWriteEvent 오류 수정 | ✅ | `fix/extension-terminal-api` 브랜치 |

---

## Q2-A — Control Plane Core

**전제**: Q1 DoD 달성 후 시작

### Q2-A1: Identity & Device

| # | 항목 | 상태 | 메모 |
|---|---|---|---|
| A1-1 | Google/GitHub OIDC 로그인 | 🔲 | ADR-006: 14일 체크포인트, 21일 초과 시 BaaS 피봇 |
| A1-2 | Device enrollment + Token 발급 | 🔲 | |
| A1-3 | OS Keychain 저장 | 🔲 | |
| A1-4 | Heartbeat 1분 간격 | 🔲 | |
| A1-5 | Device 폐기 → 다음 heartbeat 차단 | 🔲 | |

**DoD**: OIDC 로그인, Device Token Keychain 저장, heartbeat 동작, 폐기 후 차단

### Q2-A2: Organization & Project

| # | 항목 | 상태 |
|---|---|---|
| A2-1 | Organization / Workspace / Project / User / OrgMember 모델 | 🔲 |
| A2-2 | RBAC (owner/admin/developer/approver/auditor/viewer) | 🔲 |
| A2-3 | 멀티테넌트 org_id 격리 (미들웨어 + PostgreSQL RLS) | 🔲 |

### Q2-A3: Audit & Sync

| # | 항목 | 상태 |
|---|---|---|
| A3-1 | AuditLog (hash chain, org_id 단위 monotonic sequence) | 🔲 |
| A3-2 | offline pending queue + 재전송 | 🔲 |
| A3-3 | S3 Object Lock WORM 장기 보관 | 🔲 |

---

## Q2-B — Governance

**전제**: Q2-A 완전 안정화 후 시작

| # | 항목 | 상태 | 메모 |
|---|---|---|---|
| B-1 | OPA server (REST API 방식, ADR-003) | 🔲 | fail-closed 원칙 |
| B-2 | PolicyBundle 무결성 검증 (sha256 + version) | 🔲 | |
| B-3 | 5단계 OPA 출력 (allow/deny/escalate 등) | 🔲 | |
| B-4 | Preset Policy Templates 5개 | 🔲 | 체크박스 → Rego 변환 |
| B-5 | Web UI 기반 2인 승인 (Must) | 🔲 | 쐐기 시나리오 직접 필요 |
| B-6 | Mini-Wedge 시나리오 동작 | 🔲 | OPA 차단 → 팀장 2인 승인 흐름 |

---

## Q3 — Cloud Execution

**전제**: Q2-B DoD 달성 후 시작

| # | 항목 | 상태 | 메모 |
|---|---|---|---|
| C-1 | Cloud Preflight Assistant (read-only IAM) | 🔲 | |
| C-2 | ECS Rolling Update (Must) | 🔲 | ADR-002: ECS Fargate 표준 경로 |
| C-3 | SBOM 생성 (Syft, CycloneDX, Must) | 🔲 | |
| C-4 | Trivy/Hadolint/gitleaks OPA 게이트 | 🔲 | |
| C-5 | ECS Blue/Green (Should) | 🔲 | Q3-A 안정화 후 |
| C-6 | Cosign signing (Should) | 🔲 | |

---

## Q4 — GitOps + Observability + MCP

**전제**: Q3 DoD 달성 후 시작. 쐐기 시나리오 7단계 완성 목표.

| # | 항목 | 상태 | 메모 |
|---|---|---|---|
| D-1 | GitOps ArgoCD 연동 (Must-Wedge) | 🔲 | ADR-005: Git revert PR 기본 |
| D-2 | Incident Timeline MVP | 🔲 | |
| D-3 | RCA (근거 기반 후보 제안, confidence score) | 🔲 | "원인입니다" 금지 |
| D-4 | rollback PR 자동 생성 | 🔲 | |
| D-5 | Postmortem skeleton 생성 | 🔲 | |
| D-6 | OTel Collector (Prometheus + Loki) | 🔲 | Tempo는 Q4 후반 |
| D-7 | MCP stdio PoC (recoder_analyze 1개) | 🔲 | |
| D-8 | **Final Demo: 쐐기 시나리오 10단계** | 🔲 | 실제 EKS (ADR-009) |

---

## 이슈 / 블로킹

| 날짜 | 항목 | 상태 |
|---|---|---|
| 2026-05-16 | git index.lock — sandbox 권한 문제로 commit 불가 | ⚠ PowerShell에서 수동 commit 필요 |
| 2026-05-16 | Q1 Eval DoD — LLM 실제 연동 후 pass_rate 측정 필요 | 🔄 |

---

## 업데이트 기록

| 날짜 | 내용 |
|---|---|
| 2026-05-07 | v6.4-final 기준 1차 구현 (Phase 0~8) |
| 2026-05-08 | P0-1~P0-13 전항목 완료, pytest 15건 통과 |
| 2026-05-10 | 런타임 버그 3종 수정 |
| 2026-05-12 | 런타임 버그 추가 수정 + EC2 배포 구현 |
| 2026-05-16 | **설계서 Enterprise v5.0으로 전환.** terminalDataWriteEvent 오류 수정, extension.ts 복구, TypeScript 재컴파일. Q1 Must-Core 1차 구현: AST Chunker(core/chunker/), Plan-Execute-Verify(planner/executor/verifier), Eval Harness(core/eval/, 19케이스), Q1 스키마 추가 |
