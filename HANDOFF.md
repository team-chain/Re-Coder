# 출시 점검 2차 수정 (2026-10-15, 2.0.33)

- 2차 감사 3개(보안 리뷰 / 코어 레거시 / 주변 도구). tests/test_dev_2033.py(65), webview-tests/productionAudit 1건, discord-bot test_bridge_security 확장, gateway 단가 고정.
- 생성: code_agent 가 split_mode 와 무관하게 결과 체크포인트 저장(result.job_id 항상). generation_jobs.save 는 mkstemp(0600)+replace 재시도. code_output.safe_rel_path(FORBIDDEN_DIRS .git·.vscode, 절대·드라이브·..) — gen_engine._merge_files·code_agent ops 필터. 확장 handleCodeApply 도 .git/.vscode/드라이브 거부. restore_neutralized 는 표시 수 == 뺀 줄 수일 때만 복원, 파일 키는 앞 "./" 만 제거(lstrip 버그).
- 로컬 배포: deploy._volume_args(image inspect .Config.Volumes → -v <컨테이너>-data-<경로>:<경로>, /tmp·/run·/var/cache 제외) — 배포·복원·롤백(롤백은 409 확인 뒤). _halt_crash_loop(되살릴 것 없을 때 update --restart=no + stop). local_deploy_store._keep_corrupt_copy, save replace 재시도. local_services._save → state_io(0600), ddl_from_code 는 os.walk 로 _SKIP_PARTS 가지치기.
- 실제 Docker E2E(sandbox, node 이미지): 배포 → 재배포 → 롤백에서 127.0.0.1 바인딩·unless-stopped·이름 있는 볼륨·데이터 유지, 첫 배포 즉시 종료 시 컨테이너 남김 + 재시작 끔 + 멈춤 확인.
- GitHub: repo_create_and_push — 이미 있는 저장소가 공개 범위 다르거나 size>0 이면 conflict(이 폴더 origin 이 그 저장소면 허용), branch -M 제거, 다른 origin 은 conflict, push HEAD:refs/heads/<기본>. gitops_agent 는 extraheader(그 호스트만) + stderr 토큰 가림, rstrip(".git") 버그.
- 보안: ecs_agent DB 비밀 remedy = arn:<p>:secretsmanager:<region>:<acct>:secret:<secret_name>-*, 정책 이름 recoder-db-secret-<service>-<region>. main._ensure_ca_bundle 은 기본 저장소가 있으면 그대로, 없으면 _SYSTEM_CA_FILES → certifi. _is_loopback_host(ipaddress). grounded_repair 키 짧으면 원자적 재생성, 실패 시 실행 한정 무작위 키(무키 서명 없음). dockerfile_guard: 파이프 단계 분석(_command_word 가 sudo·env·busybox·xargs·tee 통과), _inline_runs_stdin(-c 코드에 exec 없으면 데이터 읽기), 다운로드 대상(-o·-O·> ·curl -O 이름), _VERIFIED(sha*sum -c·gpg --verify·cosign), PATH 폴더 이름 실행, RUN 사이 fetched 추적, 인라인 fetch+exec, _secret_in_payload(-d·-F·-T·비인증 헤더), 비밀 마운트는 그 경로만 제외. rollback_policy paramiko RejectPolicy.
- 주변: discord-bot api_server._same_machine_client(Origin http(s) 거부, 무키면 Host 루프백만, 본문 있으면 application/json). guild_config.db·bridge_settings.json·policy.json 추적 해제 + .gitignore. preflight-action README 데모 토큰 제거. ci.yml workflow_call + release.yml tests 잡(publish.needs). watchdog install.sh venv(/opt/recoder/venv, python3-venv), unit Wants=docker. build-release notices: CPython(stdlib LICENSE), OpenSSL, npm(react·react-dom·scheduler·three·ws). paramiko 는 RUNTIME_IMPORTS 에서 빼고 spec excludes. darwin 잠금 macholib. control_plane 기본 127.0.0.1(CONTROL_PLANE_HOST), otel compose 포트 127.0.0.1. gateway·control_plane requirements ASCII. 테스트의 실제 계정 ID → 210987654321.
- 남은 위험: 코드 서명 없음(Windows SmartScreen·macOS Gatekeeper 경고), 91MB pptx 추적 중(히스토리 정리는 사용자 결정), 루트의 bedrock_test.py·show_policy.py·restart-core.bat, taskkill PID 재사용(짧은 창), CloudFront→ALB 원본 구간 HTTP(비밀 머리글이 평문 — ALB HTTPS 리스너가 필요), 생성 취소 API 없음(끊겨도 끝까지 가고 결과는 저장), sandbox 의 node:22-alpine 은 진짜 alpine 이 아니라 test_monorepo_deploy 1건 실패(환경).
- 봇 상태 파일 추적 해제: pull 하면 작업 폴더의 discord-bot/guild_config.db·bridge_settings.json 이 지워진다 — 쓰고 있었다면 pull 전에 복사해 두고 다시 넣을 것(이후는 .gitignore).

# 출시 점검 수정 (2026-10-15, 2.0.32)

- 영역별 감사 4개(AWS 배포·운영 / 크로스 플랫폼 / 보안 기능 / 확장↔코어 계약) + 리눅스 릴리스 빌드(build-release.py + release-smoke.js, 전부 PASS). tests/test_dev_2032.py, tests/aws_pattern_guard.py(봇코어 모델의 pattern·길이·enum 대조 — test_aws_policy 런타임 픽스처 전체에도 적용).
- AWS: WAF_DESCRIPTION(ASCII). HttpsRequestCertificate(Resource *, RequestedRegion us-east-1 + RequestTag recoder:managed), request_certificate 는 태그 있을 때만 Tags. DiscoverDefaultNetwork += DescribeInternetGateways·AccountAttributes·Addresses(PINNED). shield.python_reads_port(CMD/ENTRYPOINT 의 $PORT, gunicorn --bind 없음, python 파일의 environ PORT). _check_db_secret_access(production+RDS, simulate_principal_policy 로 실행 역할 GetSecretValue — denied 면 RDS 만들기 전 InfraError + put-role-policy 명령, 확인 불가면 안내만). comment_for(name, region)=recoder:<name>@<region>, 옛 표시는 원본 ALB 리전이 같을 때 채택·재표시, CallerReference 무작위 꼬리. global_acl_name(name, region). 정리: CloudFront 는 item_region, waf_cdn 은 이름 끝 리전(옛 이름은 이 리전 배포가 쓰는 것만), 인증서는 recoder:region 태그(옛 것은 이 리전 배포 별칭과 같은 도메인만). _note_leftovers(edge none·https 끔). aws_rds.wait 가 stopped 면 start_db_instance(+rds:StartDBInstance). ecs_runtime 줄이기 기준 56K/59K, recoder-db-init 도 SQL 빼고 통째로 제거. list_certificates 페이지, _zone_for 공개 영역 선택.
- 권한 점검: AwsDeploymentPermissionContext 에 edge·https·custom_domain·database, _protection_simulations(대표 액션 + 실제 이름 ARN). 확장 DeploymentCenter 가 protect 값을 보냄.
- 보안: code_security(_SIGNUP_PATH 에서 join·/:param/register 제외, 외부 인증 SDK 인정, 저장 증거 없으면 medium, _safe_sql_expr, 호출 앞 2,500자에서만 요청 이름, JWT 템플릿 ${} 제외). build_readiness._non_registry_spec + 모든 package.json(≤20)·로컬 패키지 이름 제외. grounded_repair.store HMAC(h1:, repair_store.key 0600) + 적용 전 확인·지문 없음 거부. dockerfile_guard 재작성(heredoc·이음 중 주석, TRUSTED 경로 단위, TOKEN_HOSTS, 비밀 마운트, 읽기만 비밀, 다운로드 후 실행, ADD URL 실행, env 덤프). context_gate(문자열 안 /* 무시, system prompt 패턴 제거, prompts 파일 제외, raw 보관) + code_agent._restore_neutralized_in_ops, prompt_guard 에서 raw 제거. vuln_recheck(SEMVER/ECOSYSTEM 만, bump_requirement·PEP503·extras, BOM·CRLF 보존, 못 알린 것 재알림, 상태 모양 검사, initial → "배포된 버전의 알려진 취약점", _STATE_LOCK). ops_alerts(state_io, _claim/busy·open 아니면 거부, _done_suggestion 으로 제안별 적용, cursor_known → AlertList.cursor_reset, 확장 watcher 는 reset 이면 최근 것만). mcp 감사에 없는 도구·실패.
- 환경: main._ensure_ca_bundle(frozen 이거나 기본 CA 없으면 certifi), _LoopbackDirectProxyHandler(전역 opener), /api/shutdown 은 Windows 에서 signal.raise_signal. state_io(파일별 RLock, mkstemp, Windows PermissionError 재시도, utf-8-sig 읽기). proc_tools.run(손주까지 종료, 시험 monkeypatch 존중) → npm·docker build·chrome 호출. deployment_inputs.port_publish(기본 127.0.0.1, RECODER_LOCAL_BIND). 작업 폴더 쓰기 newline=""(CRLF 원본 유지), package.json utf-8-sig. 확장: CoreManager Windows 종료는 /api/shutdown → 대기 → taskkill, StringDecoder, frontendBuild 의 docker 는 인자 배열·리눅스 chown·프로세스 나무 종료, TerminalCollector chcp 65001, 정리 패널(자유 리전·배포 리전·작업 다시 붙기·refreshError), act 시간 제한 600초, 목록 갱신 오류가 승인 버튼을 다시 켜지 않음, S3 스트림 10초 심장박동, 죽은 gitInfo 제거.
- 정리 작업 기록: RECODER_HOME/cleanup_jobs.json, 시작 시 running → interrupted. GET /api/aws/cleanup/regions, /jobs.
- 2차 검토(회귀) 반영: dockerfile_guard — _runs_file 은 셸·인터프리터 인자 또는 명령 자리만(tar·chmod 는 실행 아님), _SUBST_SH 는 sh -c "$(curl…)"·eval·<(curl…) 만, .env 는 읽는 쪽(첫 인자)이고 *.example 이 아닐 때만, helm 설치 스크립트 신뢰(제품이 내보내는 Dockerfile 112개 무오탐). 권한 점검 — CreateRule·HTTPS+WAF 의 전역 CreateWebACL 은 막지 않고 안내(OPTIONAL_PROTECTION_ACTIONS, 2.0.31 의 대체 경로가 있음). 옛 이름(리전 없음) CloudFront ACL — 배포에서 새 이름이 퍼졌거나 WAF 를 끄면 지우고, 정리는 다른 리전 배포가 쓰지 않으면 어느 리전에서나 보임(인증서도 같은 규칙). frontendBuild chown 은 docker info SecurityOptions 로 rootful 확인 때만. 지시 줄 복원 → 비밀 복원 순서. agents/code_agent 패치도 CRLF 유지. 헬스 체크 주소 127.0.0.1, 남은 docker run -p(deploy.py 2곳·rollback_policy·local_deploy_agent)도 port_publish. python_reads_port 는 CRLF·WORKDIR 절대 경로 처리, 방패 끔 사유에 파이썬 안내. repair_store.key 는 O_EXCL 로 한 번만 생성.
- 남은 위험(코드로 못 닫은 것): 실제 AWS 실행 미확인(WAF·CloudFront·ACM·RDS·원본 잠금), Windows·macOS 실기기 미확인(CI 첫 실행 확인), OSV 실응답 미확인(프록시 차단), RECODER_HOME 을 바꾸면 runtime.json 은 ~/.recoder 에 남음, rootless Docker·Podman 의 소켓 경로, 젊은 패키지 차단을 확장에서 풀 방법(RECODER_TRUSTED_SCOPES 환경변수뿐), src/api/coreClient.ts 의 옛 엔드포인트(가짜 서버 스모크 전용).

# 검토 결과 수정 (2026-10-14, 2.0.31)

- 리뷰(서브에이전트) 확인 5건 + 개연 8건. tests/test_dev_2031.py 35건, webview-tests/cleanupJob.test.js 4건.
- aws_edge: WAF 함수에 scope(REGIONAL|CLOUDFRONT). ensure_edge(cdn=True) 는 지역 WAF 를 만들지 않고, waf 를 끈 경우만 바로 detach. ecs_agent._provision_cdn 이 wafv2_global(us-east-1) 로 CLOUDFRONT 웹 ACL → ensure_distribution(web_acl_id) → 그 뒤 지역 ACL detach(보호 공백 없음). WAF 끄면 web_acl_id="" + 전역 ACL 삭제 시도(연결 중이면 안내).
- 원본 잠금: aws_cdn 이 Origin CustomHeaders X-Recoder-Origin(token_urlsafe 32, 기존 값 재사용), CdnResult.origin_secret_live(새 배포 또는 이미 머리글 있음). live 일 때만 aws_edge.lock_origin(규칙 우선순위 10 http-header → forward, 기본 동작 fixed-response 403). ensure_edge 는 잠긴 리스너면 규칙 대상만 맞추고, cdn 아님 + 잠김이면 unlock_origin(기본 forward, 규칙 삭제). 잠기지 않은 평소 재배포는 DescribeRules 를 부르지 않는다(옛 권한표 호환). 잠금 실패(권한)는 배포 실패가 아니라 rec.provisioned["origin_lock"] 안내.
- alb_subnets: internet_routable_subnets(igw 경로) 로 공개 판정, 못 보면 MapPublicIpOnLaunch. 태스크 영역은 모두 포함하되 영역마다 공개 우선, 다른 영역 3개까지. associate 36회(3분), delete_web_acl 은 WAFAssociatedItem·OptimisticLock 재시도, detach 실패는 안내만.
- aws_cdn: IdempotencyToken=sha1(domain)[:32], 검증 레코드 UPSERT 실패 → 안내, point_domain 은 list_resource_record_sets 로 확인 후 cloudfront.net 별칭이 아닌 기존 A/AAAA 는 덮어쓰지 않음(안내 반환), disable 시 WebACLId 비움. ecs_agent 가 point_domain 예외를 안내로.
- aws_rds: _secret 에서 InvalidRequestException → describe_secret DeletedDate 있으면 restore_secret(실패 시 InfraError, remedy RestoreSecret). delete_instance 는 BUSY 상태면 DbBusy.
- shield.current_settings: RECODER_SHIELD_PROXY 가 없으면 CONF_B64 를 풀어 cloudfront_viewer_address→cdn, real_ip_header→vpc.
- aws_cleanup: wafv2_global 클라이언트(GLOBAL_CLIENTS), kind waf_cdn(ORDER cloudfront 다음), ACM ResourceInUse → _Kept, rds DbBusy/InvalidDBInstanceState → _Kept, 15분 뒤에도 남아 있으면 _Kept, DB 를 못 지운 서비스의 subnet/param/secret 은 건너뜀, S3 delete_objects Errors → 실패, on_progress.
- API: DeleteRequest.background → start_job(스레드, 같은 리전 실행 중이면 409, 최근 20개 보관), GET /api/aws/cleanup/jobs/{id}. 동기 경로도 실행 중 작업 있으면 409. 확장 ApiClient.cleanupDelete 는 background + waitForCleanupJob(3초, 조회 연속 5회 실패·2시간이면 중단), 패널에 aws.cleanup.progress.
- 권한: EdgeWaf 에 global/webacl/recoder-*/*(us-east-1), EdgeWafRead 리전 조건 [리전, us-east-1], EdgeOriginLock(Create/Modify/DeleteRule), EdgeOriginLockRead(DescribeRules, 리전), HttpsDomainRecordsRead(ListResourceRecordSets), ManagedDatabaseSecret += DescribeSecret·RestoreSecret. 런타임 증거 픽스처·FLOOR 갱신. infra/recoder-iam-quickcreate.json 재생성(정책 3개 유지).
- 2차 리뷰 반영: 옛 권한표로 HTTPS+WAF 재배포 시 AccessDenied 면 2.0.30 처럼 ALB 에 지역 WAF(안내) — 멈추지 않음. origin_secret_live·web_acl_live 는 배포 전 Status=Deployed 일 때만(InProgress 중 재배포로 잠그거나 지역 WAF 를 떼지 않음). 머리글 값이 바뀌고 리스너가 이미 잠겨 있으면 widen_origin_lock(옛+새 값 OR, 다음 배포에서 새 값만). 규칙 우선순위는 10부터 빈 번호. WAF 끔 시 사용자가 붙인(recoder- 아닌) WebACLId 는 그대로. point_domain 은 ListResourceRecordSets 권한 없으면 예전처럼 UPSERT. 동기 /delete 도 start_job 으로 등록 후 기다림(겹침 409 한곳). 확장은 옛 코어의 즉시 결과도 받음. test_dev_2031 35건.
- 실제 AWS 로는 확인 못 함: CloudFront WebACLId 연결에 추가 권한이 필요한지(문서상 UpdateDistribution 으로 연결, 우리 정책은 global ACL 에 GetWebACL 포함), 원본 잠금 전파 시간, RestoreSecret 직후 GetSecretValue.

# 환경 안정화 (2026-10-14, 2.0.30)

- ci.yml: test 잡을 matrix(ubuntu·windows·macos × py3.11 + ubuntu py3.13, fail-fast false, AWS 가짜 키·메타데이터 끔), webview 잡 ubuntu·windows. 실제 Windows·macOS 결과는 sandbox 에서 볼 수 없다 — 첫 CI 실행 결과로 남은 OS 전용 문제를 확인할 것.
- 로캘: sandbox 에 ko_KR.EUC-KR 로캘을 만들어(PYTHONUTF8=0, TZ=Asia/Seoul) 전체를 돌림 — 33건 실패 중 16건은 테스트의 로캘 기본 인코딩(read_text/write_text/subprocess text=True) → 코드모드로 262곳 encoding="utf-8"(바이트 오프셋 기준 AST 삽입) + 재발 방지 테스트(test_tests_read_and_write_text_as_utf8_on_any_locale). 남은 17건은 리눅스 비 UTF-8 로캘에서만 생기는 것(프로세스 인자·한글 경로 바이트를 EUC-KR 로 넘김 — git·node 는 UTF-8 로 해석). Windows 는 인자·경로가 UTF-16 이라 해당 없음. 그래서 리눅스 로캘 잡은 CI 에 넣지 않았다.
- Python 3.11 깨끗한 venv(uv, requirements + requirements-dev): @pytest.mark.asyncio 테스트 2건이 플러그인 없어 실패 → asyncio.run 으로, 재발 방지 테스트.
- scripts/golden_path_smoke.py: generate_plan_topics 픽스처({"topics": []} → 제품의 한 번에 받기 경로), 기대 호출 갱신 — develop 에서도 실패하던 것(bb56def 에서 재현).
- node_fixups re.split 위치 maxsplit → 키워드. test_grounded_repair 기호 링크(WinError 1314 건너뜀), test_dev_2016 의 sh 기반 3건 POSIX 전용.
- 로컬 확인: core 2831(3.11·3.13), extension npm test 574 + build + verify:package, watchdog 65, discord-bot 112(py3.12 venv), golden path, npm audit 4곳·pip-audit 3개 잠금 파일 0건. commerce compose 잡은 레지스트리 차단으로 sandbox 에서 못 돌림.

# ReCoder 자신의 보안 — OWASP AI 에이전트 위험 (2026-10-14, 2.0.29)

- ASI01 context_gate.neutralize_instructions(path, content): 문서(.md/.txt/.html/README·notes 등)는 모든 줄, 코드는 주석 줄만 — 패턴(이전 지시 무시 영/한, 역할 바꾸기·시스템 프롬프트·특수 토큰, curl|wget … | sh, 비밀값 + 외부 URL 전송)이 걸린 줄을 NEUTRALIZED 로 바꾸고 {line, kind, text(마스킹)} 반환. scrub_file_entry(removed=) → code_agent._scrub_context(…, removed) → result["prompt_guard"], 요약 문장. _build_code_prompt 에 DATA_NOTICE(파일은 데이터) — 참고/직전/열린 파일이 있을 때만.
- ASI02·05 core/dockerfile_guard.py: 논리 줄(\ 이음) 단위, RUN --security=insecure·--network=host, curl|wget → sh/python/node(TRUSTED_INSTALLERS: nodesource·rustup·poetry·astral·bun·pnpm·get.docker.com·nvm·deno 만 예외), 비밀 파일(.aws/.ssh/.npmrc…), curl/wget/nc + 비밀 env + URL, docker.sock·/proc/1|self·/host·/etc/shadow, COPY/ADD .aws/.ssh. 적용: build_readiness(DOCKERFILE_UNSAFE ERROR, 자동 수정 없음), generated_validation(빌드 전 status failed + guard), code_agent._issues_for(Dockerfile op → 교정 오류), ecs_build.build_image(BuildError, docker 안 부름), local_deploy_agent.build_image(False). docker_kit 검증본·starter·file_templates 는 통과(테스트).
- ASI03 AwsStatus.security_warnings(model_validator: identity.arn 이 :root 로 끝나면) — 모든 생성 경로에 자동. 확장 AwsConnection 빨간 경고.
- ASI06 grounded_repair.store.integrity(id·workspace·manifest·status·edits·corpus_version·strategy·sources 해시) 저장, cached 는 지문이 맞을 때만, approve 는 바뀌었으면 거부(지식 문서는 이미 공식 주소·검토된 라이선스만).
- ASI09 ChatPanel 승인 카드: 요청(실제 instruction)·스택·파일 + "AI 설명"(요약, 기울임).
- ASI07 mcp_server._audit: RECODER_HOME/mcp_calls.jsonl 에 time·tool·argument_names·ok(값 없음).
- tests/test_dev_2029.py 10건. 전체 2829 통과.

# AI 코드 보안 점수 · 배포 후 취약점 재검사 (2026-10-14, 2.0.28)

- core/code_security.py: scan(files) → (Finding[], applicable). 규칙 SQL_INJECTION(.query/.execute/.raw/$queryRawUnsafe 의 템플릿·문자열 연결, req. 또는 req.body/query/params 에서 꺼낸 이름이 들어가면 precise high — Prisma $queryRaw 태그 템플릿은 안전), JWT_UNSAFE(none·리터럴 비밀값 precise, `|| '기본값'` 은 medium), CORS_WILDCARD(origin '*'/true + credentials precise), PLAINTEXT_PASSWORD(가입 라우트·users INSERT 에 password 가 있는데 bcrypt/argon2/scrypt/pbkdf2 가 없으면 precise), MISSING_AUTH(앱에 인증이 있을 때 POST/PUT/PATCH/DELETE 에 인증 미들웨어 없음 — 공개 경로·서명 웹훅·mock·app.use(prefix, auth, router) 제외), OWNER_CHECK, XSS_HTML, BRUTE_FORCE(로그인에 제한 라이브러리·직접 만든 limiter·계정 잠금(failed_login 등) 없음), ERROR_LEAK. 점수 = 통과 가중치/적용 가중치(high3·medium2·low1), 적용 규칙 없으면 None. 주석은 같은 길이 공백으로 지워 줄 번호 유지.
- 오탐 측정: commerce 기반 100점(로그인 잠금 인정), /tmp/g1(AI 생성 앱) 100점(웹훅·mock 제외 후), 테스트 픽스처 전체 2819 통과(교정 루프에 끼어든 오류 없음).
- code_agent: _issues_for 에 code_security.issues(errors_only=True)(새 앱만) → 교정 루프. 결과 result["security"] = report, 요약에 "AI 코드 보안 점검 N점", 진행 _report step=security. 확장 codeIssues.SecurityScoreNote.
- core/vuln_recheck.py: deployments(배포 기록 중 서비스별 최근 succeeded + SBOM 파일 + request.workspace_path), purls(CycloneDX 의 npm/pypi/maven/golang/cargo/gem/nuget/composer, 중첩 components 포함), query(OSV querybatch 1,000개씩), fixed_version(같은 생태계·이름의 ranges 중 현재보다 큰 가장 작은 fixed), severity(database_specific.severity), run(state RECODER_HOME/vuln_recheck.json: known[id|purl] found/notified/fixed/fix_applied, 새 것만 ops_alerts._store kind=vuln + upgrade 제안 최대 3), apply_fix(npm: package.json 직접 의존 ^to, 없으면 lock 있는 package.json overrides, npm 있으면 --package-lock-only; pypi: requirements*.txt name>=to), Rechecker(시작 2분 뒤, 24시간마다; RECODER_TEST_MODE·RECODER_VULN_RECHECK=0·RECODER_OFFLINE=1 이면 끔). main lifespan 에 시작·종료.
- ops_alerts.act: upgrade 는 제안한 값과 같을 때만, AWS 를 부르지 않고 apply_fix. ActRequest 에 upgrade. 확장 OpsAlertCard: 같은 action 여러 버튼 key, 상세 줄바꿈.
- API /api/security/code(작업 폴더 점수 — node_modules·dist·테스트 제외, 4,000파일·1MB 한도), /api/security/vulns/check, /api/security/vulns/history.
- sandbox 에서 api.osv.dev 가 프록시 403 이라 실제 OSV 응답은 확인 못 함(형식은 OSV 문서대로, 테스트는 고정 응답). PC 에서 /api/security/vulns/check 로 확인 필요.

# HTTPS 주소(CloudFront) · 관리형 DB(RDS) (2026-10-14, 2.0.27)

- core/aws_cdn.py: ensure_distribution(Comment=recoder:<edge_name> 로 찾음, 원본 ALB http-only, redirect-to-https, CachingDisabled, AllViewerAndCloudFrontHeaders-2022-06, PriceClass_200(한국 엣지 포함), Tags) — 있으면 원본·별칭만 갱신. ensure_certificate(us-east-1 ACM, DNS 검증, 기존 ISSUED/PENDING 재사용, Route 53 영역이 있으면 검증 CNAME UPSERT, 없으면 안내), point_domain(A·AAAA 별칭 → CloudFront, Z2FDTNDATAQYW2), disable_or_delete(켜져 있으면 끄고 _Kept, Deployed 면 삭제).
- aws_edge.ensure_edge(cdn=True): `{svc}-alb-cf-sg`(80, 접두사 목록 com.amazonaws.global.cloudfront.origin-facing) 로 ALB 를 만들고, 기존 ALB 는 set_security_groups 로 바꿈(끄면 다시 공개 SG). aws_infra.ensure_security_group(prefix_list=).
- shield: trust_cdn — map $http_cloudfront_viewer_address → $recoder_client(IPv4/IPv6, 없으면 $remote_addr), limit 키·로그 remote_addr·X-Real-IP 를 그것으로, 차단은 geo $recoder_client(deny 는 연결 주소만 봄), 프로토콜은 CloudFront-Forwarded-Proto. RECODER_SHIELD_PROXY(vpc|cdn) 를 컨테이너 env 에 남기고 reconfigure 가 그대로 씀 — 2.0.26 에서 ops_alerts.act 가 trust_vpc_proxy 를 잃어 ALB 뒤 모든 사용자를 한 IP 로 세던 결함 수정. 실제 nginx: 사용자 A 폭주 89×429, 사용자 B 200, 차단 IP 403, IPv6 주소 파싱.
- core/aws_rds.py: db_identifier = edge_name + "-db", start(태스크 SG 에서만 5432 인 `{svc}-db-sg`, 비밀 recoder/<svc>/database 에 비밀번호 먼저, describe_db_engine_versions 기본(실패 시 16/postgres16), 서브넷 그룹 2개 영역, 파라미터 그룹 rds.force_ssl=0, db.t4g.micro 20GB gp3 암호화 백업1일 삭제보호, Tags), wait(20초 간격, 25분, on_wait), connection(local_services.app_env 이름 규칙 + URL 인코딩), store_connection, secret_refs(`arn:…:KEY::`), init_container(postgres 이미지, 공개 스키마 테이블 0개일 때만 초기화 SQL 한 트랜잭션 — 실제 PG16 으로 첫 실행 적용·두 번째 건너뜀·실패 시 되돌림 확인), delete_instance(삭제 보호 해제 + 최종 스냅숏).
  ManageMasterUserPassword 를 쓰지 않은 이유: RDS 비밀은 사용자명·비밀번호만 담아 생성 앱이 읽는 DATABASE_URL 을 ECS 에서 만들 수 없다.
- ecs_runtime.plan(managed_db=True): postgres 사이드카 대신 managed_db{env_names,names,init_sql_b64} 를 남기고 그 이름들을 provided 로 봄(production 차단 안 함). PostgreSQL 이 없으면 안내만.
- ecs_agent: _check_edge_request — https 면 edge 최소 alb, custom_domain 은 https 필요, rds 는 db_cost_approved·provision. _provision_cdn(엣지 다음) · _start_database(SG 확정 뒤, 기다리지 않음) · _step_database_ready(태스크 정의 등록 직전 대기 → staging env / production secret_refs + 초기화 컨테이너 dependsOn COMPLETE). clients 에 cloudfront·acm_global(us-east-1)·route53·rds·secretsmanager. URL: cdn.url, 새 배포면 전파 대기 안내(실패 아님). 실패 시 db_cost_warning.
- aws_cleanup: cloudfront·acm_certificate(태그 있는 것만 목록)·rds_instance(삭제 후 최대 15분 사라질 때까지)·rds_subnet_group·rds_parameter_group·secret(복구 7일), make_clients(session).
- aws_policy: HttpsCreateDistribution(aws:RequestTag 조건)·HttpsManageDistribution·HttpsRead·HttpsCertificate(us-east-1)·HttpsDomainRecords(CNAME/A/AAAA 만)·ManagedDatabase(db/subgrp/pg/snapshot recoder-*, og:default:postgres-*)·ManagedDatabaseRead(리전 조건)·ManagedDatabaseSecret(secret:recoder/*), SetSecurityGroups, CreateServiceLinkedRole 에 rds. PINNED: cloudfront:TagResource·acm:AddTagsToCertificate·rds:AddTagsToResource·rds:CreateDBSnapshot·secretsmanager:TagResource. aws_calls: rds 약어(Db→DB), create_distribution_with_tags → cloudfront:CreateDistribution.
- 확장: EcsProtection(HTTPS 주소·내 도메인·관리형 DB·비용 확인), canvasHost·ApprovalCard(HTTPS·DB 행), ApiClient. webview httpsDb 3건, core tests/test_dev_2027.py 9건, test_aws_policy 런타임 증거(엣지 cdn 전환·인증서·배포·별칭·RDS 생성·재사용·정리 2회).
- 권한 크기: build_policy(계정·리전 채움) 압축 JSON 이 11.7K자 — 관리형 정책 6,144자(2.0.25 에서 이미 6.8K 로 초과)·역할 인라인 합계 10,240자 초과. aws_policy.split_policy/policy_size(공백 제외, 가장 긴 리전·파티션으로 잼) → quick-create 템플릿 RecoderDeployPolicy/2/3(첫 이름 유지, 사용자에 모두 붙임), aws_role 은 인라인에 안 맞으면 recoder-deploy-policy-N 관리형 정책(있으면 새 버전, 버전 5개 한도 위해 옛 것 삭제, 남는 조각 떼기, 옛 인라인 삭제). 필요 IAM: GetPolicy·CreatePolicy·CreatePolicyVersion·ListPolicyVersions·DeletePolicyVersion·AttachRolePolicy·ListAttachedRolePolicies·DetachRolePolicy(없으면 RoleSetupDenied → 콘솔 폴백).
- 실제 AWS(CloudFront 전파·인증서 발급·RDS 연결)는 사용자 계정에서 확인 필요.

# 대규모 방패(ALB + WAF) · AWS 리소스 정리 (2026-10-14, 2.0.26)

- core/aws_edge.py: edge_name(service) = recoder-<slug18>-<sha1 4>(32자, 권한표 recoder-* 와 맞춤). ensure_edge(ec2, elbv2, wafv2): alb_subnets(태스크 서브넷 영역 + 다른 영역, 2곳 미만이면 InfraError) → `{svc}-alb-sg`(80 공개) · `{svc}-task-sg`(앱 포트, ALB SG 에서만 — aws_infra.ensure_security_group(source_group=)) → ALB(internet-facing, Tags) → 대상 그룹(ip, 헬스 경로, Matcher 200-499 — 헬스 경로가 없는 앱도 살아 있으면 통과, 세밀한 판정은 컨테이너 헬스체크 몫, deregistration 30s) → 리스너 80(다른 TG 를 가리키면 ModifyListener) → WAF(web_acl_rules: 속도 제한 Block + CommonRuleSet(SizeRestrictions_BODY 는 Count) + KnownBadInputs, 있으면 한도만 update) → associate(WAFUnavailableEntityException 5초×12). waf=False 면 우리 ACL 을 떼고 지움. 실패 시 rollback(이번에 만든 것만 역순, ResourceInUse 재시도).
- ecs_agent: _check_edge_request(비용 확인·target_group_arn 충돌·provision=False) 를 AWS 호출 전에. _step_provision 에서 ensure_edge → req.security_group_ids += task SG(공개 `{svc}-sg` 는 안 만듦), req.target_group_arn = TG → 방패 trust_vpc_proxy 켜짐, 서비스 LB 대상은 recoder-shield. _step_resolve_url: ALB DNS, 10초 × (timeout/10) 회 대기. 실패 시 edge_cost_warning. clients 에 wafv2.
- shield: trust_vpc_proxy 면 `map $http_x_forwarded_proto $recoder_proto` 로 원래 프로토콜 전달(nginx -t 두 모드 확인).
- core/aws_cleanup.py: find(clients) — 권한표 범위에 맞춰 이름에 recoder 가 든 후보만 조회(로그는 /ecs/* 전부 태그 확인, SG 는 태그 또는 설명 "ReCoder", ECS 는 recoder* 클러스터 + 배포 기록의 클러스터). describe_clusters 로 INACTIVE 제외. plan(서비스별 그룹·untagged·summary·gaps — 종류별 실패는 gaps 로만), stop(태스크 0 + ops_alerts.unregister), delete(approved 필수, 다시 find 해서 태그 확인한 id 만, ORDER 순, SG 는 다른 선택 SG 를 참조하는 쪽 먼저, DependencyViolation·ResourceInUse·ClusterContains* 재시도, 다른 서비스가 남은 클러스터는 _Kept 로 둠).
- API /api/aws/cleanup/plan · stop · delete(approved + count 일치). 배포 기록(RECODER_ECS_STORE)의 옛 서비스는 untagged 로.
- aws_policy: EdgeLoadBalancer(recoder-* LB·TG·리스너), EdgeWaf(webacl recoder-* + LB), EdgeWafRead(ListWebACLs·GetWebACLForResource, 리전 조건), CleanupList/ListServices(ecs:cluster 조건)/TaskDefinitions/ReadTags/Delete, CleanupSecurityGroups(ec2:ResourceTag/recoder:managed=true), S3 GetBucketTagging·DeleteBucket·ListAllMyBuckets, iam:CreateServiceLinkedRole 에 elasticloadbalancing.amazonaws.com. PINNED: elasticloadbalancing:AddTags·SetWebAcl, wafv2:TagResource. WAF 관리 규칙 묶음(managedruleset) ARN 은 계정 칸이 AWS 라 넣지 않았다(계정 칸 불변식) — 실제 AWS 에서 CreateWebACL 이 거부되면 이 부분부터 볼 것.
- aws_calls: s3 list_buckets → ListAllMyBuckets, wafv2 약어(Acl → ACL), _snake 가 약어 뒤 새 단어를 끊음(GetWebACLForResource, 복수 s 는 붙임 — DescribeDBInstances 도 바르게).
- 확장: EcsProtection 「대규모 방패」(한도 select, 비용 문구, 확인 체크 — protectionRequest 가 확인 없으면 throw), canvasHost.validateConfig·ApprovalCard(대규모 방패 행, 접속 주소 "로드밸런서 주소"), AwsCleanupPanel(운영 화면 두 분기, Checkbox, 승인 카드), SidebarProvider aws.cleanup.plan/stop/delete, ApiClient cleanupPlan/Stop/Delete.
- 검증: tests/test_dev_2026.py(moto — 만들기·재사용·한도 갱신·WAF 끄기·실패 되돌리기(기존 ALB 는 남김)·2개 영역, ECS 연결(공개 SG 안 만듦·nginx real_ip), 정리 목록·승인·태그 없는 것 안 지움·전부 삭제 후 0·SG 순서·남의 서비스 있는 클러스터 유지·중지·권한 없는 종류는 gaps·라우트·배포 기록), test_aws_policy 런타임 증거(edge 4경로 + ModifyListener + rollback + 정리 전체), webview awsCleanup 3건. 실제 AWS(ALB·WAF 생성·차단·정리)는 사용자 계정에서 확인 필요.

# ECS 기본 방패 · 공격·장애 감지 알림 (2026-10-14, 2.0.25)

- core/shield.py: nginx 사이드카(public.ecr.aws/docker/library/nginx:stable-alpine, 컨테이너 recoder-shield). 설정은 env RECODER_SHIELD_CONF_B64 → BOOT 가 conf.d 에 쓰고 `nginx -t` 후 exec. 공개 포트 P = nginx, 앱은 PORT=P+1(앱 healthCheck 포트·모의 결제 웹훅 URL 도 바꿈). LEVELS loose 50/100/100 · normal 20/40/50 · strict 5/10/20(rate r/s / burst / IP당 동시 연결). JSON 접속 로그 `{"src":"recoder-shield",...}`. deny 는 공인 주소만(ipaddress is_global). trust_vpc_proxy(ALB 뒤)면 real_ip. decide(): readiness.port_from_env(또는 Python 런타임) & 정적 사이트 아님일 때만 on — 아니면 이유를 provisioned["shield"] 에. apply() 는 태스크 메모리 512 이상으로, reconfigure() 는 관리 밖 키(memory 등) 유지.
- ecs_agent: _plan_shield(런타임 계획 다음) → _render_task_definition(shield_decision) → ensure_service 의 컨테이너 이름이 recoder-shield. _step_alerts(ensure_service 다음, best-effort): ops_alerts.setup_aws + register. clients 에 cloudwatch·sns.
- core/ops_alerts.py: 지표 필터 Requests/Blocked/Errors(ReCoder/<service>), 경보 traffic-spike(5분 3,000 고정)·blocked-ratio>0.2·error-ratio>0.05(metric math `IF(req>=50, bad/req, 0)`), SNS recoder-<svc>-alerts(+메일 구독). evaluate(): 급증 = 24h 중앙값의 10배(287점 이상일 때, 아니면 3,000 · 바닥 300), 비율은 50건 이상. TaskWatch 3회 연속 부족 → 장애. Monitor.tick 60s(Core lifespan, RECODER_TEST_MODE·RECODER_OPS_MONITOR=0 이면 안 띄움), 저장 RECODER_HOME/ops_alerts.json·ops_monitors.json, 같은 종류 30분 쿨다운, top_sources(Logs Insights). act() 는 approved 필수 — 새 태스크 정의 리비전(shield.reconfigure) + update_service.
- API /api/ops/alerts(GET ?since, POST /{id}/act, DELETE /monitors/{key}, POST /check). aws_policy OpsAlertLogs/Read/Alarms/Topic(recoder-* 로 좁힘) — REQUIRED_DEPLOY_ACTIONS 에는 넣지 않음(없으면 알림만 부분). BUNDLED_AWS_SERVICES += cloudwatch, sns.
- 확장: src/ops/alertWatcher.ts(처음엔 마지막 id 만 저장 · 이후 open 알림만 VS Code 알림 + 디스코드, [자세히] → 운영 화면), EcsProtection(배포 센터·캔버스 승인 카드 「기본 방패」「감지 알림」), OpsAlertsPanel(운영 화면, 60s, 승인 카드).
- 검증: 실제 nginx 로 BOOT 명령 그대로 실행 — 400 요청 → 429 347건·200 53건, 헬스 영향 없음. tests/test_dev_2025.py 18건(moto 로 setup_aws·Monitor·act), test_aws_policy 런타임 증거, webview opsAlerts 4건.
- 하지 않은 것: Local Docker 방패(localhost 전용이라 불필요), runtime_check 단계에는 방패 없음, 실제 AWS 경보 발화는 사용자 계정에서 확인 필요.

# 마을 공용 게시판 · 의존 관문 (2026-10-14, 2.0.24)

- core/board.py: local_routes(라우트 호출마다 다음 라우트 전까지를 핸들러로 보고 첫 2xx `.json({...})` 최상위 키·`[ … ]`, 변수 응답은 None), 인증 = 경로 뒤 미들웨어 이름 또는 `router.use(auth)`. board_entries: 완성된 진입점의 app.use 접두사 → 없으면 약속 문서의 `/api(/vN)/<자원>` → `/api/<자원>s`(guessed). is_server_route_path(routes|routers|controllers|handlers/, 화면 폴더 제외 · backend|server/ 아래 api/), is_api_layer(화면 쪽 api|services|clients/ 또는 api·client·http·axios 이름), api_waits_for(같은 자원 줄기만, 범용 클라이언트는 전부).
- gen_engine: state["board"](체크포인트에 저장·resume 복원), _deps 에 화면 API 파일 → 같은 자원 서버 라우트 약한 의존, _dependency_context 에 게시판 블록(대상이 서버 쪽뿐이면 생략), 이벤트 board_post{items}·board_read.
- 의존 관문 `_gate`(_finish 에서 _verify_and_fix 다음, 저장 전 — 이 파일을 기다리는 파일은 아직 시작 전): 완성 파일 overlay 로 build_readiness.analyze(새 프로젝트는 빈 임시 폴더 기준). ① missing_exports 중 이 파일이 낀 쌍 → add_missing_export ② file_writes 의 NODE_TYPE_NAME_MISSING·NODE_CONTEXT_VALUE_MISSING ③ 이 파일 몫의 남은 이름 어긋남·Context member·Vite process.env → self._fix 1회(실패해도 진행). 다른 완성 파일 반영은 **이 관문이 바꾼 파일만, 그 사이 다른 관문이 안 바꿨을 때만**(동시 관문이 옛 내용으로 덮어 되돌리던 경쟁을 테스트가 잡음). 1.5s(RECODER_GATE_BUDGET_S) 넘는 일이 2번이면 그 작업에서는 끈다. 이벤트 gate_check{ok, fixed[], returned, by}.
- teamState: board·boardReaders·lastPost·gate·gates. TeamVillage: VILLAGE_HEIGHT 392→432, 작업대 이름표와 선반 사이 게시판 줄(L.board), 검토 리코더가 관문 때 gate.by 작업대 옆(말풍선 윗줄)으로, 검토 칸에 관문 통과·자동 수정·되돌림 수.
- 검증: tests/test_dev_2024.py(엔진 실행 — 화면 API 파일이 서버 라우트 뒤에 시작하고 프롬프트에 게시판, 관문 자동 export·1회 되돌림·실패해도 완료·끄기/느리면 꺼짐), webview teamVillage 3건. 마을은 renderToStaticMarkup → Playwright 캡처로 겹침 확인(정적 렌더는 <style> 의 > 를 이스케이프하니 캡처 때 되돌려야 실제 모양).

# 슬롭스쿼팅 방어 · AWS 리소스 태그 · 태그 릴리스 (2026-10-14, 2.0.23)

- core/package_trust.py: 유명 목록(core/data/popular_npm.json, 17,817개 — scripts/update_popular_npm.py 가 registry 검색 API 의 주간 다운로드로 만든다. api.npmjs.org 는 sandbox 에서 403 이라 fetch_weekly 가 검색 API 로 대체) 밖 이름만 조회. look_alike = 같은 스코프·이름 5자 이상·길이차 1 이하·편집 거리 1(인접 교환 1) 또는 구분자만 다름. judge: typo(주간 < min(5,000, max(1,000, 유명의 1%))) · young(30일 미만 & 주간 1,000 미만) → block, install_script/no_repo/deprecated → warn, 404 는 기존 NODE_DEPENDENCY_VERSION_NOT_FOUND 몫. 캐시 ~/.recoder/cache/package_trust.json 24h. 테스트 모드·RECODER_OFFLINE·사내 레지스트리면 조회 안 함.
- 오탐 측정(실제 레지스트리): 저장소 package.json 38개 이름 0건, npm 검색 표본 1,262개(주간 1천+) 차단 0건(처음 규칙 — 편집 거리 2·짧은 이름 포함 — 은 896개 중 15개 오탐이라 줄였다).
- build_readiness._check_package_trust(online 분석에서만) → NODE_PACKAGE_UNTRUSTED(block=ERROR, warn=WARNING). 짝퉁만이면 file_writes(package.json 이름·범위 + import 문) 로 자동 교정 — code_agent 의 FILE_WRITE_FIXES 루프가 AI 없이 바로 고친다. young 은 auto_fix False → edit_fix_round 로 AI 교정 + gen_engine 이 security_block 이벤트(teamState.securityBlocks, 마을 검토 칸 배지).
- aws_infra.recoder_tags/tag_created: 생성 경로에서만 best-effort 태그(재사용·사용자 리소스는 안 붙임). ecs_agent.resource_tags(project_id, service) → cluster·log group·SG·ECR·service·task def. deploy_s3._tag_bucket. aws_policy 「TagRecoderResources」(aws:TagKeys recoder:* 조건) + s3:PutBucketTagging, 런타임 증거 픽스처에 태그 경로.
- release.yml: push tags v* → version-check(scripts/check_release_tag.py, rc 면 prerelease) → 3 OS → publish(sha256 대조, actions/attest-build-provenance@v3, gh release create). 쓰기 권한은 publish job 만.
- extension webview-tests canvasConnections: `git remote get-url` 이 전역 url.insteadOf 로 바뀌어 보이는 환경(이 sandbox)에서 실패 → `git config --get remote.origin.url` 로(환경 무관).

# 함수·스키마로 읽는 필수 환경변수 · 실행 점검 설정 누락 (2026-10-13, 2.0.22)

- 실기기 2.0.21 생성(job d22de705, AI 자유 생성 쇼핑몰, Prisma): env.ts 의 `getEnv(key)`(process.env[key] + throw)로만 JWT_SECRET 을 읽어 deploy_settings.required_env 가 놓침 → runtime_check 가 값 없이 띄워 "Environment variable JWT_SECRET is required" 로 기동 실패 → GENERATED_RUNTIME_ERROR 로 AI 교정 6회 → "실행 점검 오류가 줄지 않아 교정을 멈춥니다(남은 1건)".
- deploy_settings: `_env_accessors`(process.env[인자] 를 읽는 함수, 본문에 throw/exit 이면 필수) + `_accessor_calls`(기본값 인자 있으면 선택) + 별칭 길이 검사, `_schema_env`(zod `.parse(process.env)` 의 z.string() 필수·.min(n), envalid `cleanEnv` 무옵션 항목), all_env_names 에 process.env['X']·접근 함수 호출·스키마 이름.
- runtime_check: 기동 실패 로그에서 `missing_env`(영문·한국어 "X is required/가 없습니다", "Missing required environment variables: A, B") → 만들 수 있는 값(kind_of generate)은 만들어 앱만 한 번 다시 띄움, 입력해야 하는 값은 `config_missing` + issues() 가 GENERATED_RUNTIME_CONFIG(warning) 하나로 — code_agent 교정 루프는 error 만 받음.
- 검증: 생성 결과 스냅숏 재구성 → required_env JWT_SECRET(32)·ecs_runtime.plan 이 64자 생성. Prisma 엔진 다운로드(binaries.prisma.sh)가 sandbox 에서 403 이라 그 앱 자체 기동은 못 봄. 대신 고정 앱(접근 함수 + 감지 못 하는 객체 메서드)으로 실제 Docker runtime_check: passed(재시도 경로), S3_BUCKET_NAME 필수면 failed + config_missing. 전체 2698 passed(1 실패는 sandbox 대체 node 이미지에 alpine adduser 가 없어서 — 환경 문제).
- sandbox 가 초기화돼 wine/PyInstaller 환경이 없다 — VSIX 는 GitHub Actions "Build release"(workflow_dispatch) 로 만든다.

# ECS 주소 무응답 — 시드 경합·작업 폴더 권한·헬스 경로 (2026-10-11, 2.0.21)

- 실기기 2.0.20 TEMP(데일리데스크, TS backend/·frontend/, ReCoder Dockerfile) ECS 배포: success 후 http://IP:3000 timeout, "헬스 경로를 찾지 못했습니다". TEMP 스냅숏을 sandbox 에서 빌드하고 /tmp/ld/ecs_emulate.py(태스크 정의 dependsOn 그대로)로 재현.
- ① recoder-seed(dependsOn postgres HEALTHY 만)와 앱이 동시에 떠 앱 자체 seedDatabase(SEED_DEMO)와 경합 → users_email_key unique 위반으로 앱 종료. ecs_runtime.apply 가 앱 dependsOn 에 recoder-seed COMPLETE 추가, 시드는 node -e 래퍼로 SEED_TIMEOUT_S(120) 뒤 종료.
- ② 실행 단계 `WORKDIR /app/backend` 뒤 `COPY --chown` 은 내용만 넘겨 폴더는 root → mkdir uploads EACCES. node_fixups.runtime_workdir_not_writable + 기존 DOCKERFILE_WORKDIR_NOT_WRITABLE(SQLite 전용이던 것)을 이 경우까지 확장(기존 fixer 재사용). docker_kit.render 에 `RUN chown 1000:1000 /app /app/<server>`.
- ③ ecs_health._declared_health_path 가 `app.use('/health', ident)` + ident 의 `.get('/')`(같은 파일 또는 상대 import) 를 인정.
- ④ aws_infra.diagnose_stopped_tasks(멈춘 태스크 stoppedReason·종료 코드·`ecs/<app>/<taskId>` 로그 끝부분, GetLogEvents 거부 시 안내) → url 무응답·실패·ECS 자동 롤백 시 provisioned["diagnosis"]/error_detail. _probe_public_ip 가 이번 리비전 RUNNING 태스크 우선, 무응답이면 IP 다시 찾아 한 번 더. logs:GetLogEvents 를 aws_policy·quickcreate 에 추가(PINNED, 필수 목록엔 없음).
- fakepg 에 zoneinfo 추가(TimeZone 'UTC' 오류는 대체 이미지 문제였음). 수정 후 TEMP: readiness→fix-all→Local Docker success(health ok), ECS 재현 app running·healthy·/health 200·화면 200.

# 검증된 쇼핑몰 기반 한국어·원화 (2026-10-11, 2.0.20)

- core/starter_templates/commerce: 화면·백엔드 오류 문구 한국어, KRW(원 단위 정수 — priceInCents 는 이제 원, 결제 100원~1억 원 미만, currency 'krw', 웹훅도 'krw'), 관리자 가격 정수 검증, utils/money.js(formatWon)·utils/labels.js(statusLabel·formatDateTime ko-KR), styles/theme.css(색 변수, main.jsx 맨 끝), Footer(사업자 정보 자리표시), 공지 띠, frontend/public/products/*.svg(시드 image_url), cart·주문 상세 API 에 상품 name 추가(LEFT JOIN), MockCheckout 재작성(요약·동의·안내), OrderDetail 결제 대기 동안 2초 간격 15회 재조회, ProtectedRoute/AdminRoute 가 from 을 넘김, Stripe 는 @stripe/stripe-js/pure.
- sandbox 실제 Docker 검증: Local Docker 배포 success(콘솔 오류 0, app_check ok) → Playwright(/tmp/emu/kflow.js): 비로그인 담기→로그인, 가입·검증 문구, 담기 토스트, 장바구니 ₩146,800, 동의 없이 결제→오류, 주문→약 4초 뒤 새로고침 없이 결제 완료, 주문 내역, 모바일 390px 가로 넘침 없음, 영어 잔여 0. 관리자(/tmp/emu/kadmin.js): 50원 거부, 등록·수정·주문 상세 정상. ECS 태스크 정의 재현(ecs_emulate.py, botocore OK)으로 같은 흐름 통과.
- 예전 영어 데모 DB 볼륨이 남은 PC 는 시드가 다시 들어가지 않는다(상품 0개일 때만 시드) — 데모 DB 볼륨을 지우고 다시 배포하면 한국어 상품이 들어간다.

# 설계 단계 파일 목록 0개로 생성 실패 (2026-10-11, 2.0.19)

- 실기기 2.0.18(job e077a4ca, TEST 폴더): 약속 문서 2조각(24K+6K자) 뒤 파일 목록 페이지가 9초 만에 파일 0개로 끝나 RuntimeError "AI 가 만들 파일 목록을 돌려주지 않았습니다". 체크포인트 files=[]. 응답 원문은 로그에 없었음(원인: 모델이 빈 files·more=false 또는 깨진 JSON — 2.0.18 변경과 무관한 경로).
- gen_engine.plan: 첫 페이지 전에는 "비워 두지 말라" 문구, 빈 목록+more=false 면 다시 묻기(stalls 3), 깨진 응답에서 "file" 값 건지기·페이지 크기 절반, 그래도 없으면 `_plain_file_list`(raw 텍스트 "경로 | layer | 목적" 줄, `_PLAIN_FILE_LINE`). 페이지마다 `[gen_engine] kind=plan` 로그.

# 생성 직후 실행 점검 · sandbox 실제 Docker 로 Local·ECS 재현 (2026-10-11, 2.0.18)

- sandbox 에 dockerd 가 뜨고 이전 세션이 만든 대체 이미지(node:22-alpine = 호스트 node+npm, apk 는 no-op · postgres:16-alpine = 호스트 PG16)가 있다. 레지스트리는 막혀 있어 내려받기 불가. postgres 대체 이미지를 공식 entrypoint 와 같게 보강(base64, initdb.d *.sh source, 추가 인자) — /tmp/fakepg. `docker tag postgres:16-alpine public.ecr.aws/docker/library/postgres:16-alpine`.
- 사용자 TEMP(cdf7 쇼핑몰, 2.0.16 생성) 스냅숏을 Core API(/api/deploy/readiness → fix-all → plan local_docker → settings demo → execute)로 실제 Local Docker 배포: 112초 success, app_check 가 /api/products 500 을 APP_RUNTIME_ERROR 로 잡음. ECS 는 `_render_task_definition` 결과(11KB, botocore 검증 OK)를 한 네트워크 네임스페이스로 재현(/tmp/ld/ecs_emulate.py): postgres(초기화 SQL)·모의 결제·앱 정상 기동, 앱 자체 500 동일. S3 는 moto 로 버킷·웹사이트·업로드·정리 정상(화면만, API 없음 — 안내 있음).
- 새 `core/runtime_check.py`: route_table(Express app.use+router.*), scenario(목록·상세·가입·로그인·인증 목록·장바구니·주문, 5xx 만 실패, 요청별 새 로그), source_files(스택 dist→src), useful_lines(오류 객체 덤프 제거), run(ecs_runtime.plan staging 그대로 docker 로 — sleep 컨테이너의 netns 공유, 임의 호스트 포트, finally 정리), issues → GENERATED_RUNTIME_ERROR. generated_validation.verify_proposal(run_app=True) 가 빌드 통과 시 실행, code_agent 루프가 실행 오류를 errors 로 교정(요청 단위 진척 비교·악화 시 되돌림), 최종 결과에 남은 실행 오류. RECODER_RUNTIME_CHECK=0 으로 끔.
- 화면 제어(사용자 PC)는 캡처가 0x0 으로 실패해 못 함 — PC 에서의 최종 확인(생성·Local·S3·ECS)은 사용자 실행 필요. AWS 정리 기능 없음(수동 삭제 안내).

# ECS SBOM 단계 실패(cp949) · Core 인코딩 전수 정리 (2026-10-11, 2.0.17)

- 실기기 2.0.16 ECS(82b0255b, 새로 생성한 쇼핑몰 cdf7): 빌드·ECR·Trivy 통과 뒤 SBOM 단계 실패 `'cp949' codec can't decode byte 0xc8 in position 235511`. sbom.py 가 `output_path.read_text()`(로캘 인코딩). SBOM(312KB, 410개)은 UTF-8 이고 의존성 작성자 `Ștefan Rusu` 등이 있음 — 새 앱의 의존성(multer·aws-sdk)부터 드러남.
- 고침: Core 전체 read_text/write_text·subprocess(text=True) 에 encoding="utf-8"(+errors="replace") — sbom·security_scan(Trivy·Gitleaks 결과)·local_deploy·preflight·git 등 23개 파일. main.py stdout/stderr 를 UTF-8 로(확장이 UTF-8 로 읽음 — core.log 한글 깨짐). 테스트 `test_core_io_never_depends_on_the_windows_locale`(AST 전수) · `test_sbom_with_non_ascii_is_read_under_a_non_utf8_locale`(LC_ALL=C 에서 옛 코드는 실패 재현). EUC-KR 로캘(localedef)로 전체 테스트를 돌려 남은 실패는 테스트 코드 쪽뿐(제품 코드는 sandbox 경로 이름 문제 외 없음).
- local_services.seed_command: 스크립트가 가리키는 파일이 프로젝트에 있는 첫 이름만. node_fixups.relax_generated_tsconfig: module/moduleResolution 맞춤(TS5110·TS5095). `optional_object_methods` → NODE_TS_OPTIONAL_OBJECT_METHOD(TS1162).
- cdf7 e2e: 빌드·DB(schema.sql)·모의 결제·앱 기동 OK. 남은 것(AI 코드 오류, 자동 교정 불가): 상품 목록 SQL 의 $ 번호가 하나 건너뜀 → 500, 화면이 오류 객체를 그대로 그려 React #31. 생성 단계에 실제 실행 점검(앱을 띄워 API 호출)이 없어 못 잡음 — 다음 과제.

# 출시 전 재검토 — 파일 사이 이름 어긋남 · 웹훅 키 · 검토 3회 반영 (2026-10-11, 2.0.16)

- 검토: 독립 검토 에이전트 3회(1차 19건·2차 9건·3차 11건) 전부 반영 — node_fixups `stmt_end`/`add_react_named`/`imports_end` 등 공통 도우미로 자동 교정이 코드를 깨지 않게(판단 불가면 건너뜀), docker_kit `ui_tsc_vite`·`server_tsc_only` 일 때만 타입 오류 허용·`_FATAL_TS`(pretty 출력 포함), ECSAgent `_runtime` 정리, gen_engine root = target_folder, undouble 템플릿 안 중첩, tailwind 감지 강화(lock 있고 의존성 없으면 손대지 않음) 등.
- 실기기 체크포인트 9개(~/.recoder/generation) e2e: 83ed 카페·0ce6 쇼핑몰·c395 카페 = 가입·로그인·담기·장바구니(합계) 문제 0건(0ce6 결제 완료·주문 목록까지, 83ed 주문까지). 0d40·f3c4 빌드 통과(Prisma 실행은 sandbox 엔진 차단). 4a06 APP_SERVER_MISSING, c356·3f89 NODE_LOCAL_IMPORT_MISSING, 3ca1 NODE_IMPORT_NAME_MISSING(없는 함수) — 막고 안내.
- 새로: node_fixups `context_value_missing`/`context_value_gaps`(Context Provider value·훅 마지막 return 객체, 쓰는 쪽은 import 경로로 확인, 같은 함수 최상위 선언·단축형 키만 식에 사용, `_ALIASES`/`_CALL_ALIASES`, useMemo deps·타입 멤버 추가, 결과 다시 파싱) → NODE_CONTEXT_VALUE_MISSING(오류·자동)/NODE_CONTEXT_VALUE_UNDEFINED(경고). build_readiness `_ESM_TYPE_IMPORT` → NODE_TYPE_NAME_MISSING, `_esm_exports` 가 `export type {…}`·`export type *` 이해. deploy_settings `_PROVIDERLESS_WEBHOOK`(allow-list) → generate. ecs_runtime production 주 DB(redis 제외 첫 서비스) 필수. gen_engine `_definition_first` 정렬: 정의 파일 → 실행/문법/비 TS 오류 파일 → 나머지.
- 남은 것: c395 의 주문은 createOrder(token, req) 를 인자 하나로 부르는 AI 코드 오류(생성 때 검증 빌드 미통과로 남은 것 — 새 생성에서는 AI 교정이 이 종류를 먼저 고침). AI 생성 단계 자체는 sandbox 에 모델이 없어 사용자 PC 확인 필요. Core 2,682 · 확장 557(실패 1건 = sandbox git ssh→https, 변경 전과 같음).

# 실제 생성 결과로 개발→배포 끝까지 확인 — 타입 오류 배포, 화면 동작 결함 (2026-10-11, 2.0.15)

- 방법(sandbox, 스크립트는 세션 scratchpad e2e.py·crawl.js): 사용자 ~/.recoder/generation 체크포인트(83ed 카페 Sequelize, 0ce6 쇼핑몰 pg, 0d40 쇼핑몰 Prisma, 4a06 화면만)의 ops → `_autofix_ops`·`_apply_docker_kit`(생성 직후와 같음) → `apply_all_fixes`([모두 적용]) → docker_kit 과 같은 빌드(npm install·UI_BUILD·server_build) → `ecs_runtime.plan` 대로 Postgres(초기화 SQL)·시드 명령·모의 결제·앱(NODE_ENV=production) → Playwright 순회(페이지·가입·로그인·담기·장바구니·결제·주문 목록, 4xx/5xx·pageerror 수집).
- 결과: 카페·쇼핑몰(0ce6) 흐름 문제 0건. 0d40 빌드 통과(실행은 Prisma 엔진 다운로드가 차단돼 확인 못 함, 앱이 S3_BUCKET_NAME 을 시작 때 요구 → ECS 는 막고 이유 안내). 4a06 → APP_SERVER_MISSING.
- 고침: docker_kit `UI_BUILD`/`server_build`(배포 빌드만 타입 오류 허용, verify 는 엄격), build_readiness 서버 진입 파일 backend/src/* 폴백(+ import 한 app 파일의 express()), ORM 이면 서버 package.json 의 드라이버로 services, `_check_server_missing`(APP_SERVER_MISSING), deploy 로컬 Dockerfile 폴백의 unknown 오류 문구, `_esm_exports` 내보내기 없는 ESM 은 빈 집합, `_type_only_use` → NODE_TYPE_NAME_MISSING(경고·자동 수정), node_fixups `auth_user_persist`·`api_envelope_unwrap`(배열은 배열에 envelope 키를 붙임)·`cart_persist`·`drop_ai_mock_server_imports`·esm_commonjs_globals 의 식 안 require → createRequire·tailwind 의존성 늘 선언(CDN 이면 건너뜀), gen_engine `undefined_css_classes`+`_complete_styles`(새 앱, 8개 이상·30% 이상 → src/styles/recoder-complete.css, 불러오는 화면 코드를 importers 로), `_file_hint` 서버·화면 규칙 추가, code_agent `_server_missing_issues` 가 baseURL 상수도 봄.
- 남은 것: AI 생성 자체(설계 보강·스타일 채우기)는 이 환경에 모델이 없어 가짜 응답으로만 확인 — 사용자 PC 에서 새로 생성해 확인 필요. Prisma 실행, S3 를 꼭 요구하는 앱의 ECS(버킷·태스크 역할 준비)는 미지원(막고 안내).

# 서버 없는 생성 결과 · 엉뚱한 Dockerfile 자동 수정 (2026-10-11, 2.0.14)

- 실기기 2.0.13 job 4a06(Haiku, TEMP): ops 41개 = 화면 30 + mock-payment-server/{package,tsconfig}.json + ADR 10, 서버·루트 package.json·Dockerfile 없음. 요약은 "Express·Prisma·PostgreSQL". core.log 06:14:46 `kind=other`(설계 검토 생략 추정 — 한글 로그가 깨져 문구 확인 불가). 배포: local "스택 unknown", ECS 승인 카드 막는 문제 = remediation 이 쓴 FastAPI Dockerfile(COPY requirements.txt / ./app).
- 원인: `plan_gaps` 는 서버 코드가 하나라도 있을 때만 서버 쪽 빈 곳을 메움 · `_complete_fullstack_manifest` 는 서버 package.json 이 있을 때만 루트 파일 · `_review_plan` 4096 토큰 · `plan_with_kit` 이 서버 없으면 AI mock 을 안 뺌 · `_gen_missing_dockerfile` 의 else = FastAPI.
- 고침: plan_gaps 서버 뼈대(약속 /api 묶음 있고 서버·루트 진입 없음 → backend/{package.json,tsconfig.json,src/db/…,src/middleware/auth,src/utils/errors,src/scripts/seed} + 기존 로직이 routes·server), `_complete_plan` 이 서버가 생기면 fullstack 루트 파일 다시 보강, 검토 8192·최대 25개, plan_with_kit 은 늘 mock 제외, code_agent `_server_missing_issues`(최종 결과에만 — AI 교정 대상 아님), remediation `_project_dockerfile`(docker_kit.for_workspace) → verbatim 템플릿, Python 은 requirements.txt, Node 는 package.json 시작 파일이 있을 때만, 그 외 auto_apply False. ecs_runtime: Prisma 앱은 "테이블 SQL 없음" 경고 안 함(docker_kit PRISMA_START 가 만듦).
- 검증: job 4a06 의 실제 목록 + 요약으로 plan_gaps → 서버 12개 + 루트 4개, mock 제외, 결제 모듈 자리(backend/src/payments/stripe.ts). 고친 0ce6 구조에서는 Dockerfile 자동 수정이 ReCoder 검증본, 화면만 있는 폴더는 자동 수정 없음. Core 2,631. 실제 AI 생성은 이 환경에 모델이 없어 사용자 PC 에서 다시 생성해 확인 필요.

# 배포한 쇼핑몰 화면 품질 · 자동 수정 한 번에 (2026-10-11, 2.0.13)

- 실기기 2.0.12: ECS 배포 성공(98.92.137.162:3000) — 그런데 승인 카드 자동 수정 4건을 하나씩 눌러야 했고, 화면은 스타일 없는 HTML·상품 0개.
- 원인(실제 0ce6 결과물을 sandbox 에서 ECS 태스크와 같은 구성으로 띄워 Playwright 로 가입→장바구니→결제→주문 목록까지): tailwind.config·postcss.config·@tailwind 없음(클래스 359개), useApi 가 useRef 에만 저장(다시 안 그림), execute('/api/products') + baseURL `VITE_API_BASE_URL || '/api'`(.env.example 에만 정의) → /api/api, 시드 스크립트가 rootDir 밖(dist 에 없음), formatPrice /100(KRW), PaymentResult 가 /api/health(서버는 /health). 0d40 결과물도 Tailwind 미연결·/api/health 동일.
- 고침(node_fixups → build_readiness FILE_WRITE_FIXES, 생성 직후 자동·배포 점검 자동 수정): `tailwind_setup`(NODE_TAILWIND_NOT_CONFIGURED), `ref_state_hooks`(NODE_REACT_REF_STATE — useReducer 다시 그리기, 훅 인자 deps → ref), `seed_script_into_build`(NODE_SEED_SCRIPT_OUTSIDE_BUILD, @ts-nocheck, 옛 위치는 import 한 줄), `health_alias`(NODE_HEALTH_PATH_MISMATCH — 앱 라우터만, mock 제외, /api 아래 마운트면 안 함), `krw_minor_units`(NODE_KRW_MINOR_UNITS), `missing_style_content`(없는 CSS = 지시문 또는 BASE_STYLESHEET), api 중복: `call:<함수>` 래퍼·env 폴백(`_env_defined`, 예시 파일 제외). local_services.seed_command 이름에 init-db 등(node_fixups.SEED_SCRIPT_NAMES). ecs_runtime 시드 컨테이너가 앱 environment·secrets 복사.
- 한 번에: `build_readiness.apply_all_fixes`(다시 판정하며 최대 4회, 요청 코드 + 새로 드러난 error) + `POST /api/deploy/readiness/fix-all`; 확장 `workspace.deploy.remediation.applyAll`(readiness 는 fix-all 한 번, 나머지 수정안은 차례로) · ApprovalCard `fixableProposals` [모두 적용](2건 이상) · 적용 뒤 같은 설정으로 승인 카드 다시 열기.
- 검증(sandbox): 고친 0ce6 를 ECS 태스크 정의(botocore 검증) 그대로 — postgres 부팅 스크립트 → 시드 컨테이너 명령(`node dist/scripts/init-db.js`, 상품 6개) → 모의 결제·앱 → Chromium: 스타일 적용, 상품 6개(₩39,900 등), 상세, 가입·로그인, 장바구니, 결제 → "결제 완료 ₩4,900", 주문 목록 결제 완료. 0d40 화면 CSS 5KB → 14KB. Core 2,623, 확장 557(실패 1건은 sandbox git ssh→https 재작성, 변경 전과 같음).
- 남은 것: 상품 이미지는 placehold.co(sandbox 에서는 차단되어 확인 못 함), 일부 영어 문구는 이미 만든 결과물에 남음(새 생성부터 지시), 화면 tsc 오류 6건(vite 는 빌드됨 — ErrorAlert prop 이름 등, AI 교정 대상).

# ECS 배포 — 실행 파일 템플릿 경로 · 앱 실행 설정(DB·서명 키·모의 결제) · 실제 결과물 실행 오류 (2026-10-11, 2.0.12)

- 실기기 2.0.11 ECS(배포 3970e520): 1~7단계(점검·인프라·검사·빌드·ECR·취약점·SBOM) 통과 뒤 8단계 태스크 정의에서 FileNotFoundError `_MEI…\core\registry\file_templates\ecs-task-definition.json.template`. spec 은 데이터를 `registry/…`(번들 맨 위)에 넣는데, 배포 라우트가 `core.agents.ecs_agent` 로 불러 `Path(__file__).parent.parent` 가 `_MEI…/core` 를 가리킴. 소스 실행·self-check(agents.ecs_agent 로만 import)에서는 드러나지 않았다.
- `core/resources.py resource()` — core 기준 경로 → `_MEIPASS` → `_MEIPASS/core` 순으로 찾음. ecs_agent·commerce_starter·registry·grounded_repair 에 적용. release_check: 두 import 이름 모두로 템플릿·commerce·registry·지식 문서 확인.
- ecs_health: `const app: Express = express()` 인식(TEMP app.ts 의 GET /health 를 못 찾아 헬스체크 없이 배포).
- ECS 앱 실행 설정(`core/agents/ecs_runtime.py`): `plan()` = required_env + readiness.services + demo_supported → staging 은 같은 태스크 안 보조 컨테이너(postgres `public.ecr.aws/docker/library/postgres:16-alpine`, `sh -c` 로 RECODER_INIT_SQL_B64 → initdb.d 스크립트(한 트랜잭션, 실패해도 DB 는 뜸), listen 127.0.0.1 · 모의 결제 = 앱 이미지 `node -e MOCK_PAYMENT_JS`(127.0.0.1) · 시드 스크립트(local_services.seed_command 있을 때, essential false)), 앱 dependsOn HEALTHY/START, 메모리 최소 1024(mongo 2048), 서명 키·DB 비밀번호는 `~/.recoder/ecs_runtime/<region-cluster-service>.json`(0600, RECODER_ECS_RUNTIME_DIR). production 은 missing → `InfraError`(파이프라인 0-1단계, `_clients` 전). `apply()` 가 렌더링 결과에 합침(사용자 env·secrets 우선, 로그 설정 복사). 값은 `ECSAgent._runtime[id(req)]` 메모리에만.
- `/api/deploy/preflight` 에 `env_names`·`environment`(ecs) → `ecs_runtime` 요약, ECS_APP_SETTING_MISSING(막음)·ECS_APP_RUNTIME(참고). 확장 `runtimeNames(config)` 가 이름만 보냄(prepare·execute 둘 다).
- 실제 TEMP 결과물(job 0ce6, 사용자가 로컬 배포한 그 폴더)을 sandbox 에서 실행해 찾은 결함: ESM 확장자(moduleResolution node), ESM require/__dirname(NODE_ESM_COMMONJS_GLOBAL), 화면 제공 오탐(ui_runs)·ESM 제공(serve_frontend_module, 멱등), NODE_FRONTEND_PRODUCTION_ONLY(모의 결제 앱만), NODE_CURRENCY_CASE_COMPARE.
- 검증(sandbox, Docker 레지스트리 차단이라 컨테이너 대신 같은 구성을 프로세스로): 렌더링한 태스크 정의를 botocore RegisterTaskDefinition 스키마로 검증 → postgres 보조 컨테이너의 부팅 스크립트를 그대로 실행(base64 → initdb 스크립트 → psql 한 트랜잭션, 7개 테이블) → 모의 결제 서버·앱을 태스크 정의의 환경변수 그대로 실행: `/`·`/cart`(화면), `/health`, 가입·로그인·장바구니·주문 → 모의 결제 웹훅 → 주문 paid.
- 남은 것: 데모 상품 시드 — 이 결과물은 SEED_DEMO 시드를 서버 시작이 아닌 `backend/scripts/init-db.ts`(빌드 밖, package.json 경로도 틀림)에 둬서 로컬·ECS 모두 상품이 비어 있음. Prisma 앱의 ECS 테이블 생성(migrate) 미확인. 실제 AWS 에서 보조 컨테이너 기동은 사용자 PC 에서 확인 필요.
- 검증: 실행 파일을 wine 에서 --self-check ok(2.0.12, ecs_runtime 포함 — 두 import 이름 확인은 하위 모듈이 빠지면 실패하도록 엄격하게), Core 2,605 · 확장 556(실패 1건은 sandbox git 의 ssh→https 재작성 탓, 변경 전에도 동일).

# 실제 Haiku 결과로 검증 — 빈 파일 멈춤·두 겹 중괄호·Prisma 어긋남 (2026-10-11, 2.0.11)

- 실기기 2.0.10 쇼핑몰(job 0d40eae842e74e6d, Haiku, 빈 TEMP): 설계 46개 + ReCoder 결정적 7 + 검토 9 = 62개(서버 진입점·라우트·서비스·미들웨어 모두 생김, 없는 import 0). `prisma/migrations/.gitkeep` 를 AI 가 빈 내용(25자 중 0자)으로 줬는데 implausible "내용이 비어 있음" 으로 3회 거절 → 멈춤, [이어서] 도 같은 방법 1회라 다시 멈춤.
- 체크포인트 61개를 sandbox 에서 생성 직후 자동 교정 → 실제 도구로 빌드: 서버 `npm install` ETARGET(jsonwebtoken@^9.1.2) · prisma generate P1012(@@validate) · tsc 78건(enum PAID vs 'paid', BigInt vs number, AuthenticatedRequest 가 Request 미확장, 관계 items vs order_items, { expiresIn }) / 화면 tsc 9건(결제 화면 3개 중괄호 두 겹).
- 고침: `placeholder_file`(빈 파일은 AI 없이), `undouble_braces`(clean_content·_autofix_ops), `prisma_schema_alignment`(NODE_PRISMA_SCHEMA_ALIGN), `express_request_interfaces`(NODE_EXPRESS_REQUEST_TYPE), NODE_DEPENDENCY_VERSION_NOT_FOUND 를 FILE_WRITE_FIXES 로(생성 직후 첫 회차 online), ts_safe `{ expiresIn }`, `prisma_relation_renames`(검증 빌드 로그 TS2353 *Include → 스키마 관계 필드 이름), `_file_hint`(Prisma 규칙), 서버 파일 생성·교정 맥락에 schema.prisma.
- 결과(같은 실기기 출력): 서버 tsc 3건(CreateOrderRequest 미선언, startMockPaymentServer 미선언, mock 결제 status 문자열 — AI 교정 대상), 화면 vite 빌드 통과. Prisma generate 는 엔진 차단이라 더미 엔진 경로로 클라이언트 타입만 생성해 확인.
- 못 한 것: 실제 AI 교정 회차(이 환경에 모델·사용자 키 없음), Docker 이미지 빌드·로컬 배포(Docker Hub·Prisma 엔진 차단).

# 설계 빠진 파일 → 빌드 실패 — 설계 검토·빠진 파일 마저 만들기 (2026-10-10, 2.0.10)

- 실기기 2.0.9 쇼핑몰(job c356ecab736c46e7, Haiku 4.5, 빈 TEMP): 설계 65개 중 서버 10개(유틸·헬스·업로드·재고·결제 모듈)뿐 — server.ts·db/client·middleware/auth·utils/errors·상품/주문/인증/장바구니 라우트·schema.prisma 없음. 약속이 길어(22.8k) 목록을 페이지로 받다 AI 가 more=false 로 일찍 끝낸 것으로 보임. 남은 문제: 없는 import 3파일, 미선언 패키지(@aws-sdk/client-s3·presigner·multer — 표에 없는 게 섞여 전부 안 넣음), NodeNext 확장자(TS2834/2835), 결제 웹훅·결제 시작 미연결(진입점 없음), validation.ts unknown 타입. ECS 승인 버튼은 이 오류들 때문에 막힘(정상) — 단 막는 이유가 경고와 섞여 보였음.
- `gen_engine`: `plan_gaps`(uses 닫힘·서버 진입점·약속 /api/<묶음> 라우트·Prisma 스키마/클라이언트·화면 진입점, 디스크에 있으면 안 더함) + `_review_plan`(검토 에이전트 `review_code_manifest`, agent=review, 새 앱이거나 12파일 이상, 최대 2회, 실패는 건너뜀 — 한도·인증 오류만 올림) → `plan_added` 이벤트. `missing_local_imports`(상대 import 해석: `./x.js`→x.ts, 폴더 index, 자원 제외, 이름·기본 내보내기·네임스페이스 멤버) + `_fill_missing_imports`(run 끝, 최대 3회, 새 파일 entry.importers → `_dependency_context` 가 불러오는 쪽 전체 코드를 보여 줌). `fill_missing_files`: 전체 점검 중 NODE_LOCAL_IMPORT_MISSING 이면 edit 교정 대신 만들기(code_agent). run 결과 data 에 contracts.
- 결정적: `node_fixups.esm_import_extensions`(NODE_ESM_IMPORT_EXTENSION, type module + NodeNext/Node16), `_declare_known` 일부만이라도·`node_manifests.version_for`(표 → 레지스트리 ^최신 정식, 없으면 넣지 않음). 테스트는 conftest 가 레지스트리 조회를 끔.
- 배포: ApprovalCard 막는 문제/참고 분리, canvas.read_target ClusterNotFoundException → exists False.
- 검증(sandbox): 실기기 결과물에 새 교정 → 확장자 3파일·패키지 3개 자동 수정, 빠진 3파일을 넣으면 tsc 남은 오류는 Prisma generate 전 타입(검증 빌드가 generate)·validation unknown(AI 교정 대상)뿐. 엔진 흉내 테스트: 검토가 라우트 추가·진입점 결정적 추가·없는 오류 유틸을 불러오는 코드를 보며 생성. Core 2,577.
- 못 한 것: 실제 AI 로 새로 생성(이 환경에 모델 없음) — 실기기에서 같은 요청으로 확인 필요.

# 개발 단계 어긋남·공통 기반 1명 — 생성 순서 재설계 (2026-10-10, 2.0.9)

- 실기기 2.0.8 카페(job c395e3a96baa4573): 빌드 오류 "2 → 36" 으로 교정 멈춤 — 화면 빌드가 먼저 실패해 서버 오류가 숨어 있었다. 남은 16건 대부분이 화면이 컴포넌트·훅과 다르게 씀(ErrorAlert onClose 필수 10곳, UseCartReturn total, CartContextType setItems). 원인: 계층(layer) 단위로 화면·컴포넌트를 동시에 쓰고, 맥락은 목록 앞 파일 64k(서버 파일)라 화면이 컴포넌트를 못 봄. 공통 기반은 1명 순차.
- `gen_engine._schedule`: 파일별 의존(설계 uses + 같은 쪽 `_rank` 하위 단계, uses 가 있으면 uses+1단계만) DAG, 팀 인원 동시, 교착 시 직접 의존 없는 것부터. `_dependency_context`: uses 파일 전체(48k) + 완성 파일 `_export_summary`(28k). PLAN_SCHEMA files.uses, 약속에 props/훅 반환. wave 이벤트 layer=-1(마을 전 칸 열림).
- 고치기: `_definition_first`(같은 타입 이름 2파일 이상 → 정의 파일 한 번), workers=팀 인원, max_files 16. 검증 빌드 `_verification_ops`(docker_kit verify=True: 폴더마다 끝까지 빌드, 마지막 단계가 실패한 폴더 출력을 찍음 — 캐시돼도 로그 남음). 진전 판정 `_build_error_keys`(줄 번호 뺀 오류 집합), 나빠지면 직전 상태 복원. 파일별로 뽑힌 오류가 이미 있으면 Dockerfile 오류를 덧붙이지 않음.
- 결정적 교정: hoisted_clients(NODE_CLIENT_NOT_CREATED), namespace_imports(NODE_MODULE_NOT_IMPORTED), prisma_cli_missing, ts_safe_rewrites(NODE_TS_SAFE_REWRITE, 조용히 — env 확인 뒤 as string·jwt expiresIn·...(x && {})), tailwind_self_apply(NODE_TAILWIND_SELF_APPLY). build_log_issues 파일당 20줄. 보안: 상수 이름 값은 비밀 아님.
- docker_kit: Prisma(generate·migrate diff --to-schema/--to-schema-datamodel·recoder-start.cjs base64→node 로 기록·openssl, CMD node recoder-start.cjs entry), ENV NODE_ENV=production. readiness: @prisma/client → datasource provider 로 postgres, CMD 의 recoder-start.cjs 다음 인자를 진입 파일로. 배포: 빈 DB 첫 배포에 `seed_command`(kit 일 때 db:seed → dist/…js) docker exec, 결과 `seeded`(AppSeedNote).
- 검증(sandbox, 실제 AI 대신 사람이 고친 답): 실기기 쇼핑몰(Prisma) 2.0.7 결과 — 결정적 8건 뒤 빌드 오류 25건(12파일) → 1회 교정 → 빌드 통과. 실기기 카페 2.0.8 결과 — 결정적 교정 뒤 29건(17파일) → 1회 교정 → 통과 → 로컬 배포 성공(PostgreSQL·첫 화면·/api/menu·관리자 로그인·주문). hadolint 0·gitleaks 0. 생성 순서 흉내(카페 76파일, 6명): 265s → 175~190s, 화면 프롬프트가 ErrorAlert props·UseCartReturn 을 봄(예전 못 봄). Core 2,566 통과·확장 554/555(sandbox git insteadOf).
- 못 한 것: 실제 AI(Haiku)로 새로 생성(이 환경에 모델 없음), Prisma 엔진 다운로드 차단으로 Prisma 앱 실행, trivy DB 다운로드 차단.

# 개발 — "문제 발견 → 고치는 중" 무한 반복 (2026-10-10, 2.0.8)

- 실기기: 파일 생성 완료 뒤 전체 점검이 6회 돌며 "자동 검사에서 문제 발견 → 고치는 중" 반복 → "빌드·실행 실패 예상 10건"(ImportMeta.env, useOrders ordersData, PaginationProps·ErrorAlertProps, UseCartState&UseCartActions 에 cart/loading/removeFromCart 없음, TS6133 등).
- 원인: edit_fix_round 가 고칠 파일 하나만 보여 줌(정의 파일 모름 → 파일 사이 어긋남을 못 고침), 순차 처리, 오류 줄 원문 없음, 줄지 않아도 6회 반복. vite-env.d.ts 없음·tsconfig.app.json 등 변형 tsconfig 의 린트 옵션은 결정적으로 고칠 수 있었음.
- `gen_engine.edit_fix_round`: FIX_SCHEMA(edit 에 file 선택), `_related`(상대 import 1단계 → ops, 36k 한도) 참고 블록, ThreadPoolExecutor 3, 고정 op 보호, find 정확히 1회일 때만 적용. 이벤트 "전체 점검 — X 오류 N건 고치는 중"(마을 말풍선은 그대로 표시).
- `node_fixups.build_log_issues(contents=)` 오류에 ← `소스 줄`, `vite_env_types` + readiness NODE_VITE_ENV_TYPES_MISSING(자동, FILE_WRITE_FIXES).
- `code_agent`: `_autofix_ops(new_app=)` tsconfig*.json 모두 relax(새 앱이거나 새로 만드는 파일), 일관성 루프 `build_counts` — 빌드 오류 수가 줄지 않으면 멈춤 + 회차·추세 메시지.
- 검증: Core 전체 통과, 확장 555개(1건 sandbox git insteadOf), lint 통과. 실제 AI 로 TEMP 재생성은 사용자 실기기에서 확인 필요.

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
