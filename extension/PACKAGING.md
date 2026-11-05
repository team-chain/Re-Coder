# ReCoder 사용자 배포본 빌드

배포 대상은 **Windows x64**, **Linux x64**, **macOS Apple Silicon(arm64)** 이다. 최종 산출물은 Python 런타임과 Core가 들어 있는
플랫폼 지정 VSIX(`dist/recoder-<버전>-win32-x64.vsix`, `-linux-x64.vsix`, `-darwin-arm64.vsix`)이며,
Core 실행에는 사용자 PC의 Python·Node.js 설치가 필요 없다. Windows x64는 Git·Node/npm이 없으면 고정 SHA256의 공식 도구를 자동 준비한다. 다른 OS의 Git·로컬 빌드 도구는 기존 설치를 사용한다. VS Code·AWS/AI 계정 연결은 별도다. Docker Desktop은 내 PC에서 컨테이너를 실행하는 로컬 모드에 필요하다. 관리형 S3/ECS 빌드·보안 검사는 AWS에서 실행한다. Intel Mac(darwin-x64)은 잠근 의존성(grpcio 등)의 x86_64 휠이 없어 대상에서 뺐다. `nobinary` 파일은 개발용이다.

현재 새 설치본의 대상은 Windows x64다. 버전별 빌드·설치본 실행 검사 결과는 `docs/managed-execution-release.md`에 기록한다. Linux·macOS 같은 버전의 설치 검증과 AWS 실환경 검증을 완료하기 전까지 해당 플랫폼·관리형 기능의 출시 완료로 표현하지 않는다.

`npm run package`는 Core가 포함되지 않으면 실패한다. 웹뷰 빌드와 `verify:package` 통과만으로 사용자 설치본이 완성되었다고 판단하지 않는다. 사용자 배포본은 아래 `build-release.py`로 플랫폼에 맞는 Core를 새로 빌드하고 버전을 확인한 뒤 생성한다. Core 없는 3.0.4 일반 VSIX는 설치만으로 작동하는 배포본이 아니며, 사용자 설치 안내에서 제외한다.

플랫폼별 Core는 해당 OS에서 빌드한다. Windows 실행 파일을 macOS/Linux용으로 이름만 바꾸어
배포하지 않는다. 기존 `package-extension.sh`는 준비된 다른 플랫폼 바이너리 패키징용으로 유지한다.

## 재현 가능한 빌드

저장소 루트에서 Python 3.12와 Node.js 20 이상으로 실행한다. 잠금 파일은 OS별로 따로 있다.

Windows (PowerShell):

```powershell
python -m venv .venv
./.venv/Scripts/python.exe -m pip install -r core/requirements-release-win32-x64.txt
npm ci --prefix extension
./.venv/Scripts/python.exe scripts/build-release.py
node extension/harness/release-smoke.js
```

Linux (bash):

```bash
python3.12 -m venv .venv
./.venv/bin/python -m pip install -r core/requirements-release-linux-x64.txt
npm ci --prefix extension
./.venv/bin/python scripts/build-release.py
node extension/harness/release-smoke.js
```

macOS (Apple Silicon, bash):

```bash
python3.12 -m venv .venv
./.venv/bin/python -m pip install -r core/requirements-release-darwin-arm64.txt
npm ci --prefix extension
./.venv/bin/python scripts/build-release.py
node extension/harness/release-smoke.js
```

macOS 잠금 파일은 Linux 잠금 파일과 같다(모든 항목에 macOS arm64 휠이 있음을 확인했다).
Linux 잠금 파일은 Windows 잠금 파일과 같은 버전을 쓰고, Windows 전용 패키지(`colorama`,
`pefile`, `pywin32-ctypes`)를 빼고 `uvloop`를 더한 것이다. 한쪽 버전을 올리면 다른 쪽도 같이 올린다.
Linux Core는 오래된 배포판에서도 돌도록 glibc가 낮은 환경(CI는 Ubuntu 22.04)에서 빌드한다.

`build-release.py`는 다음을 수행한다.

1. 격리된 Python 환경과 의존성 충돌을 확인한다.
2. 단일 `core/recoder-core.spec`으로 Core를 빌드한다.
3. 빈 사용자 홈에서 실행 파일의 `--self-check`를 실행한다. AWS·AI를 호출하지 않는다.
4. Core/확장 버전 일치를 확인하고 오픈소스 고지 파일을 생성한다.
5. 네이티브 Core를 임시로 `bin/`에 넣고 최신 웹뷰를 빌드해 플랫폼 지정 VSIX를 만든다.
6. 기존 `bin/`을 복구하고 SHA-256 체크섬·의존성 명세를 남긴다.

`release-smoke.js`는 실제 VSIX를 별도 폴더에 VS Code와 같은 방식(파일 권한 유지)으로 풀어
설치 모드의 CoreManager로 실행한다. Python이 없는 PATH, 빈 사용자 홈에서 세션 인증·AWS 미연결
화면 데이터·파일/함수 분석·Core 재시작·종료를 확인한다. 다른 사용자의 실행 중 Core에는 연결하지 않는다.

`THIRD_PARTY_NOTICES.txt`는 빌드한 OS의 의존성으로 다시 생성된다. 릴리스용이 아닌 빌드에서
생긴 변경은 커밋하지 않는다.

## 산출물

`extension/dist/`에 플랫폼마다 아래 세 파일이 생성된다.

- `recoder-<버전>-<플랫폼>.vsix`: 사용자 설치 파일
- `recoder-<버전>-<플랫폼>.vsix.sha256`: 배포 파일 무결성 확인값
- `recoder-<버전>-<플랫폼>.release.json`: 버전·플랫폼·Core/VSIX 해시·Python 의존성 명세

개발자의 `.env`, 자격증명, 테스트 하네스, 소스맵, 구형 UI는 포함하지 않는다.
명령/인프라 템플릿, CodeBuild를 포함한 AWS 서비스 정의, TLS 인증서, 분석 worker, WebSocket·ZIP 런타임을 포함한다. `verify-built-vsix.js`가 실제 VSIX의 필수 런타임과 Core를 현재 빌드 파일과 바이트 단위로 비교한다.

Windows 도구 자동 준비는 `node extension/scripts/check-portable-tools.js`로 별도 검증한다. 시스템 Git·Node/npm을 PATH에서 제외하고 공식 도구 설치, 임시 저장소의 커밋·로컬 원격 저장소 푸시, 실제 npm 빌드를 실행한다. GitHub 공개 푸시나 AWS 배포 성공 증거로 대체하지 않는다. 관리형 컨테이너 경로는 실제 CodeBuild→검사→ECR→승인→ECS→상태 확인까지 검증해야 한다.

## CI

GitHub Actions의 **Build release**를 수동 실행하면 `windows-vsix`(windows-latest)와
`linux-vsix`(ubuntu-22.04) 잡이 각자의 잠금 파일로 테스트·빌드·설치본 스모크를 수행하고
VSIX·체크섬·명세를 `recoder-win32-x64`, `recoder-linux-x64` artifact로 제공한다.
이 워크플로는 마켓플레이스 게시나 클라우드 리소스 변경을 수행하지 않는다.

## 개발과 설치 실행

- F5 개발 호스트는 활성 확장 옆의 `core/main.py`를 사용한다.
- 일반 설치는 설치 폴더의 `bin/recoder-core(.exe)`를 사용한다. 소스 Core로 대체 실행하지 않는다.
- 서로 다른 Core 경로를 동시에 사용하려 하면 기존 서버를 임의로 종료하지 않는다.
  F5 개발 호스트를 닫고 사용할 창에서 `ReCoder: Restart Core`를 실행한다.

## 마켓플레이스 게시 (현재 보류)

아직 게시하지 않는다. 게시할 때는 아래 순서를 따른다. 토큰은 게시하는 사람이 직접 만들고
직접 입력하며, 파일·저장소·CI 로그에 남기지 않는다.

1. **퍼블리셔 만들기** — [Marketplace 관리 페이지](https://marketplace.visualstudio.com/manage)에서
   ID가 `recoder-team`인 퍼블리셔를 만든다. `package.json`의 `publisher`와 정확히 같아야 한다.
   이미 다른 사람이 쓰는 ID면 새 ID로 만들고 `package.json`의 `publisher`를 바꾼다.
2. **게시 자격증명** — Azure DevOps에서 Personal Access Token을 만든다.
   Organization은 *All accessible organizations*, Scope는 *Marketplace → Manage*.
3. **로그인** — `npx @vscode/vsce login recoder-team` 후 토큰을 붙여 넣는다.
4. **릴리스 artifact 준비** — CI **Build release**의 두 artifact를 받아 `.sha256`으로 무결성을 확인한다.
5. **플랫폼별 게시** — 같은 버전을 두 번 게시한다. Preview 표시는 `package.json`의 `preview: true`로 붙는다.

   ```bash
   npx @vscode/vsce publish --packagePath recoder-<버전>-win32-x64.vsix
   npx @vscode/vsce publish --packagePath recoder-<버전>-linux-x64.vsix
   ```

6. **확인** — 마켓 페이지의 README·아이콘·Preview 배지, 각 OS의 설치 후 Core 시작을 확인한다.

게시 전 점검: `CHANGELOG.md`에 해당 버전 항목, README의 지원 환경 표가 실제 게시 플랫폼과 일치,
마켓 상세 페이지용 스크린샷(현재 README에 없음) 추가 여부.
같은 버전은 다시 게시할 수 없으므로 문제가 있으면 버전을 올려 재게시한다.
참고: [VS Code 공식 배포 문서](https://code.visualstudio.com/api/working-with-extensions/publishing-extension).
