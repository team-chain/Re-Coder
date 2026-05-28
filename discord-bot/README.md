# ReCoder Discord Bot

> "VSCode 밖에서도 작동하는 ReCoder" — Discord 안에서 바로 ECS 배포·롤백·코드 분석·Standup 브리핑까지.

SaaS 멀티-서버 모드로 운영되는 단일 봇이 여러 Discord 서버를 동시에 지원합니다. 각 서버 관리자는 `/recoder setup` 슬래시 커맨드로 자기 서버의 ReCoder Core API 엔드포인트와 알림 채널을 등록합니다.

---

## 빠른 시작

```bash
cd discord-bot
cp .env.example .env       # 이미 토큰이 들어있다면 그대로 사용
./run.sh                   # 가상환경 자동 생성 + 의존성 설치 + 봇 실행
```

내부적으로 `run.sh` 가 수행하는 일:
1. `.venv/` 가상환경이 없으면 생성
2. `requirements.txt` 가 갱신됐을 때만 의존성 재설치
3. `.env` 검증 (`DISCORD_BOT_TOKEN` 필수)
4. `python bot.py` 실행

윈도우에서 직접 돌리려면 `python bot.py` 만 호출하면 됩니다.

---

## 환경변수 (`.env`)

| 키 | 필수 | 설명 |
|---|---|---|
| `DISCORD_BOT_TOKEN` | ✅ | Discord Developer Portal → Bot → Token |
| `DISCORD_CLIENT_ID` | 권장 | `/recoder invite` 초대 URL 생성용 |
| `DEV_GUILD_ID` | 선택 | 설정 시 해당 서버에만 즉시 명령 동기화 |
| `STANDUP_CRON` | 선택 | Daily Standup cron (기본: `0 9 * * 1-5`, 평일 9시) |
| `BOT_HTTP_PORT` | 선택 | VSCode 자동 등록 + GitHub Webhook 포트 (기본 8765) |
| `BOT_REGISTRATION_KEY` | 선택 | VSCode 확장과 공유하는 비밀 키 |
| `GITHUB_WEBHOOK_SECRET` | 선택 | GitHub Webhook HMAC 검증용 |
| `RECODER_BRIDGE_BIND` | 선택 | 모바일 ↔ VSCode 브리지 바인드 주소 |
| `RECODER_BRIDGE_PORT` | 선택 | 브리지 포트 (기본 7780) |
| `RECODER_BRIDGE_TOKEN` | 권장 | 봇 ↔ VSCode 확장 공유 토큰 |

서버별 ReCoder API 엔드포인트는 `.env` 가 아니라 **Discord 내부 슬래시 커맨드** 로 설정합니다. 다음 절 참고.

---

## 슬래시 커맨드

### 일반
| 커맨드 | 설명 |
|---|---|
| `/recoder preflight cluster:<name> service:<name>` | ECS 배포 사전 점검 |
| `/recoder status [session_id:<id>]` | 현재 상태 조회 |
| `/recoder deploy cluster:<name> service:<name> image_tag:<tag>` | ECS 배포 (모달 확인 후) |
| `/recoder rollback cluster:<name> service:<name>` | 이전 리비전으로 롤백 |
| `/recoder code prompt:<text>` | 에러/코드 분석 |
| `/recoder forecast [service:<name>] [window_days:<n>]` | 배포 일기예보 (§41) |
| `/recoder workbench` | 인터랙티브 대시보드 |
| `/recoder invite` | 봇 초대 URL 표시 |

### 관리자 (`manage_guild` 권한)
| 커맨드 | 설명 |
|---|---|
| `/recoder setup api url:<url> token:<token>` | Core API 엔드포인트 등록 |
| `/recoder setup channel type:<deploy\|incident\|standup> channel:#chan` | 알림 채널 지정 |
| `/recoder setup user action:<add\|remove\|list> [user:@U]` | §6.1.4 user_id 화이트리스트 |
| `/recoder setup role action:<add\|remove\|list> [role:@R]` | 허용 역할 |
| `/recoder setup status` | 현재 설정 요약 |

설정은 `discord-bot/guild_config.db` (SQLite) 에 영속됩니다.

---

## 인증 (§6.1.4 Hybrid)

OR 결합 — 어느 하나라도 통과하면 허용:
1. 서버 관리자(`manage_guild`) → 항상 허용
2. `user_id` 가 서버 화이트리스트에 등록됨 (1차 게이트)
3. 사용자가 허용 역할을 보유 (보조 게이트)
4. DM 은 무조건 거부 (서버 단위 격리)

---

## 개발

### 단위 테스트

```bash
./run.sh test
# 또는
python -m pytest tests/ -v
```

`tests/` 구성:
- `test_guild_store.py` — SQLite CRUD + 격리 컨텍스트 매니저
- `test_recoder_client.py` — httpx.MockTransport 기반 HTTP 페이로드 검증
- `test_auth.py` — §6.1.4 Hybrid 인증 로직
- `test_embeds.py` — 임베드 빌더 Discord 한계(필드 25개, 길이 256/1024/4096) 검증
- `test_bridge_settings.py` — 채널 설정 영속화

### 정적 검증

```bash
./run.sh check          # pyflakes + bot.py import dry-run
```

---

## 파일 구조

```
discord-bot/
├── bot.py                    # 진입점 (RecoderBot, slash command 등록)
├── api_server.py             # 봇 내장 aiohttp 서버 (VSCode 등록 + GitHub Webhook)
├── recoder_client.py         # ReCoder Core API HTTP 클라이언트
├── recoder_bridge.py         # 모바일 → VSCode 실시간 코드 스트리밍 hub
├── guild_store.py            # 서버별 설정 SQLite 영속화
├── bridge_settings.py        # 브리지 채널 설정 JSON 영속화
├── make_handler.py           # /make 채널 메시지 → Bedrock 스트리밍
├── commands/                 # 슬래시 커맨드 빌더 (preflight/status/deploy/...)
├── middleware/auth.py        # Hybrid 인증
├── scenarios/                # §37.4~6 시나리오 (출근 브리핑 / 새벽 인시던트 / 팀 협업)
├── tests/                    # pytest 단위 테스트
├── requirements.txt
├── run.sh                    # 가상환경 + 의존성 + 봇 실행 헬퍼
├── .env.example              # 환경변수 템플릿
└── README.md
```

---

## 문제 해결

| 증상 | 원인 | 조치 |
|---|---|---|
| `DISCORD_BOT_TOKEN이 설정되지 않았습니다` 후 종료 | `.env` 미작성 | `.env.example` 복사 후 토큰 채우기 |
| `/recoder` 커맨드가 안 보임 | 전역 동기화는 최대 1시간 소요 | `DEV_GUILD_ID` 에 서버 ID 입력하면 즉시 |
| `이 서버에서 ReCoder 봇이 아직 설정되지 않았습니다` | API 미등록 | 관리자가 `/recoder setup api` 실행 |
| `🚫 접근 거부` | 화이트리스트/역할 미등록 | 관리자가 `/recoder setup user add @사용자` 또는 `/recoder setup role add @역할` |
| Standup 미전송 | `standup` 채널 미지정 또는 Core API 다운 | `/recoder setup channel type:standup channel:#chan` 확인 |
