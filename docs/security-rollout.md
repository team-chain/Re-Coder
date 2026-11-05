# Security 통합 화면 및 PentAGI 연동

## 출시 판정

2026-11-05 / 3.0.8 최신 기준은 [기존 흐름 보존 검증](security-preserved-flow-release.md)이다. 기본 보안 검사와 AI 감사는 기존 Core·Develop AI 연결을 사용한다. 관리형 실행은 선택 기능이며 기존 인증·개발·배포 흐름을 대체하지 않는다. 아래는 최초 연동 구현의 설명이며, 실제 클라우드 실행과 PentAGI 전체 AI 검사·최종 보고서 검증은 아직 끝나지 않았다.

이전 Core 누락 산출물 `recoder-3.0.4-nobinary-preview.vsix`는 사용하지 않는다. 2026-11-02 현재 Core 3.0.4의 네이티브 빌드·동결 자체 검사와 Windows VSIX 생성을 확인했다. `npm run package`는 Core가 누락되면 실패한다. 패키징 성공은 클라우드 실행 검증을 대신하지 않는다.

현재 변경은 출시 전 통합 구현이다. PentAGI 실서버, 격리된 대상 앱, 패키징한 Core가 함께 검증되기 전에는 정식 출시 완료로 취급하지 않는다. 기존 설치 확장을 자동 교체하거나 마켓플레이스에 게시하지 않는다.

## 화면과 기존 동작

첫 화면은 확인할 항목 수, 우선순위가 높은 코드 발견 항목, 접을 수 있는 검사 상세, 배포 후 감시, 레드팀 테스트 진입점으로 구성한다. 검사 상세에는 기존 이미지/의존성(Trivy), Dockerfile(Hadolint), 시크릿(Gitleaks), 수정안 검토/승인/백업/재검사 UI를 재사용한다. 기존 Develop와 Deploy의 실행·승인·롤백 경로는 변경하지 않는다.

코드 검사는 기존 `/api/security/code`의 JavaScript/TypeScript 규칙 검사다. Cloudflare security-audit 심층 에이전트 감사로 표시하지 않는다. 지원 규칙이 없는 프로젝트, 미실행, 실패, 검사 대상 없음은 통과로 합산하지 않는다. 이 화면의 결과는 배포 정책 승인서를 대체하지 않는다.

OPA 연결 상태와 정책 번들 상태를 읽어 표시한다. 실제 배포 정책 평가는 기존 배포 파이프라인에서 실행한다. 임의로 구성한 검사 입력으로 운영 배포를 허용하지 않는다.

배포 후 감시는 이 PC에 저장된 전체 배포 이력을 대상으로 한다. SBOM이 없는 경우 미실행을 표시한다. 수동 OSV 요청 실패와 불완전한 응답은 성공으로 처리하지 않는다. 업데이트 제안은 기존 Operate의 승인 흐름으로 연결한다. 파일 변경은 배포된 이미지의 취약점 해결을 의미하지 않는다.

## 로컬 Docker 자동 실행

Security → 레드팀 테스트 → 레드팀 테스트 시작에서 로컬 앱 주소를 선택한다. `localhost:포트`는 검사 컨테이너가 접근하는 `host.docker.internal:포트`로 변환된다. 기존 ReCoder OpenAI 키가 있으면 재사용하고, 없으면 VS Code 네이티브 비밀번호 입력창에서 한 번 받는다. 키 변경은 ‘실행 조건 · 연결 설정’에서 할 수 있다. 개발 AI 공급자 설정을 변경하지 않는다. 이 자동 설치 경로는 OpenAI 또는 Anthropic API 키를 사용한다. 개발 AI 공급자 설정과 분리해 저장한다.

대상·비용·Docker 자원 생성을 확인한 뒤 다음 순서로 실행한다.

1. Docker Desktop Linux 엔진과 로컬 컨텍스트를 확인한다. 원격 Docker 컨텍스트는 거절한다.
2. 검증한 digest로 고정한 PentAGI, pgvector, Docker-in-Docker 이미지를 준비한다.
3. 무작위 세션 이름과 소유 라벨이 있는 전용 엔진·DB·네트워크·볼륨을 만든다. 호스트 Docker 소켓이나 프로젝트 폴더는 전달하지 않는다. 전용 엔진은 privileged 컨테이너이며 컨테이너 탈출까지 보증하는 보안 샌드박스는 아니다.
4. 포트를 열지 않은 초기화 컨테이너에서 마이그레이션한 뒤 기본 관리자 비밀번호를 무작위 값으로 바꾼다. API는 `127.0.0.1`의 임시 포트로만 열며, 임시 토큰을 자동 발급한다. DB와 전용 Docker API의 호스트 포트는 열지 않는다.
5. 별도 작업 프로세스가 실제 Flow·Task 상태를 조회하고 보고서를 저장한다. 전체 작업은 최대 30분이며, 다운로드 중에도 중단 요청을 확인한다. 창과 VS Code를 닫아도 프로세스는 계속된다.
6. 완료·실패·사용자 중단·시간 초과 시 보고서 저장을 시도한 다음 이 세션 소유 자원을 삭제한다. 준비 중인 Flow 취소에는 공식 `finishFlow`를 사용한다. 일반 `stopFlow`는 아직 로드되지 않은 Flow를 취소하지 못한다.

보고서는 VS Code globalStorage의 `redteam/rc-rt-…/report.json`에 남고 화면에서 열 수 있다. 임시 API 토큰·관리자 비밀번호는 보고서에 기록하지 않는다. 사용자 OpenAI 키는 SecretStorage에 남으며 Docker 실행 환경에 전달된다. Docker 관리자 권한을 가진 로컬 사용자는 컨테이너 환경을 볼 수 있다. 이미지 캐시는 다음 실행을 위해 유지하며 기존 프로젝트 컨테이너·볼륨·이미지는 삭제하지 않는다. `docker system prune`은 사용하지 않는다.

PC 강제 종료·Docker 중단 등으로 작업이 끊기면 다음 상태 조회에서 정리 필요 상태를 표시한다. Docker를 켠 뒤 ‘환경 정리 재시도’를 누르면 정확한 세션 이름과 소유 라벨을 확인해 정리한다. 자동 정리가 실패한 상태에서는 새 검사를 시작하지 않는다. 디스크 쓰기 실패 시 최종 보고서를 완전히 보존하지 못할 수 있으므로, 비용과 실행을 멈추는 정리를 우선하고 마지막 성공 스냅샷을 남긴다.

검증 명령(임시 Docker 환경을 만들며 실제 모델 키·유료 생성·외부 대상 공격을 사용하지 않음):

```powershell
cd extension
npm run compile
$env:RECODER_PENTAGI_DOCKER_CHECK = '1'
node scripts/check-local-pentagi.js
```

이번 작업 환경에서는 Docker CLI 29.3.1이 보이지만 Linux 엔진 named pipe 접근이 거부되어 실제 이미지 pull, 기동, PentAGI Flow 실행은 검증하지 못했다. mock 기반 실행 수명주기 테스트만 통과했다. 실제 Docker 엔진에서 초기 인증, 로컬 대상 접근, Flow 생성·조회·취소, 자원 정리, 기존 컨테이너 보존을 검증한 뒤 출시해야 한다.

## 별도 PentAGI 서버 연결

1. 직접 운영하는 PentAGI 서버를 준비한다. 원격 API는 신뢰할 수 있는 TLS 인증서가 있는 HTTPS가 필요하다. 인증서 검증을 끄는 옵션은 제공하지 않는다. 루프백 주소만 HTTP를 허용한다.
2. PentAGI에서 모델 공급자를 설정하고 Settings → API Tokens에서 토큰을 발급한다.
3. ReCoder Security → 레드팀 테스트 → PentAGI 연결을 누른다. 서버 주소와 토큰은 VS Code 네이티브 입력창에서 입력한다. 토큰은 SecretStorage에만 보관하며 웹뷰·로그·프로젝트 파일에 전달하지 않는다.
4. 대상 선택에서 PentAGI가 접근할 수 있는 테스트 앱의 주소를 입력한다. 지원 대상은 localhost, loopback, host.docker.internal, RFC1918 사설 IPv4다. URL 안의 사용자 정보·쿼리·fragment는 거절한다. 컨테이너 안의 localhost는 사용자 PC가 아니라 컨테이너 자신을 가리킨다.
5. 대상·서버·모델·범위·비용 안내를 네이티브 승인 창에서 확인한 뒤 실행한다.

공식 API 계약: https://pentagi.com/docs/api/overview/

GraphQL 스키마: https://github.com/vxcontrol/pentagi/blob/master/backend/pkg/graph/schema.graphqls

사용하는 API는 `providers`, `createFlow`, `flow`, `tasks`, `stopFlow`, 로컬 종료용 `finishFlow`다. 서버가 숫자로 반환하는 GraphQL ID도 처리한다. 5초 간격으로 실제 상태를 조회하며 가짜 백분율을 만들지 않는다. 작업 결과는 HTML 실행 없이 텍스트로 표시한다. `finished`는 실행 완료이지 취약점 없음이나 배포 허용을 의미하지 않는다.

## 별도 서버의 실패·중단·복구

- 실행 요청 전에 프로젝트별 실행 의도를 저장한다. 생성 요청은 자동 재시도하지 않는다.
- 명백한 HTTP 거절은 실패로 남긴다. 연결 끊김 등 실행 여부가 불확실하면 새 실행을 차단하고 서버 실행 목록의 ID로 복구한다.
- 중단 요청 성공과 실제 서버 상태를 구분한다. 중단 요청이 실패하면 실행 완료로 변경하지 않는다.
- VS Code를 닫아도 PentAGI 서버는 계속 실행된다. 서버 운영자가 작업·컨테이너를 정리할 수 있어야 한다.
- 최근 실행 메타데이터 20개를 프로젝트별로 보관한다. 자세한 과거 보고서는 PentAGI 서버에 남는다.
- API 요청은 30초 제한, 리다이렉트 거절, 최대 응답 2MB 제한을 적용한다.

## 서버에서 반드시 검증할 사항

대상 입력의 로컬 주소 검증과 PentAGI 프롬프트는 네트워크 격리를 대체하지 않는다. 현재 로컬 DinD 워커 egress를 승인한 대상에 방화벽으로 제한하지 않으며, 외부 네트워크 대상으로 접근할 가능성이 있다. UI와 네이티브 승인 화면은 이를 밝히고 격리된 테스트 환경만 허용하도록 안내한다. 이것은 프로덕션 격리 보장이 아니므로 실제 배포 전에 승인 대상 allowlist를 강제하는 네트워크 정책을 구현하고 우회 시험해야 한다. 운영 데이터/운영 자격증명/호스트 파일 마운트는 사용하지 않는다.

모델 예산, 실행 최대 시간, 요청 속도, 작업 동시성, 로그/보고서의 비밀값 마스킹·보존·삭제, 사용자별 권한 및 감사 이력은 실제 서버 설정과 함께 검증해야 한다. ReCoder의 창 종료를 비용 제한이나 중단 수단으로 사용하면 안 된다.

Cloudflare security-audit는 별도 실행기를 통해 소스 스냅샷, 단계별 검증, 구조화된 결과 검증을 연결해야 한다. 이번 코드 규칙 검사 연결만으로 해당 심층 감사 연동이 끝난 것은 아니다.

## 검증 명령

```powershell
cd extension
npm run lint
npm run build
npm test
node scripts/verify-package.js
```

```powershell
python -m pytest core/tests/test_security_dashboard.py core/tests/test_security_fix.py core/tests/test_security_scan_docker_fallback.py core/tests/test_vuln_advice.py core/tests/test_opa_policy_bundle.py -q
```

로컬 소켓이 차단된 Windows 샌드박스에서는 asyncio 생성과 확장 테스트의 로컬 서버가 멈출 수 있다. 이 경우 테스트를 통과로 간주하지 말고 로컬 소켓을 사용할 수 있는 격리된 테스트 환경에서 실행한다.

UI 픽스처는 `HARNESS_SECURITY=1`과 `PREVIEW_PORT=4183`으로 `extension/harness/workspace-preview.js`를 실행한다. 실제 웹뷰와 호스트를 사용하지만 검사 결과는 명시적인 예시이며 실 PentAGI/클라우드/스캐너를 호출하지 않는다.

출시 전 추가 확인: 고정 PentAGI 버전에서 생성·상태·결과·중단·네트워크 단절·재시작 복구, egress 차단 시험, 토큰 만료, 실제 스캐너 회귀, 새 Core를 포함한 Windows VSIX 설치 및 이전 버전 업그레이드. 이 검증 전에는 기존 배포판을 대체하지 않는다.
