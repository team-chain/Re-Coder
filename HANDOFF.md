# ReCoder — Coding Agent Handoff Guide
> **설계서 버전**: v6.4-final (설계 동결)  
> **최종 업데이트**: 2026-05-12 (런타임 버그 수정 + EC2 배포 구현 반영)
> **작성 목적**: 이 파일을 읽는 AI 코딩 에이전트(Claude, Cursor, Cline 등)가 현재 코드베이스 상태와 v6.4 설계서 사이의 간극을 정확히 파악하고, 불필요한 재분석 없이 곧바로 구현에 들어갈 수 있도록 한다.

> **⚠ 다음 에이전트에게**: P0-1~P0-13 전항목이 2026-05-08에 완료되었고, 2026-05-12에 런타임 버그 6종 수정 + EC2 배포(2S-1/2S-3) 구현이 완료되었습니다. `PROGRESS.md`를 먼저 읽고, 남은 작업은 **선택 구현(S-1·S-3·S-6) 및 E2E 실 Docker 테스트**입니다.

---

## 1. 프로젝트 한 줄 요약

ReCoder는 VSCode Extension + PyInstaller Local Core + EC2 Watchdog으로 구성된 AI DevOps 에이전트다.  
타겟: 코드는 짤 수 있지만 Docker/EC2/운영에서 막히는 주니어 백엔드 개발자.  
슬로건: **From Error to Operation** — 에러 수정부터 운영 대응까지, VSCode 안에서 승인 기반으로.

---

## 2. 아키텍처 개요 (v6.4-final)

```
┌─────────────────────────────────────────────────────┐
│  VSCode Extension (TypeScript)                      │
│  - Sidebar Webview (React + Tailwind)               │
│  - TerminalShellIntegration 기반 출력 수집           │
│  - 활성 파일·워크스페이스 컨텍스트 수집              │
│  - Local Core 생명주기 관리 (lazy spawn)             │
│  - Polling 기반 상태 동기화 (3~5초)                  │
└─────────────────┬───────────────────────────────────┘
                  │ HTTP REST (127.0.0.1:17894)
                  │ ~/.recoder/runtime.json에서 포트/토큰 읽기
┌─────────────────▼───────────────────────────────────┐
│  Local Core (Python 3.11, PyInstaller 단일 실행파일) │
│  FastAPI (127.0.0.1 only) + Uvicorn                 │
│  - Context Gate (마스킹 16종)                        │
│  - LLM Provider Router (Bedrock → Gemini 폴백)      │
│  - Orchestrator FSM                                  │
│  - Code Agent / Infra Agent / Deploy Agent           │
│  - Ops Agent (Stage 3, 2학기)                       │
│  - Risk Validator                                    │
│  - CommandTemplate Registry                          │
│  - FileTemplate Registry                             │
│  - Session Logger (SQLite + JSONL)                  │
└─────────────────┬───────────────────────────────────┘
                  │ Docker Engine / SSH / ECR
┌─────────────────▼───────────────────────────────────┐
│  Local Runtime                                       │
│  Docker build/run, git apply/backup/rollback        │
│  Trivy·Hadolint·gitleaks (일회성 컨테이너)          │
└─────────────────────────────────────────────────────┘
                  │ EC2 SSH / 인증 API (2학기)
┌─────────────────▼───────────────────────────────────┐
│  Remote (사용자 AWS 계정)                            │
│  EC2 + ECR + EC2 Watchdog                           │
│  Watchdog: incident.jsonl + Discord Webhook         │
└─────────────────────────────────────────────────────┘
```

**핵심 통신 원칙**
- Extension ↔ Local Core: REST + Polling (WebSocket 미사용)
- 포트/토큰: `~/.recoder/runtime.json`으로 공유
- Local Core는 `127.0.0.1`만 바인딩, `X-Session-Token` 헤더로 보호

---

## 3. 기능 구조 (3-Stage)

| Stage | 이름 | 내용 | 활성화 조건 |
|---|---|---|---|
| Stage 1 | **Build** | 에러 분석 + 코드 패치 | AI Ready |
| Stage 2 | **Ship** | Dockerfile + docker build/run + Health Check | AI Ready + Docker Ready |
| Stage 3 | **Operate** | EC2 incident 조회 + 운영 대응 제안 (2학기) | AI Ready + AWS Deploy Ready + Ops Ready |

---

## 4. 현재 코드베이스 상태 (v6.4 구현 완료 기준)

> 2026-05-08 기준 — **1학기 필수 구현 95% 완료, 데모 가능 상태**

### 완료된 핵심 파일

| 파일 | 위치 | 상태 | 비고 |
|---|---|---|---|
| `server.py` | `core/server.py` | ✅ 완료 | 6개 엔드포인트 실배선. Mock 완전 제거 |
| `main.py` | `core/main.py` | ✅ 완료 | stale PID kill, attached_pids, SIGTERM 처리 |
| `schemas.py` | `core/schemas.py` | ✅ 완료 | v6.4 스키마 전체. 중복 없음 |
| `analyzer.py` | `core/analyzer.py` | ✅ 완료 | AnalyzeRequest 입력, LLM 분석 |
| `code_agent.py` | `core/code_agent.py` | ✅ 완료 | SHA256 검증 + 백업 + diff + rollback |
| `infra_agent.py` | `core/infra_agent.py` | ✅ 완료 | FileTemplate Registry 연동 |
| `local_deploy_agent.py` | `core/local_deploy_agent.py` | ✅ 완료 | docker build/run + Health Check |
| `quality_runner.py` | `core/quality_runner.py` | ✅ 완료 | Trivy + Hadolint + (gitleaks S-3) |
| `session_logger.py` | `core/session_logger.py` | ✅ 완료 | cost 집계, /api/cost 결선 |
| `coreManager.ts` | `extension/src/core/` | ✅ 완료 | spawn exec/args 분리, SIGTERM→SIGKILL |
| `coreClient.ts` | `extension/src/api/` | ✅ 완료 | 모든 엔드포인트 타입 정의 + 호출 메서드 |
| `sidebarProvider.ts` | `extension/src/ui/` | ✅ 완료 | 파싱 정상화, Ready 카드, deploy polling |
| `tests/` | `core/tests/` | ✅ 완료 | pytest 15/15 통과 |
| `coreClient.smoke.ts` | `extension/src/test/` | ✅ 완료 | node:test 기반 TS smoke |

### 남은 작업 (선택 구현 S-1~S-10)

| 항목 | 파일 | 담당 | 우선순위 |
|---|---|---|---|
| PyInstaller 패키징 | `core/build_scripts/` 신규 | 이동규 | S-1 |
| gitleaks 통합 | `core/quality_runner.py` 추가 | 이동규 | S-3 |
| SQLite 세션 영속화 | `core/session_logger.py` 보강 | 이동규 | S-6 |
| git_agent 이식 | `core/git_agent.py` 신규 | 이동규 | S-8 |
| Local Container Rollback | `core/local_deploy_agent.py` 보강 | 이동규 | S-9 |
| 다중 파일 PatchProposal UI | `extension/src/ui/sidebarProvider.ts` | 윤세빈 | S-4 |
| 3-Mode UI 마감 | `extension/src/ui/sidebarProvider.ts` | 윤세빈 | S-5 |
| Cost UI 데이터 바인딩 | `extension/src/ui/sidebarProvider.ts` | 윤세빈 | S-7 |
| Level 3~4 UI 골격 | `extension/src/ui/sidebarProvider.ts` | 윤세빈 | S-10 |
| E2E Docker 테스트 | 로컬 환경 필요 | 공통 | 필수 |

v5 레거시 경로: `Re-Coder/ReCoder/ai-workspace-assistant/agent/` (참조 전용, 수정 금지)

---

## 5. 모듈별 마이그레이션 분류

### ✅ 그대로 이식 (수정 최소)

| 모듈 | 이식 대상 경로 | 주요 내용 | 주의사항 |
|---|---|---|---|
| `context_gate.py` | `core/context_gate.py` | 마스킹 16종 완성, quality_score, GateResult | async 함수로 전환 (FastAPI 루프 블로킹 방지, §18.2) |
| `trigger_detector.py` | `core/trigger_detector.py` | trigger_score 계산, error_fingerprint, 60초 중복 차단 | 기존 로직 그대로 사용 가능 |
| `llm/base.py` | `core/llm/base.py` | LLMProvider, LLMRequest, LLMResponse, LLMError | 변경 없음 |
| `llm/bedrock_provider.py` | `core/llm/bedrock_provider.py` | Bedrock Converse API 호출 | Structured Output 1순위 추가 확인 필요 (§13.1) |
| `llm/gemini_provider.py` | `core/llm/gemini_provider.py` | Gemini Flash 폴백 | 변경 없음 |
| `llm/router.py` | `core/llm/router.py` | Circuit Breaker + 폴백 체인 + 비용 추적 | 기존 로직 그대로 사용 가능 |
| `command_safety.py` | `core/command_safety.py` | 위험 명령 차단 규칙 | 변경 없음, CommandTemplate Registry와 연동 |
| `git_agent.py` | `core/git_agent.py` | 보호 브랜치 차단, git commit/push | 변경 없음 |

---

### 🔧 수정 필요 (구조 유지, 내용 변경)

#### `schemas.py` → `core/schemas.py`
**가장 중요한 파일.** v5 대비 다음을 추가/변경한다.

추가할 스키마:
- `ProjectProfile` — workspace_path, stack, package_manager, default_run_command, default_port, health_check_path, dockerfile_path, compose_path, deployment_target
- `AnalyzeRequest` — workspace_path, active_file_path, selected_text, terminal_output, command, project_files_summary, project_id
- `DeploymentRecord` — deployment_id, project_id, method, image, image_digest, git_commit, container_name, health_check_path, deployed_at, rollback_target, status
- `AlertRecord` — alert_id, source, project_id, environment, host, container_name, alert_type, severity, detected_at, logs_excerpt, health_check_result, metric_snapshot, recent_deployment_id, fingerprint, mask_version
- `ResponseProposal` — schema_version, alert_id, action_type, target_container, command_template_id, parameters, risk_level, risk_reasons, approval_level
- `CommandTemplate` — template_id, action_type, allowed_params, command_pattern, risk_level, approval_level, version
- `FileTemplate` — template_id, file_type, base_content, customizable_sections, version

변경할 스키마:
- `PatchProposal` — `approval_level` 필드 추가 (모든 Proposal 공통)
- `InfraFileProposal` — `approval_level` 필드 추가
- `DeploymentPlan` — `command_template_id` 필드 추가, method에 `local_docker` 추가
- `SessionRecord` — `project_id` 추가, `raw_content_saved` 항상 false 고정
- **중복 제거**: 현재 `schemas.py`에 `SessionError`, `LLMUsageSummary`, `SessionRecord`, `OrchestratorUpdate`가 두 번씩 정의되어 있음 → 반드시 중복 제거

모든 Proposal 공통 필드 (§20.11):
```python
schema_version: str
risk_level: RiskLevel
risk_reasons: list[str]
approval_level: int  # 1~4
```

#### `code_agent.py` → `core/code_agent.py`
- 기존 SHA256 검증 + 백업 + diff 적용 + rollback 로직 유지
- `AnalyzeRequest`를 입력으로 받도록 인터페이스 변경
- `approval_level` 결과를 PatchProposal에 포함
- 다중 파일 PatchProposal은 1학기 선택 구현 (우선 단일 파일)

#### `infra_agent.py` → `core/infra_agent.py`
- 기존 4개 스택 템플릿 (python-fastapi, python-flask, node-express, node-next) 유지
- **FileTemplate Registry와 연동**으로 구조 변경: 템플릿 하드코딩 → Registry 조회 방식
- LLM은 어느 부분을 커스터마이징할지만 제안, 실제 파일 조립은 Registry가 수행
- `InfraFileProposal`에 `approval_level` 추가

#### `deploy_agent.py` → `core/deploy_agent.py`
- 기존 EC2 SSH 배포 로직 유지 (2학기 기능이지만 코드는 이식)
- **Local Docker 배포** (Stage 2 1학기) 로직 추가: `docker build` + `docker run` + Health Check
- **CommandTemplate Registry와 연동**: `docker_build`, `docker_run`, `docker_stop`, `docker_logs` 템플릿 사용
- `DeploymentRecord` 저장 로직 추가

#### `orchestrator.py` → `core/orchestrator.py`
- 기존 FSM 전이표 유지
- 새 상태 추가 고려: `SECURITY_SCANNING` (Trivy/Hadolint 실행 중), `DOCKER_BUILDING`, `HEALTH_CHECKING`
- `WAITING_USER_ACTION` 이후 Stage 2 흐름 연결

#### `analyzer.py` → `core/analyzer.py`
- v5의 화면 분석(PIL, RapidOCR) 관련 코드 전부 제거
- `AnalyzeRequest`를 입력으로 받아 LLM 분석 수행하도록 인터페이스 정리
- 응답 스키마를 v6.4 `_RESPONSE_SCHEMA`에 맞게 업데이트

#### `server.py` → `core/server.py`
- 기존 FastAPI + 보안 미들웨어 구조 유지 (127.0.0.1, X-Session-Token, CORS)
- SSE 제거 → Polling 방식으로 전환 (`GET /api/status` 엔드포인트)
- 새 엔드포인트 추가:
  - `POST /api/analyze` — Stage 1 에러 분석 진입점
  - `POST /api/infra/generate` — Stage 2 Dockerfile 생성
  - `POST /api/deploy/local` — Stage 2 docker build/run
  - `GET /api/project` — ProjectProfile 조회
  - `POST /api/project/scan` — 워크스페이스 스캔
  - `GET /api/health` — Core Ready 확인 (Extension Polling 대상)
  - `GET /api/status` — 현재 Orchestrator 상태 반환
  - `GET /api/cost` — 일일/월별 비용 조회

#### `first_run.py` → `core/first_run.py`
- PowerShell hook 관련 코드 전부 제거 (터미널 수집은 Extension이 담당)
- v6.4 §11 First Run 진단 마법사로 완전 재작성:
  - Core Ready, AI Ready, Docker Ready 순서로 진단
  - 결과를 `~/.recoder/diagnostics.json`에 저장
  - `resolved_model_id`, `resolved_region`, `provider_type`, `validation_time` 포함
- Windows ACL Soft Fail 처리 유지

---

### 🗑 제거 (v6.4에서 사용하지 않음)

| 모듈 | 제거 이유 |
|---|---|
| `monitor.py` | 화면/터미널 감지는 VSCode Extension의 TerminalShellIntegration으로 대체 |
| `capture_agent.py` | 화면 캡처(mss + RapidOCR) 불필요 |
| `widget.py` | PyQt6 floating widget → VSCode Sidebar Webview로 대체 |
| `gui_windows.py` | PyQt6 다이얼로그 → VSCode Webview로 대체 |
| `tray_app.py` | 시스템 트레이 → VSCode Extension 명령으로 대체 |
| `collectors/terminal_output.py` | PowerShell hook → TerminalShellIntegration으로 대체 |
| `collectors/collect.py` | OS 스냅샷 수집 불필요 |
| `collectors/source_context.py` | VSCode Extension이 활성 파일 컨텍스트 수집 |
| `collectors/docker_collector.py` | 대체 가능 (Deploy Agent에 통합) |
| `collectors/k8s_collector.py` | 범위 외 |
| `local_server.py` | server.py(FastAPI)로 통합됨, 레거시 |
| `prompt_generator.py` | 레거시, 미사용 |
| `uploader.py` | S3 업로드 2학기 이후로 연기 |
| `ws_client.py` | WebSocket → Polling으로 전환됨 |
| `dashboard/index.html` | VSCode Sidebar Webview로 대체 |

---

### 🆕 신규 구현 필요

#### Python (Local Core)

| 모듈 | 경로 | 내용 |
|---|---|---|
| CommandTemplate Registry | `core/registries/command_registry.py` | docker_build, docker_run, docker_stop 등 11개 템플릿 관리, 파라미터 검증 |
| FileTemplate Registry | `core/registries/file_registry.py` | Dockerfile 4종, docker-compose, GitHub Actions 템플릿 관리 |
| Risk Validator | `core/risk_validator.py` | Proposal의 risk_level 검증, rollback 가능성 평가 |
| Quality Tools Runner | `core/quality_runner.py` | Trivy/Hadolint/gitleaks 일회성 컨테이너 실행, 결과 필터링 |
| Ops Agent | `core/ops_agent.py` | Stage 3, 2학기. AlertRecord → ResponseProposal 생성 |
| Session Logger | `core/session_logger.py` | SQLite 세션 관리 + JSONL 로그 |
| Project Scanner | `core/project_scanner.py` | 워크스페이스 스캔 → ProjectProfile 생성 |
| Local Deploy Agent | `core/local_deploy_agent.py` | Stage 2 docker build/run + Health Check (기존 deploy_agent와 분리) |
| Entry Point | `core/main.py` | PyInstaller 엔트리포인트, lazy spawn 지원, core.lock 관리, runtime.json 저장 |

#### TypeScript (VSCode Extension)

| 모듈 | 경로 | 내용 |
|---|---|---|
| Extension Entry | `extension/src/extension.ts` | activate/deactivate, Local Core lazy spawn |
| Core Manager | `extension/src/core/coreManager.ts` | singleton 관리, lock file, health check, port fallback |
| Terminal Collector | `extension/src/collectors/terminalCollector.ts` | TerminalShellIntegration API, recoder run fallback |
| Context Collector | `extension/src/collectors/contextCollector.ts` | 활성 파일, 워크스페이스 컨텍스트 수집 |
| Sidebar Provider | `extension/src/ui/sidebarProvider.ts` | Webview 관리, 메시지 브릿지 |
| Webview App | `extension/src/webview/App.tsx` | React + Tailwind, 3-Mode 탭 UI |
| API Client | `extension/src/api/coreClient.ts` | Local Core REST 통신, Polling 루프 |
| Approval UI | `extension/src/ui/approvalPanel.ts` | Approval Level 1~4 UI |

---

## 6. 핵심 데이터 계약 요약 (v6.4)

```python
# 공통 필드 (모든 Proposal)
schema_version: str       # "6.4"
risk_level: RiskLevel     # low | medium | high
risk_reasons: list[str]
approval_level: int       # 1~4

# Approval Level 정의
# Level 1: 로컬 파일 생성·수정 (단순 승인 버튼)
# Level 2: 로컬 명령 실행 (명령 미리보기 + 승인)
# Level 3: 원격 인프라 변경 (영향 대상, 명령, 롤백 경로, 리스크)
# Level 4: 민감 설정 변경 (diff, 범위, 롤백, 추가 타이핑 확인)
```

---

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

## 10. 현재 마일스톤 상태 (2026-05-12 기준)

**✅ 완료 — Extension ↔ Local Core 전체 배선 + 런타임 버그 수정 + EC2 배포**

- `POST /api/analyze` → `analyzer.analyze` + `code_agent.generate_patch` 체인 실동작
- `POST /api/patch/approve` → `code_agent.apply_patch` 실적용 + rollback 보장
- `POST /api/infra/generate` → `infra_agent.generate` + hadolint 품질 검사
- `POST /api/deploy/local` → `LocalDeployAgent.deploy()` background task + `/api/deploy/status` polling
- `POST /api/security/scan` → Trivy + Hadolint 결과 반환
- `POST /api/git/push` → GitHub 토큰 없을 때 `git_agent.push()` 폴백
- `POST /api/deploy/ec2` → ECR push + SSH docker deploy 파이프라인 (신규)
- `GET /api/deploy/ec2/status` → EC2 배포 상태 polling (신규)
- Bedrock 모델: `us.anthropic.claude-sonnet-4-6` (Primary) 등 ACTIVE 모델로 교체
- 스택 감지: 부모 폴더 오픈 시 하위 폴더 자동 탐색
- pytest 15/15 통과

**2026-05-12 수정 사항 요약**

| 파일 | 변경 내용 |
|---|---|
| `core/server.py` | git push 폴백 + EC2 배포 엔드포인트 3개 추가 |
| `core/local_deploy_agent.py` | rollback_latest() 추가, enum 역직렬화 수정, ports/env 누락 수정 |
| `core/infra_agent.py` | GitHub Actions npm ci→npm install 수정, 스택 감지 하위 폴더 탐색 추가 |
| `core/llm/bedrock_provider.py` | Bedrock 모델 ID를 ACTIVE inference profile로 교체 |
| `core/deploy_agent.py` | EC2 배포 파이프라인 신규 구현 (ECR + SSH) |
| `extension/src/api/coreClient.ts` | EC2 배포 API 메서드 3개 추가 |
| `extension/src/ui/sidebarProvider.ts` | EC2 배포 UI 및 메시지 핸들러 추가 |

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
