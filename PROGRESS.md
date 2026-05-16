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
| Q1 | AI 품질 기반 (AST 청킹, PEV, Eval) | ✅ **80%** | Must-Core 구현 완료. Eval 실측 + 빌드 자동화 남음 |
| Q2-A | Control Plane Core (Identity/Org/Audit) | ✅ **100%** | 21개 파일, 20개 API 엔드포인트 완료 |
| Q2-B | Governance (OPA, 2인 승인) | ✅ **100%** | 11개 엔드포인트, DoD 16/16 통과 |
| Q3 | Cloud Execution (ECS, SBOM) | 🔲 0% | Q2-B 완료됨 — 시작 가능 |
| Q4 | GitOps + Observability + MCP | 🔲 0% | Q3 완료 후 시작 |

---

## Q1 — AI 품질 기반

**DoD 기준 (설계서 §Q1)**
- [x] AST Chunker 구현 (Python AST, JS line-based fallback)
- [x] Plan-Execute-Verify 체인 구현
- [x] Eval Harness 구현 (6카테고리 19케이스)
- [ ] 카테고리별 pass_rate 60% 이상 (LLM 실측 필요)
- [ ] Safety violation 0건 (LLM 실측 필요)
- [ ] Windows x64, Linux x64 빌드 자동화 완료
- [ ] 청킹 인덱스 업데이트 지연 3초 이내
- [ ] Eval 실행 평균 소요 시간 5분 이내

### Must-Core 1차: AI 품질 기반

| # | 항목 | 파일 | 상태 | 메모 |
|---|---|---|---|---|
| Q1-1 | **AST Chunker 독립 모듈** | `core/chunker/` | ✅ | Python AST FunctionDef/ClassDef, JS line-based fallback, 1500토큰 상한, source text 미포함 (ADR-004) |
| Q1-2 | **Plan-Execute-Verify 체인** | `core/planner.py`, `executor.py`, `verifier.py`, `plan_execute_verify.py` | ✅ | PlannerAgent(LLM→최대 5단계), Executor(결정론적), VerifierAgent(LLM없음), 재시도 2회 |
| Q1-3 | **Eval Harness** | `core/eval/` | ✅ | 6카테고리×19케이스, SafetyChecker, CI gate |
| Q1-4 | **Q1 스키마 추가** | `core/schemas.py` | ✅ | ChunkMetadata, ExecutionPlan, VerificationResult, EvalCase/Result/Report |
| Q1-5 | **Eval 실제 실행 + DoD 달성** | `core/eval/` | 🔲 | LLM 연동 후 pass_rate 측정 필요 |
| Q1-6 | **PyInstaller 빌드 자동화** | `core/recoder.spec` | 🔲 | Windows x64, Linux x64 |

### 기존 Local Core

| 항목 | 상태 |
|---|---|
| FastAPI 서버 + 세션토큰 인증 | ✅ |
| ContextGate (16종 마스킹) | ✅ |
| LLM Router (Bedrock/Gemini) | ✅ |
| CodeAgent / InfraAgent / DeployAgent / OpsAgent | ✅ |
| Orchestrator FSM + RiskValidator | ✅ |
| Registry (Command/File Templates) | ✅ |
| VSCode Extension + Shell Integration + Sidebar | ✅ |
| terminalDataWriteEvent 오류 수정 | ✅ |

---

## Q2-A — Control Plane Core ✅

**브랜치**: `feat/q1-enterprise-foundation`

### Q2-A1: Identity & Device ✅

| # | 항목 | 파일 | 상태 |
|---|---|---|---|
| A1-1 | Google/GitHub OIDC 로그인 | `control_plane/api/routes/auth.py` | ✅ |
| A1-2 | Device 등록 + Token 발급 (raw→OS Keychain, DB에 SHA-256만) | `control_plane/services/identity.py` | ✅ |
| A1-3 | Device Token TTL (짧은 lease) | `identity.py` | ✅ |
| A1-4 | Heartbeat 1분 간격 + 차단 | `control_plane/api/routes/devices.py` | ✅ |
| A1-5 | Device 폐기 → 다음 heartbeat 즉시 차단 | `identity.py` | ✅ |
| A1-6 | 오프라인 Level 1~2/3/4/production 정책 | `identity.py` | ✅ |

### Q2-A2: Organization & RBAC ✅

| # | 항목 | 파일 | 상태 |
|---|---|---|---|
| A2-1 | Organization / Workspace / Project / User / OrgMember ORM | `control_plane/db/models.py` | ✅ |
| A2-2 | RBAC 6역할 × 15권한 매핑 | `control_plane/models/schemas.py` | ✅ |
| A2-3 | 멀티테넌트 org_id 격리 (미들웨어 + RLS) | `device_auth.py`, `migrations.py` | ✅ |
| A2-4 | 최소 1 owner 보장 | `org_service.py` | ✅ |
| A2-5 | Org/Workspace/Project CRUD 20개 엔드포인트 | `control_plane/api/routes/orgs.py` | ✅ |

### Q2-A3: AuditLog & Sync ✅

| # | 항목 | 파일 | 상태 |
|---|---|---|---|
| A3-1 | hash chain (SHA-256, org 단위 monotonic seq) | `control_plane/services/audit.py` | ✅ |
| A3-2 | SELECT FOR UPDATE 동시성 안전 | `audit.py` | ✅ |
| A3-3 | UPDATE/DELETE 금지 트리거 | `control_plane/db/migrations.py` | ✅ |
| A3-4 | PostgreSQL Row Level Security | `migrations.py` | ✅ |
| A3-5 | 오프라인 pending queue 재전송 | `control_plane/api/routes/audit.py` | ✅ |
| A3-6 | hash chain 무결성 검증 API | `audit.py` | ✅ |

---

## Q2-B — Governance ✅

**브랜치**: `feat/q1-enterprise-foundation`

### OPA 정책 엔진 ✅

| # | 항목 | 파일 | 상태 |
|---|---|---|---|
| B-1 | OPA REST API 클라이언트 (ADR-003) | `core/opa_client.py` | ✅ |
| B-2 | fail-closed (Level 3~4 차단) | `opa_client.py` | ✅ |
| B-3 | OPA 5단계 판정 | `control_plane/models/schemas.py` | ✅ |
| B-4 | PolicyBundle sha256 + version 부여 | `control_plane/services/policy_service.py` | ✅ |
| B-5 | Preset 5개 → Rego 자동 생성 | `policy_service.py` | ✅ |
| B-6 | PolicyBundle 로컬 캐시 + sha256 검증 | `core/policy_cache.py` | ✅ |
| B-7 | Rego → OPA 자동 로드 | `policy_cache.py` | ✅ |
| B-8 | AuditLog 정책 변경/평가 100% 기록 | `control_plane/api/routes/policy.py` | ✅ |
| B-9 | deny_with_fix_suggestion 수정 가이드 | `opa_client.py`, `policy.py` | ✅ |

### Multi-Approver 승인 흐름 ✅

| # | 항목 | 파일 | 상태 |
|---|---|---|---|
| B-10 | ApprovalRequest 생성 (allow_with_approval 시) | `control_plane/services/approval_service.py` | ✅ |
| B-11 | 자기 승인 방지 | `control_plane/api/routes/approvals.py` | ✅ |
| B-12 | 거부 사유 필수 입력 | `approval_service.py` | ✅ |
| B-13 | 1건 거부 → 즉시 rejected | `approval_service.py` | ✅ |
| B-14 | required_approvers 충족 → approved | `approval_service.py` | ✅ |
| B-15 | 타임아웃 24시간 기본 | `control_plane/models/schemas.py` | ✅ |
| B-16 | 모든 투표 AuditLog 추적 | `approval_service.py` | ✅ |

**남은 항목 (Should)**
- [ ] Slack 버튼 승인 연동
- [ ] 이메일 알림
- [ ] Mini-Wedge 시나리오 E2E 테스트

---

## Q3 — Cloud Execution

**전제**: Q2-B 완료 ✅ → 시작 가능

| # | 항목 | 상태 | 메모 |
|---|---|---|---|
| C-1 | Cloud Preflight Assistant (read-only IAM) | 🔲 | ecr/ecs/iam/logs/elb describe 권한만 |
| C-2 | ECS Rolling Update (Must) | 🔲 | ADR-002: ECS Fargate 표준 경로 |
| C-3 | SBOM 생성 (Syft, CycloneDX, Must) | 🔲 | |
| C-4 | Trivy/Hadolint/gitleaks OPA 게이트 | 🔲 | |
| C-5 | ECS Blue/Green (Should) | 🔲 | Q3-A 안정화 후 |
| C-6 | Cosign signing (Should) | 🔲 | |

---

## Q4 — GitOps + Observability + MCP

**전제**: Q3 DoD 달성 후 시작. 쐐기 시나리오 7단계 완성 목표.

| # | 항목 | 상태 |
|---|---|---|
| D-1 | GitOps ArgoCD 연동 (Must-Wedge) | 🔲 |
| D-2 | Incident Timeline MVP | 🔲 |
| D-3 | RCA (근거 기반 후보 제안, confidence score) | 🔲 |
| D-4 | rollback PR 자동 생성 | 🔲 |
| D-5 | Postmortem skeleton 생성 | 🔲 |
| D-6 | OTel Collector (Prometheus + Loki) | 🔲 |
| D-7 | MCP stdio PoC | 🔲 |
| D-8 | **Final Demo: 쐐기 시나리오 7단계** | 🔲 |

---

## 업데이트 기록

| 날짜 | 내용 |
|---|---|
| 2026-05-07 | v6.4-final 기준 1차 구현 |
| 2026-05-16 | Enterprise v5.0 전환. terminalDataWriteEvent 수정, Q1 Must-Core 구현 |
| 2026-05-16 | **Q2-A 완료**: Control Plane Core 21파일, 20 API (Identity/Device/Org/RBAC/AuditLog) |
| 2026-05-16 | **Q2-B 완료**: OPA 정책 엔진 + Multi-Approver. 11 API, DoD 16/16 통과 |
