# ReCoder 운영자 런북 (1학기 MVP 트라이얼)

대상: 운영자(게이트웨이 AWS 계정 보유자). 기간: 약 5일 / 20–30명.

## A. 트라이얼 전 (배포 준비)
1. **게이트웨이 배포 확인**
   - `gateway/` 에서 `sam deploy` 완료 상태인지 확인.
   - **게이트웨이 URL** 확보: `sam list stack-outputs` 또는 `sam deploy` 출력의 `ApiUrl`/`GatewayUrl`.
   - 헬스 체크: enroll/invoke가 실제 응답하는지 1회 호출 테스트.
2. **반 코드(EnrollCode)·정원(MaxStudents) 확인** — 배포 시 `--parameter-overrides` 로 설정한 값(예: `EnrollCode=recoder2026`, `MaxStudents=30`).
3. **확장에 게이트웨이 URL 박기 (필수)**
   - `extension/package.json` → `recoder.gateway.url` 의 `default` 를 배포된 URL로 설정.
   - 재빌드: `npm run build` → `npm run package` 로 새 VSIX 생성.
   - ⚠ 이 단계를 빼면 학생은 AI를 못 씁니다(자동 enroll 미동작).
4. **Core 변경 반영(코드 에이전트·시크릿 스캐너 사용 시):** PyInstaller exe 재빌드
   - `cd core && pyinstaller recoder-core.spec` → 새 `bin/recoder-core.exe` 로 VSIX 재패키징.
5. **(Discord 사용 시)** 봇 토큰·privileged intents(Message Content, Server Members) 설정, 봇 상시 구동 환경 준비(아래 C-2).

## B. 비용·한도 설계 (예산 보호)
- 전체 크레딧 $100, **10월까지 유지** 필요 → 트라이얼 풀 상한을 보수적으로(예: **$20**) 잡기.
- per-student 토큰·쿼터로 1인 과다사용 차단(발급 스크립트: `gateway/scripts/issue_tokens.py`).
- 사용 모델은 저비용(Claude 3 Haiku). 크레딧을 실제로 깎는 건 Bedrock 호출뿐(Lambda/DynamoDB/API GW는 Always-Free 한도 내).

## C. 트라이얼 중 운영
1. **비용 모니터링(매일):** AWS Billing/Cost Explorer로 누적 사용액 확인. 풀 상한 근접 시 알림.
2. **(Discord 사용 시) 봇 구동:** 운영자 PC/서버에서 `python discord-bot/bot.py` 상시 실행.
   - 학생이 다른 PC에서 접속하므로, 봇 브리지(ws:7780)를 외부에서 접근 가능하게 해야 함 → **무료 터널**(Cloudflare Tunnel/ngrok)로 노출하고, 학생 설정 `recoder.bridge.host` 에 그 주소 주입(또는 배포 VSIX 기본값에 포함). 평문 ws 대신 터널(wss)로 감싸 토큰 노출 방지.
3. **장애 대응:** 학생 "AI 안 됨" 다발 → 게이트웨이 헬스/크레딧 잔액 확인. 특정 학생만 → 토큰/쿼터 상태 확인.

## D. 트라이얼 종료 (5일 후)
1. **신규 등록 차단:** EnrollCode 무효화(재배포로 코드 변경) 또는 게이트웨이 스택 비활성/삭제.
2. **(Discord) 봇·터널 종료.**
3. **비용 정산:** 최종 사용액 기록, 남은 크레딧이 10월까지 충분한지 확인.

## E. 비상 시나리오
- **비용 급증:** 게이트웨이 스택 일시 삭제(`sam delete`)로 즉시 호출 차단.
- **계정 만료(크레딧 소진/6개월):** 새 프리티어 계정으로 전환(`core/switch_aws.py`) → `sam deploy` → 새 URL을 VSIX에 박아 재배포.
