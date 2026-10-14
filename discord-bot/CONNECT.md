# Discord 로그인 연결

기존 Re-Coder 앱을 그대로 사용합니다. 일반 사용자는 봇 토큰이나 웹후크 URL을 입력하지 않습니다. 운영자가 아래 초기 설정을 한 뒤 봇 서버를 계속 실행해야 합니다.

## 1. 기존 앱 설정 — 운영자 1회

Discord Developer Portal https://discord.com/developers/applications 에서 기존 Re-Coder 앱을 선택합니다.

`discord-bot/.env.example`을 `.env`로 복사한 다음 아래 값을 넣습니다. 비밀 값은 채팅이나 Git에 올리지 않습니다.

- `DISCORD_BOT_TOKEN`: Bot 페이지의 봇 토큰. 기존 값을 잃어버렸다면 재발급해야 하며, 기존 실행 환경도 새 값으로 갱신해야 합니다.
- `DISCORD_CLIENT_ID`: General Information의 Application ID.
- `DISCORD_CLIENT_SECRET`: OAuth2의 Client Secret.
- `DISCORD_PUBLIC_URL`: 처음에는 `http://127.0.0.1:8765`.
- `DEV_GUILD_ID`: 테스트 서버 ID를 선택적으로 입력하면 해당 서버에 명령을 동기화합니다.

OAuth2 → Redirects에 다음 주소를 정확히 등록합니다.

```
http://127.0.0.1:8765/api/v1/connect/callback
```

일반 채팅 개발 요청을 사용하려면 **Bot → Privileged Gateway Intents → Message Content Intent**를 켜고 저장한 뒤 `.env`의 `DISCORD_MESSAGE_CONTENT=1`로 설정하고 봇을 재시작합니다. VS Code의 **Discord에서 개발 요청**을 켜면 해당 채널에 본인이 보내는 일반 채팅을 개발 요청으로 처리합니다. 개발 요청 전용 채널을 사용하세요. `/recoder develop`은 Message Content와 Server Members 특권 인텐트 없이도 작동합니다.

## 2. 같은 Mac에서 실행

저장소 루트의 터미널에서:

```bash
cd discord-bot
PYTHON_BIN=python3.12 bash run.sh doctor
bash run.sh
```

`doctor`는 토큰을 출력하지 않고 필수 설정의 존재와 Redirect URI를 확인합니다. `ReCoder Bot 준비 완료`가 나온 뒤 Discord 프로필의 온라인 상태를 확인합니다. 이 터미널을 켜 둡니다.

다른 터미널에서 상태 확인:

```bash
curl http://127.0.0.1:8765/api/v1/connect/info
```

`bot_ready`와 `oauth_ready`가 모두 `true`여야 합니다. 이 확인은 로그인/채널 전송 성공을 대신하지 않습니다.

## 3. 확장 설치 및 사용자 연결

Node.js 20 이상에서 저장소 루트 기준:

```bash
npm ci --prefix extension
npm run package:nobinary --prefix extension
code --install-extension ./extension/dist/recoder-1.1.10-nobinary.vsix --force
```

이 파일은 개발용 확장입니다. 일반 배포 기능의 Core는 별도 실행/설정이 필요하며, Discord 봇도 포함하지 않습니다. `Developer: Reload Window`를 실행합니다.

VS Code에서 작업할 프로젝트를 열고 ReCoder의 Discord 화면으로 이동합니다.

1. 연결할 프로젝트를 선택하고 **Discord 연결**을 누릅니다.
2. 브라우저에서 Discord 로그인/접근을 승인합니다. VS Code 알림과 브라우저에 같은 확인 코드가 표시되는지 보고 **이 프로젝트 연결**을 누릅니다.
3. 서버를 선택합니다. 봇이 없으면 **서버에 봇 초대**를 눌러 관리자가 설치하고 **목록 새로고침**을 누릅니다.
4. 채널을 선택합니다. 원격 개발도 쓰려면 **Discord에서 개발 요청**을 켜고 **이 채널에 연결**을 누릅니다.
5. **테스트 알림 보내기**로 실제 전송을 확인합니다. 배포 알림은 **배포 결과 알림** 스위치로 켭니다.

다른 서버의 채널, 본인이 쓸 수 없는 채널, 봇이 메시지/임베드를 보낼 수 없는 채널에는 연결할 수 없습니다. 동일 사용자의 한 채널은 한 프로젝트에만 연결할 수 있습니다. 프로젝트를 바꿔 연결하려면 이전 연결을 해제하거나 다른 채널을 선택합니다.

## 4. 개발 요청과 배포 알림

연결된 채널에 명령 접두사 없이 다음처럼 입력합니다:

```
hello.html 파일에 간단한 인사 페이지 만들어줘
```

기존 슬래시 명령도 사용할 수 있습니다:

```
/recoder develop prompt:hello.html 파일에 간단한 인사 페이지 만들어줘
```

본인의 연결된 프로젝트로만 생성 결과를 전달합니다. 새 연결은 기존 봇의 **단일 파일 Bedrock 생성기**를 재사용합니다. 임의의 다중 파일 에이전트로 확장한 것은 아닙니다. 서버 운영자는 Bedrock을 사용할 AWS 프로필/IAM 역할, 리전과 모델 접근 권한을 준비해야 합니다. 템플릿 예제 외 실제 AI 생성은 모델 호출 비용이 발생할 수 있습니다.

생성·수정은 허용한 프로젝트 파일에 반영되며 셸 실행은 VS Code의 확인을 거칩니다. 일반 채팅의 진행/완료 응답은 같은 채널에 답글로 표시되며, 슬래시 명령의 응답은 요청한 사용자에게만 표시됩니다. 봇·웹후크·DM·시스템 메시지는 처리하지 않으며, 작업 중에는 새 요청을 시작하지 않습니다. 개발 기능을 끄거나 연결 해제하면 새 요청을 처리하지 않습니다. 같은 채널의 다른 사용자는 자신의 프로젝트를 별도로 연결한 경우에만 자기 프로젝트에 요청할 수 있습니다.

배포·검사·롤백 알림은 배포 캔버스의 새 이벤트를 전송합니다. **배포 결과 알림** 설정은 프로젝트별로 저장되어 화면을 다시 열거나 VS Code를 다시 로드해도 유지됩니다. 로컬 Docker도 실행 결과를 전송하며 헬스 확인 전에는 대기, 확인 통과 후에는 완료, 배포 또는 헬스 확인 실패 시에는 실패로 알립니다. 동일 배포 결과의 반복 조회는 중복 전송하지 않습니다. 캔버스를 닫거나 VS Code를 종료한 동안의 이력을 나중에 자동 전송하지 않습니다. 여러 폴더를 연 경우 캔버스가 사용하는 첫 프로젝트의 알림은 그 프로젝트 연결로만 전송하며 다른 프로젝트를 선택한 상태에서는 알림 체크박스를 표시하지 않습니다.

## 5. 팀에 배포할 때

운영자가 계속 실행되는 서버와 HTTPS 도메인을 준비합니다. `DISCORD_PUBLIC_URL`을 그 주소로 변경하고 OAuth2 Redirect도 `<HTTPS 주소>/api/v1/connect/callback`으로 변경합니다. 같은 서버의 reverse proxy에서 `/api/v1/connect/*`를 로컬 봇 포트로 전달하고 `/api/v1/connect/ws`의 WebSocket 업그레이드를 허용합니다. 기존 운영자 등록 API는 외부에 공개할 필요가 없습니다.

각 사용자의 VS Code 설정 `recoder.discord.serverUrl`을 이 HTTPS 주소로 지정합니다. 배포 기본값을 이 주소로 제공하면 일반 사용자는 주소를 입력하지 않아도 됩니다. 현재 개발 빌드의 기본값은 localhost이며, 실제 운영 도메인은 아직 지정하지 않았습니다. 봇 토큰/Client Secret은 서버에만 두고 VSIX에 넣지 않습니다.

로그인은 5분 내 승인해야 하며 연결 세션은 30일 후 다시 로그인합니다. VS Code에는 프로젝트별 세션이 SecretStorage에 저장되고 서버에는 토큰 해시만 저장됩니다. 서버를 재시작해도 연결 설정은 SQLite에 유지됩니다. **연결 해제**는 세션과 프로젝트 연결을 폐기하고 연결된 소켓을 닫습니다.

## 검증 범위

자동 테스트는 Discord API/AI 응답을 대역으로 처리합니다. 실제 사용자 앱의 OAuth 승인, 서버 권한, Discord 전송, AWS 모델 호출은 자격증명 설정 후 별도로 확인해야 합니다.

기존 공유 키 기반 봇 화면은 **연결 관리 → 기존 봇 서버**에서 사용할 수 있습니다. 기존 학생 토큰 WebSocket을 계속 쓰려면 `recoder.bridge.legacyEnabled=true`를 지정합니다.
