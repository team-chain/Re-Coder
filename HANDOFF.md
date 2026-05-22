# ReCoder Enterprise — 인계 문서

> **설계서**: Enterprise Final v5.0
> **현재 브랜치**: `feat/q1-enterprise-foundation`
> **마지막 업데이트**: 2026-05-16 (develop 머지 완료 + Q2-A/Q2-B/Q4 통합 — 7차 세션)

---

## 현재 상태 요약

**완료된 분기**: Q1 + Q2-A + Q2-B + Q3 + **Q4 ✅ (전체 완료)**

### 2026-05-16 7차 세션에서 수행한 작업

1. **Q2-A Control Plane Core 구현** — OIDC/Device Token/RBAC/AuditLog (이동규)
2. **Q2-B Governance 구현** — OPA PolicyBundle Preset 7개 + 2인 승인 (이동규)
3. **Q3 ECS 에이전트 구현** — Preflight/Rolling Update/Circuit Breaker/SBOM/보안스캔 (이동규)
4. **Q4 GitOps/Incident/OTel 에이전트 구현** — ArgoCD/Incident/Rollback PR/Observability (이동규)
5. **`develop` 브랜치 머지** (`b25810a`) — 팀원(윤세빈) Q3/Q4/workbench 코드 채택 (`--theirs` 전략으로 3개 충돌 파일 해결)
6. **schemas 호환 브리지 추가** (`9889adc`) — 팀원 스키마 클래스명과 이동규 에이전트 코드 간 호환성 확보

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

<<<<<<< HEAD
## 구현된 모듈 목록

### Local Core (`core/`)

| 모듈 | 역할 | 출처 |
|------|------|------|
| `main.py` | FastAPI 앱 (포트 17894) | 이동규 |
| `orchestrator.py` | FSM 오케스트레이터 | 이동규 |
| `chunker/ast_chunker.py` | Python AST + JS line-based 청킹 | 이동규 |
| `planner.py` | PlannerAgent (LLM → ExecutionPlan 최대 5단계) | 이동규 |
| `executor.py` | 결정론적 디스패처 (allowlist + 30초 타임아웃) | 이동규 |
| `verifier.py` | VerifierAgent (schema/sha256/test 검증) | 이동규 |
| `plan_execute_verify.py` | PEV 파이프라인 조율자 | 이동규 |
| `eval/harness.py` | Eval Harness (6카테고리 19케이스) | 이동규 |
| `eval/safety.py` | SafetyChecker (violation 0건 CI 강제) | 이동규 |
| `opa_client.py` | OPA REST API 클라이언트 (fail-closed) | 이동규 |
| `policy_cache.py` | PolicyBundle 로컬 캐시 + sha256 검증 | 이동규 |
| `context_gate.py` | 16종 민감정보 마스킹 | 이동규 |
| `api/routes/policy.py` | 로컬 OPA 평가 엔드포인트 | 이동규 |
| `agents/preflight_agent.py` | read-only IAM ECS/ECR/IAM/CloudWatch 점검 | 이동규 |
| `agents/ecs_agent.py` | ECS Rolling Update 전체 파이프라인 오케스트레이터 | 이동규 |
| `security_scan.py` | Trivy/Hadolint/gitleaks 병렬 스캔 | 이동규 |
| `sbom.py` | Syft CycloneDX JSON SBOM 생성 | 이동규 |
| `api/routes/ecs.py` | ECS 배포 API (/deploy, /status, /cancel, /preflight, /scan) | 이동규 |
| `agents/argocd_agent.py` | ArgoCD Application 동기화, 폴링, 직접 rollback (ADR-006) | 이동규 |
| `agents/incident_agent.py` | 장애 등록, 타임라인, RCA (LLM+휴리스틱), Postmortem | 이동규 |
| `agents/rollback_pr_agent.py` | GitHub API로 git revert PR 자동 생성 (ADR-005) | 이동규 |
| `observability.py` | OTel Tracer + Prometheus 메트릭 + Loki 로그 push | 이동규 |
| `api/routes/gitops.py` | GitOps API (/sync, /syncs, /apps/{app}/status, /rollback-pr) | 이동규 |
| `api/routes/incident.py` | Incident API (/open, /event, /rca, /postmortem, /resolve) | 이동규 |
| `mcp_server.py` | MCP stdio 서버 (JSON-RPC 2.0, recoder_analyze 도구) | **팀원(윤세빈)** — develop 머지로 채택 |
| `schemas.py` | 전체 Pydantic 스키마 + 호환 브리지 (말미 추가) | 팀원 기반 + 이동규 브리지 (`9889adc`) |

### Control Plane (`control_plane/`)

| 모듈 | 역할 | 출처 |
|------|------|------|
| `main.py` | FastAPI 앱 (포트 18000) | 이동규 |
| `db/models.py` | SQLAlchemy ORM (User/Device/Org/AuditEvent/PolicyBundle/ApprovalRequest) | 이동규 |
| `db/migrations.py` | PostgreSQL RLS + AuditLog 불변 트리거 | 이동규 |
| `services/identity.py` | OIDC User + Device 등록/heartbeat/폐기 | 이동규 |
| `services/org_service.py` | Org/Workspace/Project CRUD + RBAC | 이동규 |
| `services/audit.py` | hash chain AuditLog (SELECT FOR UPDATE) | 이동규 |
| `services/policy_service.py` | Preset 7개 → Rego 자동 생성, sha256 부여 (Q3: +SBOM_REQUIRED, +HADOLINT_ERROR) | 이동규 |
| `services/approval_service.py` | 2인 승인 흐름, 거부 즉시 rejected | 이동규 |
| `api/middleware/device_auth.py` | DeviceContext 주입, require_permission_dep | 이동규 |
| `api/routes/auth.py` | Google/GitHub OIDC 콜백, Device enroll | 이동규 |
| `api/routes/devices.py` | heartbeat, Device 관리 | 이동규 |
| `api/routes/orgs.py` | 멀티테넌트 Org/Member/Workspace/Project | 이동규 |
| `api/routes/audit.py` | AuditLog 조회/재전송/무결성 검증 | 이동규 |
| `api/routes/policy.py` | PolicyBundle CRUD + OPA 평가 proxy | 이동규 |
| `api/routes/approvals.py` | 승인 대기 목록/투표 | 이동규 |

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

## 브랜치 구조

| 브랜치 | 커밋 | 내용 |
|--------|------|------|
| `feat/q1-enterprise-foundation` | `9889adc` | **현재 HEAD** — schemas 호환 브리지 추가 |
| `feat/q1-enterprise-foundation` | `b25810a` | develop 머지 완료 (팀원 Q3/Q4/workbench 채택) |
| `develop` | `b25810a` | 팀원 최신 — Q3/Q4 팀원 버전 |
| `fix/extension-terminal-api` | — | terminalDataWriteEvent 오류 수정 |

> **다음 작업**: `feat/q1-enterprise-foundation` → `develop` push  
> ```powershell
> git push origin feat/q1-enterprise-foundation:develop
> ```

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

**develop 머지 후 이동규 추가분 (인계 정보)**
- Q2-A Control Plane: OIDC/Device/Org/RBAC/AuditLog — `control_plane/` 전체
- Q2-B Governance: PolicyBundle Preset 7개(SBOM_REQUIRED_BLOCK, HADOLINT_ERROR_BLOCK 포함), 2인 승인
- Q4 에이전트: argocd_agent, incident_agent, rollback_pr_agent, observability + 해당 API 라우터
- schemas 호환 브리지: ArgoSyncPhase/ArgoHealthStatus/ArgoSyncRequest/ArgoSyncRecord, IncidentStatus/TimelineEvent/IncidentRecord, RollbackPRRequest/RollbackPRRecord, MCPToolDefinition(alias), MetricPoint/TraceSpan/ObservabilityConfig

**팀원(윤세빈) develop 채택분 (인계 정보)**
- `mcp_server.py`: MCPToolDescriptor 기반, recoder_analyze 단일 도구, SESSION_TOKEN 검증
- `schemas.py` 기반: MCPToolDescriptor, IncidentEvent, IncidentTimeline 등 팀원 클래스명
- Q3 팀원 버전 에이전트 및 workbench 기능

### 주의 사항

- `control_plane/main.py`는 `lifespan`에서 `init_db()`를 호출한다. PostgreSQL 없이 시작하면 오류 발생.
- OPA는 별도 프로세스로 실행해야 한다: `opa run --server --addr :8181`
- `policy_cache.py`의 캐시 파일: `~/.recoder/policy_cache.json`, `~/.recoder/policy.rego`
- **git commit은 반드시 PowerShell에서 수행** (sandbox의 index.lock 문제)
- schemas.py 수정 시 말미의 호환 브리지 클래스 보존 필수 (MCPToolDefinition alias 포함)

### 환경변수 목록

```
# Local Core
OPA_URL=http://localhost:8181
CONTROL_PLANE_URL=http://localhost:18000
POLICY_CACHE_TTL_HOURS=1
SESSION_TOKEN=<자동 생성 — runtime.json 참조>

# MCP stdio
MCP_SESSION_TOKEN=<SESSION_TOKEN과 동일>
DEV_MODE=1  # 개발 중 토큰 검증 skip

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

# Q4 ArgoCD (선택)
ARGOCD_URL=https://argocd.example.com
ARGOCD_TOKEN=<ArgoCD API token>

# Q4 GitHub Rollback PR (선택)
GITHUB_TOKEN=<Personal Access Token>
GITHUB_REPO=owner/repo

# Q4 Observability (선택)
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
PROMETHEUS_GATEWAY_URL=http://localhost:9091
LOKI_URL=http://localhost:3100
```
=======
## 7. 저장 구조

```
~/.recoder/
├── runtime.json              # Core 포트, session token
├── core.lock                 # lock file (PID 목록)
├── diagnostics.json          # 환경 진단 결과, resolved model metadata
├── projects/{project_id}.json  # ProjectProfile
├── sessions/{session_id}/    # 세션 기록 (JSONL)
├── backups/{session_id}/     # 패치 백업 (90일 보관)
├── logs/terminal-{date}.jsonl  # 터미널 로그 (masked only)
└── templates/                # CommandTemplate / FileTemplate Registry
```

파일 권한: `runtime.json`, `diagnostics.json`, `projects/`, `sessions/`, `logs/` → `0600` (macOS/Linux)  
백업 디렉터리: `0700`  
Windows ACL 설정 실패 시 Soft Fail (경고 로그만 남기고 Core 정상 시작)

---

## 8. LLM 호출 규칙

- LLM은 **직접 명령을 생성하지 않는다.**
- LLM 출력: `PatchProposal`, `InfraFileProposal`, `DeploymentPlan`, `ResponseProposal`만 허용
- 실제 명령: **CommandTemplate Registry**가 생성
- 실제 파일: **FileTemplate Registry**가 조립
- 모든 LLM 출력은 **schema validation** 통과 후 사용
- Bedrock 호출: Converse API Structured Output 1순위 → Tool Use → JSON 추출 → schema repair → fallback model

---

## 9. 1학기 One Scene (최소 성공 기준)

```
FastAPI 앱 실행
  → ModuleNotFoundError 발생
  → "Run with ReCoder" 또는 Shell Integration으로 에러 수집
  → 사이드바에 분석 결과 표시 (PatchProposal)
  → diff preview 확인 + 사용자 승인
  → patch 자동 적용
  → 앱 재실행 성공
  → "Dockerfile 생성하시겠습니까?" 제안
  → Trivy 0건 + Hadolint 통과
  → 사용자 승인 → docker build → docker run → Health Check OK
```

---

## 10. 현재 마일스톤 상태 (2026-05-08 기준)

**✅ 완료 — Extension ↔ Local Core 전체 배선 완료**

- `POST /api/analyze` → `analyzer.analyze` + `code_agent.generate_patch` 체인 실동작
- `POST /api/patch/approve` → `code_agent.apply_patch` 실적용 + rollback 보장
- `POST /api/infra/generate` → `infra_agent.generate` + hadolint 품질 검사
- `POST /api/deploy/local` → `LocalDeployAgent.deploy()` background task + `/api/deploy/status` polling
- `POST /api/security/scan` → Trivy + Hadolint 결과 반환
- Sidebar Ready 카드(Core/AI/Docker), diff preview, Ship 탭 ▶ 실행 모두 정상화
- pytest 15/15 통과

**다음 목표 — 로컬 환경에서 One Scene 완주**
1. `python core/main.py` 실행 → Core 기동 확인
2. VSCode에서 Extension 실행 → Ready 카드 확인
3. FastAPI 앱에서 ModuleNotFoundError 유발 → Sidebar에 diff preview 확인
4. 승인 → 패치 적용 → 재실행 성공 확인
5. Dockerfile 생성 제안 → 승인 → docker build → docker run → Health Check OK

---

## 11. 주의사항 / 금지사항

- `monitor.py`, `widget.py`, `capture_agent.py`는 v6.4에서 사용하지 않는다. 수정하지 말 것.
- `schemas.py`의 중복 클래스: 이미 해결됨. 재작업 불필요.
- LLM이 생성한 명령을 직접 `subprocess.run()`하는 코드는 절대 작성하지 않는다. CommandTemplate Registry를 통해서만 실행.
- `gitleaks` 결과의 secret 원문은 LLM에 전송하지 않는다. 파일 경로·라인 번호·타입·rule_id만 전달.
- raw context는 메모리에서만 처리. 저장소(파일, DB, 로그)에는 masked 데이터만 저장.
- Local Core는 `127.0.0.1`만 바인딩. 외부 노출 절대 금지.

---

## 12. 파일 소유권 (충돌 방지)

```
이동규 전담 (core/ — Python)       윤세빈 전담 (extension/ — TypeScript)
────────────────────────────────   ──────────────────────────────────────
core/server.py                     extension/src/api/coreClient.ts
core/git_agent.py (신규)           extension/src/ui/sidebarProvider.ts
core/local_deploy_agent.py         extension/package.json
core/quality_runner.py
core/session_logger.py
core/first_run.py
core/schemas.py (변경 시 TS 공유)
core/recoder.spec (신규)
```

신규 API 계약 (미리 확정, 변경 금지):
- `POST /api/git/commit` → `{ workspace_path, message, session_id }` → `{ status, commit_hash, message }`
- `POST /api/deploy/rollback` → `{ plan_id }` → `{ status, message, logs[] }`

PROGRESS.md와 HANDOFF.md는 양쪽 모두 수정 가능. 작업 완료 시 해당 행 ✅로 변경.
>>>>>>> 74cf4369799da45d0fa49de67d56e58e01a2cc27
