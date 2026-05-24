# ReCoder Local Core — REST API 명세 v10

이 문서는 **4명 팀원(A/B/C/D)의 단일 참조점**입니다. Local Core가 제공하는 모든
HTTP 엔드포인트를 정의합니다. 변경 시 본 문서와 `core/schemas.py` 동시 PR 필수.

---

## 공통 규칙

### 인증

모든 엔드포인트는 `X-Session-Token: <token>` 헤더 필수.
예외: `GET /api/health` (인증 면제).

토큰은 `~/.recoder/runtime.json`에서 읽음. Extension/Discord Bot은 같은 PC의
Local Core에 접근할 때만 이 토큰 사용.

### 호스트

- 항상 `127.0.0.1:<port>` (loopback). 외부 노출 금지.
- 기본 포트 17894, 사용 중이면 17895~17910 fallback.

### 응답 헤더

모든 응답에 다음 헤더 포함:
- `X-Request-ID: <uuid>` — 디버깅 / 로그 추적용
- `Content-Type: application/json`

### 에러 응답 표준 (4xx/5xx)

**모든 에러 응답은 `schemas.ErrorResponse` 형식 통일.**

```json
{
  "error": true,
  "code": "INVALID_REQUEST",
  "message": "사용자에게 보여줄 한 줄",
  "detail": "디버깅용 — 마스킹된 값만",
  "request_id": "01HX...",
  "timestamp": "2026-05-24T03:45:12Z"
}
```

`code` 가능한 값은 `schemas.ErrorCode` enum 참조 (15종).

핵심:
- `INVALID_REQUEST` (400) — 요청 형식 오류
- `UNAUTHORIZED` (401) — X-Session-Token 없음/잘못됨
- `NOT_FOUND` (404) — 리소스 없음
- `CONFLICT` (409) — base_sha256 mismatch 등 동시성 문제
- `UNPROCESSABLE` (422) — Pydantic validation 실패
- `PREFLIGHT_FAILED` (422) — 도메인 거부
- `INTERNAL_ERROR` (500) — 서버 일반 오류
- `LLM_PROVIDER_ERROR` (502) — Bedrock/Gemini 실패
- `ROLLBACK_INFEASIBLE` (409) — snapshot 부재

### Idempotency

`POST` 중 부수효과 있는 엔드포인트(`/remediations/{id}/apply`, `/deployments`,
`/deployments/{id}/rollback`)는 `Idempotency-Key` 헤더 권장. 같은 키로 중복 호출 시
이전 결과 그대로 반환.

---

## 엔드포인트 목록

### Health (Phase 1 v6.4 기존)

| Method | Path | 담당 | 설명 |
|--------|------|------|------|
| GET    | `/api/health` | A | 인증 면제. `{status, version, uptime_seconds, port}` |
| POST   | `/api/diagnostics/run` | A | 5단계 Ready 진단 → `DiagnosticsResult` |
| GET    | `/api/diagnostics/latest` | A | 캐시된 `DiagnosticsResult` |

### Analyze (Phase 1 v6.4 기존)

| Method | Path | 담당 | 설명 |
|--------|------|------|------|
| POST   | `/api/analyze` | A | `AnalyzeRequest` → `PatchProposal` (기존 흐름 유지) |
| POST   | `/api/analyze/approve` | A | patch 승인 |

### Release Contract (§29) — A

| Method | Path | 설명 |
|--------|------|------|
| GET    | `/api/projects/{project_id}/contract` | 현재 `ReleaseContract` 조회 |
| PUT    | `/api/projects/{project_id}/contract` | `ReleaseContract` 갱신 (Wizard가 호출) |
| POST   | `/api/projects/{project_id}/contract/generate` | First Run Wizard용 — 자동 추정 결과 반환 |

### Preflight (§30, §31) — A + B

| Method | Path | 담당 | 설명 |
|--------|------|------|------|
| POST   | `/api/preflight/run` | A(Static) + B(Runtime) | `RunPreflightRequest` → `PreflightRun`. Static + Runtime 통합 실행. `include_runtime=false`면 Static만. |
| GET    | `/api/preflight/{run_id}` | A | 단일 `PreflightRun` 조회 |
| GET    | `/api/preflight` | A | 최근 N개 (`?project_id=&limit=`) |

### Remediation (§32, §33.2) — A

| Method | Path | 설명 |
|--------|------|------|
| GET    | `/api/remediations/{proposal_id}` | `RemediationProposal` 조회 |
| POST   | `/api/remediations/{proposal_id}/apply` | `ApplyRemediationRequest` → `ApplyRemediationResponse`. approve=false면 거절. |
| GET    | `/api/remediations/by-preflight/{run_id}` | 해당 PreflightRun이 생성한 proposal 목록 |
| GET    | `/api/remediation-runs/{run_id}` | `RemediationRun` 조회 (적용 이력) |

### Deployment (§33.3) — A + B

| Method | Path | 담당 | 설명 |
|--------|------|------|------|
| POST   | `/api/deployments` | A(Ledger) + B(실행) | `CreateDeploymentRequest` → `DeploymentLedger`. Preflight 통과 후만 가능. |
| GET    | `/api/deployments/{id}` | A | 단일 `DeploymentLedger` |
| GET    | `/api/deployments` | A | 최근 N개. C의 Replay/Standup이 사용 |
| POST   | `/api/deployments/{id}/rollback` | B | `RollbackDeploymentRequest` → `DeploymentLedger`. Approval Level 3, typing_confirm 필수 |
| POST   | `/api/deployments/{id}/cv-tick` | B | Continuous Verification 결과 (5분 감시) 보고 |

### Replay (§38) — A 데이터 + C 가공

| Method | Path | 설명 |
|--------|------|------|
| GET    | `/api/replay/{deployment_id}` | `ReplayTimeline` 반환. C의 Replay UI/Discord가 사용 |

### Standup (§39) — A 집계 + C 전송

| Method | Path | 설명 |
|--------|------|------|
| GET    | `/api/standup/today` | 오늘의 `StandupBriefing` 생성 (LLM Haiku 호출 가능) |
| POST   | `/api/standup/deliver` | `{channel, recipient}` — Discord/Email 전송 트리거 |

### Forecast / Diff (§41, §42) — A

| Method | Path | 설명 |
|--------|------|------|
| GET    | `/api/forecast` | 오늘의 `DeployForecast` |
| GET    | `/api/diff?before=&after=` | 두 DeploymentLedger 비교 → `DeploymentDiff` |

### IncidentMemory (§35) — A

| Method | Path | 설명 |
|--------|------|------|
| GET    | `/api/incident-memory/{fingerprint}` | 매칭 결과 (없으면 404) |
| GET    | `/api/incident-memory` | 전체 리스트 (`?project_id=`) |
| DELETE | `/api/incident-memory/{fingerprint}` | 학습 데이터 삭제 |

### Discord ChatOps (§37) — C, A 등록 라우트

| Method | Path | 담당 | 설명 |
|--------|------|------|------|
| GET    | `/api/discord/identities` | A | 등록된 Discord 사용자 목록 |
| POST   | `/api/discord/identities` | A | `RegisterDiscordIdentityRequest` — 새 사용자 등록 |
| DELETE | `/api/discord/identities/{discord_user_id}` | A | 해제 |
| POST   | `/api/discord/command` | C(client) → A(server) | `DiscordCommandRequest` → `DiscordCommandResult`. Bot이 명령 받아 Local Core에 전달하는 어댑터 |

### Cloud Relay (§46) — B

| Method | Path | 담당 | 설명 |
|--------|------|------|------|
| GET    | `/api/cloud-relay/queue` | B | 대기 중인 명령 목록 — Local Core가 polling |
| POST   | `/api/cloud-relay/queue/{entry_id}/execute` | B | `ExecuteCommandQueueRequest` — 큐 entry 실행 + 결과 보고 |
| POST   | `/api/cloud-relay/incident-notification` | B | Watchdog → Cloud Relay → Discord 라우터 (incoming webhook) |

### Misc / Cost / Session

| Method | Path | 설명 |
|--------|------|------|
| GET    | `/api/cost` | 일일/월간 비용 (기존) |
| GET    | `/api/session/{session_id}` | SessionRecord 조회 |
| DELETE | `/api/session/data?older_than_days=30` | 보관 정책 정리 |

---

## 페이로드 모델 매핑

각 엔드포인트의 요청/응답 본문은 `core/schemas.py`의 Pydantic 모델 기준.

| 엔드포인트 | 요청 모델 | 응답 모델 |
|-----------|----------|----------|
| POST /api/preflight/run | `RunPreflightRequest` | `PreflightRun` |
| POST /api/remediations/{id}/apply | `ApplyRemediationRequest` | `ApplyRemediationResponse` |
| POST /api/deployments | `CreateDeploymentRequest` | `DeploymentLedger` |
| POST /api/deployments/{id}/rollback | `RollbackDeploymentRequest` | `DeploymentLedger` |
| POST /api/discord/identities | `RegisterDiscordIdentityRequest` | `DiscordIdentity` |
| POST /api/discord/command | `DiscordCommandRequest` | `DiscordCommandResult` |
| POST /api/cloud-relay/queue/{id}/execute | `ExecuteCommandQueueRequest` | `CommandQueueEntry` |

---

## 4명 팀원별 담당 / 호출 매트릭스

| 팀원 | 구현하는 엔드포인트 | 호출만 하는 엔드포인트 |
|------|---------------------|----------------------|
| **A** | Health, Analyze, Contract, Preflight(static), Remediation, Deployment(ledger), Replay, Standup, Forecast/Diff, IncidentMemory, Discord identities | (없음 — 모든 데이터의 생산자) |
| **B** | Preflight(runtime 부분), Deployment(실행/롤백), CV-tick, Cloud Relay 3종 | `/api/preflight/run` (Static 결과 받기), `/api/contract` (Contract 읽기) |
| **C** | (Discord Bot 자체는 별도 프로세스) | `/api/discord/command`, `/api/replay/*`, `/api/standup/*`, `/api/cloud-relay/incident-notification` (Push 라우터) |
| **D** | (UI/Wizard만, 라우트 구현 없음) | `/api/contract`, `/api/preflight/run`, `/api/remediations/*`, `/api/deployments/*` (모두 표시용) |

---

## 보안 가드레일

1. **Approval Level 3~4 작업**은 `typing_confirm` 필수
   - `POST /api/deployments/{id}/rollback`
   - `POST /api/remediations/{id}/apply` (target_type=DOCKER_RUNTIME 또는 RELEASE_CONTRACT 등 risk_level=high)

2. **Discord 명령**은 `DiscordPermissionTier` 검증
   - `READ_ONLY`: GET 계열만
   - `APPROVE_L1_L2`: PatchProposal/RemediationProposal 승인까지
   - `REQUIRE_DESKTOP`: Level 3~4는 데스크탑 강제

3. **Cloud Relay**는 옵트인:
   - 흐름 ① 명령 큐: `CloudRelayUserMapping.enable_command_queue=true`
   - 흐름 ② 인시던트 알림: `enable_incident_relay=true`
   - 흐름 ③ 긴급 롤백: `enable_emergency_rollback=true` (P2)

4. **모든 에러 응답**은 `ErrorResponse` 형태 통일 — 내부 traceback 절대 노출 금지.
   `context_gate.mask_secrets()` 통과 후 detail에 포함.

---

## 변경 절차

본 문서나 `schemas.py`를 변경하려면:

1. 영향받는 팀원에게 변경 의도 공유 (Slack/Discord)
2. PR 작성 — 본 문서 + schemas.py 동시 수정
3. 다른 팀원 1명 리뷰
4. 머지 후 develop 통합

---

## 변경 이력

- **2026-05-24**: v10 PART II/III/IV 인터페이스 합의 산출물 (Phase A-1) — A 작성
- (이후 변경 시 여기에 추가)
