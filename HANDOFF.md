# ReCoder Enterprise — 인계 문서

> **설계서**: Enterprise Final v5.0
> **현재 브랜치**: `feat/q1-enterprise-foundation`
> **마지막 업데이트**: 2026-05-16

---

## 현재 상태 요약

**완료된 분기**: Q1 + Q2-A + Q2-B

Q1 Must-Core, Control Plane Core(Q2-A), OPA 정책 엔진 + Multi-Approver(Q2-B)가 구현됐다.
다음 작업은 **Q3 — ECS Fargate 배포 + SBOM**이다.

---

## 아키텍처 개요 (3 Plane)

```
Local Execution Plane      Control Plane           Cloud Execution Plane
─────────────────────      ─────────────           ─────────────────────
VSCode Extension    ──▶    FastAPI (18000)    ──▶   ECS Fargate (Q3)
Local Core (17894)          PostgreSQL               EKS + ArgoCD (Q4)
OPA (8181)                  PolicyBundle             OTel Collector
policy_cache.py             AuditLog (hash chain)
```

---

## 구현된 모듈 목록

### Local Core (`core/`)

| 모듈 | 역할 |
|------|------|
| `main.py` | FastAPI 앱 (포트 17894) |
| `orchestrator.py` | FSM 오케스트레이터 |
| `chunker/ast_chunker.py` | Python AST + JS line-based 청킹 |
| `planner.py` | PlannerAgent (LLM → ExecutionPlan 최대 5단계) |
| `executor.py` | 결정론적 디스패처 (allowlist + 30초 타임아웃) |
| `verifier.py` | VerifierAgent (schema/sha256/test 검증) |
| `plan_execute_verify.py` | PEV 파이프라인 조율자 |
| `eval/harness.py` | Eval Harness (6카테고리 19케이스) |
| `eval/safety.py` | SafetyChecker (violation 0건 CI 강제) |
| `opa_client.py` | OPA REST API 클라이언트 (fail-closed) |
| `policy_cache.py` | PolicyBundle 로컬 캐시 + sha256 검증 |
| `context_gate.py` | 16종 민감정보 마스킹 |
| `api/routes/policy.py` | 로컬 OPA 평가 엔드포인트 |

### Control Plane (`control_plane/`)

| 모듈 | 역할 |
|------|------|
| `main.py` | FastAPI 앱 (포트 18000) |
| `db/models.py` | SQLAlchemy ORM (User/Device/Org/AuditEvent/PolicyBundle/ApprovalRequest) |
| `db/migrations.py` | PostgreSQL RLS + AuditLog 불변 트리거 |
| `services/identity.py` | OIDC User + Device 등록/heartbeat/폐기 |
| `services/org_service.py` | Org/Workspace/Project CRUD + RBAC |
| `services/audit.py` | hash chain AuditLog (SELECT FOR UPDATE) |
| `services/policy_service.py` | Preset 5개 → Rego 자동 생성, sha256 부여 |
| `services/approval_service.py` | 2인 승인 흐름, 거부 즉시 rejected |
| `api/middleware/device_auth.py` | DeviceContext 주입, require_permission_dep |
| `api/routes/auth.py` | Google/GitHub OIDC 콜백, Device enroll |
| `api/routes/devices.py` | heartbeat, Device 관리 |
| `api/routes/orgs.py` | 멀티테넌트 Org/Member/Workspace/Project |
| `api/routes/audit.py` | AuditLog 조회/재전송/무결성 검증 |
| `api/routes/policy.py` | PolicyBundle CRUD + OPA 평가 proxy |
| `api/routes/approvals.py` | 승인 대기 목록/투표 |

---

## 핵심 설계 결정 (ADR 요약)

| ADR | 결정 |
|-----|------|
| ADR-003 | OPA = REST server 방식. Go 라이브러리 임베딩 금지 |
| ADR-004 | raw source code Control Plane 업로드 금지. embedding + metadata만 |
| ADR-005 | rollback = Git revert PR 기본. ArgoCD 직접 rollback은 Severity 1만 |
| ADR-006 | Google/GitHub OIDC만. 비밀번호 인증 없음. D+14 체크포인트, D+21 BaaS 피봇 |
| ADR-007 | Q1 Node.js = line-based fallback. tree-sitter는 Q4 이후 |
| ADR-008 | Q3=ECS, Q4=EKS+ArgoCD. 동시 운영 없음 |
| ADR-009 | Final Demo = 실제 EKS. k3d/kind는 로컬 전용 |

---

## 다음 에이전트 인계 사항

### 즉시 해야 할 것 (Q3 시작)

1. **Cloud Preflight Assistant** — read-only IAM으로 ECS/ECR/ALB 사전 점검
2. **ECS Rolling Update** — TaskDefinition JSON 생성 → ECR push → update-service
3. **SBOM 생성** — Syft CycloneDX JSON, DeploymentRecord에 sbom_path 추가
4. **Trivy/Hadolint OPA 게이트** — policy_service.py Preset에 추가

### 주의 사항

- `control_plane/main.py`는 `lifespan`에서 `init_db()`를 호출한다. PostgreSQL 없이 시작하면 오류 발생.
- OPA는 별도 프로세스로 실행해야 한다: `opa run --server --addr :8181`
- `policy_cache.py`의 캐시 파일: `~/.recoder/policy_cache.json`, `~/.recoder/policy.rego`
- git commit은 반드시 PowerShell에서 수행 (sandbox의 index.lock 문제)

### 환경변수 목록

```
# Local Core
OPA_URL=http://localhost:8181
CONTROL_PLANE_URL=http://localhost:18000
POLICY_CACHE_TTL_HOURS=1

# Control Plane
CONTROL_PLANE_DATABASE_URL=postgresql+asyncpg://...
APPLY_RLS=true
GOOGLE_CLIENT_ID=...
GOOGLE_CLIENT_SECRET=...
GITHUB_CLIENT_ID=...
GITHUB_CLIENT_SECRET=...
DEVICE_TOKEN_TTL_HOURS=24
OPA_URL=http://localhost:8181
```

---

## 브랜치 구조

| 브랜치 | 내용 |
|--------|------|
| `feat/q1-enterprise-foundation` | **현재 메인 작업 브랜치** (Q1+Q2-A+Q2-B) |
| `fix/extension-terminal-api` | terminalDataWriteEvent 오류 수정 |
| `develop` | 기반 브랜치 |

---

## 알려진 남은 작업

- Q1 Eval pass_rate 실측 (LLM 연동 필요)
- PyInstaller 빌드 자동화 (Windows/Linux)
- Mini-Wedge 시나리오 E2E 테스트 (OPA 차단 → 2인 승인)
- Q3 ECS Fargate 전체 구현
