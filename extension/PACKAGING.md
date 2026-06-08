# ReCoder 확장 패키징 — "설치만 하면 동작" VSIX 만들기

학생이 **확장 하나만 설치 → 반 코드 1회 입력 → AWS 키 없이 AI** 가 되도록,
Local Core 를 바이너리로 묶어 VSIX 에 동봉하고 게이트웨이 URL 을 설정한다.
(아래 1·3은 Windows 에서 직접 실행. 2는 운영자 설정.)

## 1. Local Core 바이너리 빌드 (PyInstaller)
```powershell
cd C:\ReCoder\Re-Coder\core
.\.venv\Scripts\Activate.ps1
pip install pyinstaller
pyinstaller recoder-core.spec --noconfirm
# 산출물: dist\recoder-core.exe
```
확인: `dist\recoder-core.exe` 를 직접 실행해 `Application startup complete` + `http://127.0.0.1:178xx` 가 뜨는지.
- 첫 실행에서 `ModuleNotFoundError: X` 가 나면 → `recoder-core.spec` 의 `hidden` 리스트에 `'X'` 추가 후 재빌드.
- 데이터 파일(template) 못 찾으면 → spec `datas` 에 경로 추가.

확장이 자동 탐색하는 위치에 복사:
```powershell
mkdir C:\ReCoder\Re-Coder\extension\bin -Force
copy dist\recoder-core.exe C:\ReCoder\Re-Coder\extension\bin\recoder-core.exe
```
> CoreManager 가 `extension/bin/recoder-core.exe` 를 1순위로 찾아 자동 spawn 합니다(학생이 python 실행 불필요).

## 2. 게이트웨이 URL 베이크 (운영자)
학생이 설정 안 해도 되게, 배포한 게이트웨이 URL 을 확장 기본값으로 넣는다.
`extension/package.json` 의 `recoder.gateway.url` default 를 실제 URL 로:
```json
"recoder.gateway.url": {
  "type": "string",
  "default": "https://tfuvwq54xg.execute-api.ap-northeast-2.amazonaws.com",
  "description": "..."
}
```
(반 코드(EnrollCode)는 학생이 최초 실행 시 입력 — 베이크하지 않음.)

## 3. VSIX 빌드
```powershell
cd C:\ReCoder\Re-Coder\extension
npm run build          # 확장 TS + 웹뷰 번들
npm run package        # = vsce package --no-dependencies  → recoder-1.0.0.vsix
```
`bin\recoder-core.exe` 가 VSIX 에 포함되는지 확인(없으면 `.vscodeignore` 에서 `bin/` 제외되지 않았는지 점검).

## 학생 설치 경험
1. `recoder-1.0.0.vsix` 설치 (VSCode → Extensions → "Install from VSIX").
2. 최초 실행 시 "반 코드를 입력하세요" → 강사가 준 EnrollCode 입력.
3. 끝 — Core 자동 실행 + 게이트웨이로 AI. (Ship 기능은 Docker Desktop 필요)
   - 재발급/변경: 명령 팔레트 `ReCoder: Connect to Gateway (Enroll)`

## 주의
- 바이너리는 **OS별**. Windows 학생용은 Windows 에서 빌드. (mac/linux 는 각 OS 에서 별도 빌드 → `bin/recoder-core`)
- PyInstaller 로 FastAPI 묶기는 보통 1~2회 hidden import 보강이 필요합니다. 첫 실행 에러를 보고 spec 을 다듬으세요.
- Discord 연동까지 원하면: 봇은 운영자가 호스팅, 학생은 워크벤치 Discord 탭에서 봇 초대 + `/recoder link`.
