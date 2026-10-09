# DB 구조 확인 · 데모 전용 DB · 배포 후 확인 · 리코더 캐릭터 · 지도 스타일 표시 (2026-10-09, 2.0.0 — 메이저 버전 2 시작, 예전 1.1.33)

- 실기기(세 번째 영상): 컨테이너 이름 `temp` 를 12개 앱이 같이 써서 `temp-postgres` 에 다른 앱의 테이블이 남아 있었고, `/health` 는 통과했지만 `/api/products` 가 500(`errorMissingColumn`). 재현 확인.
- `local_services`: `db_suffix`(실제 "", "-2"…, 데모 "-demo", "-demo-2"…), `start_new_db`(번호 증가, 같은 이름의 컨테이너·`-data` 볼륨이 있으면 건너뜀, 예전 볼륨 유지), `accept_schema`/`schema_accepted`(DB 이름+초기화 SQL 지문), `expected_schema`(CREATE TABLE 파싱), `schema_status`(information_schema 와 비교, 확인 못 하면 막지 않음). 상태 폴더는 `RECODER_LOCAL_SERVICES_DIR`(테스트 격리).
- `/api/deploy/execute`: 동반 DB 준비 직후 구조가 다르고 허용하지 않았으면 `stage: "db_schema"`, `diagnosis.code = DB_SCHEMA_MISMATCH`, `db_choice{db, missing_tables, missing_columns, other_tables}` 로 멈춘다(앱 컨테이너는 건드리지 않음). `POST /api/deploy/db-choice {plan_id|container_name, choice: "new"|"keep"}`.
- 배포 후: `app_check` — 코드에서 찾은 조회 API(Express `app.use('/api/..')`·`app.get`, FastAPI/Flask, 최대 3개, webhooks·auth 등 제외)를 GET. 5xx 면 `docker logs` 로 `DB_SCHEMA_MISMATCH`/`APP_RUNTIME_ERROR` 진단. `demo_seed{ok, script, message}`.
- 웹뷰: `DbChoiceCard.tsx`(DbSchemaChoice·AppCheckWarning·DemoSeedWarning), ShipMode 는 선택 뒤 같은 계획으로 다시 실행. 앱 확인 오류면 초록 배너를 숨긴다.
- 캐릭터: `recoderCharacter.ts`(Recoder Character.png 배경 제거 96px data URI, 워터마크 제외). `teamAnimals.ts` 의 `AnimalKind` 는 저장된 팀 구성 호환용 내부 번호로만 남고 그림은 모두 리코더. 팀원 이름은 `memberLabel`(설계·개발 N·검토). `DeliveryTrack` aria "리코더가 배포 박스를 옮기는 중". 캔버스 드래그 배달은 사용자 요청으로 뺐다.
- 지도(`src/codemap/analyzer.ts`): 스타일 확장자 `.css/.scss/.sass/.less` → 층 `style`, 플래그 `style`(+연결 못 찾으면 `style_unlinked`), 과부하·고립 대상 아님. 도구 설정 파일 → 플래그 `config`. 부수 효과 import·CSS `@import`/`@use`/`@forward`·루트 기준(`/x`, `public/`, `static/`, `src/`)·`@/` 별칭·SCSS partial 해석. UI(`CodeMap.tsx` `projSub`, 캔버스 `flagText`)는 중립 표시.
- 검증: Core 2,455개·확장 530개 통과. code-server UI 로 (1) 다른 앱 테이블이 있는 DB → 선택 카드 → [새 DB로 시작] → 남아 있던 `-3` 볼륨 건너뛰고 `shop-postgres-4` → 배포 완료·`/api/products` 200, 배달 트랙은 리코더 이미지, (2) 팀 모드 구성·보드에 리코더 6명(설계·개발 1~4·검토), (3) 아키텍처 지도에서 CSS 16개가 모두 "스타일"(고립 0), `vite.config.js` 는 "도구 설정". 클라우드 샌드박스의 git URL 재작성 환경변수 때문에 `canvasConnections` 1건이 실패하는 것은 환경 문제(변수를 빼면 통과).

# 로컬 Docker 배포 — 필요한 설정 · 결제를 끈 로컬 데모 (2026-10-09, 1.1.32)

- 실기기: 검증된 쇼핑몰 기반(develop #83)을 로컬 Docker 로 배포하면 `JWT_SECRET`·`STRIPE_SECRET_KEY`·`STRIPE_WEBHOOK_SECRET` 이 없어 빌드 뒤 즉시 종료, 안내는 "auth.js 코드를 고치라" 였다.
- `core/deploy_settings.py`: 코드에서 "없으면 throw/exit/raise" 하는 환경변수를 찾는다(다른 if 안의 조건부 검사·프론트엔드 폴더는 제외, `.length < N` 로 최소 길이). 앱 내부 서명 키는 자동 생성, 외부 키는 입력, `PAYMENT_MODE`·`STRIPE_MOCK_HOST`·`STRIPE_MOCK_PORT` 를 읽는 Node 앱은 로컬 데모 지원.
- 값 보관: `~/.recoder/deploy_settings/<컨테이너>.json`(0600, 테스트는 `RECODER_DEPLOY_SETTINGS_DIR`). 계획·응답·배포 기록에는 이름·상태만. 실행·복구·롤백 때 `-e` 로만 넘긴다. 우선순위: 계획(DB 등) > 저장한 설정(데모 값은 항상) > PC `.env`(이미 정한 이름은 건너뜀). 저장한 값은 PC `.env` 에 같은 이름이 있으면 쓰지 않는다.
- 계획(`/api/deploy/plan`): `settings`·`settings_missing`·`demo`. 비어 있으면 실행(`/api/deploy/execute`)이 빌드 전에 `stage: "settings"`, `diagnosis.code = APP_MISSING_SETTING` 으로 멈춘다.
- 입력·데모 전환: `POST /api/deploy/settings {plan_id, values, demo}` — 계획에 없는 이름·최소 길이 미만·줄바꿈은 거절.
- 데모: 모의 결제 서버를 **앱 이미지의 node** 로 `<컨테이너>-payment-mock` 에 띄운다(외부 이미지 없음, `--no-healthcheck`). 결제 의도 2.5초 뒤 서명한 `payment_intent.succeeded` 웹훅을 앱에 보내 주문이 결제 완료(테스트)가 된다. `backend/init-db.js` 가 있으면 데모 상품을 한 번 넣는다. 데모를 끄면 모의 서버를 내린다.
- `build_failure`: "X must be set / is required / is not set / Missing env X" → `APP_MISSING_SETTING`(+`missing_env`). 화면은 [문서 근거로 오류 수정] 대신 [필요한 설정 입력하고 다시 배포].
- UI: `DeploySettingsPanel`(planReady), 설정이 비면 승인 잠금. `DeliveryTrack` 동물 배달 애니메이션(Docker·S3·ECS).
- 검증: 이 저장소 클라우드에서 실제 Docker 로(베이스 이미지는 Docker Hub 차단 때문에 로컬 대체 이미지) 쇼핑몰 기반을 code-server UI 로 개발→적용→Docker 패널→데모 실행→헬스 통과, 브라우저로 회원가입·장바구니·테스트 주문→결제 완료 확인. 키 입력 경로, 실패 시 이전 컨테이너 복구(데모 설정 유지)도 확인.

# 대규모 코드 생성 · 팀 모드 (2026-10-08, 1.1.31)

- **원인(실기기 "긴 요청이 잘린다")**: 학생용 게이트웨이는 HTTP API(통합 제한 30초, 늘릴 수 없음) 뒤 Lambda 라 호출당 출력을 4096 토큰(`GW_MAX_TOKENS_CEILING`)으로 깎고 `stop_reason` 을 주지 않았다. 코어는 잘림을 형식 오류로 착각해 같은 큰 요청을 한 번 더 보냈고, 분할 생성은 파일 목록을 36개에서 자르고(`files[:36]`), 파일 하나가 한도를 넘으면 실패했다. 확장은 응답 하나를 15분까지만 기다렸다.
- **엔진** `core/gen_engine.py`: 설계(요약·약속·목록 — 목록은 25개씩 `more` 가 끝날 때까지, 약속이 길면 조각으로) → layer 0(공통 기반) 순서대로 → layer 1·2 를 에이전트 N명이 동시에(기반 코드를 그대로 보며) → 한도를 넘는 파일은 150줄씩 이어 쓰기(상한 없음, 진행이 없을 때만 멈춤) → 파일마다 문법(JSON·Python·`node --check`)·비밀값 검사 후 edits(find→replace)로 부분 교정 → 전체 점검(build_readiness) 교정도 부분 교정.
- **속도·안정**: 게이트웨이 모드면 분당 9회로 미리 제한(`RECODER_LLM_RPM`), 동시 에이전트 `agents`(1~8, 기본 3, `RECODER_GEN_CONCURRENCY`). 일시적 오류는 3·8·20·45·60초 재시도, 분당 한도는 61초 대기, 일일·총량 한도는 일시 정지.
- **체크포인트** `core/generation_jobs.py`: `~/.recoder/generation/<job_id>.json`(`RECODER_GENERATION_DIR`). 요청 지문(요청문·결정·대상 폴더·프로젝트)이 같을 때만 이어 쓴다. 끝난 결과도 남겨 연결이 끊긴 확장이 같은 작업 ID 로 바로 받는다. 7일 지나면 정리.
- **API**: `POST /api/code/generate/stream`(SSE, 10초 심장박동, 이벤트 planning·planned·wave·file_start·file_split·file_part·fixing·verify_failed·file_done·retry·waiting·consistency·generated·resumed·done·error). `error.resumable=true` 면 `resume_job` 으로 같은 요청을 다시 보낸다. 예전 `/api/code/generate` 는 그대로이고 일시 정지는 409 + 같은 정보. 요청에 `mode`("auto"|"team"), `resume_job`, `agents` 추가.
- **게이트웨이 잘림 추정**(`llm/gateway_provider.py`): JSON 구조를 요구한 호출에서만, 요청 상한 1024 이상이고 쓴 토큰이 상한(min(요청, `RECODER_GATEWAY_MAX_OUTPUT`=4096))에 닿으면 잘림으로 본다. 64 미만(연결 확인 ping)과 평문 응답(배포·운영 요약)은 예전처럼 잘림 오류를 내지 않는다. 게이트웨이(`gateway/src/common.py`)는 이제 `stop_reason` 을 돌려준다 — 다시 배포하면 추정 대신 그 값을 쓴다(배포하지 않아도 동작).
- **확장**: `ApiClient.generateCodeStream`(404면 예전 경로), `codeStream.ts`(유휴 120초만 제한, 끊기면 일시 정지로), 웹뷰 `TeamBoard`·`teamState`·`teamAnimals`(오리지널 SVG 6종 — 강아지·고양이·토끼·다람쥐·펭귄·판다, 겹치지 않게 무작위). 팀 모드를 꺼도 요청이 크면 코어가 자동으로 같은 엔진으로 전환한다.
- **검증**: 가짜 게이트웨이(4096 자르기·stop_reason 없음)로 실제 code-server UI 에서 파일 24개 쇼핑몰 생성 → 840줄 결제 파일 원본과 동일, 하드코딩 JWT 키 자동 교정, 모두 적용; 일일 한도 일시 정지 → [이어서 만들기] → 완료. Core 2,407개·확장 516개 통과. **실제 Haiku 생성 품질은 아직 측정하지 않았다**(테스트 키 필요).

# ECS 배포 진행 계약 (A3)

2026-09-23 기준. `core/schemas.py`의 추가 계약:

- `ECSDeployStep`: `key`, `label`, `status` (`pending`, `running`, `done`, `failed`, `cancelled`, `skipped`, `warning`), `started_at`, `finished_at`.
- `ECSDeployRecord.progress_steps`: 기본 빈 목록. 과거 기록을 불러올 때 존재하지 않는 단계 이력을 추정하지 않는다.
- `/api/deploy/ecs/status`: 기존 `running`, `stage` 토큰 계약을 유지한다. `stage_text`에 현재 실행 단계를 표시하고 `steps`, `error_detail`, `warnings`, `observed_at`을 추가한다. 기존 `service_url`도 확장 화면까지 전달한다.
- `deployment_id` 쿼리는 선택이다. 지정하면 해당 배포만 반환하고, 없으면 기존의 최신 배포 조회를 유지한다. 없는 ID는 404다.
- 단계 변경은 기존 ECS 기록 저장소에 저장한다. Core 재시작으로 중단된 배포는 실행 중이던 단계도 실패로 종료한다.
- 빌드 워커는 단계 이벤트를 Core 이벤트 루프에 전달한다. ECR 로그인·push 시작부터 업로드 단계로 표시한다. 진행 기록 저장 오류는 기존 배포 작업을 중단시키지 않는다.
- 웹뷰의 상태 조회 결과는 별도 ECS 진행 카드에 전달한다. 일반 배포 안내 메시지를 덮어쓰지 않으며, 요청 실패·조회 실패·배포 실패를 구분한다. 오래된 조회 응답은 완료 상태를 진행 중으로 되돌리지 않는다.

## 검증 범위

AWS와 Docker 동작을 대체한 파이프라인 테스트에서 단계 순서, 실패 위치, 취소, 생략, URL, 기록 복구를 확인한다. React 실제 렌더와 상태 전이, 호스트 전달 경로도 테스트한다. 실제 AWS 배포 검증은 별도로 수행해야 하며, 테스트를 위해 ECS 서비스를 자동으로 시작하지 않는다.

# 배포 이력 계약 (A2)

- `GET /api/deploy/history`: ECS와 로컬 배포 기록을 최신순으로 읽는다. `source=all|local|ecs`, `limit=1..500`. 환경변수와 원본 요청은 응답에 포함하지 않는다. 현재 AWS나 Docker에 접속하지 않는다.
- `POST /api/deploy/history/{source}/{deployment_id}/rollback`: `approved: true`만 허용. 로컬은 최신 컨테이너 기록과 실제 이미지 ID를 잠금 안에서 재검사하고, ECS는 기존 승인 대기 제안과 서비스 변경 검사를 재사용한다. 임의 과거 시점 복원은 제공하지 않는다.
- 로컬 실제 배포 라우트의 기록을 `~/.recoder/local_deployments.json`에 원자적으로 저장한다(0600). 이전 `projects/*_deployments.jsonl`도 읽되 새 스냅샷이 우선한다.
- `DeploymentRecord`에 `rollback_status`, `rollback_completed_at`, `rollback_error` 추가. 재시작 중단은 완료로 추정하지 않고 `unknown`으로 표시한다.
- 기존 로컬 기록에는 환경변수와 포트 등 복원 정보가 포함된다. 이력 API는 표시용 필드만 반환한다.

# 로컬 롤백 결과·감시 (B1)

- 로컬 롤백 응답에 `health_ok`(미검사 시 null), `health_check_url`, `restored_deployment_id`를 추가한다. 기존 `verification_resumed`와 함께 사용한다.
- Ship 화면은 롤백 응답의 배포 ID를 대조하고 복구된 배포의 감시를 조회한다. 늦게 도착한 실패 배포의 감시 응답은 무시한다. 헬스 미확인이나 감시 부재를 복구·감시 중으로 단정하지 않는다.

# 구조 지도 (B3)

- 파일 계층 안에서 화면 폭에 맞춰 노드를 줄바꿈한다. 많은 노드는 세로 스크롤로 확인하며 파일 이름 전체는 툴팁으로 제공한다.
- JavaScript의 인라인 화살표 함수·익명 함수도 정적 분석에 포함한다. 일반적인 Express 라우트는 HTTP 메서드·경로로 표시한다. 동적 호출과 복잡한 문법은 정적 분석의 한계가 있다.
- 새로고침 중에는 버튼을 잠그고, 식별 결과가 없을 때 실제 함수가 없다고 단정하지 않는다.

# ECS 비밀값 참조 (2026-10-08)

- `ECSDeployRequest.secret_refs`는 환경변수 이름 → Secrets Manager/SSM ARN 매핑이다. 태스크 정의의 `secrets`로 전달하며 원문 비밀값을 배포 기록에 넣지 않는다. `env_vars`/PORT와 중복되면 요청을 거절한다.
- 실행 역할에는 지정한 ARN에 대한 `secretsmanager:GetSecretValue` 또는 `ssm:GetParameters` 권한(고객 관리 KMS 키 사용 시 해당 키 복호화 권한)이 필요하다. 자동으로 사용자 IAM 역할의 권한을 늘리지 않는다.
- ECS 배포 캔버스·배포 센터의 앱 실행 설정에서 일반 환경변수와 비밀값 ARN을 JSON 객체로 입력한다. 확장 호환 API도 `env_vars`·`secret_refs`를 유지하며, 비밀값 참조의 잘못된 ARN·중복 이름을 거절한다. 실행 역할의 읽기 권한은 사용자가 구성한다.
- 요청한 Trivy·Hadolint·Gitleaks 검사가 실패하거나 보고서를 읽지 못하면 배포를 차단한다. 네이티브 도구가 없을 때는 기존 Docker 폴백을 사용한다.

## 실제 쇼핑몰 검증 (2026-10-08, 1.1.30)

- 기록은 `benchmarks/shop/validation-2026-10-08.json`에 있다. Core 2,382개·확장 504개가 통과했다. Linux에서 건너뛴 검사도 호스트 Docker에서 별도로 실행했다(OPA 정책 12개, Hadolint 템플릿, 모노레포 빌드).
- 생성 후 모델 수정과 수동 검토를 거친 참고 앱은 로컬 36개, 실제 AWS 26개, 재배포 데이터 유지 6개를 통과했다. 고의 ECS 헬스체크 실패와 이전 버전 롤백 후 결제 완료 주문 유지까지 확인했다. 임시 AWS 자원은 정리 후 조회로 삭제를 검증했다.
- **생성기 운영 준비 완료를 뜻하지 않는다.** 최신 코드의 새 Haiku 생성 결과는 malformed package.json, 존재하지 않는 `@stripe/js`, 없는 Docker COPY 경로로 설치·빌드에 실패했고 preflight에서 차단됐다. 수동 수정한 앱의 성공을 생성기 성공으로 계산하지 않는다.
- 결제사는 사용자 선택에 따라 HTTP 모의 서버를 사용했다. 실제 결제사 API·카드 UI·환불·정산·배송·운영 HTTPS·부하·백업 복구는 미검증이다. 확장 런타임 audit는 0건이나 개발/빌드 의존성에는 10건(High 8, Moderate 2)이 남았다.

# 진단·AWS 연결 표시 (B2)

- AI 진단은 설정된 primary 모델/Provider 기본값을 먼저 ping하고 Provider 후보로만 폴백한다. 카탈로그의 임의 모델은 사용하지 않는다. 화면에서는 진단 시 성공한 모델임을 명시하며 작업별 실제 모델로 단정하지 않는다.
- 사용하지 않는 ListFoundationModels 권한을 정책 생성기와 온보딩 템플릿에서 제거했다. 실제 계정의 기존 IAM 정책은 변경하지 않는다.
- 역할 모드는 AWS 상태 응답과 화면 모두 임시 키 끝자리를 숨긴다. credentials 파일의 기본 프로필은 자동 연결로 표시하고 저장 위치 설명을 구분한다.
- 허브 홈과 로컬 Docker 카드에서 자동 조치를 직접 실행한다. 조치 상태는 항목별로 관리하고 다른 항목·이전 진단 응답이 Docker 대기 버튼을 풀지 않는다.
