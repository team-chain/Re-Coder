# ReCoder

> **AI DevOps Platform** — 코드 수정부터 클라우드 배포·운영까지, 결정론적·감사 가능한 흐름으로 자동화.
> *Local Core · Control Plane · Cloud Execution* 3-Plane 엔터프라이즈 아키텍처.

ReCoder 는 개발자가 VSCode 사이드바(또는 Discord)에서 한 번만 누르면, AI 가 코드 에러를 분석해 패치를 제안하고, **Release Contract** 기반 정적·런타임 검증을 거쳐 안전하게 배포한 뒤, 5분간 감시하며 자동 롤백까지 제안합니다. 조직 단위에서는 **Control Plane** 이 OIDC 인증·RBAC·OPA 정책·2인 승인·불변 감사 로그로 거버넌스를 강제하고, **Cloud Execution Plane** 이 ECS Fargate Rolling Update 와 EKS + ArgoCD GitOps 까지 자동화합니다. 같은 사고가 재발하면 과거 해결책을 자동으로 떠올리는 **IncidentMemory** 와, 장애 발생 시 RCA·Postmortem 을 자동 생성하는 **Incident** 흐름도 내장.

> **설계 기준**: v6.4-final + Enterprise Final Design v5.0 (1년 로드맵 Q1~Q4 Must-Core 전체 완료).

---

## 3-Plane 아키텍처

ReCoder 는 책임에 따라 세 개의 Plane 으로 나뉩니다.

```
Local Execution Plane          Control Plane                 Cloud Execution Plane
──────────────────────         ─────────────                 ──────────────────────
VSCode Extension (TS/React)    FastAPI (18000)               ECS Fargate Rolling Update (Q3)
Discord Bot (7780/8765)        PostgreSQL + RLS              EKS + ArgoCD GitOps (Q4)
Local Core (FastAPI, 17894)    PolicyBundle (OPA Rego)       SBOM / 보안스캔 / 서명
OPA (8181) + policy_cache      AuditLog (hash chain, 불변)    OTel Collector / Prometheus / Loki
                               OIDC · Device · RBAC · 2인 승인
```

| Plane | 책임 | 주요 구성요소 |
|-------|------|--------------|
| **Local Execution** | 코드 분석·패치, 로컬 Preflight·배포, 사고 학습 | `core/`, `extension/`, `discord-bot/` |
| **Control Plane** | 조직 통제 — 인증·권한·정책·승인·감사 | `control_plane/` (FastAPI · PostgreSQL) |
| **Cloud Execution** | ECS / EKS 배포, GitOps, 관측성 | `core/agents/`, `deploy/`, `watchdog/` |

전체 솔루션 아키텍처 다이어그램: <img width="824" height="357" alt="architecture" src="https://github.com/user-attachments/assets/18e3072e-0160-4ffe-841f-53ea0a1a598c" />


---

## 핵심 컨셉

| 개념 | 설명 |
|------|------|
| **Release Contract** (`recoder.yml`) | 프로젝트의 배포 계약 — 스택, 포트, health 경로, 필수 환경 변수, 자동 롤백 트리거 등을 명시. First Run Wizard 가 자동 생성. |
| **Static Preflight** | 배포 전 정적 검사 (env, code, Dockerfile, 포트, 의존성, secret leak). 결과를 0~100 점수와 BLOCKED/WARN/PASSED 상태로 종합. |
| **Runtime Preflight** | 임시 docker 컨테이너를 띄워 health probe + smoke tests + 로그 패턴 검사. 자동 정리(try/finally). |
| **Deterministic Remediation** | 같은 입력 → 같은 `proposal_id`(SHA256). LLM 직접 코드 생성 대신 **결정론적 템플릿 치환**으로 재현성 보장. |
| **Plan-Execute-Verify (PEV)** | LLM Planner 가 최대 5단계 ExecutionPlan 생성 → 결정론적 Executor(allowlist + 30초 타임아웃)가 실행 → Verifier 가 schema/sha256/test 검증. |
| **3-Layer Audit** | `PreflightRun` → `RemediationRun` → `DeploymentLedger` 의 SQLite 영속화. Layer 3 는 append-only + 4-state 상태머신. |
| **IncidentMemory** | 사고 fingerprint(마스킹 후 SHA256) 기반 매칭. 재발 시 과거 해결책 자동 제안. 옵트인 학습. |
| **Continuous Verification** | 배포 후 5분 감시 — health 실패율, 분당 에러 로그율, 메모리 사용률 추적 → 자동 롤백 제안. |
| **Governance (Control Plane)** | OIDC 로그인 + Device 토큰 + RBAC, OPA PolicyBundle 7종 프리셋, 위험 작업 2인 승인, hash-chain 불변 AuditLog. |
| **Cloud Execution** | ECS Fargate Rolling Update(Circuit Breaker 내장), EKS + ArgoCD GitOps 동기화, SBOM·보안스캔. |
| **Observability** | OpenTelemetry Tracer + Prometheus 메트릭 + Loki 로그 push. |
| **Multi-channel** | VSCode 워크벤치 + Discord ChatOps(`/recoder deploy`, `/recoder rollback`, `/recoder forecast` 등) 양쪽 지원. |

---

## 구성요소

| 구성요소 | 디렉토리 | 실행 | 포트 |
|---------|---------|------|------|
| **Local Core** | `core/` | `python main.py` 또는 번들 exe | 17894~ (자동 spawn) |
| **Control Plane** | `control_plane/` | `python main.py` (PostgreSQL 필요) | 18000 |
| **OPA** | — | `opa run --server --addr :8181` | 8181 |
| **Bedrock Gateway** | `gateway/` | `sam deploy` (AWS Serverless) | API Gateway (HTTPS) |
| **Discord Bot** | `discord-bot/` | `python bot.py` | 7780(ws) · 8765(http) |
| **VSCode Extension** | `extension/` | F5 또는 VSIX 설치 | — |
| **EC2 Watchdog** | `watchdog/` | systemd 데몬 | — |

> **Gateway** 는 학생/팀원이 개인 AWS 키 없이 운영자 계정의 Amazon Bedrock 을 쓰도록 중계하는 서버리스 게이트웨이입니다. 자세한 셋업은 [SETUP.md](SETUP.md) 참고.

---

## 빠른 시작

### 사전 요구사항
- Python 3.11+
- Node.js 20+
- Docker Desktop (배포 흐름 사용 시)
- AWS 자격 (Bedrock 직접 사용 또는 ECS 배포 시) / AWS SAM CLI (Gateway 배포 시)
- PostgreSQL (Control Plane 사용 시)

### Local Core
```bash
cd core
python -m venv .venv
.venv\Scripts\Activate.ps1          # Windows
# source .venv/bin/activate         # macOS/Linux
pip install -r requirements.txt
python main.py
# → http://127.0.0.1:17894 (사용 중이면 17895~)
```
AWS 자격은 `~/.aws/credentials` 의 default 프로파일 또는 IAM Role 에서 자동 로드. 키 없이 쓰려면 Gateway 경유(`core/.env` 에 `RECODER_LLM_GATEWAY_URL`·`RECODER_STUDENT_TOKEN`).

### VSCode Extension
```bash
cd extension
npm install
npm run build              # 확장 TS + 웹뷰(React/Tailwind) 번들
# VSCode 에서 extension 폴더 열고 F5 (Extension Development Host)
```
확장이 켜지면 사이드바에 ReCoder 아이콘 표시 → 워크벤치 카드 UI 등장.

### Control Plane (조직 통제, 선택)
```bash
cd control_plane
python -m venv .venv && .venv\Scripts\Activate.ps1
pip install -r requirements.txt
# PostgreSQL 준비 후 CONTROL_PLANE_DATABASE_URL 설정
python main.py
# → http://127.0.0.1:18000  (lifespan 에서 init_db 호출 — PostgreSQL 없으면 기동 실패)
opa run --server --addr :8181     # 별도 프로세스로 OPA 기동
```

### `.env` 설정 예시 (`core/.env`)
```env
# AWS Bedrock
AWS_REGION=ap-northeast-2
BEDROCK_PRIMARY_MODEL_IDENTIFIER=anthropic.claude-3-haiku-20240307-v1:0
BEDROCK_SECONDARY_MODEL_IDENTIFIER=apac.anthropic.claude-sonnet-4-5-20251022-v1:0
BEDROCK_FAST_MODEL_IDENTIFIER=anthropic.claude-3-haiku-20240307-v1:0

# Quality 임계
RECODER_QUALITY_MIN=0.4
RECODER_QUALITY_WARN=0.6
RECODER_TRIGGER_THRESHOLD=0.5

# Control Plane / OPA 연동 (선택)
OPA_URL=http://localhost:8181
CONTROL_PLANE_URL=http://localhost:18000
POLICY_CACHE_TTL_HOURS=1
```

> 전체 환경변수 목록(Control Plane OIDC, ECS, ArgoCD, GitHub Rollback PR, Observability)은 [HANDOFF.md](HANDOFF.md) 참고. 처음부터 끝까지의 운영자 셋업 순서는 [SETUP.md](SETUP.md).

---

## 사용 흐름

1. **첫 실행**: First Run Wizard 가 Docker / AWS / Bedrock 연결을 진단하고, 프로젝트를 스캔해 `recoder.yml` 을 자동 생성.
2. **에러 분석**: 터미널 에러 감지 또는 사이드바 "에러 분석" → Context Gate 마스킹 → Bedrock 호출 → `PatchProposal` 카드 → 사용자 승인 후 적용(SHA256 검증 + 백업 + diff).
3. **배포 검증 (로컬)**:
   - **Static Preflight** → blocker / warning 카드 표시
   - blocker 가 있으면 **RemediationProposal** 자동 제안 (DIFF / FILE_CONTENT / COMMAND / GUIDANCE 4가지 preview)
   - 사용자 승인 후 적용 → 재검사 → 통과 시 다음 단계
   - **Runtime Preflight**: 임시 컨테이너로 health + smoke 검증
   - **배포 + CV**: docker run → 5분간 health/error/memory 감시 → STABLE / WARNING / AUTO_ROLLBACK_PROPOSED
4. **클라우드 배포 (Q3/Q4)**:
   - **ECS Fargate**: Cloud Preflight(read-only IAM) → SBOM·보안스캔 → Rolling Update(Circuit Breaker) → 필요 시 Rollback Proposal
   - **EKS + ArgoCD**: Application sync → 폴링(최대 10분) → Healthy 확인 → 롤백은 Git revert PR 자동 생성(ADR-005)
5. **거버넌스 (Control Plane)**: 위험 작업은 OPA 정책 평가 → 2인 승인 → 모든 행위는 hash-chain AuditLog 에 불변 기록.
6. **장애 대응**: Incident open → Timeline → RCA(LLM + 휴리스틱, confidence 포함) → Postmortem 자동 생성. IncidentMemory 가 같은 fingerprint 재발 시 과거 fix 제안(옵트인).

---

## Discord ChatOps

VSCode 밖에서도 ECS 배포·롤백·코드 분석·예측까지 Discord 슬래시 커맨드로 수행합니다(`discord-bot/`).

| 커맨드 | 설명 |
|--------|------|
| `/recoder code` | 코드 에러 분석 + 패치 제안 |
| `/recoder preflight` | Static/Runtime Preflight 실행 |
| `/recoder deploy` | 배포 트리거 |
| `/recoder rollback` | 롤백 (Git revert PR 또는 직접) |
| `/recoder status` | 현재 배포/검증 상태 |
| `/recoder forecast` | 배포 위험 예측 |
| `/recoder setup` / `workbench` | 길드 설정 / 워크벤치 연동 |

---

## 분기별 진척 (Enterprise v5.0 1년 로드맵)

| 분기 | 범위 | 상태 |
|------|------|------|
| **Q1 — AI 품질 기반** | AST 청킹, Plan-Execute-Verify, Eval Harness(6카테고리 19케이스), Safety Checker, Context Gate(16종 마스킹), OPA 클라이언트, PolicyBundle 캐시, Static/Runtime Preflight, Deterministic Remediation, 3-Layer Audit, IncidentMemory, Continuous Verification | ✅ 완료 |
| **Q2-A — Control Plane Core** | FastAPI(18000), SQLAlchemy ORM, PostgreSQL RLS + 불변 트리거, OIDC User + Device 등록/heartbeat, Org/Workspace/Project RBAC, hash-chain AuditLog | ✅ 완료 |
| **Q2-B — Governance** | OPA PolicyBundle 7종 프리셋(Rego 자동 생성 + sha256), 2인 승인 흐름, 승인 대기/투표 API | ✅ 완료 |
| **Q3 — Cloud Execution** | Cloud Preflight(read-only IAM), ECS Rolling Update, Circuit Breaker(5분 50% 초과 자동 중단), Rollback Proposal, SBOM(Syft CycloneDX), 보안스캔(Trivy/Hadolint/gitleaks) | ✅ 완료 |
| **Q4 — GitOps + Observability + MCP** | ArgoCD 에이전트(sync/status/rollback), Rollback PR 자동 생성(ADR-005), Incident 관리 + RCA + Postmortem, OpenTelemetry + Prometheus + Loki, MCP stdio PoC(`recoder_analyze`) | ✅ 완료 |

**남은 작업(Should/Optional)**: Eval pass_rate 실측(LLM 연동), PyInstaller 빌드 자동화, ECS Blue/Green, Cosign 이미지 서명, ArgoCD manifest 자동 생성, Slack/이메일 알림, 실제 EKS Final Demo E2E.

---

## 개발

### 테스트
```bash
cd core
$env:PYTHONPATH = "."
python -m pytest tests/ -v             # 단위 테스트 (216 test 함수)
python -m eval.v10                     # v10 백본 자동 평가 (Safety Gate)
```
`python -m eval.v10` 은 exit code 로 CI 통합 가능 (0 = pass, 1 = fail). 보안 회귀 1건이라도 발견 시 머지 차단.

### 디렉토리 구조
```
Re-Coder/
├─ core/                          # Local Core (Python · FastAPI, 17894)
│  ├─ preflight/                  #   Static + Runtime Preflight
│  │   ├─ checks/                 #     env/code/docker/port/deps 검사
│  │   ├─ static.py               #     StaticPreflightRunner
│  │   ├─ runtime.py              #     RuntimePreflightRunner (docker)
│  │   └─ continuous_verification.py
│  ├─ remediation/                # 결정론적 수정 제안
│  │   ├─ registry.py             #     FileTemplate / CommandTemplate
│  │   ├─ generator.py            #     blocker 별 generator
│  │   ├─ applier.py              #     DIFF / FILE_CONTENT / COMMAND / GUIDANCE
│  │   └─ fingerprint.py
│  ├─ cv/                         # Continuous Verification (5분 폴링 + 트리거)
│  ├─ persistence/                # 3-Layer SQLite (preflight/remediation/ledger)
│  ├─ incident_memory/            # 사고 학습 (fingerprint/learner/matcher/store)
│  ├─ agents/                     # 클라우드/운영 에이전트
│  │   ├─ ecs_agent.py            #     ECS Rolling Update + Circuit Breaker
│  │   ├─ argocd_agent.py         #     ArgoCD GitOps sync/rollback
│  │   ├─ incident_agent.py       #     Incident · RCA · Postmortem
│  │   ├─ rollback_pr_agent.py    #     Git revert PR 자동 생성
│  │   ├─ preflight_agent.py      #     Cloud Preflight (read-only IAM)
│  │   └─ code_agent.py / infra_agent.py / ops_agent.py / deploy_agent.py
│  ├─ llm/                        # Bedrock / Gemini / Gateway provider + router
│  ├─ eval/                       # Eval Harness + v10 Safety Gate
│  ├─ api/routes/                 # FastAPI 라우트 (analyze/deploy/ecs/gitops/incident/policy 등 13종)
│  ├─ observability.py            # OTel + Prometheus + Loki
│  ├─ sbom.py / security_scan.py  # SBOM(Syft) / Trivy·Hadolint·gitleaks
│  ├─ plan_execute_verify.py      # PEV 파이프라인 (planner/executor/verifier)
│  ├─ opa_client.py / policy_cache.py
│  ├─ context_gate.py             # 16-pattern secret 마스킹
│  ├─ orchestrator.py             # FSM 오케스트레이터
│  ├─ schemas.py                  # Pydantic 데이터 모델 (186 클래스)
│  ├─ mcp_server.py               # MCP stdio 서버 (JSON-RPC 2.0)
│  └─ tests/                      # 단위 테스트
├─ control_plane/                 # Control Plane (FastAPI · PostgreSQL, 18000)
│  ├─ db/                         #   SQLAlchemy ORM + RLS 마이그레이션
│  ├─ services/                   #   identity/org/audit/policy/approval
│  └─ api/routes/                 #   auth/devices/orgs/audit/policy/approvals
├─ gateway/                       # Bedrock 서버리스 게이트웨이 (AWS SAM)
├─ discord-bot/                   # Discord ChatOps 봇
├─ watchdog/                      # EC2 컨테이너 모니터링 데몬
├─ deploy/                        # 배포 매니페스트 (OTel 등)
├─ extension/                     # VSCode Extension (TS + React/Tailwind)
└─ docs/                          # 가이드 + architecture.svg
```

---

## 핵심 설계 원칙

1. **결정론적 자동 수정** — 같은 입력 → 같은 `proposal_id`. LLM 비결정성 차단. 감사 가능·캐시 가능.
2. **Secret 절대 비노출** — 16-pattern Context Gate 가 LLM 호출 전 마스킹. `mask_for_fingerprint` 가 로그/메시지에도 적용. AWS / GitHub / OpenAI / Stripe / Slack 토큰 자동 제거.
3. **Append-only 감사** — Local 의 `DeploymentLedger`(INSERT only + 4-state)와 Control Plane 의 hash-chain `AuditLog`(불변 트리거)로 이중 감사.
4. **Fail-closed 거버넌스** — OPA 평가 실패 시 차단. 위험 작업은 2인 승인 필수. `raw source code 는 Control Plane 에 업로드하지 않고 embedding + metadata 만 전송`(ADR-004).
5. **Safety Gate** — CI 단계에서 보안 회귀 1건이라도 발견 시 머지 차단. `python -m eval.v10` → exit 1.
6. **옵트인 학습** — `IncidentMemory` 는 `user_consent=True` 일 때만 저장. GDPR 호환 `delete_incident_memory` 지원.

### ADR 요약
- **ADR-003** OPA = REST server 방식 (Go 라이브러리 임베딩 금지)
- **ADR-004** raw source code Control Plane 업로드 금지 (embedding + metadata 만)
- **ADR-005** Production rollback = Git revert PR 기본, ArgoCD 직접 rollback 은 Severity 1만
- **ADR-006** Google/GitHub OIDC 만, 비밀번호 인증 없음
- **ADR-007** Q1 Node.js = line-based fallback, tree-sitter 는 Q4 이후
- **ADR-008** Q3=ECS, Q4=EKS+ArgoCD (동시 운영 없음)
- **ADR-009** Final Demo = 실제 EKS (k3d/kind 는 로컬 전용)

---

## 문서
- [SETUP.md](SETUP.md) — 처음부터 끝까지 운영자 셋업 가이드 (Core · Gateway · Discord · Extension)
- [HANDOFF.md](HANDOFF.md) — 팀 인수인계 (모듈 목록 + 전체 환경변수)
- [PROGRESS.md](PROGRESS.md) — 누적 진행 사항 (Q1~Q4 진척 트래커)
- [docs/MENTOR_DEMO.md](docs/MENTOR_DEMO.md) — 멘토링 데모 가이드
- [docs/OPERATOR_RUNBOOK.md](docs/OPERATOR_RUNBOOK.md) — 운영자 런북
- [docs/API_v10.md](docs/API_v10.md) — API 레퍼런스
- [docs/architecture.svg](docs/architecture.svg) — 솔루션 아키텍처 다이어그램

---

## 담당
- **이동규** — 백엔드 · 인프라 (Local Core, Control Plane, Cloud Execution 에이전트)
- **윤세빈** — Extension · 코드 분석 (VSCode UI, code_agent, MCP, workbench)

## 라이선스
내부 학기 프로젝트 — 추후 결정.
