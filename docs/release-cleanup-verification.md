# 배포 캔버스 개선 후 회귀 검증과 배포 정리

검증일: 2026-09-24. 작업 브랜치: `codex/improvements`.
승인된 UI 구현 기록은 [Workspace UI 검증](workspace-ui-verification.md)을 참고한다.

## 정리한 항목과 근거

`extension/src/extension.ts`, 분석 worker 진입점, `webview-src/index.tsx`에서
TypeScript import 경로를 추적하고 파일 경로로 로드하는 자산을 별도로 검색했다.

| 정리 | 근거 |
| --- | --- |
| `src/ui/sidebarProvider.ts`와 `src/collectors/`의 수집기 두 개 | 실제 확장 활성화 경로에서 참조되지 않으며 구형 provider는 tsconfig에서도 제외됨 |
| `src/sidebar/WorkbenchPanel.ts` | 생성하는 호출자가 없음. 현재 `ReCoderPanel`과 등록된 `WorkbenchSidebarProvider`는 유지 |
| `media/sidebar.js`, `media/workbench.js` | 현재 HTML은 React 번들 또는 `workbenchHtml.ts`의 스크립트를 사용. 이 두 파일을 로드하는 활성 경로 없음 |
| `media/icon.svg`, `media/recoder-logo.svg` | 로고는 화면 내부 SVG를 사용하고 확장 manifest는 `icon.png`와 `recoder-icon.svg`만 참조 |
| 예전 Home·ActionCard·StepBar와 미사용 아이콘·스타일·import | TypeScript의 미사용 선언 검사 및 호출 검색으로 확인 |
| ESLint 관련 개발 의존성 | 설정 파일 없이 실패하던 lint 명령을 실제 호스트·웹뷰 TypeScript 검사로 교체. 직접 의존성 3개, 설치 트리 77개 제거 |
| 패키지에 섞이던 테스트 하네스와 빌드 잔여물 | 전체 `harness/`를 VSIX에서 제외. 삭제한 소스의 JS/map은 다음 compile에서 제거 |

삭제한 구형 파일 6개는 합계 3,652줄이다. 기존 Workbench, 로컬 Docker,
ECS·S3, GitHub, Discord, 보안 게이트, 승인, 이력·롤백 경로는 유지했다.
Gemini의 두 SDK는 각각 실제 호출 경로가 있으므로 유지했다.
사용자 작업 파일·백업 bundle·별도 worktree는 삭제하지 않았다.

## 검증 중 발견해 수정한 문제

- `npm test`가 존재하지 않는 `out/test/runTest.js`를 가리켰다. 이제 API 스모크와 전체 확장 회귀 검사를 실행한다.
- 새 캔버스의 조회 호출과 IAM 정책이 불일치했다. 태스크 정의 조회를 추가하고, 로드밸런서 조회는 선택 리전으로 제한하고 예산 읽기는 해당 계정의 ARN으로 제한했다. API 이름과 IAM 이름이 다른 ELBv2·Budgets 매핑도 수정했다. 생성형 온보딩과 `infra/recoder-iam-quickcreate.json`을 동기화했다.
- 실행 중인 서비스의 이미지·digest는 기존에 허용된 ECS 태스크 조회에서 먼저 얻는다. 태스크가 없을 때만 태스크 정의를 조회한다. 추가 조회 권한을 기존 배포의 필수 시뮬레이션 목록에 강제로 넣지 않았다.
- Windows에서 종료된 프로세스의 핸들이 아직 존재하면 Core가 살아 있다고 오판했다. 이제 프로세스의 종료 신호를 확인하며, 조회 권한 오류를 죽은 프로세스로 취급하지 않는다. 경쟁 실행·이전 소유자 정리·충돌 종료 후 재시작 검사를 통과했다.
- Windows 드라이브 문자와 CRLF를 잘못 처리하던 Docker 스캐너 테스트 픽스처를 수정했다. 심볼릭 링크 권한 부족은 명시적 skip, POSIX 모드 검사는 POSIX에서만 수행한다. 실제 파일 내용·저장 복원·롤백 검사는 Windows에서도 실행한다.
- Discord의 비동기 테스트 의존성이 누락돼 있었다. 별도 `requirements-dev.txt`와 명시적 async fixture로 보완했다. 제품 런타임 의존성에는 pytest를 추가하지 않았다.

권한 범위는 AWS의 [ECS 권한표](https://docs.aws.amazon.com/service-authorization/latest/reference/list_ecs.html),
[ELBv2 권한표](https://docs.aws.amazon.com/service-authorization/latest/reference/list_elbv2.html),
[Budgets 권한표](https://docs.aws.amazon.com/service-authorization/latest/reference/list_budgets.html)를 기준으로 확인했다.
Windows 종료 판별은 [Microsoft의 프로세스 종료 설명](https://learn.microsoft.com/en-us/windows/win32/procthread/terminating-a-process)과
[WaitForSingleObject](https://learn.microsoft.com/en-us/windows/win32/api/synchapi/nf-synchapi-waitforsingleobject)의 계약을 따른다.

## 실행 결과

| 검사 | 결과 |
| --- | --- |
| 프로덕션 빌드·호스트/웹뷰 타입 검사 | 통과 |
| 전체 확장 `npm test` | 361 통과, 0 실패, Windows 환경 조건 2 skip |
| 전체 Core | 1,827 통과, 0 실패, 환경 조건 3 skip (374.87초) |
| Discord | 79 통과 |
| Watchdog | 65 통과 |
| 확장 호스트 → HTTP Core | 채팅·승인·재시작 인계·설계 결정·코드/ADR 생성·실제 파일 적용 통과 |
| 골든패스 | 픽스처 AI → 승인 → 파일 적용 → moto S3 업로드 확인, 7단계 통과 |
| 실제 React 브라우저 | Three.js 활성(`data-renderer=three`), 프로젝트→파일→함수 탐색, Develop 진입, 화면 이동 후 상태 유지, 콘솔 오류 0 |
| VSIX 구성 | 필수 실행/배포 파일 51개 확인. worker·웹뷰·ws 포함, fixture·소스맵·구형 화면 제외 |
| 실제 VSIX 압축 검사 | 53개 엔트리, 웹뷰 바이트가 프로덕션 빌드와 일치 |

확장의 2 skip은 POSIX 소유자 파일 모드와 Windows 심볼릭 링크 권한이다.
Core의 3 skip은 Windows 심볼릭 링크 권한, OPA 실행 파일 부재에 따른 Rego 직접 실행,
POSIX 파일 모드 검사다. 네 테스트 모음 합계는 2,332 통과, 0 실패, 5 skip이다.
Core에서는 기존 `datetime.utcnow()` 등 폐기 예정 API 경고 2,256건이 발생했다.
테스트 실패는 아니지만 향후 Python 의존성 갱신 시 별도 정리가 필요하다.
첫 Core 실행의 임시 디렉터리 접근 오류는 샌드박스 권한 문제였으며,
최종 실행은 저장소 밖의 새 임시 디렉터리를 사용했다. 저장소 안에 임시 Git 비저장소를
만들면 상위 저장소가 발견되므로, 브랜치 판별 테스트도 독립 임시 경로에서 수행했다.

검증 로그는 Git에서 제외된 `.canvas-qa/`에 보관한다:
`extension-release-tests.txt`, `core-release-final.txt`, `discord-release-tests.txt`,
`watchdog-release-tests.txt`, `release-e2e.txt`, `release-golden-path.txt`,
`release-package.txt`, `release-archive-audit.json`.

## 사용자 설치본 후속 작업 (1.1.0)

후속 작업에서 Core·Python 런타임을 동봉한 Windows x64 설치본
`extension/dist/recoder-1.1.0-win32-x64.vsix`를 만들었다.
이전 `1.0.0-nobinary`는 개발용으로만 남긴다.

- 서로 달랐던 PyInstaller 빌드 경로를 `recoder-core.spec`으로 통합했다.
- 명령 템플릿 JSON, 동적 모듈, AWS 모델 데이터, TLS 인증서와 스케줄러 플러그인을 포함했다.
- 빈 사용자 홈에서 바이너리 자체 검사 7개 항목이 통과했다.
- 실제 VSIX를 별도로 풀어 Python 없는 PATH에서 설치 모드 CoreManager로 기동했다.
  세션 인증, AWS 미연결 응답, 파일/함수 분석, 재시작, Windows 부모·자식 프로세스 종료가 통과했다.
- 별도 VS Code 사용자·확장 디렉터리에 CLI로 설치해 `recoder-team.recoder@1.1.0`으로 인식됨을 확인했다.
- 기동·종료 변경 후 확장 361개, Core 기동/인증 113개 회귀 검사가 통과했다.
- 고정 의존성 파일, 배포 빌드 스크립트, Windows CI, 체크섬과 의존성 명세를 추가했다.

설치 절차는 [1.1.0 설치 안내](releases/1.1.0-windows.md),
재빌드 절차는 [PACKAGING.md](../extension/PACKAGING.md)를 참고한다.
후속 로그는 `.canvas-qa/windows-release-build.txt`, `windows-release-smoke.txt`,
`vscode-1.1.0-install.txt`, `extension-1.1.0-tests.txt`, `core-1.1.0-startup-tests.txt`다.

## 사용자 실환경 검증과 배포 채널

현재 자동 검증은 실제 AWS 배포·유료 AI 호출·사용자 환경의 Docker Desktop 설치·
사용자 계정의 실환경 검증을 대체하지 않는다. 사용자 요청에 따라 AWS·AI를 사용하는
최종 기능 검증은 사용자가 수행한다. 설치본은 직접 전달할 수 있으며 Marketplace 공개
게시, 원격 게이트웨이 및 AWS 계정 리소스 변경은 이 작업에 포함하지 않았다.
기존 계정에 연결된 IAM 정책이나 CloudFront가 제공하는 온보딩 자산은 이 작업에서
변경하지 않았다. 새 조회 권한이 필요한 환경은 업데이트한 정책·템플릿의 배포가 필요하다.
