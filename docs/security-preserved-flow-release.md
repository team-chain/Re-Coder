# 기존 흐름을 유지한 보안 기능 개선 — 3.0.8

기준: 2026-11-05. 기존 Core·AI/AWS 인증·배포·승인 구조를 유지하고 기능을 추가한다. 새 서버나 인증 체계로 전환하지 않는다. 사용자는 추가 AWS 정책 적용을 거절했다. 이 권한을 다시 요구하거나 기본 기능의 선행 조건으로 넣지 않는다.

3.0.8에서는 기존 Bedrock을 실제로 호출하면서 발견한 감사 응답 오류를 수정했다. 단계별 필수 필드·타입을 모델에게 명시하고 Core에서 다시 검사한다. 같은 줄의 SQL 인젝션을 다른 제목·인용 범위·수정 문장으로 반복하는 경우 하나로 합치되, 같은 줄의 별도 권한 문제는 유지한다. 중복 후보 중 높은 심각도를 보존하고 독립 소스 검토 두 번을 그대로 적용한다.

## 사용자 동작

1. Security의 **보안 검사 시작**: 기존 Core의 코드 규칙, 이미지, Dockerfile, 시크릿 검사와 정책 상태 확인. 추가 AWS 환경 준비 없음.
2. **AI 감사 → 검사할 소스 검토**: 현재 Develop의 AI 연결 사용. 최대 100개 파일·160KiB를 검토 후 전달하고 최대 20회 호출·15분으로 제한. 기존 비용 기록과 공급자 선택을 사용한다.
3. 감사 결과의 **Develop에서 수정 검토**: 기존 코드 생성 입력창에 요청을 준비한다. 자동 전송·적용하지 않으며 원래 승인·diff 흐름을 유지한다.
4. **레드팀 테스트**: 실행 방식 선택 후에만 해당 기능의 상태를 확인한다. 로컬 테스트 앱 또는 현재 AWS 연결의 앱 사본 검사. 새 환경은 그 추가 기능을 선택한 경우에만 검토 후 준비한다.
5. 기존 S3/ECS 배포는 원래 로컬 빌드·승인·SSE·롤백을 사용한다. 원격 빌드는 `recoder.experimental.managedExecution=true`로 명시적으로 켜야 선택 단계가 추가된다.

## 정확성·실패 처리

- AI는 프로젝트 파일을 실행하거나 수정하지 않는다. 실제 파일·줄·인용문이 일치하는 후보만 독립 검토 두 번을 거쳐 표시한다. 실행 증명으로 과장하지 않는다.
- .env·키 파일·숨김 경로·설치 의존성·빌드 출력은 수집에서 제외하며, 알려진 비밀값은 기존 Core 필터로 가린다. 하드코딩된 모든 비밀을 제거한다고 보장하지 않는다.
- 승인 거절은 모델 호출 0회다. 중단 요청 뒤 새 모델 호출을 보내지 않으며 이미 진행 중인 응답이 끝날 때까지 중단 대기를 표시한다.
- 실패·잘린 응답·시간 초과를 검사 통과로 처리하지 않는다. 저장 실패가 기능을 영구적으로 잠그지 않는다.
- 실행 중 소스가 바뀌면 이전 사본 결과라고 표시한다. 확장 재시작 후 미완료 기록은 중단된 검사로 표시한다.
- 프로젝트를 전환하면 기존 검사 큐를 끊고 이전 응답을 무시한다. 원격 사본의 성공 결과가 현재 프로젝트의 미검사 상태를 덮지 않는다.

## 검증 증거

- 최종 확장 전체: 801개 중 798개 통과, 3개 건너뜀, 실패 0. `npm run lint` 및 웹뷰 production 빌드 통과.
- 이번 Core 관련 회귀: 94개 통과. 소스 감사 응답 계약, Security, 스캐너 Docker 폴백, 기존 AWS 연결·AI 연결 갱신, 로컬 빌드·Docker/S3 SSE를 포함한다. 기존 3.0.7의 267개 회귀와 중복 합산하지 않는다.
- 기존 Docker 빌드 테스트가 오래된 subprocess 호출을 가로채 실제 Docker를 호출하던 문제를 발견했다. 현재 프로세스 래퍼를 가로채도록 시험 격리를 수정했고 성공/오류/시간 초과 계약은 그대로 검증한다. Docker가 실제로 빌드됐다는 증거로 세지 않는다.
- production 웹뷰 + 실제 SidebarProvider/SourceAuditHost를 별도 Edge headless 프로필에서 검증했다. 기본 검사 클릭, 기존 AI 어댑터의 7단계 실행, 저장 결과 재열기, 레드팀 방식 선택/결과, 이전 프로젝트 응답 무시, 폭 700px 레이아웃, 런타임 예외 없음 확인.
- 위 UI 검사의 스캐너·모델·PentAGI 응답은 명시적 fixture다. 실제 유료 서비스 검증을 의미하지 않는다. 화면: `.test-artifacts/security-3.0.7-ui.png`.
- Windows 네이티브 Core 자체 검사와 VSIX 패키징 통과. 설치 파일 내부의 74개 파일을 현재 빌드와 비교했다.
- VSIX를 별도의 빈 사용자 환경에 풀어 Python 없는 PATH에서 실행했다. Core 인증, AI 키 없는 상태의 진단, 새 감사 API의 입력 검증, 소스/파일 분석, 배포 계획과 SSE 취소, Core 재시작/종료를 확인했다. GitHub는 미인증 상태의 오류 처리 검증이며 실제 원격 push를 수행하지 않았다.
- 실제 VS Code 1.144.1의 분리된 사용자 프로필에서 3.0.7 설치 후 3.0.8 업그레이드, 기존 테스트 설정 유지, 확장 활성화, 설치된 Core 시작·인증, Develop·Security·Deploy 명령과 Workbench 웹뷰 탭, 종료를 확인했다. 테스트용 별도 확장을 실행 드라이버로 사용했고 ReCoder 자체는 설치 모드로 실행했다. 이 시험은 네이티브 화면의 픽셀 검수나 실제 사용자 계정의 로그인 UI 검증은 아니다.
- 기존 Bedrock `global.anthropic.claude-haiku-4-5-20251001-v1:0`으로 네이티브 Core 3.0.8의 실제 코드 감사를 완료했다. 합성 SQL 인젝션 한 건을 한 번만 표시, 13회 호출, 결과 저장·재열기, 소스 불변, 기존 AWS 연결 불변을 확인했다. 사용자 프로젝트 코드는 모델에 보내지 않았다. 다른 모델·프로젝트 전체의 정확도를 보장하는 시험은 아니다.
- 실제 Docker의 Hadolint·Gitleaks·Trivy 실행, OPA 저장소 정책 23개, 공개 OSV와 합성 SBOM 대조·수정 버전·이력 저장·중복 알림 방지를 확인했다. Trivy의 990개 발견은 검사 대상으로 고른 Docker 도구 이미지의 결과이며 사용자 앱이나 ReCoder의 취약점 수가 아니다. 외부 알림은 전송하지 않았다.
- 실제 로컬 Docker 배포 API에서 별도 HTTP 앱의 보안 검사, 이미지 빌드, 이미지 검사 통과, 실행, 상태 확인, HTTP 본문, 완료 이전 SSE 전달을 확인했다. `queued → build → scan → start → health → screen → record → done`. 보안 검사 생략 옵션을 사용하지 않았다. 시험 중 만든 컨테이너와 이미지 태그만 정리했다.
- 실제 PentAGI Docker 초기화·API 인증·중첩 컨테이너의 테스트 앱 접근·작업 생성/조회/취소·소유 리소스 정리를 확인했다. 모델 키 대체값을 사용했으므로 실제 AI 검사 완료나 최종 보고서 검증으로 세지 않는다.

로그:
- `.test-artifacts/3.0.8-extension-tests.log`
- `.test-artifacts/3.0.8-lint.log`
- `.test-artifacts/3.0.8-core-regression.log`
- `.test-artifacts/3.0.8-live-audit-native.log`
- `.test-artifacts/live-security-20261105-203646/verification.json`
- `.test-artifacts/3.0.8-live-scanners.log`
- `.test-artifacts/live-scanners-20261105-003153/verification.json`
- `.test-artifacts/3.0.7-live-pentagi-runtime.log`
- `.test-artifacts/3.0.8-live-deploy.log`
- `.test-artifacts/live-deploy-20261105-204353/verification.json`
- `.test-artifacts/vscode-3.0.7/verification-3.0.8.json`
- `.test-artifacts/3.0.7-ui-smoke.log`
- `.test-artifacts/security-3.0.7-ui-requests.json`
- `.test-artifacts/release-3.0.8-build.log`
- `.test-artifacts/release-3.0.8-installed-smoke.log`

## 설치 파일

- `extension/dist/recoder-3.0.8-win32-x64.vsix` — 43,498,953 bytes
- VSIX SHA256: `b3de2a9ceed9493f1f164f6ac233845f002e92aefdfd350ba7806324339310c8`
- Core SHA256: `2e3c392ab5635dba79f46cb6c20f20b291520e7e4d9c93c7b3e51032e3965ef3`
- 빌드 명세: `extension/dist/recoder-3.0.8-win32-x64.release.json`
- VS Code의 **Extensions → … → Install from VSIX…**에서 이 파일을 선택하고 **Developer: Reload Window**를 실행한다. 기존 AI/AWS 연결을 다시 등록할 필요는 없다.

## 외부 환경 검증 범위

Docker Desktop 4.67의 재시작 시 임시 AF_UNIX 소켓 접근 오류가 재발했다. 로그와 0바이트 소켓만 있는 실제 경로를 확인한 뒤 `AppData/Local/Docker/run` 및 `AppData/Local/docker-secrets-engine` 폴더를 `.recoder-verification-<시각>` 이름으로 보존하고 재시작했다. 이미지·컨테이너·볼륨·설정 초기화나 Docker 업그레이드를 하지 않았다. 마지막 복구 백업은 `20261105-203603`이다. 이 환경 오류의 영구 해결을 제품 수정으로 주장하지 않는다.

Docker 재시작 직후 첫 배포에서 이미지 ID 조회/실행 실패가 한 번 발생했다. 직접 Docker 빌드·ID 실행·태그 실행은 성공했고 이후 API 배포는 연속 두 번 이미지 검사와 실행을 완료했다. 첫 성공 뒤 시험 도구가 원래 요청 포트로 접속하던 문제를 수정했으며, 마지막 시험은 승인된 포트의 HTTP 응답까지 확인했다. 최초 Docker 이미지 실패의 원인을 확정하거나 재발 불가능하다고 보장하지 않는다.

AWS 연결 자체는 실제 STS로 확인했으며 새 자격증명을 등록하지 않았다. CloudFormation 권한 부족 상태는 사용자가 추가 정책을 거절했으므로 그대로 유지한다. 관리형 스택·CodeBuild 실행과 PentAGI의 실제 AI 전체 테스트/보고서는 미검증이다. 실제 GitHub 원격 push, Discord 전송, S3/ECS 재배포는 이번 검증에서 수행하지 않았다. 작업 검토용 브랜치로 공유할 수 있지만 모든 선택 기능의 프로덕션 출시 완료로 표시하지 않는다.
