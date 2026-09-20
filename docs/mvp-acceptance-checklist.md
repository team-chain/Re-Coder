# MVP 수용 기준 최종 체크리스트

보드 카드 「MVP 수용 기준 최종 체크」(DoD: 전 항목 ✅)의 실행 문서.
"기계 검증" 열이 ✅ 인 항목은 자동 테스트가 이미 잠갔다 — 사람은 "실기기"
열만 F5(확장 개발 호스트)에서 확인하면 된다.

기계 검증 재실행: `cd core && python3 -m pytest tests/` ·
`cd extension && node --test webview-tests/*.test.js`

## A. 개발 (Develop)

| # | 기준 | 기계 검증 | 실기기 확인 |
|---|---|---|---|
| A1 | 말로 요청 → 설계 결정 카드가 뜬다 (0건이면 확인 카드) | ✅ planSafeguards | ☐ 요청 1회 |
| A2 | 결정 선택 → 코드 생성, 제외된 결정은 사유 표시 | ✅ planSafeguards | ☐ |
| A3 | 생성 코드 적용(디프 승인) 동작 | — | ☐ |
| A4 | 구조 지도: 프로젝트/파일 2단계 줌, 고립·과부하 표시 | — | ☐ 탭 열기 |
| A5 | AI 실패가 원인·다음 행동과 함께 표시(가짜 200 없음) | ✅ placeholder/ai_failure | ☐ |

## B. 검사 (Security)

| # | 기준 | 기계 검증 | 실기기 확인 |
|---|---|---|---|
| B1 | 스캔 미실행은 "확인 못 함"으로 — 통과 위장 금지 | ✅ scanSkipped | ☐ Docker 끄고 1회 |
| B2 | 빌드 전 이미지를 Trivy 에 안 넘김, 실행 시 1회 스캔 | ✅ trivy_prebuild | ☐ |
| B3 | 미검증 배포는 이중 확인 요구 | ✅ trivy_prebuild | ☐ |
| B4 | 전송 전 민감정보(.env 등) 업로드 제외 + 사유 표시 | ✅ s3Deploy(excluded) | ☐ |

## C. 배포 (Deploy)

| # | 기준 | 기계 검증 | 실기기 확인 |
|---|---|---|---|
| C1 | S3 정적 배포 — 진행률 실시간, URL 반환 | ✅ s3 stream/progress | ☐ 실 AWS 1회 |
| C2 | 로컬 Docker 배포 — 태그 이미지 입력해도 400 없음 | ✅ containerName | ☐ |
| C3 | ECS 배포 시작·상태 폴링 | — | ☐ 실 AWS 1회 |
| C4 | 배포 정책 게이트(rego) — 조건 미달 차단 + 사유 | ✅ opa 12/12 | ☐ |
| C5 | 리전 불일치는 1회 경고 후 진행 가능(차단 아님) | ✅ regionGate | ☐ |
| C6 | 존재하지 않는 API 를 부르는 버튼 없음 | ✅ uiRequestContract | — |

## D. 감시·롤백 (Operate)

| # | 기준 | 기계 검증 | 실기기 확인 |
|---|---|---|---|
| D1 | 배포 후 연속 검증(헬스) 자동 시작·중지 | ✅ replace_safety | ☐ |
| D2 | 이상 감지 → 롤백 **제안** 카드 (자동 실행 금지) | ✅ rollbackApproval | ☐ 리허설 |
| D3 | 승인 시 이전 버전 복귀, 기록 남음 | ✅ rollback_target/run_args | ☐ 리허설 |
| D4 | 교체 실패 시 이전 컨테이너 복원 | ✅ replace_safety | ☐ |

## E. 온보딩·운영 비용

| # | 기준 | 기계 검증 | 실기기 확인 |
|---|---|---|---|
| E1 | AWS 프로필 원클릭 연결 (키 재입력 없음) | ✅ awsProfileConnect | ☐ |
| E2 | 키 없는 사용자 — 원클릭 IAM 셋업 동작 | ✅ awsOnboarding | ☐ 브라우저 1회 |
| E3 | 최소권한: 권한표·점검·템플릿이 한 원본 | ✅ aws_onboarding drift | — |
| E4 | 예산 알람 스크립트 동작 | ✅ budget payload | ☐ 1회 실행 |

## F. 게시

| # | 기준 | 기계 검증 | 실기기 확인 |
|---|---|---|---|
| F1 | VSIX 빌드 (경량) | ✅ dist 산출 확인 | ☐ 설치 스모크 |
| F2 | 플랫폼별 VSIX (Core 동봉) | — | ☐ OS 별 1회 |
| F3 | 의존성 취약점 0 (또는 사유 문서) | ✅ security-audit.md | — |

실기기 확인이 끝나면 이 파일의 ☐ 를 ✅ 로 바꿔 커밋한다 — 그 커밋이
카드의 "전 항목 ✅" 증빙이다.
