# ReCoder Enterprise — 인계 문서

> **설계서**: Enterprise Final v5.0
> **현재 브랜치**: `feat/q1-enterprise-foundation`
> **마지막 업데이트**: 2026-05-16

---

## 현재 상태 요약

**완료된 분기**: Q1 + Q2-A + Q2-B + Q3 + **Q4 ✅ (전체 완료)**

Q1 Must-Core, Control Plane Core(Q2-A), OPA 정책 엔진 + Multi-Approver(Q2-B), ECS Fargate Rolling Update + SBOM + 보안스캔(Q3), GitOps ArgoCD + Incident Timeline + RCA + OTel + MCP(Q4)가 모두 구현됐다.
**설계서 §Q1~Q4 Must-Core 전체 완료. 쐐기 시나리오 7단계 파이프라인 완성.**

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
| `agents/preflight_agent.py` | read-only IAM ECS/ECR/IAM/CloudWatch 점검 |
| `agents/ecs_agent.py` | ECS Rolling Update 전체 파이프라인 오케스트레이터 |
| `security_scan.py` | Trivy/Hadolint/gitleaks 병렬 스캔 |
| `sbom.py` | Syft CycloneDX JSON SBOM 생성 |
| `api/routes/ecs.py` | ECS 배포 API (/deploy, /status, /cancel, /preflight, /scan) |
| `agents/argocd_agent.py` | ArgoCD Application 동기화, 폴링, 직접 rollback (ADR-006) |
| `agents/incident_agent.py` | 장애 등록, 타임라인, RCA (LLM+휴리스틱), Postmortem |
| `agents/rollback_pr_agent.py` | GitHub API로 git revert PR 자동 생성 (ADR-005) |
| `observability.py` | OTel Tracer + Prometheus 메트릭 + Loki 로그 push |
| `mcp_server.py` | MCP stdio 서버 (JSON-RPC 2.0, 6개 도구, Extension 연동) |
| `api/routes/gitops.py` | GitOps API (/sync, /syncs, /apps/{app}/status, /rollback-pr) |
| `api/routes/incident.py` | Incident API (/open, /event, /rca, /postmortem, /resolve) |

### Control Plane (`control_plane/`)

| 모듈 | 역할 |
|------|------|
| `main.py` | FastAPI 앱 (포트 18000) |
| `db/models.py` | SQLAlchemy ORM (User/Device/Org/AuditEvent/PolicyBundle/ApprovalRequest) |
| `db/migrations.py` | PostgreSQL RLS + AuditLog 불변 트리거 |
| `services/identity.py` | OIDC User + Device 등록/heartbeat/폐기 |
| `services/org_service.py` | Org/Workspace/Project CRUD + RBAC |
| `services/audit.py` | hash chain AuditLog (SELECT FOR UPDATE) |
| `services/policy_service.py` | Preset 7개 → Rego 자동 생성, sha256 부여 (Q3: +SBOM_REQUIRED, +HADOLINT_ERROR) |
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

### 전체 구현 완료 ✅

**Q1~Q4 Must-Core 모두 구현됐습니다.**

남은 작업 (Should/Optional):
1. Q1 Eval pass_rate 실측 (LLM 연동 필요)
2. PyInstaller 빌드 자동화 (Windows/Linux)
3. ECS Blue/Green 배포 (Q3 Should)
4. Cosign image signing (Q3 Should)
5. SBOM Control Plane 전체 업로드 opt-in (Q3 Should)
6. ArgoCD Application manifest 자동 생성 (Q4 Should)
7. Slack/이메일 알림 연동 (Q2-B Should)
8. **실제 환경 Final Demo** — 실제 EKS + ArgoCD 쐐기 시나리오 E2E

**Q3 완료된 것 (인계 정보)**
- ECS Rolling Update 전체 파이프라인 (Preflight → Scan → SBOM → TaskDef → update-service → Poll → CircuitBreaker → Rollback)
- Circuit Breaker: 5분 내 실패율 50% 초과 → 자동 중단, Level 3 rollback proposal
- 보안 스캔: Trivy(critical=block), Hadolint(error=block), gitleaks(secret_redacted=True, always-block)
- SBOM: Syft CycloneDX JSON, get_upload_metadata()로 메타데이터만 Control Plane 전송
- OPA Preset 7개: 기존 5개 + SBOM_REQUIRED_BLOCK + HADOLINT_ERROR_BLOCK

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

# Q3 ECS 배포 (선택)
ECS_EXECUTION_ROLE_ARN=arn:aws:iam::ACCOUNT_ID:role/ecsTaskExecutionRole
ECS_TASK_ROLE_ARN=arn:aws:iam::ACCOUNT_ID:role/ecsTaskRole
AWS_DEFAULT_REGION=ap-northeast-2
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
- Q4 GitOps ArgoCD + OTel + MCP 구현
