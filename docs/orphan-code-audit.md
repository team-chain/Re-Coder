# 고아 코드 감사 (자동 생성)
생성: `python3 scripts/orphan_audit.py` · 기준 커밋 시점의 core 149개 모듈 중 엔트리포인트(main + api/*)에서 **닿지 않는 모듈 51개, 15,624줄**.
한계: 정적 import 만 본다 — 문자열 기반 동적 import, 외부에서 직접 실행하는 스크립트성 모듈은 아래 참조 열로만 잡힌다. 삭제 전 반드시 전체 테스트를 돌릴 것.

## 판정 요약
| 분류 | 기준 | 처리 |
|---|---|---|
| T1 삭제 후보 | 어디서도 import 되지 않음 (클러스터째 죽어 있음) | 팀 확인 후 클러스터 단위 삭제 |
| T2 테스트만 | core/tests 에서만 import | 기능을 살릴 게 아니면 테스트와 함께 삭제 |
| T3 배선 후보 | 설계서 기능으로 보이는 에이전트/저장소 | 지울지 연결할지 **팀 결정** — 결정 전 삭제 금지 |

## 클러스터 (삭제 단위)
고아끼리 서로 import 하는 묶음이다. 지우려면 묶음째 지워야 한다.

### 클러스터 1 — 2,377줄 · T3 포함 — 팀 결정 필요
| 모듈 | 줄 | 분류 | 외부 참조 | 첫 docstring |
|---|---|---|---|---|
| `agents.code_agent` | 573 | T3 | — | ReCoder Code Agent — Error analysis and PatchProposal generation. |
| `executor` | 313 | T3 | — | ReCoder Q1 — Executor (결정론적 디스패처) |
| `plan_execute_verify` | 178 | T3 | — | ReCoder Q1 — Plan-Execute-Verify 파이프라인 조율자 |
| `planner` | 233 | T3 | — | ReCoder Q1 — PlannerAgent |
| `risk_validator` | 870 | T3 | — | ReCoder Core — Risk Validator (v6.4 §17) |
| `verifier` | 210 | T3 | — | ReCoder Q1 — VerifierAgent |

### 클러스터 2 — 876줄 · T3 포함 — 팀 결정 필요
| 모듈 | 줄 | 분류 | 외부 참조 | 첫 docstring |
|---|---|---|---|---|
| `incident_timeline` | 259 | T3 | — | incident_timeline.py — Incident Timeline MVP 빌더 (설계서 §Q4 Must-Wedge). |
| `observability.otel_query_service` | 149 | T1 | — | otel_query_service.py — Incident Timeline / RCA 가 호출하는 통합 관측성 API. |
| `replay.timeline_builder` | 468 | T1 | — | core/replay/timeline_builder.py — Deploy Replay 타임라인 빌더 (설계서 §38) |

### 클러스터 3 — 736줄 · T3 포함 — 팀 결정 필요
| 모듈 | 줄 | 분류 | 외부 참조 | 첫 docstring |
|---|---|---|---|---|
| `command_safety` | 189 | T1 | — | Command Safety Layer (설계서 v5 §10.4) |
| `local_deploy_agent` | 547 | T3 | — | Local Deploy Agent — Stage 2 로컬 Docker 배포 (설계서 §9.2, §14, §17). |

### 클러스터 4 — 312줄 · 테스트 정리 동반
| 모듈 | 줄 | 분류 | 외부 참조 | 첫 docstring |
|---|---|---|---|---|
| `cv.triggers` | 141 | T1 | — | CV 자동 rollback 트리거 평가 — 순수 함수 (§34). |
| `persistence.ledger_store` | 171 | T2 | core/tests/unit/test_persistence.py | Layer 3 — DeploymentLedger CRUD (§33.3). |

### 클러스터 5 — 438줄 · T1 — 삭제 후보
| 모듈 | 줄 | 분류 | 외부 참조 | 첫 docstring |
|---|---|---|---|---|
| `eval.harness` | 287 | T1 | — | ReCoder Q1 — Eval Harness |
| `eval.safety` | 151 | T1 | — | ReCoder Q1 — Safety Checker |

### 클러스터 6 — 979줄 · 테스트 정리 동반
| 모듈 | 줄 | 분류 | 외부 참조 | 첫 docstring |
|---|---|---|---|---|
| `aws_calls` | 979 | T2 | core/tests/test_aws_policy.py | aws_calls.py — ReCoder 가 실제로 호출하는 AWS API 를 찾아내는 두 가지 방법 (FR-04-02). |

### 클러스터 7 — 69줄 · T1 — 삭제 후보
| 모듈 | 줄 | 분류 | 외부 참조 | 첫 docstring |
|---|---|---|---|---|
| `check_ai` | 69 | T1 | — | AI 불이 왜 안 켜지는지 진단. Core 와 같은 자격증명/리전으로 Bedrock 을 직접 찔러본다. |

### 클러스터 8 — 480줄 · T1 — 삭제 후보
| 모듈 | 줄 | 분류 | 외부 참조 | 첫 docstring |
|---|---|---|---|---|
| `chunker.ast_chunker` | 480 | T1 | — | ReCoder Q1 — AST-based Code Chunker |

### 클러스터 9 — 358줄 · 테스트 정리 동반
| 모듈 | 줄 | 분류 | 외부 참조 | 첫 docstring |
|---|---|---|---|---|
| `cv.monitor` | 358 | T2 | core/tests/unit/test_cv.py | Continuous Verification Monitor (§34). |

### 클러스터 10 — 91줄 · T1 — 삭제 후보
| 모듈 | 줄 | 분류 | 외부 참조 | 첫 docstring |
|---|---|---|---|---|
| `eval.v10.__main__` | 91 | T1 | — | CLI 진입점. |

### 클러스터 11 — 32줄 · T1 — 삭제 후보
| 모듈 | 줄 | 분류 | 외부 참조 | 첫 docstring |
|---|---|---|---|---|
| `eval.v10.categories` | 32 | T1 | — | v10 Eval — 6 evaluation categories (§38). |

### 클러스터 12 — 96줄 · T1 — 삭제 후보
| 모듈 | 줄 | 분류 | 외부 참조 | 첫 docstring |
|---|---|---|---|---|
| `eval.v10.gate` | 96 | T1 | — | CI Safety Gate (§38, §44). |

### 클러스터 13 — 702줄 · T1 — 삭제 후보
| 모듈 | 줄 | 분류 | 외부 참조 | 첫 docstring |
|---|---|---|---|---|
| `eval.v10.runner` | 702 | T1 | — | v10 Backbone Eval Runner (§38). |

### 클러스터 14 — 397줄 · T1 — 삭제 후보
| 모듈 | 줄 | 분류 | 외부 참조 | 첫 docstring |
|---|---|---|---|---|
| `forecast.deploy_forecast` | 397 | T1 | — | core/forecast/deploy_forecast.py — Deploy Forecast (배포 일기예보) (설계서 §41) |

### 클러스터 15 — 523줄 · T3 포함 — 팀 결정 필요
| 모듈 | 줄 | 분류 | 외부 참조 | 첫 docstring |
|---|---|---|---|---|
| `gitops_agent` | 523 | T3 | — | gitops_agent.py — GitOps ArgoCD 연동 에이전트 (설계서 §Q4 Must-Wedge) |

### 클러스터 16 — 362줄 · T3 포함 — 팀 결정 필요
| 모듈 | 줄 | 분류 | 외부 참조 | 첫 docstring |
|---|---|---|---|---|
| `incident_correlator` | 362 | T3 | — | incident_correlator.py — Incident ↔ Deployment 상관관계 계산 (설계서 §Q4). |

### 클러스터 17 — 164줄 · T1 — 삭제 후보
| 모듈 | 줄 | 분류 | 외부 참조 | 첫 docstring |
|---|---|---|---|---|
| `incident_memory.fingerprint` | 164 | T1 | — | Incident Fingerprint — 결정론적 사고 시그니처 (§35.1). |

### 클러스터 18 — 112줄 · T1 — 삭제 후보
| 모듈 | 줄 | 분류 | 외부 참조 | 첫 docstring |
|---|---|---|---|---|
| `incident_memory.learner` | 112 | T1 | — | IncidentMemory Learner (§35.2). |

### 클러스터 19 — 90줄 · T1 — 삭제 후보
| 모듈 | 줄 | 분류 | 외부 참조 | 첫 docstring |
|---|---|---|---|---|
| `incident_memory.matcher` | 90 | T1 | — | IncidentMemory Matcher (§35.2). |

### 클러스터 20 — 295줄 · T3 포함 — 팀 결정 필요
| 모듈 | 줄 | 분류 | 외부 참조 | 첫 docstring |
|---|---|---|---|---|
| `mcp_server` | 295 | T3 | — | mcp_server.py — MCP 서버화 (설계서 §Q4 Must — local stdio PoC). |

### 클러스터 21 — 232줄 · T1 — 삭제 후보
| 모듈 | 줄 | 분류 | 외부 참조 | 첫 docstring |
|---|---|---|---|---|
| `observability._legacy_manager` | 232 | T1 | — | core.observability._legacy_manager — 패키지화 호환 어댑터. |

### 클러스터 22 — 201줄 · T1 — 삭제 후보
| 모듈 | 줄 | 분류 | 외부 참조 | 첫 docstring |
|---|---|---|---|---|
| `observability.loki_adapter` | 201 | T1 | — | loki_adapter.py — LogQL 로그 쿼리 어댑터 (설계서 §Q4 ObservabilityAdapter). |

### 클러스터 23 — 176줄 · T1 — 삭제 후보
| 모듈 | 줄 | 분류 | 외부 참조 | 첫 docstring |
|---|---|---|---|---|
| `observability.prometheus_adapter` | 176 | T1 | — | prometheus_adapter.py — PromQL 메트릭 쿼리 어댑터 (설계서 §Q4 ObservabilityAdapter). |

### 클러스터 24 — 189줄 · T1 — 삭제 후보
| 모듈 | 줄 | 분류 | 외부 참조 | 첫 docstring |
|---|---|---|---|---|
| `persistence.db` | 189 | T1 | — | SQLite connection manager for ReCoder 3-Layer persistence (§33). |

### 클러스터 25 — 108줄 · 테스트 정리 동반
| 모듈 | 줄 | 분류 | 외부 참조 | 첫 docstring |
|---|---|---|---|---|
| `persistence.preflight_store` | 108 | T2 | core/tests/unit/test_persistence.py | Layer 1 — PreflightRun CRUD (§33.1). |

### 클러스터 26 — 109줄 · 테스트 정리 동반
| 모듈 | 줄 | 분류 | 외부 참조 | 첫 docstring |
|---|---|---|---|---|
| `persistence.remediation_store` | 109 | T2 | core/tests/unit/test_persistence.py | Layer 2 — RemediationRun CRUD (§33.2). |

### 클러스터 27 — 439줄 · T3 포함 — 팀 결정 필요
| 모듈 | 줄 | 분류 | 외부 참조 | 첫 docstring |
|---|---|---|---|---|
| `postmortem_agent` | 439 | T3 | — | postmortem_agent.py — Postmortem skeleton 자동 생성 에이전트 (설계서 §Q4 Must-Wedge) |

### 클러스터 28 — 470줄 · T3 포함 — 팀 결정 필요
| 모듈 | 줄 | 분류 | 외부 참조 | 첫 docstring |
|---|---|---|---|---|
| `quality_runner` | 470 | T3 | — | Quality Runner — Trivy·Hadolint·gitleaks 보안 스캔 (Stage 2). |

### 클러스터 29 — 401줄 · T3 포함 — 팀 결정 필요
| 모듈 | 줄 | 분류 | 외부 참조 | 첫 docstring |
|---|---|---|---|---|
| `rca_agent` | 401 | T3 | — | rca_agent.py — RCA MVP (설계서 §Q4 RCA MVP 성공 기준). |

### 클러스터 30 — 25줄 · 테스트 정리 동반
| 모듈 | 줄 | 분류 | 외부 참조 | 첫 docstring |
|---|---|---|---|---|
| `relay` | 25 | T2 | core/tests/test_relay_analyze.py<br>core/tests/test_analyze_flow.py | core.relay — Hybrid Cloud Relay (설계서 §6.4.2 흐름 1) |

### 클러스터 31 — 328줄 · T1 — 삭제 후보
| 모듈 | 줄 | 분류 | 외부 참조 | 첫 docstring |
|---|---|---|---|---|
| `remediation.applier` | 328 | T1 | — | RemediationProposal Applier (§32.3). |

### 클러스터 32 — 67줄 · 테스트 정리 동반
| 모듈 | 줄 | 분류 | 외부 참조 | 첫 docstring |
|---|---|---|---|---|
| `remediation.fingerprint` | 67 | T2 | core/tests/unit/test_remediation.py | Deterministic fingerprint for RemediationProposal (§32.2). |

### 클러스터 33 — 683줄 · 테스트 정리 동반
| 모듈 | 줄 | 분류 | 외부 참조 | 첫 docstring |
|---|---|---|---|---|
| `remediation.generator` | 683 | T2 | core/tests/unit/test_remediation.py | RemediationProposal Generator (§32). |

### 클러스터 34 — 263줄 · 테스트 정리 동반
| 모듈 | 줄 | 분류 | 외부 참조 | 첫 docstring |
|---|---|---|---|---|
| `remediation.registry` | 263 | T2 | core/tests/unit/test_remediation.py | FileTemplate / CommandTemplate Registry (§32, §22). |

### 클러스터 35 — 692줄 · T3 포함 — 팀 결정 필요
| 모듈 | 줄 | 분류 | 외부 참조 | 첫 docstring |
|---|---|---|---|---|
| `rollback_policy` | 692 | T3 | — | ReCoder Core — Rollback Policy (설계서 §17) |

### 클러스터 36 — 518줄 · T3 포함 — 팀 결정 필요
| 모듈 | 줄 | 분류 | 외부 참조 | 첫 docstring |
|---|---|---|---|---|
| `rollback_pr_agent` | 518 | T3 | — | core/rollback_pr_agent.py — **Helm-values flow** rollback PR 생성기 (ADR-005) |

### 클러스터 37 — 257줄 · T3 포함 — 팀 결정 필요
| 모듈 | 줄 | 분류 | 외부 참조 | 첫 docstring |
|---|---|---|---|---|
| `sbom_agent` | 257 | T3 | — | sbom_agent.py — SBOM 공급망 보안 에이전트 (설계서 §Q3 Must) |

### 클러스터 38 — 357줄 · T1 — 삭제 후보
| 모듈 | 줄 | 분류 | 외부 참조 | 첫 docstring |
|---|---|---|---|---|
| `standup.generator` | 357 | T1 | — | core/standup/generator.py — Daily Standup 매일 아침 운영 브리핑 생성기 (설계서 §39) |

### 클러스터 39 — 114줄 · T1 — 삭제 후보
| 모듈 | 줄 | 분류 | 외부 참조 | 첫 docstring |
|---|---|---|---|---|
| `switch_aws` | 114 | T1 | — | ReCoder — AWS 계정 교체 도구. |

### 클러스터 40 — 17줄 · T1 — 삭제 후보
| 모듈 | 줄 | 분류 | 외부 참조 | 첫 docstring |
|---|---|---|---|---|
| `test_models` | 17 | T1 | — | (docstring 없음) |

### 클러스터 41 — 489줄 · T1 — 삭제 후보
| 모듈 | 줄 | 분류 | 외부 참조 | 첫 docstring |
|---|---|---|---|---|
| `visual_diff.infra_differ` | 489 | T1 | — | core/visual_diff/infra_differ.py — Visual Diff for Infrastructure (설계서 §42) |

## 다음 단계 (제안)
1. T1 클러스터: 이 문서 리뷰에서 이견 없으면 클러스터 단위 삭제 PR — 삭제 후 전체 테스트로 검증.
2. T2: 대응 테스트 파일과 함께 지우거나, 살릴 기능이면 T3 로 옮겨 결정.
3. T3: 카드별로 「연결(회차 배정)」 또는 「삭제(설계서에 미구현 명시)」 결정 — 보드 이슈 카드의 DoD.
