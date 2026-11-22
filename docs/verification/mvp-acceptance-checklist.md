# MVP 수용 기준 최종 체크리스트

보드 카드 「MVP 수용 기준 최종 체크」(DoD: 전 항목 ✅)의 실행 문서.
"기계 검증" 열이 ✅ 인 항목은 자동 테스트가 이미 잠갔다 — 사람은 "실기기"
열만 F5(확장 개발 호스트)에서 확인하면 된다.

기계 검증 재실행: `cd core && python3 -m pytest tests/` ·
`cd extension && node --test webview-tests/*.test.js`

## A. 개발 (Develop)

| # | 기준 | 기계 검증 | 실기기 확인 |
|---|---|---|---|
| A1 | 말로 요청 → 설계 결정 카드가 뜬다 (0건이면 확인 카드) | ✅ planSafeguards | ✅ 09-22 직접 선택 |
| A2 | 결정 선택 → 코드 생성, 제외된 결정은 사유 표시 | ✅ planSafeguards | ✅ 09-22 (제외 사유 표시는 코드 검증) |
| A3 | 생성 코드 적용(디프 승인) 동작 | — | ✅ 09-22 diff +41/-3 · ADR 2 |
| A4 | 구조 지도: 프로젝트/파일 2단계 줌, 고립·과부하 표시 | — | ✅ 09-22 (import 소실 수정 후) |
| A5 | AI 실패가 원인·다음 행동과 함께 표시(가짜 200 없음) | ✅ placeholder/ai_failure | ✅ 09-22 미준비 게이트 (요청 중 실패는 코드 검증) |

## B. 검사 (Security)

| # | 기준 | 기계 검증 | 실기기 확인 |
|---|---|---|---|
| B1 | 스캔 미실행은 "확인 못 함"으로 — 통과 위장 금지 | ✅ scanSkipped | ✅ 09-22 Docker 꺼진 상태→자동 시작→확인 못 함 |
| B2 | 빌드 전 이미지를 Trivy 에 안 넘김, 실행 시 1회 스캔 | ✅ trivy_prebuild | ✅ 09-22 실행 직전 CRITICAL 차단 확인 |
| B3 | 미검증 배포는 이중 확인 요구 | ✅ trivy_prebuild | ✅ 09-22 (체크박스 2단계 수정 후) |
| B4 | 전송 전 민감정보(.env 등) 업로드 제외 + 사유 표시 | ✅ s3Deploy(excluded) | ✅ 09-22 .env·secrets.json 제외 표시, 실제 404 |

## C. 배포 (Deploy)

| # | 기준 | 기계 검증 | 실기기 확인 |
|---|---|---|---|
| C1 | S3 정적 배포 — 진행률 실시간, URL 반환 | ✅ s3 stream/progress | ✅ 09-22 버킷 생성·URL 응답 확인 |
| C2 | 로컬 Docker 배포 — 태그 이미지 입력해도 400 없음 | ✅ containerName | ✅ 09-22 build→재검사→run→Up 3456 |
| C3 | ECS 배포 시작·상태 폴링 | — | ✅ 09-22 Fargate 실배포 → 공개 IP /health v1 응답 (스캐너 Docker 폴백 후) |
| C4 | 배포 정책 게이트(rego) — 조건 미달 차단 + 사유 | ✅ opa 12/12 | ✅ 09-22 폼에 배포 환경 선택 추가 후 production × test/policy-deny 브랜치 → 거절 카드(사유·현재 브랜치·수정 방법, AWS 리소스 미생성). main 브랜치는 통과. OPA 없음 → 로컬 내장 규칙 |
| C5 | 리전 불일치는 1회 경고 후 진행 가능(차단 아님) | ✅ regionGate | ✅ 09-22 확인 카드([그대로 진행]/[되돌리기]) — 같은 버튼 재클릭 확인은 더블클릭에 뚫려 별도 버튼으로 수정 |
| C6 | 존재하지 않는 API 를 부르는 버튼 없음 | ✅ uiRequestContract | — |

## D. 감시·롤백 (Operate)

| # | 기준 | 기계 검증 | 실기기 확인 |
|---|---|---|---|
| D1 | 배포 후 연속 검증(헬스) 자동 시작·중지 | ✅ replace_safety | ✅ 09-22 감시 패널 |
| D2 | 이상 감지 → 롤백 **제안** 카드 (자동 실행 금지) | ✅ rollbackApproval | ✅ 09-22 헬스 3회 실패→제안, 자동 실행 없음 |
| D3 | 승인 시 이전 버전 복귀, 기록 남음 | ✅ rollback_target/run_args | ✅ 09-22 v2→v1 복귀(고정 태그), rolled_back 기록 |
| D4 | 교체 실패 시 이전 컨테이너 복원 | ✅ replace_safety | ✅ 09-22 run 실패→직전 컨테이너 복원 |

## E. 온보딩·운영 비용

| # | 기준 | 기계 검증 | 실기기 확인 |
|---|---|---|---|
| E1 | AWS 프로필 원클릭 연결 (키 재입력 없음) | ✅ awsProfileConnect | ✅ 09-22 default 프로필로 연결·S3 배포 |
| E2 | 키 없는 사용자 — 원클릭 IAM 셋업 동작 | ✅ awsOnboarding | ✅ 09-22 프로그램 안 역할 생성→assume→해제 (검증 1) |
| E3 | 최소권한: 권한표·점검·템플릿이 한 원본 | ✅ aws_onboarding drift | — |
| E4 | 예산 알람 스크립트 동작 | ✅ budget payload | ✅ 09-22 recoder-monthly $15 · 80%/100% 생성 |

## F. 게시

| # | 기준 | 기계 검증 | 실기기 확인 |
|---|---|---|---|
| F1 | VSIX 빌드 (경량) | ✅ dist 산출 확인 | ✅ 09-22 314KB 설치→코어 자동 기동→준비됨 |
| F2 | 플랫폼별 VSIX (Core 동봉) | — | ✅ 09-22 darwin-arm64: PyInstaller 65MB → `package-extension.sh darwin-arm64` → 설치 → 샘플 앱에서 동봉 `bin/recoder-core` 로 자동 기동(17894). win32-x64·linux-x64 는 해당 OS 에서 빌드 필요(미실행) |
| F3 | 의존성 취약점 0 (또는 사유 문서) | ✅ security-audit.md | — |

실기기 확인이 끝나면 이 파일의 ☐ 를 ✅ 로 바꿔 커밋한다 — 그 커밋이
카드의 "전 항목 ✅" 증빙이다.
