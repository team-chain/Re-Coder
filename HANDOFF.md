# ReCoder Enterprise — AI 에이전트 인계 문서

> **설계서**: Enterprise Final v5.0 (1년 로드맵)
> **최종 업데이트**: 2026-05-16
> **목적**: 다음 AI 에이전트가 재분석 없이 곧바로 이어서 구현할 수 있도록 현재 상태를 정확히 기술한다.

---

## 1. 지금 어디까지 됐나

### 완료된 것

**Extension 안정화 (fix/extension-terminal-api 브랜치, 2026-05-16)**
- `terminalDataWriteEvent` proposed API 실제 호출 완전 제거 (주석만 남음)
- `onDidChangeTerminalShellIntegration` try/catch 가드 추가 (VSCode <1.93 대응)
- `extension.ts` 잘린 `getDefaultRunCommand` 함수 복원
- TypeScript 재컴파일 완료 (Exit: 0, proposed API 호출 없음 확인)
- `extension/media/` 아이콘 파일 생성 (recoder-icon.svg, icon.png)

**Q1 Must-Core 1차 (같은 브랜치)**
- `core/chunker/ast_chunker.py`: ASTChunker 독립 모듈
- `core/planner.py`: PlannerAgent (LLM → ExecutionPlan 최대 5단계)
- `core/executor.py`: Executor 결정론적 디스패처
- `core/verifier.py`: VerifierAgent (LLM 없음)
- `core/plan_execute_verify.py`: PEV 파이프라인 조율자
- `core/eval/`: EvalHarness + SafetyChecker + 6카테고리 19케이스
- `core/schemas.py`: Q1 스키마 11개 추가

### 남은 것 (우선순위 순)

1. **Q1 DoD 달성** — LLM 실제 연동 후 Eval Harness 실행, pass_rate ≥ 60% 확인
2. **PyInstaller 빌드 자동화** — Windows x64, Linux x64
3. **Q2-A 시작** — OIDC + Device Token (ADR-006 타임박스 엄수)

---

## 2. 코드베이스 구조

```
Re-Coder/
├── core/                          # Python FastAPI Local Core
│   ├── main.py                    # 진입점, singleton lock, 포트 바인딩
│   ├── schemas.py                 # 모든 Pydantic 스키마 (Q1 스키마 추가됨)
│   ├── orchestrator.py            # FSM (IDLE→ANALYZING→AWAITING_APPROVAL 등)
│   ├── context_gate.py            # 16종 PII/시크릿 마스킹
│   ├── risk_validator.py          # 패치 위험도 평가
│   ├── planner.py                 # [Q1 신규] PlannerAgent
│   ├── executor.py                # [Q1 신규] Executor
│   ├── verifier.py                # [Q1 신규] VerifierAgent
│   ├── plan_execute_verify.py     # [Q1 신규] PEV 파이프라인
│   ├── chunker/                   # [Q1 신규] AST Chunker
│   │   ├── __init__.py
│   │   └── ast_chunker.py         # ASTChunker, Python AST + JS line-based
│   ├── eval/                      # [Q1 신규] Eval Harness
│   │   ├── __init__.py
│   │   ├── harness.py             # EvalHarness
│   │   ├── safety.py              # SafetyChecker
│   │   └── cases/                 # JSON 테스트 케이스 (19개)
│   ├── agents/
│   │   ├── code_agent.py          # 에러 분석 + PatchProposal
│   │   ├── infra_agent.py         # Dockerfile 생성
│   │   ├── deploy_agent.py        # Docker/EC2 배포
│   │   └── ops_agent.py           # 운영 진단
│   ├── api/routes/                # FastAPI 라우터
│   │   ├── analyze.py             # POST /api/analyze
│   │   ├── deploy.py              # POST /api/deploy/*
│   │   ├── health.py              # GET /api/health, /api/status
│   │   ├── ops.py                 # 운영 관련
│   │   └── session.py             # 세션 관리
│   ├── llm/                       # LLM 프로바이더
│   │   ├── provider_router.py     # Circuit Breaker + 비용 추적
│   │   ├── bedrock_provider.py    # AWS Bedrock (1순위)
│   │   └── gemini_provider.py     # Google Gemini (폴백)
│   └── registry/                  # CommandTemplate + FileTemplate
│
├── extension/                     # VSCode Extension (TypeScript)
│   ├── src/
│   │   ├── extension.ts           # 진입점 (try/catch 가드 적용됨)
│   │   ├── core/
│   │   │   ├── CoreManager.ts     # lazy spawn, runtime.json 읽기
│   │   │   ├── ApiClient.ts       # Core HTTP 클라이언트
│   │   │   └── PollingService.ts  # 3~5초 폴링
│   │   ├── sidebar/
│   │   │   └── SidebarProvider.ts # Webview 관리
│   │   ├── terminal/
│   │   │   └── TerminalCollector.ts # Shell Integration 수집
│   │   └── types.ts
│   ├── out/                       # 컴파일된 JS (tsc 출력)
│   └── media/                     # 아이콘 (recoder-icon.svg, icon.png)
│
├── PROGRESS.md                    # 분기별 진척률 트래커
├── HANDOFF.md                     # 이 파일
└── README.md                      # 프로젝트 소개
```

---

## 3. 핵심 설계 원칙 (설계서 v5.0)

### 쐐기 시나리오 7단계 (Q4 최종 목표)
1. FastAPI 프로덕션 서비스에서 컨테이너 비정상 종료
2. OTel 데이터로 자동 감지
3. Incident Timeline과 RCA 생성
4. rollback PR 자동 생성
5. 2인 승인
6. ArgoCD 적용
7. Postmortem skeleton 자동 생성

### ADR 핵심 요약
| ADR | 결정 |
|---|---|
| ADR-003 | OPA = REST server 방식 (Go 라이브러리 임베딩 금지) |
| ADR-004 | raw source code Control Plane 업로드 금지. embedding + metadata만 |
| ADR-005 | production rollback = Git revert PR 기본 |
| ADR-006 | 인증 직접 구현 + 14일 체크포인트. 21일 초과 시 BaaS 피봇 (협상 불가) |
| ADR-007 | Q1 Node.js = line-based fallback (tree-sitter 미사용) |
| ADR-009 | Final Demo = 실제 EKS (k3d/kind 타협 금지) |

### Non-goals (1년 내 구현 안 함)
Terraform apply 자동화, Datadog 대체, K8s 클러스터 직접 생성, LLM 직접 shell 실행, raw source code 업로드, 무승인 프로덕션 배포, Azure/GCP, Plugin Marketplace, On-premise LLM, Self-hosted Control Plane (Q4 이후)

---

## 4. Q1 신규 모듈 사용법

### ASTChunker
```python
from chunker import ASTChunker

chunker = ASTChunker(workspace_path="/path/to/project")
index = chunker.build_index()          # 전체 인덱스 빌드 (<3초 DoD)
chunks = chunker.chunk_file("/path/to/app.py")  # 단일 파일

# 쿼리 시점에 source 재읽기 (ContextGate 통과 후)
source = chunker.read_chunk_source(chunks[0])
```

### PEV 파이프라인
```python
from plan_execute_verify import PlanExecuteVerifyPipeline
from schemas import AnalyzeRequest

pipeline = PlanExecuteVerifyPipeline(provider_router, context_gate)
result = await pipeline.run(request)
# result.proposal       : PatchProposal | None
# result.verification   : VerificationResult | None
# result.needs_approval : bool
# result.needs_manual_review : bool (재시도 2회 소진 시 True)
```

### Eval Harness
```python
from eval import EvalHarness

harness = EvalHarness(pipeline=pipeline, cases_dir="eval/cases")
report = await harness.run_all()
assert report.ci_gate_passed  # safety_violations==0 AND pass_rate>=0.6
```

---

## 5. 환경 변수 (.env)

```
# LLM
BEDROCK_PRIMARY_MODEL=us.anthropic.claude-sonnet-4-6
BEDROCK_FALLBACK_MODEL=us.anthropic.claude-haiku-4-5
AWS_REGION=us-east-1
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...

# 개발
DEV_MODE=1
```

---

## 6. 브랜치 상태

| 브랜치 | 내용 |
|---|---|
| `develop` | 기준 브랜치 |
| `fix/extension-terminal-api` | Extension 수정 + Q1 구현 완료 (PR 생성 권장) |

---

## 7. 다음 에이전트가 해야 할 것

### 즉시 (Q1 DoD 완성)
1. LLM 연동 후 `EvalHarness.run_all()` 실행
2. 카테고리별 pass_rate ≥ 60% 확인
3. Safety violation 0건 확인
4. PyInstaller 빌드 스크립트 작성 (`core/recoder.spec`)

### 그 다음 (Q2-A)
- ADR-006 타임박스 시작 즉시 날짜 기록
- D+14 체크포인트: Device Token Keychain 저장 + heartbeat 동작 여부
- D+21 넘기면 무조건 BaaS(Auth0 또는 Supabase) 피봇
