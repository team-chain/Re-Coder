# ReCoder 구현 설계서 (현행 구현 기준)

> **문서 성격**: 현재까지 *실제로 구현·검증된* 내용만을 기준으로, "이 문서만 보고도 동일하게 재구현할 수 있는" 수준으로 기술한다. 각 항목에는 **구현 근거**(왜 이렇게 설계했는가)를 함께 적는다.
> **작성 기준일**: 2026-05-30 · **대상 브랜치**: `develop`
> **표기**: 🟢 구현·검증 완료 / 🟡 구현됨(데모 미시연·부분) / ⚪ 2학기 범위

---

## 0. 한눈에 보기

ReCoder는 **"자연어 한 줄 → 코드 생성 → 공개 배포"** 를 하나의 흐름으로 잇는 AI Ops 도구다. 사용자는 **VSCode 확장만 설치**하면, 본인 AWS 키 없이 **운영자 계정의 AI(Bedrock)** 를 정해진 한도 안에서 사용한다.

핵심 구성 5가지:

| 컴포넌트 | 한 줄 정의 | 런타임 | 상태 |
|---|---|---|---|
| **VSCode 확장** | 사용자 클라이언트(사이드바·워크벤치·코드 에이전트) | 각 사용자 VSCode | 🟢 |
| **Local Core** | 확장이 띄우는 로컬 엔진(FastAPI) — 분석·생성·배포 오케스트레이션 | 사용자 PC(localhost) | 🟢 |
| **Bedrock 게이트웨이** | 운영자 계정에서 AI·정적배포를 *대행*하는 서버리스 백엔드 | AWS(Lambda) | 🟢 |
| **Discord 봇 + 작업 패널** | 채팅으로 개발·배포하는 ChatOps 채널 | 상시 프로세스 | 🟢 |
| **정적 배포(S3)** | 생성물을 공개 URL로 올리는 호스팅 | AWS(S3) | 🟢 |

**설계 대전제(구현 근거)**: 사용자에게 AWS 자격증명을 절대 노출하지 않는다 → 모든 AI·배포 호출은 **게이트웨이가 대행**하고, 사용자는 **공개 URL + 본인 토큰**만 가진다. 운영자 키는 확장/봇 어디에도 없고, AWS의 **IAM 역할**로만 사용된다.

---

## 1. 제품 개요

### 1.1 목적
- 비전공자·입문자도 **자연어로 요청 → 동작하는 결과물 → 인터넷 공개**까지 한 흐름으로 경험.
- 운영자(교수자/제공자)는 **AWS 계정 하나**로 다수 사용자에게 AI를 제공하되, **한도와 보안을 통제**.

### 1.2 1학기 MVP 범위
- **개발**: 자연어 → 단일 HTML/JS 정적 결과물 생성(에러 자동 수정 포함).
- **배포**: 생성물을 **S3 정적 호스팅**으로 공개 URL 발급.
- **사용 모델**: 사용자는 확장 설치 + 반 코드 입력만. AI는 운영자 계정(Bedrock)으로, **per-user 쿼터 + 풀 비용 캡** 안에서.

**구현 근거**: "코드 생성"만으로는 Copilot/Cursor와 차별이 약하다. ReCoder의 차별점은 **개발→배포까지 한 번에 + 호스팅 포함 + 키 없이 사용**이다. 정적 HTML로 범위를 좁힌 이유는 1학기에 *가장 구현 쉬운 한 줄기를 끝까지 잘 동작*시키기 위함(LLM이 단일 HTML을 안정적으로 생성, 배포도 S3 정적이 가장 단순·저비용).

### 1.3 파이프라인(목표 형태)
```
개발(Build) → 깃허브(GitHub) → 배포(Deploy) → 운영(Operate)
```
- 1학기: **개발 → 배포(S3)** 골든 패스. 깃허브/운영은 구현되어 있으나 데모 핵심 경로 밖(🟡/⚪).

---

## 2. 시스템 아키텍처

### 2.1 전체 구조

```
 [사용자 VSCode]                         [운영자 AWS 계정 — 서버리스]
 ┌───────────────────────┐               ┌──────────────────────────────┐
 │ ReCoder 확장 (TS/React)│   HTTPS       │ API Gateway (HTTP, TLS 자동)  │
 │  - 사이드바/워크벤치   │──(enroll)────▶│  ├ /enroll  → EnrollFn        │
 │  - 코드 작성 패널      │               │  ├ /llm/invoke → InvokeFn ────┼──▶ Bedrock
 │  - 에러 자동분석       │               │  ├ /deploy/s3 → DeployFn ─────┼──▶ S3(공개 버킷)
 └──────────┬────────────┘               │  └ /admin   → AdminFn         │
            │ spawn(127.0.0.1)            │         DynamoDB(토큰·쿼터·풀) │
            ▼                             └──────────────────────────────┘
 ┌───────────────────────┐                         ▲
 │ Local Core (FastAPI)  │── 토큰+URL(env) ─────────┘   (사용자는 본인 토큰만,
 │  분석·생성·배포 라우트 │                              운영자 키는 IAM 역할로만)
 └───────────────────────┘
                                          ┌──────────────────────────────┐
 [Discord]  ── /recoder panel ──────────▶ │ Discord 봇 (상시 프로세스)    │
   버튼/모달/Select/진행임베드            │  make_handler(생성)           │──▶ Bedrock(직접/IAM)
                                          │  control_panel(패널)          │
                                          │  deploy_client ──────────────┼──▶ /deploy/s3
                                          │  recoder_bridge(ws:7780)──────┼──▶ 사용자 VSCode(옵션)
                                          └──────────────────────────────┘
```

### 2.2 두 가지 사용 경로
1. **확장 경로(서버리스, 권장)**: 확장 → Local Core → **게이트웨이(Lambda)** → Bedrock/S3. 켜둘 서버 없음.
2. **Discord 경로(ChatOps)**: 디스코드 → **상시 봇** → Bedrock(직접) + 게이트웨이(/deploy/s3). 봇만 호스팅 필요.

**구현 근거**: AI·배포의 "대행 계층(게이트웨이)"을 두면 ① 사용자에 키 미노출 ② per-user 미터링·쿼터 ③ 최소권한 IAM으로 사고 반경 축소 — 세 가지를 동시에 만족한다. 이는 사실상 멀티테넌트 SaaS의 과금/통제 백본과 동일 구조다.

### 2.3 기술 스택
- 확장: TypeScript, React + Webpack(webview), VSCode Extension API.
- Core: Python 3.11, FastAPI/uvicorn, boto3(Bedrock). 배포 시 PyInstaller 단일 exe로 번들.
- 게이트웨이: AWS SAM(API Gateway HTTP + Lambda(py3.11) + DynamoDB + S3), 최소권한 IAM.
- 봇: Python, discord.py 2.x(app_commands/ui), aiohttp, qrcode.

---

## 3. 컴포넌트 상세

### 3.1 VSCode 확장 🟢

**역할**: 사용자 진입점. Core를 자동 기동하고, 사이드바/워크벤치 UI와 코드 에이전트를 제공.

**구성(주요 파일)**
- `src/extension.ts` — 활성화 진입점. 명령 등록, 사이드바·워크벤치 뷰 등록, 최초 enroll 트리거.
- `src/core/CoreManager.ts` — Core **lazy-spawn**(번들 exe→PATH→`~/.recoder/bin`→python 소스 순으로 탐색), 포트/토큰 관리, 게이트웨이 env 주입.
- `src/core/ApiClient.ts` — Core(127.0.0.1:포트) REST 클라이언트. `analyze`/`generateCode`/`deployStatic`(예정) 등. 가변 타임아웃(코드생성 90s).
- `src/gateway/enroll.ts` — 최초 실행 자가발급 UX. 게이트웨이 URL이 있으면 반 코드 입력 → `/enroll` → 토큰 SecretStorage 저장 + `/recoder link` 명령 복사 안내.
- `src/sidebar/SidebarProvider.ts` — React webview 메시지 허브(analyze/code.generate/apply/diff/pickFolder/pickContext 등).
- `webview-src/App.tsx` — 사이드바 루트(미니멀 라인 UI: 워크플로 Build/Deploy/Operate + 연결 GitHub/AWS/Discord).
- `webview-src/components/CodeAgent.tsx` — **코드 작성 패널**: 대상 폴더 지정·컨텍스트 첨부·멀티턴·op별 적용/diff/시크릿 경고.
- `webview-src/components/BuildMode.tsx` — 에러 분석(자동 감지) + 스캔 + 코드 패널 호스팅.
- `src/sidebar/workbenchHtml.ts` — 워크벤치(GitHub/배포/Discord 탭) HTML/CSS. 세그먼트 탭 + 스텝카드 + iOS형 토글.

**핵심 설정값**(`recoder.*`): `gateway.url`(공개 게이트웨이 주소·기본값 박음), `core.autoStart`, `core.port`(17894~), `bridge.host/port/studentId`, `discord.clientId` 등.

**구현 근거**
- **번들 exe 자동 탐색 + lazy-spawn**: "확장만 깔면 동작(수동 시작 0)"을 위해 Core 실행파일을 VSIX에 동봉하고 확장이 자동 기동. exe가 없으면 소스 fallback → 개발/배포 모두 커버.
- **gateway.url 기본값 박기**: 학생이 주소를 몰라도 설치 즉시 enroll 가능. URL은 비밀이 아니므로 박아도 안전(보호는 토큰/반코드).
- **코드 적용을 전체파일 내용으로**: 생성물을 unified-diff가 아니라 "최종 전체 내용"으로 다뤄 적용 실패를 없앰(작은 정적 파일엔 diff보다 안정적).

### 3.2 Local Core 🟢

**역할**: 확장 뒤에서 도는 로컬 FastAPI 엔진. 에러 분석/코드 생성/시크릿 스캔/(인프라·배포) 오케스트레이션.

**구성·핵심 라우트**(`core/server.py`, 토큰 인증 `X-Session-Token`):
- `POST /api/analyze` — 에러+컨텍스트 → `code_agent.generate_patch` → PatchProposal(diff·위험도·승인레벨).
- `POST /api/code/generate` — 자연어 → `code_agent.generate_code` → 파일 작업 ops(create/edit, 전체내용) + op별 시크릿 경고.
- `POST /api/secrets/scan` — 프로젝트 전체 정규식 시크릿 스캔(바이너리 불필요) → 파일:라인, 값 마스킹.
- `POST /api/infra/generate`, `/api/deploy/{local,ec2,ecs}`, `/api/github/*`, `/api/git/*` — 인프라·배포·깃 연동(🟡 1학기 데모 밖).
- `GET /api/health`/`/api/ready`/`/api/cost` — 상태/비용.

**LLM 경로**: `code_agent`가 `llm.router.get_router().call(...)`로 호출. 라우터가 게이트웨이(운영자 토큰) 또는 BYO 자격증명으로 분기. 게이트웨이 사용 시 env `RECODER_LLM_GATEWAY_URL`+`RECODER_STUDENT_TOKEN`(CoreManager가 주입).

**생성 코어**(`core/code_agent.py`): `_project_root` 탐색 → 컨텍스트 파일 수집 → 프롬프트(전체파일 ops JSON 강제) → 라우터 호출 → `_extract_json`(코드펜스/잡텍스트 견딤) → ops 정규화 → 시크릿 사전검사 첨부.

**시크릿 스캐너**(`core/security_scan.py`): 독립 함수 `scan_text_for_secrets`/`scan_project_for_secrets` — AWS키/GitHub·Slack 토큰/Google키/private key/JWT/일반 시크릿 할당 패턴 + placeholder 제외. gitleaks 있으면 `--no-git`로 병행, 위치 기준 중복 제거.

**프로세스 모델**: 단일톤(`runtime.json`/`core.lock`), 포트 17894→17910 폴백, frozen(exe)일 때 uvicorn에 app 객체 전달.

**구현 근거**
- **Core를 분리한 이유**: 파일시스템·git·docker 같은 로컬 작업은 확장(웹뷰)에서 직접 못 하므로, 로컬 엔진이 필요. REST로 분리해 확장(TS)·봇(py) 양쪽이 재사용.
- **전체파일 ops + JSON 강제 + 견고한 추출**: LLM 출력의 흔들림(코드펜스·설명문)을 흡수해 "생성 실패"를 줄임.
- **시크릿 스캐너 자체 구현**: 바이너리(gitleaks) 미설치 환경(학생 PC)에서도 동작해야 하므로 순수 파이썬 폴백을 1순위로. 값은 항상 마스킹(유출 방지).

### 3.3 Bedrock 게이트웨이 (서버리스 백엔드) 🟢

**역할**: 운영자 계정에서 **AI 호출·정적 배포를 대행**. 사용자에게 키를 노출하지 않고 per-user 쿼터/비용을 통제.

**구성**(AWS SAM, `gateway/`):
- `API Gateway(HTTP)` — TLS 자동 종단. 엔드포인트 4종.
- `Lambda(python3.11)` 4개: Invoke / Enroll / Admin / Deploy. 공용 로직 `src/common.py`.
- `DynamoDB(RecoderGateway)` — pk/sk, TTL(토큰 7일·rate 윈도우 자동만료), PAY_PER_REQUEST(트라이얼 규모 사실상 $0).
- `S3(공개 정적 버킷)` `recoder-deploy-<AccountId>-<Region>` + BucketPolicy(공개 읽기) + 웹사이트 호스팅.

**엔드포인트**
| 경로 | 함수 | 인증 | 동작 |
|---|---|---|---|
| `POST /enroll` | EnrollFn | 반 코드 | 자가발급: 난수 student_id + 토큰 발급(정원·디스코드 1:1 제한) |
| `POST /llm/invoke` | InvokeFn | Bearer 토큰 | 쿼터 확인 → Bedrock Converse 대행 → 사용량 기록 |
| `POST /deploy/s3` | DeployFn | Bearer 토큰 | 파일 PUT(학생/프로젝트 prefix) → 공개 URL 반환 |
| `POST /admin` | AdminFn | X-Admin-Key | 토큰 발급/쿼터/활성 관리(운영자) |

**IAM(최소권한, 구현 근거의 핵심)**
- InvokeFn: `bedrock:InvokeModel`(+Converse류)만. **다른 AWS 자원 접근 0**.
- DeployFn: `S3CrudPolicy`(그 버킷 한정) + DynamoDB.
- 정적 키 없음 — Lambda 실행 **역할(Role)** 로만.

**쿼터/비용 파라미터**: `MaxStudents`(정원), `DefaultMaxTotalTokens`/`DefaultMaxDailyTokens`/`DefaultRpm`(per-user), `PoolCapUsd`/`PoolSoftUsd`(풀 전체 캡), `TokenTtlDays`, `AllowedModels`(서버 강제 모델 allowlist).

**구현 근거**
- **서버리스(Lambda)**: 켜둘 서버가 없어 운영자 PC와 무관, 요청당 과금이라 트라이얼 비용 거의 0.
- **토큰 해시만 저장**: 원문 미저장(sha256), TTL 만료·폐기·회전 가능 → 유출 시 피해를 1인 쿼터로 한정.
- **모델 allowlist를 서버가 강제**: 클라이언트가 비싼 모델을 임의 호출 못 하게 막아 비용 통제.
- **풀 비용 캡**: 최악의 남용도 금액 상한에서 차단(예산 보호).

### 3.4 Discord 봇 + 작업 패널 🟢

**역할**: 채팅으로 개발·배포하는 ChatOps 채널. 상시 프로세스로 동작.

**구성**(`discord-bot/`):
- `bot.py` — discord.py 클라이언트, `/recoder` 슬래시 그룹(panel/link/invite/status/preflight/deploy/rollback), DEV_GUILD_ID 즉시 동기화, 브리지·등록 API 기동.
- `control_panel.py` — `/recoder panel`의 View. 버튼 [개발][깃허브][배포][운영][전체 실행]. **개발→입력 모달**, **배포→Select(S3/로컬)**, **전체 실행→파이프라인+진행 임베드**, 배포 성공 시 **URL+QR 채널 공개 게시**.
- `make_handler.py` — 생성 코어. 의도 분류(create/run/modify/delete), 파일명 준수, 세션 메모리(`_SESSIONS`), Bedrock `converse_stream`(직접). 채널 메시지 경로(`/make`)와 패널 재사용 함수 `run_generation` 공유.
- `deploy_client.py` — 게이트웨이 `/deploy/s3` 업로드(aiohttp). env `RECODER_GATEWAY_URL`+`RECODER_DEPLOY_TOKEN`.
- `recoder_bridge.py` — WebSocket 허브(ws:7780). 사용자 VSCode 연결 시 파일 생성 이벤트 라우팅(per-student `send_to_student` / broadcast).
- `guild_store.py` — discord_user_id ↔ student_id 바인딩(SQLite), Make 채널 설정.

**모델**: env `BEDROCK_PRIMARY_MODEL_IDENTIFIER`로 지정(Haiku→Sonnet 등 교체). 모델별 maxTokens 한도 표로 자동 클리핑.

**구현 근거**
- **버튼/모달/Select 패널**: 명령어 암기 없이 클릭만으로 진행 → 입문자 친화. 모달은 채널을 어지럽히지 않고 자유 입력을 받기 위함.
- **QR 즉석 생성·공개 게시**: 발표 라이브에서 청중이 폰으로 바로 접속(URL 길이 무관, 사전 발급 불필요).
- **봇은 Bedrock 직접 호출**: 봇은 운영자가 돌리는 신뢰 프로세스라 IAM 역할/운영자 자격으로 직접 호출이 단순. 배포만 게이트웨이 토큰 사용(일관된 미터링).

### 3.5 정적 배포 (S3) 🟢

**역할**: 생성물(정적 HTML/JS)을 공개 URL로 호스팅.

**동작**(`gateway/src/deploy.py`):
1. Bearer 토큰 인증 → student_id 추출.
2. `files[{path,content}]` 수집(파일당 3MB·30개 한도), 경로 sanitize(`..` 차단), content-type 추론.
3. `s3:PutObject`를 `<student_id>/<project>/` prefix로. `index.html` 없으면 단일 HTML을 index.html로도 복제(폴더 URL 즉시 열림).
4. 반환: `http://<bucket>.s3-website.<region>.amazonaws.com/<student>/<project>/`.

**구현 근거**
- **학생 토큰 prefix 분리**: 사용자별 폴더로 충돌 방지 + 본인 쿼터로 귀속.
- **index.html 자동 복제**: 생성 파일명이 tetris.html이어도 폴더 URL이 바로 열리게(UX).
- **공개 읽기 버킷**: 데모용 정적 결과물은 공개가 목적. 비용은 Always-Free 한도 내라 사실상 0.

---

## 4. API / 데이터 계약

### 4.1 게이트웨이 엔드포인트(요청/응답)

**POST /enroll** — 자가발급
```
요청  { "code": "<반 코드>", "name": "", "discord_user_id": "" }
응답  { "student_id": "abc123", "token": "rcdr_abc123_<secret>" }   // 토큰 원문은 1회만
```

**POST /llm/invoke** — AI 대행 (헤더 `Authorization: Bearer <토큰>`)
```
요청  { "messages":[{"role":"user","content":[{"text":"..."}]}], "system":"", "max_tokens":2048, "model":null }
응답  { "text", "model_used", "input_tokens", "output_tokens", "cost_usd" }
```

**POST /deploy/s3** — 정적 배포 (헤더 `Authorization: Bearer <토큰>`)
```
요청  { "project":"tetris", "files":[{"path":"index.html","content":"<html>…"}] }
응답  { "url":"http://<bucket>.s3-website.<region>.amazonaws.com/<sid>/tetris/", "files":[...], "count":N }
```

**POST /admin** — 운영자 (헤더 `X-Admin-Key: <키>`) — 토큰 발급/쿼터/활성 토글.

### 4.2 Core 주요 라우트(헤더 `X-Session-Token`)
- `POST /api/code/generate` → `{ summary, ops:[{action,file,language,content,rationale,secret_warnings[]}], model }`
- `POST /api/analyze` → `PatchProposal{ proposal_id, summary, risk_level, approval_level, patches[], test_command }`
- `POST /api/secrets/scan` → `{ root, count, findings:[{rule,severity,file,line,masked,fix}] }`

### 4.3 브리지 프로토콜(WebSocket ws:7780)
봇 → VSCode 이벤트(JSON): `{type:"start",filename,language,prompt}` → `{type:"chunk",text}` → `{type:"end",filename,auto_run}` / `{type:"delete",filename}`. 확장 `BridgeClient`가 수신해 파일 생성/수정/삭제·자동실행.

### 4.4 DynamoDB 스키마(RecoderGateway)
| pk | sk | 주요 속성 |
|---|---|---|
| `STUDENT#<sid>` | `META` | token_sha256, active, max_total/daily_tokens, rpm, used_total/today_tokens, ttl |
| `DISCORD#<uid>` | `META` | student_id, ttl (1:1 바인딩 역조회) |
| `POOL` | `META` | used_total_tokens, used_cost_usd, enrolled_count |
| `RATE#<sid>` | `<minute>` | count, ttl (분당 윈도우) |

**구현 근거**: 단일 테이블 + TTL로 토큰 만료·rate 윈도우를 자동 정리(운영 부담↓). 비용·정원·바인딩을 모두 원자적 업데이트로 처리(경합 안전).

---

## 5. 핵심 시나리오

### 5.1 최초 사용(확장)
1. VSIX 설치 → 확장 활성화 → CoreManager가 번들 exe 기동.
2. `gateway.url`이 있으므로 `ensureEnrolled` → 반 코드 입력 → `/enroll` → 토큰 SecretStorage 저장.
3. 이후 코드 생성/에러분석 시 Core가 게이트웨이로 Bedrock 호출(키 없이).

### 5.2 코드 생성(확장)
`CodeAgent` 입력 → `code.generate` → Core `/api/code/generate` → 게이트웨이 → ops 수신 → 카드(적용/변경보기/시크릿 경고) → 적용 시 워크스페이스에 파일 쓰고 에디터 오픈.

### 5.3 디스코드 전체 실행(ALL)
`/recoder panel` → [전체 실행] 모달 입력 → `run_generation`(개발) → `deploy_client`(S3) → 진행 임베드 갱신(개발✓→배포✓) → **URL+QR 채널 게시**.

### 5.4 정적 배포
생성물(`_SESSIONS`/워크스페이스) → `/deploy/s3`(본인/봇 토큰) → 공개 URL.

---

## 6. 보안 설계

| 위협 | 대응(구현) |
|---|---|
| 운영자 키 유출 | 확장·봇·VSIX에 키 0. Lambda **IAM 역할**로만. |
| 권한 확대 | InvokeFn은 `bedrock:InvokeModel`만, DeployFn은 단일 버킷 S3만(최소권한). |
| 토큰 유출 | 해시만 저장, TTL 7일, 폐기/회전 가능 → 피해를 1인 쿼터로 한정. |
| 비용 폭주 | per-user 쿼터 + 풀 비용 소프트/하드 캡. |
| 시크릿 누출 | Context Gate 마스킹(LLM 전송 전) + 시크릿 스캐너(값 마스킹). |
| 전송 도청 | API Gateway TLS 자동. |

**운영 수칙**: 반 코드는 정원까지 self-enroll 가능 → 비공개 배포. AdminKey는 마스터급이라 절대 노출 금지(영문 ASCII). 토큰 CSV는 git 추적 제외.

**구현 근거**: "키를 어디에도 두지 않고, 권한을 최소화하며, 토큰을 폐기 가능하게" 세 원칙으로 사고 반경을 구조적으로 축소. 사용자는 *내가 정한 한도 안에서, 내 계정의 Bedrock만* 사용.

---

## 7. 비용·쿼터 모델
- **변동비 = LLM 토큰**. 모델 allowlist + per-user(총/일/rpm) + 풀 캡으로 통제.
- 기본 모델 **Claude 3 Haiku**(빠름·저렴, 데모용). 완성도 필요 시 env로 **Sonnet 4.x**(inference profile) 교체 — 비용↑.
- S3/Lambda/DynamoDB는 트라이얼 규모에서 사실상 Always-Free 안.

**구현 근거**: 무제한 구독은 헤비 유저에서 적자 → 쿼터·캡으로 마진/예산 보호. 모델은 env 한 줄로 품질·비용 절충.

---

## 8. 배포·운영 런북(요약)
**운영자 1회 셋업**: ① `aws configure`(운영자 키) → ② Bedrock 모델 액세스 활성화 → ③ `sam deploy`(게이트웨이=새 URL/버킷) → ④ 토큰 발급(`issue_tokens.py`) → ⑤ 봇 `.env`(URL·토큰) + 봇 기동 → ⑥ 확장 `gateway.url`에 URL 박고 exe 재빌드 + VSIX 패키징 → 학생 배포.
**계정 교체**: `aws configure`(새 키) → 모델 액세스 → `sam deploy`(새 URL) → 토큰 재발급 → 봇/확장 URL 갱신. 옛 스택 `sam delete`로 정리.
**봇 상시화(2학기)**: EC2 프리티어/Fargate + IAM 역할(키 없음) → PC 무관 24시간.

---

## 9. 구현 현황 & 2학기 범위

**🟢 완료·검증**: 게이트웨이(enroll/invoke/deploy/admin)·per-user 쿼터·풀 캡 / 확장 코드 에이전트(폴더·컨텍스트·멀티턴·diff·시크릿경고) / 에러 자동분석 / 시크릿 스캐너 / 디스코드 작업 패널(버튼·모달·Select·ALL·QR) / S3 정적 배포 / enroll 자가발급.

**🟡 구현됨·데모 경로 밖**: 깃허브 연동(로그인/푸시/시크릿/Actions), Local/EC2/ECS 배포, Preflight·보안스캔(Trivy/Hadolint)·OPA·Postmortem·Replay·Watchdog.

**⚪ 2학기**: 확장 내 배포 버튼(올인원), 봇 상시 호스팅(EC2/Fargate), 깃허브/운영 파이프라인 자동화, 결제(Stripe)·티어 쿼터(SaaS화), 커스텀 도메인(CloudFront), 학생별 디스코드 배포 격리.

---

## 부록 A. 환경변수
- **게이트웨이(SAM 파라미터)**: AdminKey, EnrollCode, MaxStudents, AllowedModels, PoolCapUsd/SoftUsd, Default*Tokens/Rpm, TokenTtlDays.
- **봇(.env)**: DISCORD_BOT_TOKEN, DEV_GUILD_ID, RECODER_GATEWAY_URL, RECODER_DEPLOY_TOKEN, BEDROCK_PRIMARY_MODEL_IDENTIFIER, BEDROCK_REGION, RECODER_MAKE_CHANNEL_ID, RECODER_BRIDGE_*.
- **Core(env, 확장이 주입)**: RECODER_LLM_GATEWAY_URL, RECODER_STUDENT_TOKEN, RECODER_PROJECT_ROOT, BEDROCK_REGION.

## 부록 B. 파일 맵(요약)
- `extension/` — 확장(TS/React) + 번들 `bin/recoder-core.exe`
- `core/` — Local Core(FastAPI) + `code_agent.py`/`security_scan.py` + `recoder-core.spec`(PyInstaller)
- `gateway/` — SAM 템플릿 + `src/{common,invoke,enroll,admin,deploy}.py` + `scripts/issue_tokens.py`
- `discord-bot/` — `bot.py`/`control_panel.py`/`make_handler.py`/`deploy_client.py`/`recoder_bridge.py`/`guild_store.py`
- `docs/` — 본 설계서 + STUDENT_GUIDE / OPERATOR_RUNBOOK / REHEARSAL_CHECKLIST
