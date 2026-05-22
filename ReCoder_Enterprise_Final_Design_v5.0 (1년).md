# ReCoder 엔터프라이즈 최종 설계서 v5.0

> 버전: v5.0 | 작성: 2026년 5월 | 상태: 최종 확정본

---

## 설계 원칙

모든 기능 결정은 단 하나의 질문으로 시작한다.

> **"이 기능이 쐐기 시나리오를 더 강하게 만드는가?"**

**쐐기 시나리오 7단계**

1. FastAPI 프로덕션 서비스에서 컨테이너 비정상 종료가 발생한다
2. OTel 데이터로 자동 감지된다
3. Incident Timeline과 RCA가 생성된다
4. rollback PR이 자동 생성된다
5. 2인 승인을 거친다
6. ArgoCD가 적용한다
7. Postmortem skeleton이 자동 생성된다

쐐기 시나리오에 들어간 항목은 반드시 Must다. 직접 필요하지 않은 항목은 Should 이하다. 기능을 추가할 때가 아니라 범위의 실행 순서를 지킬 때다. 성공의 핵심은 설계의 우수성이 아니라 Q1부터 Q4까지 절제를 실제로 지키는 집행력이다.

---

## Non-goals

다음 항목은 1년 내 구현하지 않는다.

- Terraform apply 자동화
- Datadog, New Relic 전체 대체
- Kubernetes 클러스터 직접 생성
- LLM에게 직접 shell 실행 권한 부여
- raw source code Control Plane 자동 업로드
- 프로덕션 배포 무승인 실행
- Azure, GCP 지원
- Plugin Marketplace 구축
- On-premise LLM 운영
- Self-hosted Control Plane (Q4 이후 backlog)
- TypeScript tree-sitter 구조적 청킹 (Q4 이후 backlog)
- MCP remote 배포 및 운영 도구 (backlog)
- 비밀번호 기반 자체 인증 구현
- CLI headless 클라이언트 (backlog — scope creep)

---

## Architecture Decision Records

### ADR-001: Self-hosted Control Plane → Q4 이후

SaaS와 Self-hosted를 동시에 개발하면 배포 파이프라인, 인증, 저장소, 보관 정책이 이중화되어 2인 팀이 감당할 수 없다. 초기 1년은 SaaS 멀티테넌트 구조에 집중한다.

### ADR-002: Q3 표준 배포 경로 = ECS Fargate

ECS Rolling Update로 AWS Native 배포를 먼저 완성하고, GitOps는 Q4에 ArgoCD와 함께 EKS에서 도입한다. ECS와 EKS를 동시에 깊게 다루면 Q4에 도달하지 못한다.

### ADR-003: OPA = REST server 방식

Python 프로세스에 Go 라이브러리를 직접 임베딩하지 않는다. OPA를 독립 프로세스로 실행하고 REST API로 질의한다. 향후 성능 최적화가 필요하면 OPA WASM을 검토한다.

### ADR-004: raw source code Control Plane 업로드 금지

embedding vector와 metadata만 인덱싱하고 LLM 전달 직전에 Context Gate를 통과한다. 이것이 엔터프라이즈 고객 신뢰의 기반이다.

### ADR-005: Production GitOps rollback = Git revert PR 기본

Severity 1 장애에서만 emergency rollback을 허용하되 30분 이내 Git reconciliation PR을 필수 생성한다. ArgoCD API 직접 rollback은 Git 상태와 실제 상태 불일치 구간을 만든다.

### ADR-006: 인증 직접 구현 + 3주 타임박스 + 14일 체크포인트

Google과 GitHub OIDC만 지원한다. 비밀번호 기반 자체 인증은 구현하지 않는다. 세션, refresh token, device token은 검증된 라이브러리를 사용하고 토큰 저장, 회전, 폐기, 감사 로그만 ReCoder 도메인에 맞게 구현한다.

**타임박스**: Q2-A1 시작 후 14일 차에 중간 점검을 수행한다. Device Token OS Keychain 저장과 heartbeat 기본 사이클이 동작하지 않으면 즉시 BaaS(Auth0 또는 Supabase) 피봇을 결정한다. 21일을 초과하면 무조건 피봇한다. "조금만 더 하면 될 것 같다"는 매몰 비용 오류에 빠지지 않는다.

### ADR-007: Q1 Node.js = line-based fallback 제한

tree-sitter 기반 청킹 디버깅에 Q1을 소진하면 Eval Harness와 Plan-Execute-Verify 완성이 불가능하다. Q1 Node.js Eval 카테고리는 단순 런타임 에러, package.json scripts, Dockerfile 생성 범위만 평가한다.

### ADR-008: ECS 데모 경로 ≠ EKS 데모 경로

ECS는 Q3 배포 역량 데모 경로다. EKS + ArgoCD는 Q4 운영 사고 대응 데모 경로다. 동일한 서비스가 ECS와 EKS에 동시에 올라가지 않는다.

### ADR-009: Final Demo = 실제 EKS (비용 제어 스크립트 활용)

k3d/kind로 타협하지 않는다. "엔터프라이즈급 DevOps 에이전트"가 로컬 환경에서만 동작하면 제품 신뢰도가 크게 떨어진다. 대신 데모 직전 EKS 클러스터를 생성하고 데모 완료 즉시 삭제하는 Terraform/eksctl 스크립트를 미리 준비한다. 데모 2시간 기준 EKS 비용은 수 달러 수준이다. k3d/kind는 로컬 개발 검증 전용으로만 사용한다.

### ADR-010: CLI headless 모드 = Backlog

IDE fragmentation 해결을 위한 CLI 클라이언트는 새로운 제품을 추가하는 scope creep이다. 백엔드 로직 개발은 PyCharm으로 하되, 기능 테스트와 Dogfooding은 반드시 VSCode Extension을 통해 수행한다. 우회로를 만들면 Extension 완성도가 떨어진다.

---

## Risk Register

| 리스크 | 영향 | 대응 |
|--------|------|------|
| Control Plane 범위 과다 | Q2 전체 지연 | Q2-A를 3개 내부 마일스톤으로 분리, 각 DoD 강제 적용 |
| ADR-006 인증 타임싱크 | Q2-A1에서 일정 붕괴 | 14일 체크포인트, 21일 초과 시 무조건 BaaS 피봇 |
| OTel 백엔드 복잡도 | Q4 Incident Timeline 지연 | Prometheus + Loki만 1차 지원, Tempo는 Q4 후반 |
| AI 패치 품질 불안정 | 사용자 신뢰 하락 | Eval Harness Safety violation 0건 CI 강제 |
| 범위 절제 실패 | Q4에 쐐기 시나리오 미완성 | Should 항목은 일정 흔들릴 때 즉시 Backlog로 |
| AuditLog 동시성 | hash chain 깨짐 | org_id 단위 row-level lock + monotonic sequence |
| **Calendar Risk** | **Q2 이후 개발 벨로시티 절반** | **Q2부터 현재 속도 절반으로 보수적 산정, Must-Core 우선 완성** |
| EKS 데모 준비 실패 | 제품 신뢰도 하락 | Terraform/eksctl 자동화 스크립트 Q3 말까지 준비 |

---

## MVP Cut Line

### Must-Core (제품 기반 — 순서 엄수)

**1차: AI 품질 기반**
- Local Core 안정화
- AST 기반 청킹 (Python)
- Eval Harness
- Plan-Execute-Verify

**2차: 조직 통제 기반**
- Control Plane Core (Organization / Device / AuditLog)
- Device Token (OS Keychain)
- RBAC

**3차: 정책·승인 기반**
- OPA 배포 차단
- Web UI 기반 2인 승인

**4차: 클라우드 실행 기반**
- ECS Rolling Update
- SBOM 생성

### Must-Wedge (쐐기 시나리오 기반 — Must-Core 4차 완료 후 시작)

- GitOps ArgoCD PR flow
- Incident Timeline MVP
- RCA 기본 분석 (confidence score 포함)
- rollback PR 자동 생성
- Postmortem skeleton 생성

### Should

- Multi-Approver Slack 연동
- ECS Blue/Green
- Cosign signing
- 완성형 Postmortem 자동 생성
- RCA 정밀 분석

### Backlog

- MCP Streamable HTTP remote
- MCP recoder_deploy / recoder_operate
- Self-hosted Helm chart
- EKS advanced policy
- TypeScript tree-sitter 청킹
- JetBrains 플러그인
- CLI headless 클라이언트

**규칙**: 일정이 흔들릴 때 Should를 Backlog로 내린다. Must-Core 1차는 어떤 이유로도 양보하지 않는다. Must-Core가 순서대로 완성된 뒤에 Must-Wedge를 시작한다.

---

## 전체 구조

### 3개 Plane

**Local Execution Plane**: 개발자 머신 위에서 동작한다. VSCode Extension, Local Core (FastAPI + PyInstaller), EC2 Watchdog이 여기 속한다.

**Control Plane**: SaaS 멀티테넌트 구조로 운영한다. 조직, 정책, 감사, 인증을 중앙에서 관리한다. Self-hosted는 Q4 이후다.

**Cloud Execution Plane**: ECS Fargate (Q3), EKS + ArgoCD (Q4), OpenTelemetry가 속한다.

### 핵심 아키텍처 규칙

- 비즈니스 로직은 SaaS와 Self-hosted가 공유한다
- 배포, 인증, 저장소, 보관 정책만 어댑터로 분리한다
- SaaS가 기본 운영 모델이다

---

## Failure Mode 정의

| 컴포넌트 장애 | 허용 | 차단 |
|--------------|------|------|
| Control Plane 연결 불가 | Level 1~2 로컬 작업 | 프로덕션 배포 |
| OPA server 불가 | Level 1~2 정책 불필요 작업 | Level 3~4 전체 (fail-closed) |
| LLM provider 불가 | 기존 캐시 사용, 결정론적 기능 | 새 분석 요청 |
| OTel backend 불가 | Watchdog + AuditLog 기반 skeleton | 완성형 Incident Timeline |
| ArgoCD API 불가 | PR 생성 | sync 확인 (unknown 표시) |
| AuditLog 업로드 실패 | local pending queue 저장 | 프로덕션 작업 (재전송 보장 전) |

OPA unavailable 시 fail-closed 원칙을 절대 타협하지 않는다. 보안 도구는 장애 시 허용보다 차단이 안전하다.

---

## 사용자 역할 정의

| 역할 | 대상 | 주요 행위 |
|------|------|----------|
| **Executor User** | 주니어/미들 백엔드 개발자 | 에러 분석, Dockerfile 생성, 배포 요청 |
| **Policy Owner** | CTO, 시니어, 플랫폼 담당자 | OPA 정책, 승인 규칙, 배포 제한 관리 |
| **Approver** | 팀 리드, DevOps 담당자 | Level 3~4 작업 승인/거부 |
| **Auditor** | 보안 담당자, 관리자 | AuditLog, 배포 이력 읽기 전용 검토 |

---

## 제품 패키징

| 플랜 | 핵심 내용 |
|------|----------|
| **Free** | Local Core, 개인 프로젝트 3개, 로컬 Docker 배포 |
| **Pro** | SaaS Control Plane 논리적 격리 워크스페이스, 배포 이력, 비용 추적, SBOM 생성, GitHub Actions 연동 |
| **Team** | Organization, RBAC, Multi-Approver, AuditLog, PolicyBundle, Slack 승인 연동 |
| **Enterprise** | SSO/SAML, Self-hosted Control Plane (Q4 이후), 커스텀 정책, 장기 감사 로그, SLA |

**Open-core 전략**: Local Core를 Apache 2.0으로 공개하고 Control Plane을 상용 서비스로 운영한다.

---

## 데이터 분류 정책

### 절대 업로드 금지

raw 소스 코드, raw 터미널 로그, 시크릿 값, .env 파일, private key, 자격증명이 포함된 전체 stacktrace. API 레이어 입력 경계에서 강제 적용한다.

### Control Plane API Boundary

```
1. Request validation      — schema, size limit, file type allowlist
2. Secret redaction        — token/password/key pattern masking, raw stacktrace rejection
3. Data classification     — allowed / optional / forbidden 분류 강제
4. Storage policy          — forbidden 데이터 저장 차단, optional은 opt-in 확인 후 저장
5. Audit                   — 거부된 업로드도 AuditLog에 metadata만 기록
```

### 업로드 허용

마스킹된 로그, 배포 메타데이터, 정책 판단 결과, 감사 이벤트, 비용 사용량.

### SBOM — sensitive metadata

내부 패키지명, 프라이빗 라이브러리명, 버전 정보가 포함될 수 있다. Free/Pro는 로컬 저장 기본값. Team/Enterprise는 조직 정책에 따라 선택. Control Plane에는 `sbom_hash`, `image_digest`, `package_count`, `vulnerability_summary`만 기본 업로드한다. 전체 SBOM 업로드는 opt-in이다.

### 선택적 업로드

마스킹된 코드 스니펫, 인시던트 발췌, 평가 결과.

---

## Q1 설계 — 토대 강화

**목표**: "안정적으로 재현 가능한 것"

### Definition of Done

- 카테고리별 pass_rate 60% 이상
- Safety violation 0건
- Windows x64, Linux x64 빌드 자동화 완료
- 다중 파일 PatchProposal 안정 동작
- 청킹 인덱스 업데이트 지연 3초 이내
- Eval 실행 평균 소요 시간 5분 이내

### AST 기반 청킹

**chunker 언어별 전략**:
- Python: `ast` 모듈로 FunctionDef, AsyncFunctionDef, ClassDef 단위 청크 생성
- Node.js / TypeScript: line-based chunk fallback (ADR-007)
- SyntaxError: 파일 전체를 단일 청크로 처리

**청크 구조**: `chunk_id` (SHA256 앞 8자리), `file_path`, `node_type`, `name`, `start_line`, `end_line`. source 원문은 포함하지 않는다.

**인덱스 보안 정책**:
- 인덱스에는 embedding vector와 metadata만 저장
- source text는 필요 시 파일 시스템에서 다시 읽음
- LLM 전달 직전 반드시 Context Gate 통과
- .gitignore와 .recoderignore 모두 존중
- `.env`, `*.pem`, `*.key`, `*.p12`, `*credential*`, `*secret*` 패턴 기본 제외

**청킹 정책**:
- 청크 길이 상한: 1500 토큰 (초과 시 함수 시그니처와 docstring만 포함)
- 청크 오버랩 없음 (경계 케이스는 BM25 보완 검색으로 커버)
- 이 정책은 Q1 초반 회귀 케이스로 검증하고 필요 시 조정

**traceback 처리**: 심볼 추출 실패 시 에러 타입만으로 검색 fallback. 검색 결과 없으면 active file 전체를 컨텍스트로 사용.

### Plan-Execute-Verify 체인

**PlannerAgent** (Bedrock Sonnet): 최대 5단계 ExecutionPlan을 Structured Output으로 생성. 절대 실행하지 않는다.

**Executor** (결정론적 디스패처): LLM이 아니다. action 타입에 따라 CodeAgent, InfraAgent, DeployAgent, TestRunner를 호출한다. 컨텍스트를 임의로 확장하지 않는다.

**VerifierAgent** (LLM 없음): Schema validation, base_sha256 검증, test_command 실행을 체크한다.

**test_command 정책**: package.json scripts와 pyproject.toml 기반 명령만 허용. shell metacharacter 차단. 타임아웃 30초 필수. working directory 프로젝트 루트 제한.

**재시도 정책**: Execute → Verify 루프 최대 2회. 소진 시 Approval UI에 "자동 검증 실패, 수동 검토 필요" 표시 후 멈춤. 사용자 승인 없이 외부에 영향 없음.

### Eval Harness

**케이스 구축 전략**: 수동으로 처음부터 만들지 않는다. 과거 실제 발생한 에러, 오픈소스 이슈, ReCoder 개발 중 마주친 에러를 재활용한다. Q1에는 카테고리별 뼈대 케이스 3~5개씩 총 20~30개를 확보하고 파이프라인 검증에 집중한다.

**카테고리 커버리지**:
1. Python 단일 파일 에러 수정
2. Python 다중 파일 패치
3. Node.js 에러 수정 (line-based fallback 기반)
4. Dockerfile 생성
5. docker build 실패
6. Health Check 실패

**Internal Engineering Gate**: Q1 카테고리별 pass_rate 60% 이상, Q2까지 80% 이상, false positive rate 5% 이하, false negative rate 10% 이하.

**Demo Release Gate**: 핵심 데모 시나리오 pass_rate 100%, Safety violation 0건, Secret leak 0건, 존재하지 않는 라이브러리 임포트 0건, rollback 불가 상황 미고지 0건, 잘못된 shell command 생성 0건.

Safety violation이 1건이라도 발생하면 CI에서 머지를 막는다. 이것이 pass_rate보다 우선이다.

---

## Q2-A 설계 — Control Plane Core

**목표**: "중앙 통제의 최소 골격"

### Q2-A1: Identity & Device

포함 항목: OIDC login, Device enrollment, Device Token 발급, OS Keychain 저장, heartbeat.

**ADR-006 타임박스 적용**:
- D+14: Device Token Keychain 저장과 heartbeat 기본 사이클 중간 점검
- D+14에 미완성이면 즉시 BaaS 피봇 결정
- D+21 무조건 피봇 (협상 불가)

**DoD**: Google/GitHub OIDC 로그인 동작, Device Token OS Keychain 저장, heartbeat 1분 간격 동작, Device 폐기 후 다음 heartbeat에서 차단.

### Q2-A2: Organization & Project

포함 항목: Organization, Workspace, Project, User, OrgMember, RBAC.

**RBAC 권한 도메인**:

```
project:read         project:write
device:enroll        device:revoke
deployment:request   deployment:approve   deployment:override
policy:read          policy:write         policy:assign
audit:read           audit:export
secret:update
production:deploy
breakglass:execute
```

**역할 정의**:
- `owner`: 모든 권한
- `admin`: 조직과 정책 관리
- `developer`: 배포 요청 가능, 승인 불가
- `approver`: 배포 승인 가능
- `auditor`: 감사 로그 조회만
- `viewer`: 읽기 전용

**멀티테넌트 보안**: 모든 API 엔드포인트에서 미들웨어가 Device Token으로부터 `org_id`를 추출하고 모든 DB 쿼리에 자동 적용한다. PostgreSQL Row Level Security를 추가 안전장치로 적용한다.

**DoD**: RBAC가 실제로 권한을 제한하고, 멀티테넌트 org_id 격리가 검증된다.

### Q2-A3: Audit & Sync

포함 항목: AuditLog, DeploymentRecord 업로드, pending queue, offline mode.

**AuditLog 필드**: `actor_user_id`, `actor_device_id`, `action`, `resource_type`, `resource_id`, `before_state`, `after_state`, `ip_address`, `occurred_at`, `event_hash`, `previous_event_hash`, `policy_bundle_version`.

UPDATE와 DELETE를 DB 레벨 트리거로 금지한다.

**hash chain 동시성 설계**:
- org_id 단위로 hash chain을 구성한다
- 각 org_id마다 monotonic sequence를 관리한다
- insert 시 DB transaction 안에서 마지막 event_hash 조회 후 row-level lock을 잡는다
- `event_hash = hash(previous_event_hash + canonical_json(event_body))`
- AuditLog는 **tamper-proof가 아니라 tamper-evident**다 — 조작 흔적을 사후 탐지할 수 있다
- 장기 archive: S3 Object Lock WORM 모드

**DoD**: AuditLog 기록률 100%, offline Level 1~2 정상 동작, pending queue 재전송 검증, hash chain 무결성 검증.

### Device Lease Policy

- 모든 Device Token은 짧은 lease를 가진다
- Local Core는 원격 작업 전 Control Plane heartbeat를 수행한다
- production 배포는 항상 online approval이 필요하다
- staging/dev Level 3 작업: offline cache 허용, 마지막 heartbeat 1시간 이내 조건
- device revoked 상태는 다음 heartbeat 시 즉시 반영된다
- lost device 신고 시 서버에서 즉시 폐기, 이후 동기화 pending audit event는 suspicious 표시
- "즉시 무효화"는 온라인 상태에서만 가능하다는 점을 운영 문서에 명시한다

### 오프라인 모드 정책

| 작업 레벨 | 허용 조건 |
|----------|---------|
| Level 1~2 로컬 작업 | 항상 허용 |
| Level 3 staging/dev | 정책 캐시 유효 + 마지막 heartbeat 1시간 이내 |
| Level 4 민감 변경 | 항상 차단 |
| 프로덕션 배포 | 항상 차단 |

AuditLog는 local pending queue에 저장하고 복구 시 재전송한다.

### Control Plane API 구조

- SaaS: `https://api.recoder.dev`
- Self-hosted: 사용자가 `base_url` 설정
- 로컬 개발 기본 포트: 18000 (고정값 아님)
- Q2-A: OIDC + Device Token만 사용
- mTLS: Q2-B에서 Enterprise 전용 선택 적용

---

## Q2-B 설계 — Governance

**전제**: Q2-A 완전 안정화 후 시작.

**DoD**: 정책 차단 재현율 95% 이상, 정책 변경 AuditLog 기록 100%, Web UI 2인 승인 흐름 완료, deny_with_fix_suggestion 표시 동작.

### OPA 정책 엔진

**방식**: OPA server REST API. Local Core와 같은 머신에서 OPA 독립 프로세스 실행.

**PolicyBundle 무결성 검증**:
- Control Plane이 PolicyBundle에 `version`과 `sha256` digest 부여
- Local Core는 다운로드 후 sha256 검증
- 정책 캐시: `bundle_version`, `downloaded_at`, `expires_at` 저장
- 정책 평가 결과에 `policy_bundle_version` 함께 기록
- AuditLog에 어떤 정책 버전으로 allow/deny가 났는지 기록 ("그 배포가 어떤 정책 버전에서 허용됐는가"에 답할 수 있어야 한다)

**OPA 평가 출력 5단계 + UI 매핑**:

| 출력 상태 | 의미 | UI 동작 |
|----------|------|---------|
| `allow` | 즉시 진행 | 실행 |
| `allow_with_approval` | 위험도 높아 승인 필요 | Approval Level 3~4 UI 진입 |
| `deny` | 자동 차단 | 차단 메시지 표시 |
| `deny_with_fix_suggestion` | 차단 + 수정 제안 | 수정 가이드 표시 후 중단 |
| `escalate_to_security` | 보안팀 에스컬레이션 | AuditLog 기록 + 보안 담당자 알림 |

OPA unavailable → fail-closed. Level 3~4 전체 차단.

### Policy / Approval 책임 경계

```
OPA:
  - allow / deny / escalate 판단
  - required_approvers 산출
  - 정책 위반 사유 산출

Control Plane:
  - ApprovalRequest 생성
  - 승인자 목록 확정
  - 승인/거부 상태 관리
  - AuditLog 기록

Local Core:
  - 정책 평가 요청
  - 승인 완료 전 실행 대기
  - 승인 완료 후 CommandTemplate 실행
```

### 정책 UI — Preset Policy Templates

자유 형식 Rego 빌더가 아니라 Preset Policy Template 방식으로 구현한다. 체크박스를 백엔드에서 고정 Rego 템플릿의 파라미터로 변환한다.

**기본 Preset 5개**:

| Preset | 제어 방식 |
|--------|---------|
| Trivy critical 취약점 차단 | on/off |
| 프로덕션 배포는 main 브랜치만 허용 | on/off |
| 22번 포트 외부 노출 차단 (DeploymentPlan/SG 기준) | on/off |
| SECRET/PASSWORD/TOKEN env 감지 시 Level 4 격상 | on/off |
| Level 3 이상 2인 승인 필요 | on/off |

고급 사용자는 추후 Rego 소스를 직접 편집하는 확장 기능을 제공한다.

### Multi-Approver 흐름

**Must**: Web UI 기반 2인 승인 (쐐기 시나리오 직접 필요).
**Should**: Slack 버튼 승인, 이메일 알림, 타임아웃 리마인더.

**승인자에게 표시하는 정보**: action 요약, 영향 대상, 실행될 명령 미리보기, 리스크 사유, 요청자 정보, 만료 시각, 정책 번들 버전.

거부 사유는 필수 입력이다. 타임아웃 기본 24시간. 모든 승인 연쇄가 AuditLog에 추적된다.

### Mini-Wedge Scenario

Q2-B 완료 시점에 중간 데모가 동작해야 한다.

> 정책 위반 배포를 시도하면 VSCode 안에서 OPA가 차단하고, Web UI에서 팀장의 2인 승인을 요청하는 흐름.

이것만으로 일반 AI 코딩 도구와의 차별화가 입증된다. Q4 쐐기 시나리오의 티저 역할이다.

---

## Q3 설계 — Cloud Execution

**ADR-008**: Q3는 ECS Fargate 배포 역량 데모 경로다.

**DoD**: ECS Rolling Update 성공률 95% 이상 (controlled environment 기준), rollback proposal 생성 성공률 100%, SBOM 생성률 100%, Trivy critical 차단 재현율 100%.

**측정 조건**: Cloud Preflight를 통과하지 못한 환경은 측정에서 제외. 실패 유형을 preflight / build / push / update-service / healthcheck failure로 분리 기록.

### Cloud Preflight Assistant

**read-only IAM만 사용**:
```
ecr:DescribeRepositories
ecs:DescribeClusters
ecs:DescribeServices
iam:GetRole
logs:DescribeLogGroups
elbv2:DescribeLoadBalancers
elbv2:DescribeTargetGroups
```

쓰기 권한이 필요한 작업은 DeploymentPlan 생성 이후 Approval Level에 따라 별도 처리한다.

**안내 원칙**: 미충족 리소스에 대한 AWS CLI 생성 안내는 "실행 명령"이 아닌 "가이드"로 표시. IAM, Security Group, Public ALB 관련 명령은 Level 4 경고 표시. `0.0.0.0/0` 오픈 명령은 위험 경고 기본 표시.

### ECS Fargate 배포

**Q3-A (Must): ECS Rolling Update**

1. InfraFileProposal로 ECS Task Definition JSON 생성 (FileTemplate Registry 기반)
2. ECR 로그인, docker build, 이미지 태그, ECR push (CommandTemplate Registry 경유)
3. boto3를 통해 `update-service --force-new-deployment` 호출
4. CloudWatch에서 배포 상태 폴링, Sidebar에 표시
5. Health Check 실패 시 이전 Task Definition으로 rollback proposal 생성 (Approval Level 3)

**Circuit Breaker**: 최근 5분 내 Health Check 실패 비율 50% 초과 시 배포 자동 중단.

**Q3-B (Should): ECS Blue/Green**

Q3-A 완전 안정화 이후 시작. 추가 사전 조건: 두 번째 Target Group, ALB Test Listener, CodeDeploy Application, Deployment Group, rollback 트리거 CloudWatch Alarm.

### SBOM 공급망 보안

**Q3 MVP (Must)**:
- Syft로 CycloneDX JSON 형식 SBOM 생성 (일회성 컨테이너)
- DeploymentRecord에 `sbom_path`, `sbom_version` 추가
- sensitive metadata 정책에 따라 처리
- OPA: SBOM 없는 배포 차단

**Q3 확장 (Should)**:
- GitHub Actions 안에서 Cosign keyless signing
- Sigstore Fulcio CA + OIDC 토큰 인증

### Trivy/Hadolint/gitleaks OPA 게이트

| 도구 | 수준 | 처리 |
|------|------|------|
| Trivy critical | — | 차단 |
| Trivy high | — | 기본 경고, 조직 정책으로 차단 전환 가능 |
| Hadolint error | — | 차단 |
| Hadolint warning | — | 경고만 표시 |
| gitleaks 시크릿 | — | 항상 차단, 원문 LLM 미전달 |

override 승인: Approval Level 4 격상 + AuditLog 사유 기록.

---

## Q4 설계 — GitOps + Observability + MCP

**ADR-008**: Q4는 EKS + ArgoCD 운영 사고 대응 데모 경로다.

**DoD**: 쐐기 시나리오 7단계 전체 무결 동작, ArgoCD PR 머지 후 클러스터 적용 10분 이내, production-like 데모 환경에서 Postmortem skeleton 5분 이내 생성.

### GitOps ArgoCD 연동

Ship Mode에 `method: gitops_argocd` 추가. FileTemplate Registry 기반으로 `argocd-application.yaml`과 `helm/values.yaml` 생성. 지정 Git 저장소에 커밋하고 PR 생성. 사용자가 PR 머지 시 ArgoCD 적용. ReCoder가 ArgoCD API 폴링으로 동기화 상태를 Sidebar에 표시.

**GitOps 롤백 정책** (ADR-005):
- staging/dev: ArgoCD API rollback 허용
- production: 기본적으로 Git revert PR 생성
- Severity 1 장애: emergency rollback 허용, 30분 이내 Git reconciliation PR 필수

**rollback PR 입력/출력**:

```
입력:
  failed_image_tag       (실패한 이미지 태그)
  last_healthy_image_tag (마지막 정상 이미지 태그)
  helm_values_path       (Helm values.yaml 경로)
  argocd_app_name        (ArgoCD 애플리케이션 이름)
  deployment_record      (DeploymentRecord)
  incident_id            (인시던트 ID)

출력:
  values.yaml image.tag를 이전 정상 버전으로 변경
  PR title: "rollback: restore {app} to {previous_image_tag}"
  PR body: incident summary, RCA candidate, approval link, rollback risk
  AuditLog에 rollback_pr_created 기록
```

### OpenTelemetry 통합

Watchdog v2의 Fluent Bit을 OTel Collector로 대체한다. EC2에서 컨테이너로 실행하고 docker-compose.yml에 Watchdog과 함께 정의한다.

**수집 파이프라인**:
- Receiver: `otlp` (grpc:4317, http:4318), `docker_stats`, `filelog`
- Processor: `batch`, `memory_limiter`, `resource_detection`
- Exporter (1차): Prometheus remote_write, Loki (Tempo는 Q4 후반)

**Local Core 계측**: LLM 호출마다 OTel Span 생성. attributes에 `provider`, `model`, `input_tokens`, `output_tokens`, `cost_usd`, `operation` 기록. 쐐기 시나리오 7단계가 하나의 Trace로 연결된다.

**ObservabilityAdapter**:
- `PrometheusAdapter`: 메트릭 쿼리, 에러율, 레이턴시, 메모리/CPU
- `LokiAdapter`: 로그 쿼리, 컨테이너 로그 발췌, 에러 키워드 검색
- `OTelQueryService`: Incident Timeline용 통합 API 제공

### Incident Correlation 설계

가장 최근 DeploymentRecord와의 시간 근접성은 1차 후보일 뿐이다. 다음 신호들을 함께 계산해서 **correlation score**를 산출한다.

- 배포 전후 error rate 변화
- 배포 전후 latency 변화
- 변경된 파일의 영역
- container restart event 시점
- health check failure 시점
- log keyword 변화
- traffic spike 여부
- 외부 dependency error 여부

correlation score가 낮으면 "최근 배포와 직접 관련성 낮음"으로 표시한다.

### RCA MVP 성공 기준

RCA는 "확정 원인"을 말하지 않는다. **근거 기반 후보 제안**이 정의다.

**구조화 출력 4가지 (이것이 Must RCA 전부)**:
1. 가장 의심되는 배포 이벤트와 근거
2. 관련 변경 파일 목록
3. 관측된 증상 (error rate, memory, restart event, health check failure)
4. 가능성 높은 원인 후보 1~3개와 각각의 근거

**표현 원칙**:
- "원인입니다" 사용 금지 → "가장 가능성 높은 원인 후보입니다"
- confidence score 함께 표시
- 관측 데이터 부족 시 "insufficient evidence" 표시

RCA 정밀 분석은 Should다.

### Postmortem Skeleton 템플릿

```markdown
# Postmortem Draft

## Summary
- Incident ID:
- Service:
- Environment:
- Detected at:
- Resolved at:

## Impact
- Affected users:
- Affected endpoints:
- Duration:

## Timeline
- Deployment:
- Detection:
- Approval:
- Rollback:
- Recovery:

## Suspected Root Cause
- Candidate:
- Evidence:
- Confidence:

## Actions Taken
- Rollback PR:
- Approval records:
- ArgoCD sync result:

## Follow-up Items
- Preventive action:
- Owner:
- Due date:
```

OTel backend 연결 불가 시: Watchdog incident.jsonl + AuditLog 기반으로 skeleton 생성, "observability 데이터 없음" 표시. 완성형 Postmortem 자동 생성은 Should다.

### MCP 서버화

**Q4 Must**: local stdio PoC만. `recoder_analyze` 도구 하나만 제공.

**Backlog**: Streamable HTTP remote, `recoder_deploy`, `recoder_operate`, OAuth 기반 remote 인증.

| Transport | 인증 | 비고 |
|-----------|------|------|
| stdio | X-Session-Token 내부 검증 | 로컬 Claude Desktop/Cursor 연동 |
| local HTTP | X-Session-Token 필수, Origin/Host 검증 | 127.0.0.1 바인딩 |
| Streamable HTTP remote | Device Token 또는 OAuth, allowlist origin | 기본 비활성화, Backlog |

---

## Final Demo Scope

**ADR-008**에 따라 ECS 데모와 EKS/ArgoCD 데모는 분리된다.

### Final Demo A — Q3 배포 역량 데모 (AWS)

- FastAPI sample service
- ECS Fargate staging (AWS 실제 환경)
- ECR
- CloudWatch
- SBOM 생성
- OPA 게이트

### Final Demo B — Q4 쐐기 시나리오 데모 (ADR-009: 실제 EKS)

- FastAPI production-like service
- **실제 EKS cluster** (데모 2시간 전 생성, 완료 후 즉시 삭제)
- ArgoCD
- Prometheus + Loki
- SaaS Control Plane
- GitHub repo

> **"production-like environment"**: 실제 고객 프로덕션이 아니라 production-like 환경을 의미한다. 실제 EKS와 동일한 GitOps, 승인, AuditLog, OTel 흐름을 재현한다.

### 시나리오 10단계

1. 정상 배포
2. 장애 유발 커밋 배포
3. OTel 자동 감지
4. Incident Timeline 생성
5. RCA 생성 (confidence score 포함, "가능성 높은 원인 후보")
6. rollback PR 자동 생성
7. Web UI 2인 승인
8. ArgoCD 적용
9. Health Check 회복
10. Postmortem skeleton 생성

---

## Dogfooding 체크포인트

| 시점 | 체크포인트 |
|------|----------|
| Q1 끝 | 자체 Control Plane PostgreSQL 마이그레이션을 ReCoder Infra Agent로 수행 |
| Q2-A 끝 | 자체 Control Plane을 ReCoder Ship Mode로 ECS Rolling Update로 배포 |
| Q3 끝 | CloudWatch 또는 Watchdog 기반으로 자체 ReCoder 서비스 장애 감지 |
| Q4 초반 | OTel 파이프라인이 자체 ReCoder 서비스 장애 감지 (Q3 Dogfooding을 OTel로 업그레이드) |
| Q4 끝 | 자체 ArgoCD가 자체 ReCoder를 배포하고 Postmortem skeleton 자동 생성 |

> Q3 Dogfooding은 CloudWatch/Watchdog 기반으로 한다. OTel 기반 감지는 Q4 초반 체크포인트로 분리한다. Q3에 OTel까지 넣으면 Q3 범위가 다시 무거워진다.

**IDE fragmentation 대응** (ADR-010): CLI headless 모드는 Backlog다. 백엔드 로직 코딩은 PyCharm으로 하되, 기능 테스트와 시연은 반드시 VSCode Extension을 통해 수행한다. 우회로를 만들면 Extension 완성도가 떨어진다.

**매주 기록**: Slack에 "이번 주 ReCoder가 ReCoder 개발에 도움 됐던 사례 3개" 기록.

---

## 분기별 DoD 요약

| 분기 | 완료 기준 |
|------|---------|
| Q1 | 카테고리별 pass_rate 60%, Safety violation 0건, Windows/Linux 빌드 자동화, 청킹 지연 3초 이내, Eval 5분 이내 |
| Q2-A1 | OIDC/Device Token/heartbeat 동작, ADR-006 타임박스 준수 (14일 체크포인트) |
| Q2-A2 | RBAC 권한 제한 검증, org_id 격리 검증 |
| Q2-A3 | AuditLog 기록률 100%, hash chain 무결성 검증 |
| Q2-B | 정책 차단 재현율 95%, 정책 변경 AuditLog 100%, Web UI 2인 승인, Mini-Wedge 동작 |
| Q3 | ECS 배포 성공률 95% (controlled env), SBOM 생성률 100%, Trivy critical 차단 100% |
| Q4 | Final Demo 시나리오 10단계 전체 무결 동작 |

---

## 지금 내려야 할 두 가지 결정

**Open-core 여부** — Q1 시작 전에 결정한다. Local Core를 Apache 2.0으로 공개하고 Control Plane을 상용 서비스로 운영하는 open-core 모델을 권장한다. 이 결정이 늦어지면 어떤 코드를 공개할 수 있는지 판단이 불가능한 상태로 코드베이스가 뒤섞인다.

**졸업 후 지속 여부** — Q1 시작 전에 결정한다. 대표와 기술 책임의 역할 분담, 중도 하차 시 기여도 기반 지분 정산 룰을 문서로 합의하고 서명한다. Q2~Q3까지 미루면 Q4에 기술이 완성돼도 팀이 무너진다. 기술 설계보다 이것이 더 어렵고 더 중요하다.

---

## 최종 포지셔닝

> **ReCoder Enterprise는 AI가 코드를 고쳐주는 도구가 아니라, 개발자의 DevOps 실행을 조직 정책·다중 승인·감사 로그·GitOps·관측성 데이터와 연결해 프로덕션 변경을 안전하게 수행하는 DevOps Execution Platform이다.**

이 설계서는 지금부터 기능을 더 추가하지 않는다.

더 적은 기능을 더 엄격하게 완성하는 것이 목표다.

성공의 핵심은 설계의 우수성이 아니라 Q1부터 Q4까지 절제를 실제로 지키는 집행력이다.
