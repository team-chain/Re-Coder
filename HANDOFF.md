# 개발·배포·보안·설계 — 영상 18-09-09 대응 (2026-10-10, 2.0.7)

- 실기기: 결과 화면 "불러오는 이름 없음 2건"(getOrdersByAdmin·default×2·apiClient×5·OrderDetail), Docker "tsc: not found"(빌드 단계 `npm install --omit=dev`), 보안 게이트 "이상 없음 · 권고 2건"(DL3059), 설계 카드 2장(시작 방식·결제)뿐.
- 원인 추적(sandbox Docker 로 사용자 TEMP 재현): ① 실행 단계에 backend/package.json 없이 `npm start --workspace=backend` → No workspaces found ② express.static('frontend/dist') 작업 폴더 기준 ③ 테이블은 scripts/init-db.ts(tsx)로만 생성 → relation does not exist ④ main.tsx·App.tsx 이중 BrowserRouter → 빈 화면 ⑤ 제네릭 `apiClient.get<T>('/api/…')` /api 중복 ⑥ PLAN_SCHEMA maxItems 3 + 프롬프트 "1~3개".
- `docker_kit.py`: layout(서버 backend|server|api + start `node x.js`, 화면 frontend|client|web + vite/CRA outDir, cwd_root=작업 폴더 기준 화면 경로), render(빌드: 폴더별 `--workspaces=false` 설치·빌드·prune / 실행: apk upgrade·npm 제거·COPY --from·USER 1000·JSON HEALTHCHECK/CMD, hadolint 0건), runtime_breaks. 생성(`_apply_docker_kit`, fixed op), 배포 Dockerfile 생성 라우트(먼저), readiness DOCKERFILE_RUNTIME_BROKEN(자동 수정).
- `node_fixups`: build_stage_omits_dev, missing_imports(한 파일만 내보내는 이름, 지역 선언·인자·다시 내보내기 제외), missing_type_packages, nested_routers(+App 이 다시 감싸는 공급자). build_readiness: add_missing_export 에 라우터 기본 내보내기·axios 별칭, api_prefix_rewrite 제네릭·여러 줄, 이름 내보내기 따라가기, `missing_names_detail`.
- code_agent: `_name_mismatch_issues`(파일별·export 목록·쓰는 줄), `_autofix_ops` 재실행(새 파일·import 뒤), 같은 파일 한 패스에 한 번, fixed op 보호, round≥2 빌드 병행, 마지막 빌드 항상, edit_fix_round max_files 12.
- 배포: `local_services.ddl_from_code`(코드 문자열의 CREATE TABLE/INDEX/EXTENSION → IF NOT EXISTS, 런타임 코드가 IF NOT EXISTS 없이 만들면 None) → find_init_sql 폴백(~/.recoder/schema/<hash>.sql).
- 보안: `security_fix.lint_clean`(DL3019·DL3018·DL3008·DL3066·DL3059 `_merge_runs`·DL3025), DL3018 고칠 때 떨어진 예외 표시 제거, readiness 는 문제로 띄우지 않고 fix_data 만(생성 file_writes·배포 전 자동 정리), 게이트 라벨 "이상 없음", 남는 항목 제목 "참고".
- 설계: `_full_design_decisions` — 빈 프로젝트면 topics(5~8, `_TOPIC_SCHEMA`) → 3개씩 PLAN_SCHEMA 동시 3개 → id 맞춤, 실패 시 예전 한 번에.
- 검증: Core 2,547개·확장 555개(1건 sandbox git insteadOf). TEMP 원본을 API 배포: 배포 전 자동 수정 17건(이름·import·라우터·/api·@types·검증 Dockerfile) 뒤 남은 것은 AI 타입 오류 8건(빌드 진단). 타입 검사만 끈 사본으로 실행 확인: 검증 Dockerfile 빌드·데모 배포·DB 테이블 자동 생성·첫 화면 상품 5개·가입→장바구니→주문→결제 모듈→모의 결제 웹훅→paid. 가짜 게이트웨이 팀 생성 API: 설계 8장(주제 7+결제), 마지막 빌드 검증 통과.

# AI 앱 개발·배포·보안 — 실제 Docker 빌드 기준으로 고침 (2026-10-10, 2.0.6)

- 실기기 영상 16-48-26 + TEMP 쇼핑몰(AI 자유 생성)을 sandbox Docker 로 실제 빌드·배포해 원인을 순서대로 찾음: backend/·frontend/ package.json 없음 → lock 없는 npm ci → strict tsconfig 의 린트성 오류 30건·실제 타입 오류 ~10건 → frontend tsconfig references(tsconfig.node.json 없음) → 기본 내보내기 없음(.tsx 에 export interface 가 있어 `_esm_exports` 가 판단 포기) → `new URL('../../../frontend/dist')`(컨테이너에서 / 밖) → useApi 훅 무한 요청(6초 2,258번 → express-rate-limit 429) → AI 자체 결제(HMAC·mock-payment-server, 배포 모의 결제와 불일치).
- `node_manifests.py`(코드 import → package.json, KNOWN/DEV 버전표, terser), `node_fixups.py`(missing_manifests·npm_ci_without_lock(여러 줄 RUN, lock 조건문 제외)·tsconfig_missing_refs·vite_terser_missing·static_paths_outside(tsconfig rootDir→outDir 실행 위치)·relax_generated_tsconfig·build_log_issues(tsc/vite → 파일별, 스테이지 WORKDIR)·node_tsconfig·react_effect_loops(훅 최상위 마스크, useMemo deps=훅 인자+최상위 바인딩)).
- `build_readiness`: `fix_data["file_writes"][코드]` + `FILE_WRITE_FIXES`(apply_fix 가 apply_file_plan 으로 백업과 함께). 새 코드 NODE_WORKSPACE_MANIFEST_MISSING·NODE_TSCONFIG_REFERENCE_MISSING·NODE_VITE_TERSER_MISSING·NODE_STATIC_PATH_OUTSIDE_PROJECT·NODE_TSCONFIG_MISSING·NODE_REACT_EFFECT_LOOP·DOCKERFILE_NPM_CI_WITHOUT_LOCK·NODE_IMPORT_NAME_UNDEFINED(고칠 수 없는 이름 분리), NODE_UNDECLARED_DEPENDENCY 는 아는 버전이면 자동. check_script 가 `cd X` 추적, 하위 dist start 허용, `_built_entry_source` 로 포트, 서비스는 서버 쪽 import 로도.
- `code_agent`: `_autofix_ops` 가 루트+하위 package.json 폴더마다 file_writes 를 4번까지 반복 적용, 새 tsconfig 린트성 옵션 끔, `_build_failure_issues`(verify 의 `log` → 파일별), `_consistency_issues` 는 루트가 있으면 하위 단독의 `_ROOT_SCOPED`·중복 제외, `_complete_fullstack_manifest` 가 폴더 package.json/tsconfig 추가, 생성 규칙에 Dockerfile·훅·TS 지침. `_merge_ops`·`edit_fix_round` 는 `fixed` op 를 고치지 않음.
- 결제: `payment_kit.py`(TS_KIT·CJS_KIT, plan_with_kit=설계 직후 layer0 고정 op+약속 문서, AI mock 폴더 제외, wiring_issues). `gen_engine.LargeGeneration(plan_hook=)` → 고정 op 를 state.ops 에 넣고 file_done(planner) 이벤트. 한 번에 만든 결과에는 사후 주입. `payment_contract.issues` 는 모듈이 있으면 wiring 만.
- 배포: `_probe_app_api` 429 → APP_REQUEST_LOOP, `deploy_settings` 필수 설정 탐지(TS 타입 별칭·errors.push 후 exit), `build_failure._explain_ts`.
- 보안: `_created_uid` 옵션 위치 무관, DL3018/DL3008 근거 주석+`# hadolint ignore`(기존 pragma 에 덧붙임), file_registry 숫자 USER.
- 검증: Core 2,536개·확장 555개(RAG 포함, 확장 1건은 sandbox 의 git insteadOf 환경변수 때문 — 빼고 실행하면 통과). TEMP 산출물을 생성 직후 자동 교정 → 남은 정적 문제 1건(HealthResponse 미선언, AI 교정 대상) → 타입 오류를 AI 교정처럼 고친 뒤 Docker 빌드 성공 → API 배포: PostgreSQL 함께 뜸·DATABASE_URL 요청 없음·health OK, 무한 요청 수정 뒤 첫 화면 /api/products 1번. 결제 모듈을 붙인 같은 앱을 데모 모드로 배포 → createPaymentIntent → 모의 결제 서버 서명 웹훅 → 주문 paid. CJS 모듈은 ESM·CJS 양쪽 import·서명 검증(정상 200·위조 400) 확인. hadolint 실바이너리로 DL3018·DL3066 0건. 가짜 게이트웨이 팀 생성 API 로 결제 모듈 주입·약속·지목 확인.
- 남은 것: 기존 TEMP 결과물 자체의 타입 오류(약 10건)·API 응답 모양 불일치(products vs items)는 AI 교정 대상 — 2.0.6 으로 다시 만들면 빌드 검증이 파일별로 고친다.

# 대규모 생성 — 글자 그대로 이어 받기(응답 길이 한도에 막히지 않게) (2026-10-10, 2.0.5)

- 실기기: README.md 가 150→80→40줄 지시에도 매번 4096 토큰 한도에서 잘림(호출마다 ~22초 = 한도까지 씀) → 3번 실패 → [다시 쓰기]는 같은 40줄 방법을 반복해 바로 다시 실패. 잘린 JSON 응답은 통째로 버려 진행 0.
- `LLMRequest.raw_text` — gateway·api_key(anthropic/openai)·bedrock·gemini `converse(raw=True)` 가 JSON 추출·잘림 오류 없이 `{"text", "truncated"}`, `provider_router` 가 `resp.metadata["truncated"]`.
- `gen_engine.write_in_parts` 를 글자 그대로 이어 받기로 바꿈: 첫 요청/이어 쓰기 프롬프트(`END_MARKER`), `_stream_piece`(끝 표시·JSON 감싸기{content,done}/ops·말머리·코드펜스), 끊김(공급자 표시, 모르면 `STREAM_CUT_GUESS`자 이상)이면 마지막 불완전한 줄만 버림, `_strip_overlap`(끝 60줄 겹침 + 처음부터 다시 쓰기), 끊기지 않았는데 늘어난 게 없으면 다 쓴 것으로 봄, 진행 없음 3번이면 CodeOutputError. 로그 `kind=stream` 한 줄씩(받은·늘어난 글자·끝/끊김).
- 방법 단계(`STRATEGY_TEXT`): 한 번에(size=large 면 건너뜀) → 글자 그대로 이어 받기 → 맥락을 줄여(완성 파일 본문 없이) 처음부터. 받은 내용 `implausible()`(코드·CSS·HTML·JSON 으로 보이지 않으면) → 실패로 세고 다음 방법.
- 문서(`_is_doc`, ADR 제외)는 끝내 실패하면 `fallback_doc()`(요약·npm 스크립트·Dockerfile EXPOSE·process.env 이름, 지어내지 않음)로 완료, `fallback_docs` → 결과 `FallbackDocsNote`. 설계 스키마 files[].size, README 목적 문구 축소, 문서는 `_doc_hint` 로 짧게.
- 검증: Core 2,510개·확장 554개(RAG 포함). 가짜 게이트웨이를 "줄 수 무시·매번 한도까지·끝 줄 중간 끊김·앞 5줄 다시 쓰기"로 바꿔 code-server 팀 생성 — service.js(840줄)·README(520줄)가 원본과 글자까지 같게 완성. 응답이 AI 의 말뿐이면 3가지 방법 뒤 실패 패널 → [다시 쓰기](맥락 줄여서) 완료.

# 대규모 생성 — 망가진 조각·무한 이어 만들기 방지 (2026-10-10, 2.0.4)

- 실기기(사용자 체크포인트 3f89c434c07345af): 45/51 에서 [이어서 만들기] 가 매번 약 80초 뒤 같은 자리로 멈췄다. `partial["client/src/styles/pages.css"].written` 이 CSS 가 아니라 ops JSON 전체(1조각), 이어 만들 때마다 2번째 조각이 실패했고, 같은 묶음의 App.css 는 `_gen_batch` 가 한꺼번에 돌려주는 구조라 매번 버려졌다. 로그에는 원인이 남지 않았다.
- `gen_engine`: `_without_output_format(prompt)` → `context_prompt`(설계·약속·조각·교정·전체 점검 교정은 ops 형식 지시 없이), 묶음 호출만 원래 prompt. `clean_content/is_wrapped/unwrap_content`(닫히지 않은 JSON 도 content 문자열을 풀어 냄, 여러 파일 묶음이면 그 파일 것만, `\n` 글자로 들어온 한 줄은 되돌림 — .json 제외). 조각은 저장 전 검사, 3번 연속 형식 오류면 CodeOutputError. 조각 응답이 잘리면 같은 조각을 더 작은 크기로(`PART_LINES_STEPS`=150·80·40, partial 에 `lines` 저장).
- 파일 단위: `_run_single` — 시도 횟수(`state.attempts`, 체크포인트에 저장)에 따라 한 번에→150줄 / 처음부터 80줄 / 처음부터 40줄, `MAX_FILE_TRIES`=3 넘으면 `state.failed` 에 두고 나머지 계속. `_finish` 가 파일마다 즉시 저장. 묶음은 `_ask_files` 로 받고 검사 통과분만 저장, 나머지는 하나씩. 잘림·형식 외 실패(권한 등)는 바로 멈춘다(`_recoverable`).
- 끝: 실패 파일이 있으면 `GenerationPaused(failed=[{file,kind,reason}])`. `resume_from` 은 체크포인트를 다시 검사(`_check_saved`: 조각은 벗기거나 버림, 완성 파일도 검사)하고, 실패 파일은 가장 작은 조각·처음부터로 한 번 더(tries=2). `skip_failed=True`(API `skip_failed`, 이어 만들기일 때만)면 AI 를 부르지 않고 만든 것만 돌려주고 `code_agent` 가 `GENERATED_FILE_SKIPPED` 오류 + `skipped_files`.
- 관측: 이벤트 `part_retry`·`file_retry`·`file_failed`·`paused`(kind: truncation·format·quota·network·other), 코어 로그 `[gen_engine] job=… file=… kind=…`.
- 확장: `codeStream` FailedFile·GenerationPausedError(reason, failed), ApiClient `skipFailed`, 호스트 전달, `pausePanel.tsx`(실패 파일 목록 + [이 파일 다시 쓰기]/[이 파일 빼고 결과 받기], 그 밖에는 이유 + [이어서 만들기]) — TeamBoard·한 번에 만들기 공통. teamState `failed` 상태(완료로 세지 않음)·기록 문구, 마을 선반 "N개 못 만듦", file_split 기록은 실제 조각 크기.
- 배포: `_apply_settings_to_plan` 이 데모 불가 이유가 있으면 `plan.demo`(available=false)를 싣는다(2.0.3 은 빠뜨려 화면에 안 보였다).
- 검증: Core 2,506개·확장 553개(RAG 포함). 사용자 체크포인트를 그대로 불러와 가짜 AI 로 51/51 완료(pages.css·App.css 조각 정리). code-server 에서 가짜 게이트웨이로 (1) 첫 조각에 ops JSON → 정상 파일, (2) 조각 계속 실패 → 23/24 저장·실패 파일 패널 → [다시 쓰기] 완료, (3) [빼고 받기] → 남은 문제 상자. Docker 실제 배포: 결제 약속을 지킨 AI 형 앱 키 없이 데모 배포 → 주문 → 모의 결제 서버의 서명 웹훅 → paid, 모의 결제 없는 앱은 계획에 이유·설정 단계에서 멈춤, 쇼핑몰 기반(mock) 배포 → 가입·장바구니·주문 → paid, 예전 shop 재배포 성공. (샌드박스 Docker Hub 차단으로 node:22-alpine 은 로컬 대체 이미지 — dumb-init·curl 흉내를 더함.)
- 알게 된 것(미수정): 쇼핑몰 기반 `GET /api/orders/:id` 에 숫자가 아닌 id 를 주면 500(400/404 가 맞음). 검토된 기반(commerce-v1)이라 버전을 올려 고쳐야 한다.

# AI 앱 데모 결제 · 남은 문제 표시 · 문서 키 자리표시 · 미사용 파일 · 팀 화면 정리 (2026-10-10, 2.0.3)

- 실기기 영상(2026-10-10 11-07-37.mkv): AI 자유 생성 쇼핑몰이 Stripe 키 2개를 요구하는데 데모 버튼이 없었고(모의 결제는 기반만 지원), `CartPage.jsx` 의 `useCart` 를 `cartStore` 가 내보내지 않는 채로 적용됐고, README 397줄의 예시 키를 생성 검사는 놓치고 보안 게이트(gitleaks)가 잡았다. orderApi·paymentApi 는 참조 0. 팀 화면은 대기 말풍선 겹침·점검 중 완료 수 감소·이름표 붙음·카드 덩어리.
- 코어 `payment_contract.py`: `applies(요청)`(결제 의도 + 다른 결제사 미지정), `PLAN_NOTE`, `code_contract(mock|keys)`, `issues(ops)`(PAYMENT_MOCK_CONTRACT_MISSING · PAYMENT_WEBHOOK_MISSING · PAYMENT_DEMO_NO_LISTEN — 고칠 파일을 꼭 지목). `generate_plan` 은 새 프로젝트+결제 요청(또는 after_starter=custom)이면 AI 결정 뒤에 `commerce-payment` 를 붙이고 AI 가 만든 결제-모드 결정은 뺀다(상한이면 자리 확보). `generate_code` 는 AI 경로에서 계약을 프롬프트에 붙이고 `_issues_for` 로 일관성 점검에 넣는다. 결제 선택 기억은 두 경로 공통(대상 폴더 기준). 계약대로 만든 서버를 실제 모의 결제 서버(MOCK_PAYMENT_JS)와 붙여 주문→서명 웹훅→결제 완료를 확인했다.
- `deploy_settings`: `demo_supported` 는 루트 또는 한 단계 아래 package.json, `evaluate().demo.unavailable_reason`(Stripe 키를 요구하는데 데모 불가일 때).
- `build_readiness._alias_twin` + `add_missing_export` 별칭(꼬리 Store·State·Slice·Service·Api, 대소문자) — 짝이 딱 하나일 때만. 내보내기 누락은 확실한 것만이라도 `fix_data` 에 담는다(전부 확실할 때만 `auto_fix`).
- `security_scan.PROVIDER_SECRET_PATTERNS`(두 스캐너 공통), `is_doc_like`·`redact_doc_secrets`(엔트로피 3.5·10자 이상 — gitleaks 일반 규칙 기준). 생성 결과의 문서는 자리표시로 바꾸고, 문서에는 컨텍스트 비밀을 되돌려 넣지 않는다. 코드에 남은 critical/high 키 → `GENERATED_SECRET_IN_FILE` 오류. `security_fix` 는 문서 gitleaks 결과에 `doc` 자동 수정(백업 표시 "[자리표시로 바꿈]"), 권고 Dockerfile 항목 한국어 설명.
- `unused_files.py`: route(서버 미등록 API → `SERVER_ROUTE_NOT_MOUNTED` 오류, 서버 진입 파일 지목) / module(같은 API 를 부르는 화면에 `edit_fix_round` 로 한 번 연결, 새 오류면 되돌림). 파일 기반 주소 프레임워크(next 등)의 pages·app·routes·api, readdirSync 로 불러오는 쪽은 판단하지 않는다. 결과 `unused_files`.
- 확장: `codeIssues.tsx`(RemainingIssues·applyLocked·UnusedFilesNote·filesToApply), `CodeAgent` 적용 잠금·미사용 제외·배지, `DeploySettingsPanel` 데모 불가 이유. `teamState` 파일 `polish`·`by`, `polishingCount`. `TeamVillage`: 높이 392, 말풍선 두 줄 엇갈림(`bubbleMax`=2×간격−10, 가장자리 `bubbleShift`), `WAIT_TEXT`, 카드 집기 차례(420ms 간격·두 자리), 돌아오는 동안 말풍선 숨김, 완성 카드는 파일 상태 전이로 맡은 작업대에서 출발해 `shelfSlot` 으로(최대 4장, 같은 작업대 180ms 간격), 선반 숫자 = 완료 − 날아가는 카드, 잠긴 칸 표시는 칩.
- 검증: Core 2,489개·확장 550개 통과(RAG 포함). code-server 에서 개발 6명 팀 생성을 폭 1440·640 으로 0.5초마다 말풍선 겹침·잘림 측정 0, 점검 중 "5/5 1개 다듬는 중", 남은 결제 계약 오류 2건 상자·적용 잠금, 미사용 파일 제외 확인.

# 설계 결정 이어서 묻기 · 진행 확인 창 (2026-10-10, 2.0.2)

- 실기기 지적: 쇼핑몰 요청은 `/api/code/plan` 이 AI 를 부르지 않고 시작 방식 카드 1장만 냈고(0.05초), AI 자유 생성을 골라도 추가 결정 없이 생성했다.
- 코어: plan 응답에 `followups = {결정 id: {선택 key: [결정…] | "ai"}}`(commerce_starter.followups). `POST /api/code/plan {after_starter: "custom"}` 은 기반 카드를 건너뛰고 기술 결정 2~3개를 AI 에게 받는다. 기반 + `commerce-payment`(mock/keys) 선택은 `deploy_settings.remember_payment_choice(폴더)` 로 기억 → `evaluate` 가 그 폴더 첫 배포의 데모 여부로 쓴다(`demo_at` 보다 새 선택일 때만 — 배포 화면에서 직접 바꾸면 그쪽이 이김). 저장 위치 `~/.recoder/deploy_settings/_workspace_prefs.json`.
- 확장: `decisionFlow.ts`(collapseChanged·pendingFollowup·insertFollowups·isConfirmOnly — 순수 함수), `CodeAgent` 결정 창이 [다음] 때 이어 붙이고, "ai" 면 `code.planFollowup` → 호스트가 `code.followupResult/Error` 로 답한다(일반 plan 오류처럼 턴을 실패로 바꾸지 않음). `__` 예약 id 확인 카드 하나뿐이면 진행 확인 창.
- 검증: Core 2,461개·확장 541개 통과(RAG 포함). code-server 에서 쇼핑몰 요청 → AI 자유 생성 → AI 결정 이어짐 → [이전] → 기반 → 결제 시작 결정 → 생성(파일 61개, ADR 2건) → 폴더 기억 mock → 배포 설정이 데모로 시작(키 입력 없음) 확인.

# 팀 작업 화면 · 승인 카드 실행 순서 · Dockerfile 미리보기 · 공통 체크박스 (2026-10-10, 2.0.1)

- 실기기 영상(2026-10-10 02-34-18.mkv): 기존 Dockerfile 로 배포를 고르면 미리보기가 "생성 버튼을 누르면…" 으로 비었고, 승인 카드 첫 사유가 영어, 명령 미리보기가 실제(DB·모의 결제·설정값)와 달랐다.
- 코어: `local_services.service_run_args`·`deploy_settings.mock_run_args` 로 docker run 인자 조립을 한 곳으로 모으고 실행과 미리보기가 같이 쓴다. `deploy._command_steps(plan, workspace)` → `DeploymentPlan.command_steps=[{command, note}]`(값 `***`, 비밀 아닌 고정값만 노출). `_apply_settings_to_plan` 끝에서 첫 위험 사유를 지금 포트로 다시 쓴다(`deploy_agent.local_port_reason`). `/api/deploy/settings` 응답에도 `command_steps`.
- 확장: `ApprovalModal` 한글 + `commandSteps`. `ShipMode` 는 초안이 없으면 `infra.readWorkspaceFile`(프로젝트 폴더 안만, 300KB 이하)로 루트 Dockerfile·docker-compose.yml 을 읽어 보여 주고 `infra.openWorkspaceFile` 로 연다.
- `Check.tsx`(Checkbox·Switch, 스타일은 head 에 한 번, 캔버스 입력창 CSS 보다 우선). Discord 패널은 기존 스위치 유지.
- 팀 작업: `teamState.recordOf/agentName` → `TeamView.records`(최근 30). `TeamVillage.tsx`(`villageLayout` 순수 함수, 폭 520px 이상, 820px 이상이면 옆에 진행 기록). **엔진 사실**: 개발 슬롯은 같은 프롬프트의 동시 호출 자리이고, 파일별 교정은 작성한 슬롯이 하며, 검토(review) 에이전트는 마지막 전체 점검(edit_fix_round)에만 있다 — 화면도 그대로 보여 준다. "AI Village" 수준의 에이전트 간 상호작용(공용 게시판 메모·약속 문의·단계별 검토 회신·역할 기억)은 아직 없다(제안만, 승인 전).
- 검증: Core 2,458개·확장 537개 통과(RAG 문서 근거 수정 29+3 포함, 변경 없음). code-server 에서 가짜 게이트웨이로 팀 생성(개발 4명) 마을·진행 기록, 기존 Dockerfile 유지 → 미리보기 "워크스페이스 파일 사용 중", 승인 카드 5단계 명령·한글 확인.

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
