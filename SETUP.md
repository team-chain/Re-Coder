# ReCoder — 처음부터 설정·실행 가이드 (운영자용)

완전 새 환경(새 PC / 새 클론)에서 전체를 세팅·실행하는 순서.
구성요소는 4개: **Local Core**(VSCode 확장의 엔진) · **Bedrock 게이트웨이**(학생이 키 없이 AI) · **Discord 봇**(선택) · **VSCode 확장**.
학생(최종 사용자)은 맨 아래 §6만 하면 됩니다.

---

## 0. 사전 설치 (한 번)
- **Git**, **Python 3.11+**, **Node.js 20+**, **VSCode**
- 배포용: **AWS 계정** + **AWS CLI** + **AWS SAM CLI**
- Ship 기능(도커 빌드·검증)용: **Docker Desktop**
- Discord 연동용: **Discord 봇 Application**(아래 §4)

확인:
```powershell
git --version ; python --version ; node --version ; aws --version ; sam --version
```

## 1. 클론
```powershell
git clone https://github.com/team-chain/Re-Coder.git
cd Re-Coder
git checkout develop      # 최신 작업 브랜치
```

## 2. Local Core (개발 실행)
```powershell
cd core
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python main.py            # http://127.0.0.1:17894 (사용 중이면 17895~)
```
- AI를 본인 AWS로 직접(BYO) 쓰려면 `aws configure` + Bedrock 모델 액세스 필요.
- 게이트웨이로 쓰려면 §3 후 `core/.env`에 `RECODER_LLM_GATEWAY_URL`·`RECODER_STUDENT_TOKEN` 추가.

## 3. Bedrock 게이트웨이 배포 (학생이 AWS 키 없이 AI 쓰게)
```powershell
aws configure                       # 운영자 계정 자격증명
# AWS 콘솔 → Bedrock → Model access 에서 사용할 리전에 Claude 3 Haiku 활성화
cd ..\gateway
sam build
sam deploy --stack-name recoder-gateway --region <리전> --capabilities CAPABILITY_IAM --resolve-s3 --no-confirm-changeset --parameter-overrides "AdminKey=<랜덤문자열>" "EnrollCode=<반코드>" "MaxStudents=30"
```
출력 `EnrollEndpoint`/`InvokeEndpoint`/`AdminEndpoint` 확보. (상세: `gateway/README.md`)
- 검증: `/enroll`로 토큰 발급 → `/llm/invoke`로 AI 응답 확인.
- **AWS 계정 교체 시**: `core/switch_aws.py --region <리전>` 후 `sam deploy` 재실행 (`core/AWS_ACCOUNT_SWITCH.md`).

## 4. Discord 봇 (선택 — 디스코드 연동)
1. [Discord Developer Portal](https://discord.com/developers/applications) → New Application → **Bot → Reset Token** 으로 토큰 발급.
2. **Bot → Privileged Gateway Intents**: **Message Content** + **Server Members** 둘 다 ON (필수).
3. `discord-bot/.env` 에 `DISCORD_BOT_TOKEN=<토큰>` 설정.
4. 실행:
```powershell
cd ..\discord-bot
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe bot.py
```
→ 봇 로그인 + 브리지(ws 7780) + HTTP API(8765) 동시 기동. 이 터미널은 켜 둔 채로.

## 5. VSCode 확장
**(a) 개발 실행 (소스에서 바로):**
```powershell
cd ..\extension
npm install
npm run build             # 확장 TS + 웹뷰 번들
# VSCode 로 extension 폴더 열고 F5 → Extension Development Host
```
**(b) 배포본 VSIX 만들기 (학생 배포용):**
```powershell
# 1) Core 바이너리 빌드 (core 에서, venv 활성)
cd ..\core ; pip install pyinstaller ; pyinstaller recoder-core.spec --noconfirm
mkdir ..\extension\bin -Force ; copy dist\recoder-core.exe ..\extension\bin\recoder-core.exe
# 2) package.json 의 recoder.gateway.url 기본값에 §3 의 GatewayUrl 베이크
# 3) 패키징
cd ..\extension ; npm run build ; npx @vscode/vsce package --no-dependencies
# → recoder-1.0.0.vsix
```
상세: `extension/PACKAGING.md`.

## 6. 학생(최종 사용자) — 이것만 하면 됨
1. `recoder-1.0.0.vsix` 설치: VSCode → Extensions → "···" → **Install from VSIX**
   (또는 `code --install-extension recoder-1.0.0.vsix`)
2. ReCoder 사이드바 열기 → Core 자동 실행(번들 exe, Python 불필요).
3. (게이트웨이 URL 베이크된 경우) 최초 실행 시 **반 코드 입력** → AWS 키 없이 AI 사용.
4. Ship 기능엔 **Docker Desktop** 필요. Discord 연동은 워크벤치 Discord 탭에서 봇 초대 + `/recoder link`.

---

## 의존성 한눈에
| 구성요소 | 실행 | 포트 | 비고 |
|---|---|---|---|
| Local Core | `python main.py` 또는 번들 exe | 17894~17910 | 확장이 자동 spawn |
| 게이트웨이 | `sam deploy` (AWS) | API Gateway(HTTPS) | 운영자 계정, 프리티어 |
| Discord 봇 | `python bot.py` | 7780(ws)·8765(http) | 선택, 운영자 호스팅 |
| 확장 | F5 또는 VSIX 설치 | — | 학생은 VSIX만 |

## 빠른 점검
- Core 안 뜸: `~/.recoder/runtime.json`·`core.lock` 삭제 후 재시도.
- 봇 오프라인: 봇 프로세스 미기동(정상) 또는 인텐트 미설정.
- AI AccessDenied: 해당 리전 Bedrock 모델 액세스 미활성.
