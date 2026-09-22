# ReCoder 확장 패키징 — 설치만 하면 동작하는 VSIX 만들기

사용자가 **VSIX 하나 설치 → 코드 1회 입력 → AWS 키 없이 AI** 로 쓰게 하는 것이 목표다.
그러려면 Local Core 를 바이너리로 묶어 VSIX 에 동봉하고 게이트웨이 URL 을 기본값으로 넣는다.

## 왜 플랫폼별로 나누는가

Core 바이너리는 PyInstaller 산출물이라 **OS 마다 다른 파일**이다. 하나만 담아 배포하면
다른 OS 사용자는 설치는 되는데 Core 가 안 뜬다 — 확장이 조용히 아무것도 못 하는 상태가 된다.
(회차4 이전 `recoder-1.0.0.vsix` 에는 macOS 바이너리 하나만 들어 있었다.)

VS Code 는 이를 위해 platform-specific extension 을 지원한다. `--target <platform>` 으로
만든 VSIX 는 해당 플랫폼에만 배포되고, 마켓플레이스가 맞는 것을 알아서 내려준다.

## 1. Core 바이너리 빌드 (OS 별로, 각 OS 에서)

크로스 컴파일은 안 된다. **배포할 OS 에서 직접 빌드**해야 한다.

```bash
cd core
python -m venv .venv && source .venv/bin/activate   # Windows: .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt pyinstaller
pyinstaller recoder-core.spec --noconfirm
# 산출물: dist/recoder-core  (Windows 는 dist\recoder-core.exe)
```

빌드된 바이너리를 직접 실행해 `Application startup complete` 와 `http://127.0.0.1:178xx` 가
뜨는지 확인한다.

- `ModuleNotFoundError: X` → `recoder-core.spec` 의 `hidden` 목록에 `'X'` 추가 후 재빌드
- 템플릿 등 데이터 파일을 못 찾음 → spec 의 `datas` 에 경로 추가

## 2. 보관소에 넣기

패키징 스크립트가 읽는 위치는 아래 구조다. `bin/` 과 `bin-dist/` 는 둘 다 git 추적 대상이
아니다(`.gitignore`). 29MB 짜리 바이너리를 저장소에 넣지 않는다.

```
extension/bin-dist/
  darwin-arm64/recoder-core        # Apple Silicon
  darwin-x64/recoder-core          # Intel Mac
  win32-x64/recoder-core.exe       # Windows
  linux-x64/recoder-core           # Linux
```

파일 이름은 바꾸지 말 것. `CoreManager.ts` 가 `bin/recoder-core[.exe]` 라는 고정 경로를
찾는다(탐색 순서 2번). 스크립트가 대상 플랫폼의 것만 그 자리에 복사한다.

## 3. 게이트웨이 URL 베이크 (운영자)

사용자가 설정하지 않아도 되도록, 배포한 게이트웨이 URL 을 확장 기본값으로 넣는다.
`extension/package.json` 의 `recoder.gateway.url` default 를 실제 URL 로 바꾼다.

(반 코드(EnrollCode)는 최초 실행 시 사용자가 입력 — 베이크하지 않는다.)

## 4. VSIX 빌드

```bash
# bin-dist/ 에 있는 모든 플랫폼
bash scripts/package-extension.sh

# 특정 플랫폼만
bash scripts/package-extension.sh darwin-arm64

# Core 바이너리 없는 경량 VSIX (사용자가 core/ 를 직접 실행)
bash scripts/package-extension.sh --no-binary
```

산출물은 `extension/dist/recoder-<version>-<target>.vsix`.

스크립트는 끝나고 `bin/` 을 원래 상태로 되돌린다. 개발자의 F5 실행 환경을 바꿔 놓지 않기 위해서다.

## 5. 확인

VSIX 를 열어(`unzip -l`) 아래를 확인한다.

- `extension/bin/recoder-core[.exe]` 가 **하나만** 있는가 (다른 OS 바이너리가 섞이면 안 된다)
- `extension/out/webview/webview.js` 가 최신인가
- `extension/node_modules` 에 `ws` 만 있는가 — react-dom 이 보이면 `.vscodeignore` 나
  `package.json` 의 dependencies 가 되돌아간 것이다
- 크기가 **바이너리 크기 + 2~3MB** 수준인가

크기가 갑자기 몇십 MB 늘었다면 원인은 거의 항상 `node_modules` 다.

## 6. 마켓플레이스 게시 (아직 하지 않음)

현재는 **게시 가능 상태까지만** 준비돼 있다. 실제 게시하려면 추가로 필요하다.

1. Azure DevOps 조직 생성 → 퍼블리셔 등록 (`package.json` 의 `publisher` 와 이름이 같아야 한다)
2. Personal Access Token 발급 (Marketplace → Manage 권한)
3. `npx @vscode/vsce login <publisher>` 후 `npx @vscode/vsce publish --target <platform>` 를
   플랫폼마다 실행

게시 전에 확인할 것: `media/icon.png` 는 128×128 이상이어야 한다 — **현재 128×128 로 조건을 만족한다.**
(파일이 894바이트로 작지만 선 몇 개짜리 단순 도형이라 정상이다. 크기가 아니라 픽셀 치수를 볼 것.)

## 사용자 설치 경험

1. 본인 OS 용 `recoder-1.0.0-<platform>.vsix` 설치 (VS Code → 확장 → Install from VSIX)
2. 최초 실행 시 반 코드 입력 (명령 팔레트: `ReCoder: Connect to Gateway (Enroll)`)
3. 끝 — Core 자동 실행 + 게이트웨이로 AI

Ship(컨테이너 배포) 기능은 Docker Desktop 이 필요하다. 정적 사이트 S3 배포는 불필요.
