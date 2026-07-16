# ReCoder 프로덕션 분석 · 갭 리포트

> **문서 1/4** · 확정된 설계(설계 확정서 ADR 20)를 기준으로 현행 코드베이스를 분석하고, 프로덕션 MVP까지의 갭을 우선순위로 정리한다.
> 기준: VSCode 확장 중심 · 전부 BYO · 개발→검사→배포→감시→롤백 파이프라인 · ~2026-09-11

---

## 0. 반영한 설계 결정 (ADR 요약)

이 문서의 분석 범위와 갭 판정은 아래 확정 결정을 전제로 한다. (전문: `docs/ReCoder_설계_확정서_ADR.md`)

| ADR | 결정 | 이 문서에 준 영향 |
|---|---|---|
| D2 | VSCode 확장 중심 | 봇·조직관리 서버(Control Plane)는 갭 우선순위에서 제외(후순위) |
| D1 | 개발→검사→배포→감시→롤백 | 이 5단계를 "완성해야 할 축"으로 갭을 정렬 |
| D9 | 전부 BYO | "AWS 자격증명 연결" UX가 핵심 갭으로 승격 |
| D7·D8 | 앱 감지→S3/ECS Fargate | 배포 대상 갭을 "ECS 경로 완성·검증"으로 한정 |
| D15~D18 | 검사·감시·롤백 | 기존 코드(Preflight/Watchdog/rollback)의 "연결·검증"이 갭 |
| D19·D20 | 마켓플레이스·DoD | 확장 배포·품질 게이트를 갭에 포함 |

**분석 대전제(재사용 관점):** ReCoder는 이미 방대한 코드가 있다. 그래서 대부분의 갭은 "처음부터 만들기"가 아니라 **"이미 있는 것을 확정 방향에 맞게 잇고 검증하기"** 다.

---

## 1. 현행 구조 (확정 범위 기준)

확정 방향에서 **실제로 손댈 3영역**만 추리면:

| 영역 | 위치 | 역할 | 확정 방향에서의 위치 |
|---|---|---|---|
| **VSCode 확장** | `extension/` (TS/React) | 사용자 진입점·UI·코드 에이전트 | ⭐ 주 무대 (AI-DLC UI·자격증명 연결이 여기) |
| **Local Core** | `core/` (FastAPI) | 분석·생성·검사·배포·감시 오케스트레이션 | ⭐ 엔진 (plan/generate·Preflight·배포·Watchdog) |
| **Gateway** | `gateway/` (SAM) | (기존) 키 없이 AI·S3 대행 | 🔻 BYO 확정으로 역할 축소(맛보기 옵션 or 제거) |

> 봇(`discord-bot/`)·조직관리 서버(`control_plane/`)는 코드가 있으나 이번 릴리스 **범위 밖**(D2). 유지는 하되 이번 갭 정리에서는 다루지 않는다.

핵심 파일 지도(이번에 관련되는 것):
- `extension/src/core/ApiClient.ts` — Core REST 호출 (여기에 `plan`/배포 흐름 연결)
- `extension/webview-src/components/CodeAgent.tsx` — 코드 에이전트 패널 (결정 카드 UI 들어갈 곳)
- `extension/src/*` AWS 관련 — `recoder.awsConfigure` 명령·자격증명 저장 (BYO 연결 UX 확장)
- `core/code_agent.py` — `generate_code`(이미 `rationale` 필드 있음 → ADR로 확장)
- `core/preflight/`, `core/security_scan.py` — 배포 전 검사
- `core/agents/ecs_agent.py`, `core/ecs_deploy_agent.py`, `gateway/src/deploy.py` — 배포(ECS/S3)
- `watchdog/`, `core/rollback_policy.py`, `core/rollback_pr_agent.py` — 감시·롤백

---

## 2. 기술 스택

| 영역 | 기술 |
|---|---|
| 확장 | TypeScript 5 · React 18 · Tailwind · Webpack · VSCode Extension API |
| Local Core | Python 3.11 · FastAPI · uvicorn · boto3(Bedrock) · APScheduler · PyInstaller(단일 exe 번들) |
| AI | Amazon Bedrock (BYO 확정 → 사용자 자기 계정 Bedrock) |
| 배포 대상 | AWS S3(정적) · ECS Fargate(서버형) |
| 검사·보안 | 자체 시크릿 스캐너 · Preflight · (추가 예정) Trivy 이미지 스캔 |
| 감시 | (확정) CloudWatch 1차 |
| 자격증명 | 사용자 IAM 액세스 키 · VSCode SecretStorage 보관 |

---

## 3. 완성도 (재사용 관점: 있는 것 vs 검증 필요)

확정 파이프라인 5단계 + 자격증명 + 확장배포 기준.

| 단계 | 이미 있는 것 | 상태 | 이번에 할 일 |
|---|---|:--:|---|
| **개발(AI-DLC)** | `generate_code`(+op별 `rationale`), 코드 에이전트 패널 | 🟡 씨앗 있음 | `plan` 2단계·결정 카드 UI·ADR 영속화(신규) |
| **배포 전 검사** | Preflight(static/runtime/checks), 자체 시크릿 스캐너 | 🟢/🟡 | 이미지 취약점 스캔(Trivy) 추가·검사 게이트 연결 |
| **배포** | S3 정적 배포(🟢), ECS 배포 에이전트(🟡) | 🟡 | ECS Fargate 경로 완성·검증 + 앱 감지·선택 카드 |
| **자격증명(BYO)** | `awsConfigure` 명령, 자격증명 저장 일부 | 🟡 | 연결 UX·권한표·금고·점검·비용안내 보강 |
| **배포 후 감시** | Watchdog(`recoder_watchdog.py`, `docker_monitor.py`) | 🟡 | ECS/CloudWatch 신호 연결·검증 |
| **롤백** | `rollback_policy.py`, `rollback_pr_agent.py`, 승인 패턴 | 🟡 | "제안→승인" 흐름을 실제 배포에 연결·검증 |
| **확장 배포** | 확장 골격·패키징 스크립트 | 🟡 | 마켓플레이스 게시 준비(폴백 VSIX) |

**요약 판정:** 확정 파이프라인의 **뼈대는 대부분 코드로 존재**한다. 다만 대부분 "코드는 있으나 실제로 끝까지 돌려 검증 안 된(🟡)" 상태다. 그래서 이번 MVP의 본질은 **"흩어진 조각을 확정 흐름으로 잇고, 실제 AWS에서 한 번 끝까지 돌려 검증(Done-Done)"** 이다.

---

## 4. 프로덕션 갭 목록 (확정 MVP 기준)

우선순위: **P0 = MVP 데모 흐름을 막는 블로커 / P1 = 프로덕션 최소 요건 / P2 = 운영 성숙도**

### 4.1 개발 단계 (AI-DLC) — 신규 기능
- **[P0] `POST /api/code/plan` 신설** — 요청 시 코드 대신 설계 결정 목록 반환. (현재 `generate_code`는 바로 코드 생성)
- **[P0] 결정 카드 UI** — 채팅 + 팝업 결정 카드(확정 UI, 목업 v2). `CodeAgent.tsx`에 단계 추가.
- **[P0] ADR 영속화** — 선택 결과를 `docs/adr/`에 결정·근거·대안으로 기록. (`generate`가 `adr` 배열 반환)
- **[P1] 항상 선택지(D5)·사람 승인(D6)** — 사소한 변경도 카드, 승인 후 실행.

### 4.2 배포 전 검사 · 보안
- **[P1] 이미지 취약점 스캔(Trivy) 추가** — 시크릿 스캔은 있으나 컨테이너 이미지 취약점 검사 미비.
- **[P1] 검사 게이트 연결** — 실패 시 배포 차단 + 수정안 제안(D16)을 배포 흐름에 실제 연결.

### 4.3 배포
- **[P0] ECS Fargate 경로 완성·검증** — ECS 배포 에이전트가 있으나 실제 배포까지 끝까지 검증 안 됨.
- **[P0] 앱 종류 감지 + 배포 위치 선택 카드(D7)** — "정적/서버형" 판정 로직 + 배포 단계 카드 UI.
- **[P1] S3 경로 유지** — 이미 동작. BYO 전환 시 사용자 계정 S3로 배포하도록 조정.

### 4.4 AWS 자격증명 (BYO) — 확정으로 승격된 핵심 갭
- **[P0] 자격증명 연결 UX** — 사용자가 IAM 액세스 키를 넣는 흐름(D10). `awsConfigure` 확장.
- **[P0] 권한표(정책 JSON) 제공(D12)** — 최소권한 정책 텍스트를 확장이 제공 → 복붙 가이드.
- **[P0] SecretStorage 보관(D11)** — 키를 금고에 암호화 저장(토큰 저장 방식 재사용).
- **[P1] 등록 시 점검(D13)** — 유효성 + 권한 과부족 검사·경고.
- **[P1] 비용 보호(D14)** — 배포 전 예상 비용 안내 + 예산 알람 가이드.

### 4.5 배포 후 감시 · 롤백
- **[P0] Watchdog ↔ ECS/CloudWatch 연결** — 감시 대상을 실제 배포된 ECS 서비스로.
- **[P0] 롤백 "제안→승인" 검증(D17)** — 이상 감지 → 롤백 제안 → 승인 → 이전 버전 복귀를 끝까지 검증.
- **[P1] 감시 지표(D18)** — CloudWatch 헬스·에러율·p95 알람.

### 4.6 확장 배포 · 품질
- **[P1] 마켓플레이스 게시 준비(D19)** — 퍼블리셔·아이콘·README·심사. 폴백 VSIX.
- **[P1] DoD·ADR·보드 운영(D20)** — 완료 정의 강제, 결정마다 ADR, 스프린트·칸반(이미 구성).

### 4.7 정체성 관련(보안 프로젝트다운 포인트)
- **[P1] Bedrock/배포 IAM 최소권한** — 사용자 권한표를 딱 필요한 범위로.
- **[P2] 의존성 취약점 정리** — GitHub Dependabot 경고(High·Moderate) 해소. "배포 후 보안"과 결이 맞음.

---

## 5. 한 장 요약

- **구조:** 확정 방향에선 확장 + Local Core + (축소된) 게이트웨이가 무대. 봇·조직관리 서버는 범위 밖.
- **완성도:** 파이프라인 5단계의 뼈대는 대부분 코드로 존재하나 **미검증(🟡)**. 본질은 "잇고 검증(Done-Done)".
- **최대 갭(P0):** ① AI-DLC 개발(plan·결정카드·ADR) ② ECS Fargate 경로 완성·검증 ③ 앱 감지·배포 선택 카드 ④ BYO 자격증명 연결 UX(키·권한표·금고) ⑤ 감시·롤백 연결·검증.
- **정체성 포인트:** BYO·최소권한·금고·이미지 취약점 스캔·의존성 취약점 정리 = "배포 후 보안·안정성" 주제의 실증.

> 본 문서는 분석·계획이며 코드는 변경하지 않았다. 다음 문서: **2/4 AWS MVP 5회차 배포계획**.
