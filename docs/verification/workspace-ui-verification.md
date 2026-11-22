# Workspace UI 개선 검증 — 2026-09-23

사용자가 승인한 화면 구성을 실제 React 웹뷰에 적용했다. Deploy는 이전 참고 이미지의 입체 배치를 Three.js로 그린다. 기존 배포 실행·승인·차단·롤백 경로를 연결한 채 상단 탐색, 카드 배치, 연결과 상세 패널을 정리했다.

## 자동 검증

| 검증 | 결과 |
| --- | --- |
| `npm run build` | 확장 TypeScript 및 프로덕션 웹뷰 빌드 성공, 917 KiB |
| 전체 웹뷰 회귀 테스트 | 351개 중 349 통과, 실패 0, 환경 조건으로 2 건너뜀 |
| 최종 렌더링·완료 안내 변경 후 관련 테스트 | 38 통과 |
| 확장 → HTTP Core 통합 하네스 | 채팅 요청, 승인, 확장 재시작 인계, 설계 결정, 코드·ADR 생성, 실제 파일 적용 통과 |
| Three.js 자원·좌표 검사 | 입체 받침과 DOM 대상 좌표 일치, 공유 자원 중복 해제 없음 |
| 보안 검사 순서 검사 | 도구별 순차 요청, 다른 패널 응답과 이전 요청 응답 무시, 재시도 구분 |

Windows에서 불가능한 `?` 파일명은 같은 URI 인코딩 동작을 검증하는 한글·공백·`#`·`%` 파일명으로 대체한다. POSIX 파일 모드 검사는 기존 조건대로 건너뛴다. 심볼릭 링크 검사는 Windows에서 실제 생성 시 EPERM이 발생할 때만 사유와 함께 건너뛴다. 제품의 파일 처리나 권한 검사를 완화하지 않았다.

통합 하네스는 실제 컴파일된 호스트와 HTTP Core에 고정 응답의 테스트 LLM을 연결한다. 첫 실행의 임시 파일 권한 경고는 샌드박스 밖에서 같은 격리 하네스를 다시 실행해 해소했고 전 구간을 통과했다. 실제 AWS 자원 생성이나 유료 AI 호출은 이번 검증에 포함하지 않는다.

## 실제 브라우저 확인

실제 React 컴포넌트를 사용하는 격리된 미리보기에서 확인했다. AWS·GitHub·Discord 외부 요청은 발생하지 않는다.

- Three.js 렌더러 활성화, 입체 받침·게이트·연결선 표시.
- AWS 미연결 ECS로 드래그해도 설정·배포 실행 없음.
- 샘플 프로필 연결 후 실제 응답의 계정·리전과 대상 이름 반영.
- 프로젝트 드래그 → ECS 설정 → 승인 카드. 승인 전 실행 0건, 승인 후 1건.
- 프로젝트 → 파일 8개 → 함수 6개 탐색, 고립·과부하 표시.
- Security 검사 중 Develop로 이동한 뒤 돌아와도 세 결과 유지.
- 배포 완료 시 서비스 열기와 결과 상세 표시.
- 2D 선택 시 SVG 대체 및 WebGL 캔버스 제거, 해제 후 Three.js 복원.
- 390px 폭에서 공통 메뉴, Develop 한 열 카드, 배포 대상 목록, 완료 링크 접근.
- 기본 폭에서 Develop 2×2 배치, 중복 작업 전환 메뉴 제거, AWS 추가 설정 접힘.

## 실행

`extension` 폴더에서 `npm run build` 후 VS Code의 `Run Extension` 디버깅을 재시작한다. 새 Extension Development Host에서 테스트 프로젝트 폴더를 열고 ReCoder를 실행한다.

검증 명령:

```powershell
npm run test:webview
node harness/e2e-extension-flow.js
npx webpack --config harness/canvas-preview.config.js
node harness/serve-canvas-preview.js
```

미리보기: `http://127.0.0.1:4179/?layout=workspace&clean=1`.
상태 전환과 승인 요청 횟수 확인: `http://127.0.0.1:4179/?layout=workspace`.

로컬 로그는 `.canvas-qa/workspace-redesign-tests.log`, `.canvas-qa/workspace-redesign-final-tests.log`, `.canvas-qa/workspace-redesign-e2e.log`에 있으며 Git 추적에서 제외한다.
