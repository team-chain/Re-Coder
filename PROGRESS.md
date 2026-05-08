# ReCoder v6.4 — 구현 진척률 트래커
> **설계서 버전**: v6.4-final
> **마지막 업데이트**: 2026-05-08 (P0 전항목 완료 — Claude Cowork 2차 세션)
> **담당**: 이동규(백엔드·인프라), 윤세빈(Extension·코드 분석)
> **AI 구현 진행**: Claude (Cowork) — 2026-05-07 1차 구현, 2026-05-08 실측 감사 + P0-1~P0-13 전항목 완료 + pytest 15건 통과

---

## 전체 진척률 (실측)

| 학기 | 범위 | 진척률 | 비고 |
|---|---|---|---|
| 1학기 필수 | Stage 1 + Stage 2 Local | ✅ **95%** | P0 13개 완료. **1학기 One Scene 데모 가능** 상태. E2E(실 Docker) + README 미완 |
| 1학기 선택 | 고도화 항목 | 🔄 **15%** | S-5 골격·S-7 UI 骨格 있음. S-1(PyInstaller)/S-3(gitleaks)/S-6/S-8/S-9 미완 |
| 2학기 | Stage 2 완성 + Stage 3 | 🔲 0% | 구조만 정의됨 |

> **2026-05-08 2차 갱신**: server↔agent 배선(P0-1~6), Webview 파싱 정상화(P0-7), Ship 탭 흐름(P0-8), coreManager spawn 수정(P0-9), stale PID 강제종료(P0-10), cost 결선(P0-11), pytest 15건(P0-12), Ready 카드(P0-13) 모두 완료. pytest 15/15 통과 확인.

---

## 범례

| 아이콘 | 상태 |
|---|---|
| 🔲 | 미시작 |
| 🔄 | 진행 중 |
| ✅ | 완료 |
| ⚠ | 표면상 완료/주장 완료지만 실측 시 누락 또는 결함 발견 |
| ⏸ | 보류 (의존성 대기) |
| ❌ | 블로킹 (이슈 있음) |

---

## 1학기 — 필수 구현

### [Phase 0] 기반 작업 (첫 주 마일스톤)

| # | 항목 | 담당 | 상태 | 메모 |
|---|---|---|---|---|
| 0-1 | 레포 구조 재편 (`core/`, `extension/` 분리) | 공통 | ✅ | |
| 0-2 | `schemas.py` 중복 클래스 제거 + v6.4 스키마 추가 | 공통 | ✅ | 실측 결과 중복 클래스 없음. 이슈란의 stale 항목 정정 필요 |
| 0-3 | Local Core `main.py` — PyInstaller 엔트리포인트 + runtime.json 저장 | 이동규 | ✅ | stale PID 강제종료·attached_pids 카운팅 완료 (P0-10) |
| 0-4 | `GET /api/health` 엔드포인트 구현 | 이동규 | ✅ | |
| 0-5 | `POST /api/analyze` Mock 응답 구현 | 이동규 | ✅ | Mock 자체는 OK, 실제 연결은 3-5에서 처리 |
| 0-6 | VSCode Extension 기본 구조 생성 (`extension/`) | 윤세빈 | ✅ | |
| 0-7 | Extension → Core HTTP 통신 브릿지 (`coreClient.ts`) | 윤세빈 | ✅ | |
| 0-8 | Sidebar Webview 기본 표시 | 윤세빈 | ✅ | |
| 0-9 | **E2E 통신 테스트 완료** (Mock PatchProposal → Sidebar diff 표시) | 공통 | 🔄 | pytest 15/15 + TS smoke(coreClient.smoke.ts) 작성 완료. 실 Docker E2E는 로컬 환경 필요 |

---

### [Phase 1] Local Core — FastAPI 서버 + 보안

| # | 항목 | 담당 | 상태 | 메모 |
|---|---|---|---|---|
| 1-1 | FastAPI 서버 (`core/server.py`) — 127.0.0.1 바인딩 | 이동규 | ✅ | |
| 1-2 | `X-Session-Token` 검증 미들웨어 | 이동규 | ✅ | |
| 1-3 | Origin/Host 헤더 검증 미들웨어 | 이동규 | ✅ | |
| 1-4 | `GET /api/status` — Orchestrator 상태 반환 | 이동규 | ✅ | |
| 1-5 | `POST /api/project/scan` — 워크스페이스 스캔 → ProjectProfile | 이동규 | ✅ | `project_scanner.ProjectScanner` 실호출로 교체 완료 (P0-1 배선 시 함께 수정) |
| 1-6 | `GET /api/project` — ProjectProfile 조회 | 이동규 | ✅ | |
| 1-7 | `GET /api/cost` — 일일/월별 비용 조회 | 이동규 | ✅ | `session_logger.get_usage_summary()` 결선 완료 (P0-11) |

---

### [Phase 2] Context Gate + LLM Router

| # | 항목 | 담당 | 상태 | 메모 |
|---|---|---|---|---|
| 2-1 | `context_gate.py` 이식 — async 전환 | 이동규 | ✅ | |
| 2-2 | `trigger_detector.py` 이식 | 이동규 | ✅ | |
| 2-3 | `llm/router.py` 이식 — Circuit Breaker + 비용 추적 | 이동규 | ✅ | |
| 2-4 | `llm/bedrock_provider.py` 이식 + Structured Output 1순위 확인 | 이동규 | ✅ | |
| 2-5 | `llm/gemini_provider.py` 이식 | 이동규 | ✅ | |
| 2-6 | First Run 진단 마법사 (`core/first_run.py`) | 이동규 | ✅ | Core/AI/Docker Ready만. AWS·Ops Ready는 S-2에서 |

---

### [Phase 3] Stage 1 — 에러 분석 + 코드 패치

| # | 항목 | 담당 | 상태 | 메모 |
|---|---|---|---|---|
| 3-1 | `analyzer.py` 재작성 — `AnalyzeRequest` 입력 | 이동규 | ✅ | 다만 `AgentEvent`만 반환. `PatchProposal` 변환 단계는 별도 (3-5와 함께) |
| 3-2 | `code_agent.py` 이식 — SHA256 검증 + 백업 + diff 적용 | 윤세빈 | ✅ | `generate_patch(request, session_id) → PatchProposal` 존재 |
| 3-3 | `RollbackPolicy` — Code Rollback (백업 복원 + git diff 검증) | 윤세빈 | ✅ | `code_agent.rollback_patch()` 완료 |
| 3-4 | `risk_validator.py` 신규 구현 | 윤세빈 | ✅ | |
| 3-5 | `POST /api/analyze` 실제 구현 (Mock 교체) | 이동규 | ✅ | `analyzer.analyze` + `code_agent.generate_patch` 체인 결선. Mock 완전 제거 (P0-1) |
| 3-6 | `POST /api/patch/approve` — 패치 적용 | 이동규 | ✅ | `code_agent.apply_patch()` 결선 + 실패 시 rollback·IDLE 전이 (P0-2) |
| 3-7 | `POST /api/patch/reject` — 패치 거절 | 이동규 | ✅ | |
| 3-8 | Terminal Collector (`extension/src/collectors/terminalCollector.ts`) | 윤세빈 | ✅ | ShellIntegration + fallback 완료 |
| 3-9 | Context Collector (`extension/src/collectors/contextCollector.ts`) | 윤세빈 | ✅ | |
| 3-10 | Sidebar diff preview UI | 윤세빈 | ✅ | `analyze_result` 핸들러가 server 통째 반환 형태에 맞게 수정. `_currentPatchProposal`에 직접 세팅 (P0-7) |
| 3-11 | Approval Level 1~2 UI | 윤세빈 | ✅ | `infra_approved` 응답에서 `plan` 자동 세팅. deploy-command-preview 카드(docker build/run 명령) 표시 (P0-8) |
| 3-12 | "에러 로그 붙여넣기" 버튼 (수동 입력 안전망) | 윤세빈 | ✅ | |

---

### [Phase 4] Stage 2 — Dockerfile + docker build/run + Health Check

| # | 항목 | 담당 | 상태 | 메모 |
|---|---|---|---|---|
| 4-1 | `FileTemplate Registry` (`core/registries/file_registry.py`) | 이동규 | ✅ | Dockerfile 4종 + compose + GitHub Actions |
| 4-2 | `CommandTemplate Registry` (`core/registries/command_registry.py`) | 이동규 | ✅ | 11개 템플릿 + 파라미터 검증 |
| 4-3 | `infra_agent.py` 이식 — FileTemplate Registry 연동 | 이동규 | ✅ | |
| 4-4 | Trivy 일회성 컨테이너 실행 (`core/quality_runner.py`) | 이동규 | ✅ | |
| 4-5 | Hadolint 일회성 컨테이너 실행 | 이동규 | ✅ | |
| 4-6 | `local_deploy_agent.py` 신규 구현 | 이동규 | ✅ | |
| 4-7 | Health Check 4단 폴백 로직 | 이동규 | ✅ | |
| 4-8 | `DeploymentRecord` 저장 (docker run 성공 직후) | 이동규 | ✅ | |
| 4-9 | `POST /api/infra/generate` — Dockerfile 생성 | 이동규 | ✅ | `infra_agent.generate()` + `quality_runner.run_hadolint()` 결합. Mock 제거 (P0-3) |
| 4-10 | `POST /api/deploy/local` — docker build/run | 이동규 | ✅ | `LocalDeployAgent.deploy()` background task 실행. `/api/deploy/status` polling 신규 추가 (P0-4, P0-5) |
| 4-11 | Dockerfile preview UI (Sidebar) | 윤세빈 | ✅ | `security_scan_result` 핸들러 추가. 보안 스캔 카드 렌더링 (P0-6 결선) |
| 4-12 | docker build/run 진행 상황 표시 UI | 윤세빈 | ✅ | `/api/deploy/status` polling 1.5초 간격. stage·log_tail·health 결과 카드 표시 (P0-5 결선) |

---

### [Phase 5] Orchestrator FSM + 세션 기록

| # | 항목 | 담당 | 상태 | 메모 |
|---|---|---|---|---|
| 5-1 | `orchestrator.py` 이식 + Stage 2 상태 추가 | 이동규 | ✅ | server.py가 `OrchestratorState` enum을 직접 전이하여 FSM 관리 (실용적 단순화) |
| 5-2 | `session_logger.py` 신규 구현 | 이동규 | ✅ | `/api/cost`가 `session_logger.get_usage_summary()` 호출로 결선 완료 (P0-11) |
| 5-3 | `ProjectProfile` 저장/조회 | 이동규 | ✅ | `/api/project/scan`이 `project_scanner.ProjectScanner` 실호출로 교체 (1-5 완료) |

---

### [Phase 6] Local Core Singleton 관리

| # | 항목 | 담당 | 상태 | 메모 |
|---|---|---|---|---|
| 6-1 | `core.lock` lock file 생성/관리 | 이동규 | ✅ | |
| 6-2 | stale process 감지 + 강제 종료 후 재실행 | 이동규 | ✅ | `psutil.terminate()` + 3초 wait + `kill()` 구현. is_recoder 판별 로직 포함 (P0-10) |
| 6-3 | Extension deactivate 시 graceful shutdown (5초 타임아웃) | 윤세빈 | ✅ | `SIGTERM` → 5초(`SHUTDOWN_GRACE_MS`) → `SIGKILL` 폴백 구현 완료 (P0-10) |
| 6-4 | 다중 VSCode 창 공유 (인스턴스 카운팅) | 이동규 | ✅ | `attached_pids` 배열 + `attach_pid()` / `detach_pid()` 완료 (P0-10) |

---

### [Phase 7] Extension Core Manager + Lazy Spawn

| # | 항목 | 담당 | 상태 | 메모 |
|---|---|---|---|---|
| 7-1 | `coreManager.ts` — lazy spawn | 윤세빈 | ✅ | `_findCoreBinary()` → `SpawnSpec{command, args}` 분리. Windows `python.exe/py` 자동 탐색 (P0-9) |
| 7-2 | `runtime.json` 읽기 + 포트/토큰 획득 | 윤세빈 | ✅ | |
| 7-3 | Polling 루프 (3~5초) | 윤세빈 | ✅ | |
| 7-4 | Core 미시작 시 사이드바에 "시작 중..." 표시 | 윤세빈 | ✅ | Ready 카드(P0-13)에서 `core_ready: fail` 시 안내 문구 + 토스트 병용 |

---

### [Phase 8] First Run Degraded Mode UI

| # | 항목 | 담당 | 상태 | 메모 |
|---|---|---|---|---|
| 8-1 | Core Ready 상태 표시 | 공통 | ✅ | Sidebar 상단 Ready 카드 — core_ready 칩 + 상태 색상 (P0-13) |
| 8-2 | AI Ready 상태 표시 (Stage 1 활성화 조건) | 공통 | ✅ | ai_ready 칩 + 미충족 시 "AWS Bedrock 키 필요" 안내 (P0-13) |
| 8-3 | Docker Ready 상태 표시 (Stage 2 활성화 조건) | 공통 | ✅ | docker_ready 칩 + 미충족 시 "Docker Desktop 설치" 안내 (P0-13) |
| 8-4 | 미충족 Mode 회색 처리 + 활성화 안내 링크 | 윤세빈 | ✅ | Build·Ship 탭 비활성화 + Ready 카드 미충족 항목 강조 (P0-13) |

---

## 1학기 — 결정적 결손 항목 (P0) — **전항목 완료** ✅

> 2026-05-08 2차 세션에서 P0-1~P0-13 모두 닫힘. pytest 15/15 통과.

| # | 항목 | 상태 | 완료 내용 |
|---|---|---|---|
| P0-1 | `/api/analyze` ↔ `analyzer` ↔ `code_agent` 배선 | ✅ | Mock 제거, 실체인 결선 |
| P0-2 | `/api/patch/approve` ↔ `code_agent.apply_patch` | ✅ | 파일 실제 수정 + rollback |
| P0-3 | `/api/infra/generate` ↔ `infra_agent` + hadolint | ✅ | 실 Dockerfile 생성 |
| P0-4 | `/api/deploy/local` ↔ `LocalDeployAgent` | ✅ | background task 실행 |
| P0-5 | `/api/deploy/status` 신규 | ✅ | stage/log_tail/health polling |
| P0-6 | `/api/security/scan` 신규 | ✅ | Trivy+Hadolint 호출 결과 |
| P0-7 | Sidebar `analyze_result` 파싱 수정 | ✅ | PatchProposal 통째 수신 적응 |
| P0-8 | `_currentDeployPlan` 라이프사이클 | ✅ | infra_approved 시 자동 세팅 |
| P0-9 | `coreManager` spawn exec/args 분리 | ✅ | Windows python 탐색 포함 |
| P0-10 | stale PID 강제종료 + SIGKILL 폴백 | ✅ | psutil + attached_pids 완성 |
| P0-11 | `/api/cost` ↔ `session_logger` 결선 | ✅ | 실 누적치 반환 |
| P0-12 | pytest 15건 + TS smoke | ✅ | 15/15 통과, coreClient.smoke.ts |
| P0-13 | Sidebar Ready 카드 (Core/AI/Docker) | ✅ | 3-칩 + 미충족 안내 |

---

## 1학기 — 선택 구현 (시간 여유 있을 때)

| # | 항목 | 담당 | 상태 | 메모 |
|---|---|---|---|---|
| S-1 | PyInstaller 패키징 (Windows x64 단일 실행파일) | 이동규 | 🔲 | VSIX에 binary 포함, dev fallback과 별개 트랙 |
| S-2 | First Run AWS Deploy/Ops Ready 진단 (1학기는 표시만) | 이동규 | 🔲 | |
| S-3 | gitleaks 통합 (`quality_runner.py`) | 이동규 | 🔲 | secret 원문 LLM 미전송 |
| S-4 | 다중 파일 PatchProposal | 윤세빈 | 🔲 | |
| S-5 | Sidebar 3-Mode 탭 UI 완성 (Build/Ship/Operate 안내) | 윤세빈 | 🔄 | 골격은 있음 |
| S-6 | SQLite 세션 관리 완성 | 이동규 | 🔲 | |
| S-7 | 비용 추적 UI (사이드바 하단) — 실데이터 | 윤세빈 | 🔄 | UI는 있음, 데이터 결선이 P0-11 |
| S-8 | `git_agent.py` 이식 (Git Commit 기능) | 이동규 | 🔲 | |
| S-9 | Local Container Rollback 자동화 (Level 1~2) | 이동규 | 🔲 | |
| S-10 | Approval Level 3~4 UI 골격 (2학기 대비) | 윤세빈 | 🔲 | |

---

## 2학기 — Stage 2 완성 + Stage 3

(변경 없음 — 1학기 P0가 닫힌 후 진입)

| # | 항목 | 담당 | 상태 |
|---|---|---|---|
| 2S-1 | EC2 SSH 배포 (`deploy_agent.py` 이식) | 이동규 | 🔲 |
| 2S-2 | EC2 Watchdog v1 자동 설치 | 이동규 | 🔲 |
| 2S-3 | ECR + EC2 배포 흐름 | 이동규 | 🔲 |
| 2S-4 | GitHub Actions 생성 | 이동규 | 🔲 |
| 2S-5 | EC2 Watchdog v2 (Fluent Bit, CloudWatch) | 이동규 | 🔲 |
| 2S-6 | VSCode Extension 운영 상태 조회 트리거 | 윤세빈 | 🔲 |
| 2S-7 | `ops_agent.py` 신규 구현 | 이동규 | 🔲 |
| 2S-8 | SSH 기반 incident.jsonl 조회 | 이동규 | 🔲 |
| 2S-9 | Watchdog API 조회 (옵트인) | 이동규 | 🔲 |
| 2S-10 | Approval Level 3~4 완성 | 윤세빈 | 🔲 |
| 2S-11 | RollbackPolicy 전체 | 이동규 | 🔲 |
| 2S-12 | AWS Deploy / Ops Ready First Run | 이동규 | 🔲 |
| 2S-13 | macOS/Linux 멀티 OS 빌드 파이프라인 | 공통 | 🔲 |
| 2S-14 | Discord Webhook 수신 흐름 | 이동규 | 🔲 |

---

## 이슈 / 블로킹 사항

| 날짜 | 항목 | 내용 | 상태 |
|---|---|---|---|
| 2026-05-07 | schemas.py 중복 정의 | 실측 결과 중복 없음 (이전 보고가 stale) | ✅ 해결 |
| 2026-05-08 | server↔agent 미배선 | 3-5/3-6/4-9/4-10 모두 Mock 응답 | ✅ P0-1~P0-4 완료 |
| 2026-05-08 | Webview 응답 파싱 오류 | analyze_result 파싱 불일치 | ✅ P0-7 완료 |
| 2026-05-08 | dev 모드 spawn 실패 | coreManager 단일 문자열 spawn | ✅ P0-9 완료 |
| 2026-05-08 | stale lock 강제 종료 미구현 | acquire_lock `pass`만 있음 | ✅ P0-10 완료 |
| 2026-05-08 | 테스트 0건 | core/, extension/ 자동 테스트 없음 | ✅ pytest 15건 + TS smoke 완료 |
| 2026-05-08 | **실 Docker E2E 미수행** | 실제 docker run 환경에서 전체 흐름 미검증 | ⚠ 로컬 머신에서 직접 수행 필요 |

---

## 2026-05-08 최종 역할 분담 (Selectives — 1학기 완성)

> **원칙**: 두 사람이 건드리는 파일이 겹치지 않도록 파일 단위로 소유권을 분리.  
> 유일한 인터페이스 지점(신규 API 2개)은 아래 계약 섹션에서 미리 확정.

---

### 🔴 인터페이스 계약 (작업 전 확정 — 이후 변경 금지)

이동규가 추가하는 신규 엔드포인트 2개의 응답 형태를 미리 고정합니다.  
윤세빈은 이 계약만 보고 coreClient.ts 메서드를 먼저 작성할 수 있습니다.

#### `POST /api/git/commit` (S-8)
```
Request:  { workspace_path: string, message: string, session_id: string }
Response: { status: "ok"|"error", commit_hash: string, message: string }
```

#### `POST /api/deploy/rollback` (S-9)
```
Request:  { plan_id: string }
Response: { status: "ok"|"error", message: string, logs: string[] }
```

---

### 이동규 — `core/` 전담 (Python만 건드림)

**소유 파일: `core/` 디렉터리 전체 (`extension/`는 절대 건드리지 않음)**

| # | 항목 | 대상 파일 | 내용 |
|---|---|---|---|
| S-1 | PyInstaller 패키징 | `core/recoder.spec` 신규, `core/build.sh` 신규 | `recoder-core.exe` 빌드 스크립트. `extension/bin/` 복사 경로만 주석으로 명시 (실제 복사는 빌드 후 수동) |
| S-2 | First Run AWS/Ops Ready 진단 | `core/first_run.py` | AWS 자격증명 존재 여부 체크, ops_ready 미구현 플래그. `diagnostics.json` 항목 추가 |
| S-3 | gitleaks 통합 | `core/quality_runner.py` | `QualityRunner.run_gitleaks()` 메서드 추가. secret 원문 제외, file/line/rule_id만 반환 |
| S-6 | SQLite 세션 영속화 완성 | `core/session_logger.py` | `_init_db()` WAL 모드 + 인덱스, `log_llm_call()` INSERT 완성, `get_daily_cost()` / `get_monthly_cost()` SUM 쿼리 정확화 |
| S-8 | git_agent 이식 | `core/git_agent.py` 신규, `core/server.py` | git_agent.py 신규 작성 + server.py에 `POST /api/git/commit` 엔드포인트 추가 |
| S-9 | Container Rollback 자동화 | `core/local_deploy_agent.py`, `core/server.py` | Health Check 실패 시 `rollback()` 자동 호출 + `POST /api/deploy/rollback` 엔드포인트 추가 |

**추가 규칙**
- `schemas.py`에 새 필드가 필요하면 이동규가 단독으로 수정. 변경 후 윤세빈에게 Dto 형태 슬랙/구두 공유.
- `server.py`는 이동규 단독 소유. 윤세빈은 건드리지 않음.

---

### 🔵 윤세빈 — `extension/` 전담 (TypeScript만 건드림)

**소유 파일: `extension/` 디렉터리 전체 (`core/`는 절대 건드리지 않음)**

| # | 항목 | 대상 파일 | 내용 |
|---|---|---|---|
| S-4 | 다중 파일 PatchProposal UI | `extension/src/ui/sidebarProvider.ts` | `patches` 배열을 파일 탭으로 렌더링. 파일별 체크박스 토글. 승인 시 선택된 파일만 전송 |
| S-5 | 3-Mode UI 마감 | `extension/src/ui/sidebarProvider.ts` | Build→Ship 진행 막대 (analyze→patch→infra→deploy 단계 표시). Operate 탭 2학기 안내 카드 |
| S-7 | Cost UI 데이터 바인딩 | `extension/src/ui/sidebarProvider.ts` | `coreClient.getCost()` 결과를 사이드바 하단 cost 카드에 바인딩. 일/월 누적 표시 |
| S-8 (TS) | git commit 클라이언트 | `extension/src/api/coreClient.ts` | `gitCommit(workspacePath, message, sessionId)` 메서드 추가 (위 계약 기반) |
| S-9 (TS) | deploy rollback 클라이언트 + UI | `extension/src/api/coreClient.ts`, `sidebarProvider.ts` | `deployRollback(planId)` 메서드 추가 + 실패 시 롤백 버튼 UI |
| S-10 | Approval Level 3~4 UI 골격 | `extension/src/ui/sidebarProvider.ts` | Level 3·4 카드 컴포넌트 props 정의 (2학기 대비). 실제 동작 없이 disabled 상태로만 |

**추가 규칙**
- `coreClient.ts`는 윤세빈 단독 소유. 이동규는 건드리지 않음.
- 이동규가 schemas.py 변경 공유 시, 해당 TS 인터페이스(`coreClient.ts` 상단 DTO)만 수정.

---

### ⚪ 공통 (순서 의존 없음 — 각자 완료 후 합쳐서 진행)

| 항목 | 내용 | 시점 |
|---|---|---|
| E2E Docker 실행 | `python core/main.py` 기동 → VSCode Extension 연결 → One Scene 전체 주행 | 이동규 S-8·S-9 + 윤세빈 S-9(TS) 완료 후 |
| README + 데모 스크립트 | 1학기 발표용. 시나리오 흐름 + 캡처 가이드 | E2E 통과 직후 |

---

### 파일 소유권 요약

```
core/                        ← 이동규 전담
  server.py                  ← 이동규만 수정
  git_agent.py               ← 이동규 신규
  local_deploy_agent.py      ← 이동규만 수정
  quality_runner.py          ← 이동규만 수정
  session_logger.py          ← 이동규만 수정
  first_run.py               ← 이동규만 수정
  schemas.py                 ← 이동규 소유 (변경 시 TS DTO 공유)
  recoder.spec / build.sh    ← 이동규 신규

extension/                   ← 윤세빈 전담
  src/api/coreClient.ts      ← 윤세빈만 수정
  src/ui/sidebarProvider.ts  ← 윤세빈만 수정
  package.json               ← 윤세빈만 수정

PROGRESS.md / HANDOFF.md    ← 둘 다 수정 가능 (작업 완료 시 ✅ 표기)
```
5. **6-3 graceful shutdown 보강** — SIGTERM 후 5초 → SIGKILL 폴백, deactivate 단위 테스트
6. **3-11 Approval Level 1~2 UI 마감** — Level 2 명령 미리보기 카드(`docker build ...`, `docker run ...`), 영향 대상·롤백 경로 표시
7. **P0-12 (TS 측) — mocha + @vscode/test-electron 1건** — `coreClient.healthCheck()` mock 서버 1건만이라도 통과
8. **S-4 다중 파일 PatchProposal UI** — 파일 탭, 파일별 승인 토글
9. **S-5 3-Mode UI 마감** — Build → Ship 진행 상태(왼쪽 진행 막대), Operate 탭 2학기 안내 카드 디자인
10. **S-7 Cost UI 데이터 바인딩** — P0-11 완료 후 즉시 결선
11. **S-10 Approval Level 3~4 UI 골격** — 2학기 대비, props만 정의된 컴포넌트

### 공통 (둘 다 끼는 작업)

- **0-9 / P0-12 E2E smoke** — 이동규의 pytest와 윤세빈의 mocha를 합쳐 GitHub Actions에 lint+test 1단 추가 (시연 전 필수)
- **데모 시나리오 스크립트 + README** — 1학기 발표용 (FastAPI 앱 → ModuleNotFoundError → 패치 → Dockerfile → docker run → Health Check OK)
- **HANDOFF.md 갱신** — server↔agent 배선 후 흐름도 갱신, 에이전트 인계 시 혼동 방지

### 권장 진행 순서 (의존 관계)

```
W1: 이동규 P0-1 (3-5/3-6) ─┐
    윤세빈 P0-9 (spawn) ──┼─→ 이동규 P0-3/4 ─→ 이동규 P0-5/6 ─┐
    윤세빈 P0-7 ──────────┘                                     │
W2:                                                               ├─→ 공통 0-9 E2E smoke
    이동규 P0-10/11, S-1                                          │
    윤세빈 P0-8/13, 3-11                                          │
W3:                                                               │
    이동규 P0-12(py) + S-3                                        │
    윤세빈 P0-12(ts) + S-4/S-5                                    │
                                                                   ↓
                                                        🎯 1학기 데모 (One Scene)
```

W1에서 P0-1 + P0-7 + P0-9가 동시 닫혀야 데모 흐름이 처음으로 끝까지 돕니다(가장 중요한 마일스톤).

---

## 업데이트 기록

| 날짜 | 작성자 | 내용 |
|---|---|---|
| 2026-05-07 | Claude (Cowork) | v6.4-final 설계서 기준 초안 작성. 기존 v5 코드베이스 분석 완료. |
| 2026-05-07 | Claude (Cowork) | Phase 0~7 전체 구현 완료. core/ 27개 파일, extension/ 7개 파일 생성. |
| 2026-05-08 | Claude (Cowork) | **실측 감사 반영**: 표면상 ✅이던 server↔agent 배선·UI 응답 파싱·spawn·테스트의 실제 결손 항목을 ⚠/❌로 정정, P0 13개 정리, 두 사람 분담안 신설. |
| 2026-05-08 | Claude (Cowork) | **P0 전항목 완료**: server.py Mock 전면 제거 + 6개 엔드포인트 실배선, coreManager spawn 수정, sidebarProvider 파싱 정상화 + Ready 카드, main.py stale PID kill + attached_pids, pytest 15/15 통과, TS smoke 작성. 1학기 필수 95% 완료. |

---

> **규칙**: 구현 완료 시 해당 행의 상태를 ✅로 변경하고 업데이트 기록에 한 줄 추가한다.
> **규칙**: 블로킹 이슈 발생 시 이슈 섹션에 추가하고 상태를 ❌로 변경한다.
> **규칙**: ⚠ 항목은 "겉보기 완료, 실배선 미완"을 의미합니다. 새 코딩 에이전트는 ⚠/❌ 항목을 우선 살펴보세요.
