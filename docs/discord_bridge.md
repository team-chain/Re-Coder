# ReCoder Bridge — 핸드폰 Discord → 노트북 VSCode 실시간 코드 삽입

이 문서는 디스코드 채팅에서 "테트리스 만들어줘 `tetris.html`" 같은 메시지를 보내면,
봇이 Bedrock(`claude-3-5-sonnet`)으로 코드를 생성하고 노트북의 VSCode 에디터에
**한 토큰씩 실시간으로 삽입**되는 기능의 설치/사용법입니다.

## 아키텍처

```
[핸드폰] ──Discord──→ [봇 프로세스] ──Bedrock converse_stream──→ [Claude]
                          │                                          │
                          ◀────────── 토큰 청크 ─────────────────────┘
                          │
                          │ WebSocket /ws (Authorization: Bearer <token>)
                          ▼
                  [노트북 VSCode 확장] ── editor.edit() ──→ [에디터에 실시간 삽입]
```

- **A 모드 (시작용)**: 봇과 VSCode 확장이 같은 노트북에서 동작. 브리지는 `127.0.0.1`에만 바인딩.
- **B 모드 (운영)**: 봇은 클라우드(EC2 등)에 두고, 확장은 노트북에서 `wss://`로 outbound 연결. 노트북 포트 개방 불필요.

A → B 전환은 환경변수 `RECODER_BRIDGE_BIND=0.0.0.0` 으로 노출하고 앞단에 TLS 프록시(Caddy/Nginx/Cloudflare Tunnel)만 두면 끝. VSCode 설정 `recoder.bridge.url`을 `wss://your-bot.example.com/ws`로 바꾸면 됩니다.

## 모델

`core/llm/bedrock_provider.py`와 **같은 환경변수**를 읽으므로 별도 설정 없이 현재 프로젝트가 쓰는 모델이 그대로 적용됩니다.

```env
BEDROCK_PRIMARY_MODEL_IDENTIFIER=anthropic.claude-3-5-sonnet-20241022-v2:0   # 기본
BEDROCK_REGION=us-east-1
```

## 설치 — 봇 측

```bash
cd discord-bot
pip install -r requirements.txt    # boto3 새로 추가됨
cp .env.example .env               # 이미 .env가 있다면 새 변수만 추가
```

`.env`에 추가/확인해야 할 항목 (**채널 ID는 여기 안 적어요** — VSCode에서 설정):

```env
DISCORD_BOT_TOKEN=...
BOT_REGISTRATION_KEY=long-random-string-1    # 봇 API 인증
RECODER_BRIDGE_BIND=127.0.0.1                # A 모드. B 모드는 0.0.0.0
RECODER_BRIDGE_PORT=7780
RECODER_BRIDGE_TOKEN=long-random-string-2    # 양쪽에 동일하게 설정
```

> **채널 ID는 .env가 아니라 VSCode Workbench → Build 탭에서 입력합니다.** 봇이 디스크에
> `bridge_settings.json`으로 저장하고 다음 실행에도 유지됩니다. 채널을 바꾸려면 UI에서
> 다시 저장 — 봇 재시작 불필요.

## 설치 — VSCode 확장 측

```bash
cd extension
npm install          # ws, @types/ws 새로 추가됨
npm run compile
```

VSCode 설정(JSON) — `Cmd+,` → "Open Settings (JSON)" → 추가:

```json
{
  "recoder.bridge.enabled": true,
  "recoder.bridge.url": "ws://127.0.0.1:7780/ws",
  "recoder.bridge.token": "long-random-string-2",
  "recoder.bridge.botApiUrl": "http://127.0.0.1:8765",
  "recoder.bridge.botRegistrationKey": "long-random-string-1"
}
```

확장을 재시작하면 좌측 하단 상태바에 `✔ ReCoder Bridge`가 보이면 연결 성공입니다.

## 채널 설정 (.env 안 만지고 UI로)

1. 디스코드 설정 → 고급 → **개발자 모드** 켜기
2. 핸드폰/노트북에서 원하는 채널 우클릭 → "ID 복사"
3. VSCode에서 **ReCoder 사이드바 → Workbench 열기 → Build 탭**
4. 상단의 **"ReCoder Bridge — Discord → VSCode"** 카드에서:
   - 채널 ID 입력란에 붙여넣고 "저장"
   - 카드의 상태 표시가 `대기 중` → 메시지 보내면 `연결됨`으로 바뀜
5. 채널을 바꾸려면 "해제" 후 다시 저장. 봇 재시작 불필요.

## 사용

1. 노트북에서 봇 실행: `cd discord-bot && python bot.py`
2. VSCode를 켜고 **워크스페이스 폴더가 열려있는 상태**인지 확인 (없으면 파일 생성 불가)
3. Workbench → Build 탭에서 채널 ID 저장 (위 참고)
4. 핸드폰 디스코드에서 지정 채널에 입력:
   - `tetris.html 만들어줘`
   - `app.py 간단한 Flask hello world 서버`
   - `game.js 클릭하면 점수 오르는 페이지`
5. VSCode에 새 파일이 만들어지고 코드가 한 토큰씩 들어가는 게 보임

### 메시지 규칙

- 메시지 어딘가에 **확장자 포함 파일명**(예: `tetris.html`, `Dockerfile.dev`)이 들어 있으면 그게 대상 파일이 됩니다.
- 없으면 봇이 "파일명을 포함해주세요" 답장으로 안내합니다.
- 파일이 이미 있으면 충돌 방지를 위해 `tetris.2026-05-27T01-23-45-678Z.html` 형식으로 새 파일이 생성됩니다.

## B 모드 전환 (나중에 클라우드로 봇 옮길 때)

봇 쪽 변경:

```env
RECODER_BRIDGE_BIND=0.0.0.0
```

앞단에 TLS 프록시(예: Caddyfile)

```
your-bot.example.com {
    reverse_proxy /ws localhost:7780
}
```

확장 쪽 변경:

```json
{
  "recoder.bridge.url": "wss://your-bot.example.com/ws"
}
```

토큰은 그대로. **확장은 outbound로만 연결**하므로 노트북 방화벽/공유기 설정은 손댈 게 없습니다.

## 보안 체크리스트

- [ ] `RECODER_BRIDGE_TOKEN`은 32자 이상 랜덤 문자열(예: `openssl rand -hex 32`).
- [ ] `RECODER_MAKE_CHANNEL_ID`로 명령 가능한 채널을 1개로 한정. 그 채널의 게시 권한도 신뢰 가능한 인원으로만.
- [ ] B 모드에선 반드시 TLS(`wss://`).
- [ ] 봇 IAM 정책은 `bedrock:InvokeModelWithResponseStream` 만 허용.

## 트러블슈팅

| 증상 | 원인/조치 |
|------|----------|
| 상태바 `끊김` 표시 | 봇이 안 떠있거나 URL/토큰 불일치. 봇 로그 `ReCoder Bridge 시작` 라인 확인. |
| "워크스페이스 폴더가 열려있지 않습니다" | VSCode에서 폴더를 하나 열어두세요. |
| 401 unauthorized | `RECODER_BRIDGE_TOKEN`과 `recoder.bridge.token` 값이 정확히 같은지 확인. |
| `throttlingException` | Bedrock 한도. 잠시 후 재시도 or 모델 변경. |
| 코드 펜스(\`\`\`)가 파일에 섞임 | 시스템 프롬프트가 막고 있고 확장 측에서도 라인 단위로 펜스 제거. 그래도 새면 시스템 프롬프트 강화 필요. |
