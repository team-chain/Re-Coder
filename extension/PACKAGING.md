# ReCoder 사용자 배포본 빌드

현재 배포 대상은 Windows x64다. 최종 산출물은 Python 런타임과 Core가 들어 있는
`dist/recoder-1.1.1-win32-x64.vsix`이며, 사용자 PC에 Python·Node.js를 설치할 필요가 없다.
VS Code와 기능별 외부 도구(Docker Desktop, Git), 사용자 AWS/AI 인증은 별도다.
`nobinary` 파일은 개발용이다.

## 재현 가능한 Windows 빌드

저장소 루트의 PowerShell에서 Python 3.12와 Node.js 20 이상으로 실행한다.

```powershell
python -m venv .venv
./.venv/Scripts/python.exe -m pip install -r core/requirements-release-win32-x64.txt
npm ci --prefix extension
./.venv/Scripts/python.exe scripts/build-release.py
node extension/harness/release-smoke.js
```

`build-release.py`는 다음을 수행한다.

1. 격리된 Python 환경과 의존성 충돌을 확인한다.
2. 단일 `core/recoder-core.spec`으로 Core를 빌드한다.
3. 빈 사용자 홈에서 실행 파일의 `--self-check`를 실행한다. AWS·AI를 호출하지 않는다.
4. Core/확장 버전 일치를 확인하고 오픈소스 고지 파일을 생성한다.
5. 네이티브 Core를 임시로 `bin/`에 넣고 최신 웹뷰를 빌드해 플랫폼 지정 VSIX를 만든다.
6. 기존 `bin/`을 복구하고 SHA-256 체크섬·의존성 명세를 남긴다.

`release-smoke.js`는 실제 VSIX를 별도 폴더에 풀어 설치 모드의 CoreManager로 실행한다.
Python이 없는 PATH, 빈 사용자 홈에서 세션 인증·AWS 미연결 화면 데이터·파일/함수 분석·
Core 재시작·종료를 확인한다. 다른 사용자의 실행 중 Core에는 연결하지 않는다.
실제 VS Code UI 및 사용자 AWS/AI 기능 검증 절차는 [설치 안내](../docs/releases/1.1.1-windows.md)에 있다.

## 산출물

`extension/dist/`에 아래 세 파일이 생성된다.

- `recoder-1.1.1-win32-x64.vsix`: 사용자 설치 파일
- `recoder-1.1.1-win32-x64.vsix.sha256`: 배포 파일 무결성 확인값
- `recoder-1.1.1-win32-x64.release.json`: 버전·플랫폼·Core/VSIX 해시·Python 의존성 명세

개발자의 `.env`, 자격증명, 테스트 하네스, 소스맵, 구형 UI는 포함하지 않는다.
명령/인프라 템플릿, AWS 서비스 정의, TLS 인증서, 분석 worker와 WebSocket 런타임은 포함한다.

## CI

GitHub Actions의 **Build Windows release**를 수동 실행하면 동일한 잠금 파일로
빌드와 설치본 스모크를 수행하고 VSIX·체크섬·명세를 artifact로 제공한다.
이 워크플로는 마켓플레이스 게시나 클라우드 리소스 변경을 수행하지 않는다.

## 개발과 설치 실행

- F5 개발 호스트는 활성 확장 옆의 `core/main.py`를 사용한다.
- 일반 설치는 설치 폴더의 `bin/recoder-core.exe`를 사용한다.
- 서로 다른 Core 경로를 동시에 사용하려 하면 기존 서버를 임의로 종료하지 않는다.
  F5 개발 호스트를 닫고 사용할 창에서 `ReCoder: Restart Core`를 실행한다.
- 플랫폼별 Core는 해당 OS에서 빌드한다. Windows 실행 파일을 macOS/Linux용으로
  이름만 바꾸어 배포하지 않는다. 기존 `package-extension.sh`는 준비된 다른 플랫폼
  바이너리 패키징용으로 유지한다.

## 공개 게시

VSIX는 직접 배포할 수 있다. Visual Studio Marketplace 게시에는 `recoder-team`
퍼블리셔의 게시 자격증명이 필요하며, 게시 시 해당 플랫폼과 최종 VSIX를 지정한다.
제품 자격증명이나 퍼블리셔 토큰을 파일에 넣지 않는다.
참고: [VS Code 공식 배포 문서](https://code.visualstudio.com/api/working-with-extensions/publishing-extension).
