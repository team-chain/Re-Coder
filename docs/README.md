# ReCoder 문서

## 먼저 읽을 문서

| 문서 | 내용 |
|---|---|
| [모델별 비용 · 시간 측정](model-benchmark.md) | 모드(품질 우선 · 균형 · 비용 절약)와 모델 조합별 비용 · 시간, 측정 방법 |
| [하네스](harness.md) | 서비스 · DB가 여럿인 앱을 나눠 만들고, 함께 띄워 검증하고, 고치는 방법 |
| [RAG](rag.md) | 빌드 · 배포 오류를 공식 문서 근거로 고치는 방법과 실험 결과 |

## 폴더

| 폴더 | 내용 |
|---|---|
| [design/](design/) | 설계서, API 명세, 기능별 설계 (배포 캔버스, 하네스 상세, RAG 상세, 실행 정책) |
| [guides/](guides/) | 설치 · 권한 · 릴리스 · 데모 진행 안내, 코드 컨벤션 |
| [verification/](verification/) | 검증 기록 (수용 기준, 보안 감사, 레드팀, 화면 회귀) |
| [releases/](releases/) | 버전별 릴리스 노트와 출시 점검 |
| [planning/](planning/) | 계획 · 분석 문서 (갭 리포트, 배포 계획, 스프린트 운영) |
| [adr/](adr/) | 설계 결정 기록 |
| [presentations/](presentations/) | 발표 자료 (중간 · 기말 · 최종) |
| [images/](images/) | README와 문서에 쓰는 이미지 |

## design

| 문서 | 내용 |
|---|---|
| [구현 설계서 v2](design/ReCoder_구현설계서_v2.md) | 컴포넌트 · 파이프라인 · API · 보안 · 검증 · 한계 (2.0.36 기준) |
| [구현 설계서 v1](design/ReCoder_구현설계서_v1.md) | 1학기 MVP 기준 |
| [설계 확정서 (ADR 20)](design/ReCoder_설계_확정서_ADR.md) | AI-DLC로 확정한 설계 결정 20개 |
| [AI-DLC 도입 설계서](design/4_ReCoder_AI-DLC_도입_설계서.md) | AI-DLC 도입 배경과 설계 |
| [API 명세](design/API_v10.md) | Local Core REST API |
| [엔터프라이즈 하네스](design/enterprise-harness.md) | 하네스 상세 (흐름 · 통합 테스트 규칙 · 검증 기록) |
| [문서 근거 수정](design/document-grounded-repair.md) | RAG 상세 (색인 · 검색 · 재순위화 · 운영) |
| [배포 캔버스](design/deployment-canvas.md) | 배포 화면 설계 |
| [실행 정책](design/execution-policy.md) | 실행 직전 정책 통제 (OPA) |
| [코드 삭제 후보 경고](design/code-removal-warning.md) | 생성 결과의 삭제 후보 경고 |
| [Core 실행 로그](design/core-logging.md) | Core 로그 구조 |
| [UI 프로토타입](design/recoder_ui_prototype.html) | 초기 화면 프로토타입 |

## guides

| 문서 | 내용 |
|---|---|
| [학생 설치 가이드](guides/STUDENT_GUIDE.md) | 설치 요약 |
| [AWS 최소 권한](guides/aws-minimum-permissions.md) | 배포에 필요한 IAM 권한 |
| [관리형 실행 AWS 권한](guides/aws-permissions/README.md) | 관리형 실행 권한 적용 절차 |
| [릴리스 런북](guides/release-runbook.md) | 실제 AWS 검증 · 운영 · 장애 대응 |
| [데모 시나리오](guides/demo-scenario.md) | AI-DLC 데모 진행 |
| [코드 컨벤션](guides/CONVENTIONS.md) | 코드 · 브랜치 · PR 규칙 |

## verification

| 문서 | 내용 |
|---|---|
| [생성 · 배포 · 보안 검증 범위](verification/production-readiness.md) | 실제 Docker · AWS 검증과 남은 제한 |
| [MVP 수용 기준](verification/mvp-acceptance-checklist.md) | 수용 기준 체크리스트 |
| [의존성 · 최소권한 감사](verification/security-audit.md) | 의존성 취약점과 권한 감사 |
| [레드팀 검증 (2026-11-11)](verification/redteam-release-candidate-20261111.md) | PentAGI 레드팀 변경과 검증 |
| [레드팀 출시 검토 (2026-11-13)](verification/redteam-staging-review-20261113.md) | staging 레드팀 출시 검토 |
| [보안 통합 검증 (2026-11-09)](verification/security-integration-verification-20261109.txt) | 보안 기능 통합 검증 |
| [보안 실환경 후속 검증 (2026-11-10)](verification/security-live-followup-20261110.txt) | 보안 기능 실환경 검증 |
| [배포 캔버스 검증](verification/deployment-canvas-verification.md) | 배포 캔버스 검증 기록 |
| [회귀 검증과 배포 정리](verification/release-cleanup-verification.md) | 배포 캔버스 개선 후 회귀 검증 |
| [Workspace UI 검증](verification/workspace-ui-verification.md) | 화면 개선 검증 |
| [고아 코드 감사](verification/orphan-code-audit.md) | 쓰이지 않는 코드 감사 (자동 생성) |

## releases

| 문서 | 내용 |
|---|---|
| [4.0.0 출시 준비 점검](releases/release-readiness-3.1.md) | staging 출시 점검 |
| [관리형 실행 출시 검증](releases/managed-execution-release.md) | 관리형 실행 구현과 검증 현황 |
| [보안 기능 개선 (3.0.8)](releases/security-preserved-flow-release.md) | 기존 흐름을 유지한 보안 개선 |
| [Security 통합 화면](releases/security-rollout.md) | Security 화면과 PentAGI 연동 |
| 1.1.x 릴리스 노트 | [1.1.0](releases/1.1.0-windows.md) ~ [1.1.10](releases/1.1.10-windows.md) |
