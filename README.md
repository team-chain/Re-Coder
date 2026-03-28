# 🤖 AI 업무 어시스턴트 디바이스

> **Context-Aware AI Workspace Assistant**  
> 동양미래대학교 컴퓨터소프트웨어공학과 2026학년도 졸업작품  
> 이동규 · 윤세빈 · 임주현

개발자의 화면을 AI가 실시간으로 보고 있다가, 에러가 나면 **먼저 알려주고 해결까지 도와주는** AI 업무 어시스턴트 디바이스.

---

## 핵심 차별점

| 차별점 | 기존 AI 툴 (Cursor, Copilot) | 이 프로젝트 |
|---|---|---|
| 툴 종속성 | 에디터 안에서만 동작 | AWS 콘솔, Docker, VMware, WSL 전부 커버 |
| 인식 방식 | 코드만 분석 | OS 레벨 화면 전체 인식 |
| 반응 방식 | 내가 먼저 물어봐야 함 | 에러 나면 AI가 먼저 알림 |
| 기억 | 매 대화가 독립적 | 개발 히스토리 전체를 알고 있음 |
| 맥락 | 지금 이 순간만 앎 | 에러 직전 타임라인 자동 보유 |
| 반복 에러 | 매번 새 대화 | 같은 에러 몇 번째인지 앎 |

---

## 프로젝트 구조

```
ai-manager/                  # 루트 (모노레포)
├── agent/                   # 데스크탑 에이전트 (윤세빈)
│   ├── main.py              # 진입점 (freeze_support + asyncio.run)
│   ├── first_run.py         # 초기 설정 창 (Gemini 키 + 서버 로그인)
│   ├── monitor.py           # 비동기 4개 루프 메인
│   ├── analyzer.py          # Gemini Vision + 알림 + 음성
│   ├── ws_client.py         # WebSocket + httpx RAG
│   ├── uploader.py          # 서버 전송 (AI 요약 텍스트만)
│   ├── collectors/
│   │   └── collect.py       # OS 레벨 수집 (프로세스, 터미널, 클립보드, 창)
│   ├── output/
│   │   └── sessions/        # 키 프레임 + index.json (로컬만, git 제외)
│   └── requirements.txt
│
├── backend/                 # FastAPI 서버 (임주현)
│   ├── main.py
│   ├── routers/             # auth, sessions, ws, rag
│   ├── services/rag.py      # pgvector 임베딩/검색
│   └── db/init.sql
│
├── frontend/                # React 대시보드 (이동규)
│   └── src/
│       ├── pages/           # Login, Dashboard, Chat
│       └── components/      # Timeline, ErrorHistory, ChatBox, AgentStatus
│
├── .env.example             # 환경변수 템플릿 (git 포함)
├── .gitignore
└── README.md
```

---

## 시작하기

### 요구사항

- **Python 3.12** (3.13+ 미지원 — torch/easyocr 호환 문제)
- Windows 10/11 (에이전트는 Windows 전용)
- Gemini API 키 ([발급하기](https://aistudio.google.com/app/apikey))

### 설치

```bash
# 1. 저장소 클론
git clone https://github.com/your-repo/ai-manager.git
cd ai-manager/agent

# 2. 가상환경 생성 (Python 3.12 필수)
py -3.12 -m venv venv
venv\Scripts\activate

# 3. 의존성 설치
pip install -r requirements.txt
```

### 실행

```bash
# agent/ 디렉터리에서
python main.py
```

최초 실행 시 자동으로 초기 설정 창이 열립니다.
1. **탭 1 - 서버 로그인**: 이메일/비밀번호 입력
2. **탭 2 - Gemini API 키**: 발급받은 키 입력
3. **시작하기** 버튼 클릭

---

## 시스템 아키텍처

```
사용자 PC (Windows)
  5초마다 캡처 → EasyOCR(ThreadPool) + gc.collect()
  변화감지 → Gemini Vision (사용자 키, 로컬 직접 호출)
  키 프레임만 로컬 저장 (에러/해결 순간만)
  AI 요약 텍스트 → 서버 전송 (1분마다)
  WebSocket + Ping/Pong(20s) → 서버 상시 연결
            │ HTTPS + WebSocket
EC2 t3.micro (Ubuntu 24.04)
  Nginx → Uvicorn --workers 1 → FastAPI
  PostgreSQL + pgvector (세션 + RAG + 유저 통합)
  IAM Role → cron pg_dump → S3 (30일 Lifecycle)
            │
웹 대시보드 (React + S3 + CloudFront)
```

### 데이터 흐름

```
[5초마다]
화면 캡처 (mss)
  ↓
EasyOCR 텍스트 추출
  ↓
변화 감지 판단 (에러 키워드 OR 새 명령어 OR 창 전환)
  ↓ YES
Gemini Vision 분석 (이미지 + OS 스냅샷)
  ↓
결과 → Windows 알림 + 음성 브리핑 + 로컬 index.json 저장
  ↓ (1분마다)
AI 요약 텍스트만 서버(PostgreSQL) 전송
  ↓
pgvector 임베딩 저장 → RAG 검색 가능
```

### 저장 위치

| 데이터 | 저장 위치 | 설계 근거 |
|---|---|---|
| 스크린샷 (키 프레임만) | 로컬 PC만 | 가장 민감한 데이터. 절대 외부 전송 안 함 |
| index.json (세션 인덱스) | 로컬 PC만 | 원시 이벤트 로그. 서버 전송 불필요 |
| AI 요약, 세션/유저 | PostgreSQL (EC2) | JOIN 가능한 단일 통합 DB |
| 벡터 임베딩 (RAG) | pgvector (EC2 내) | PostgreSQL에 통합. 추가 비용 없음 |
| DB 백업 | S3 db-backups/ (30일) | IAM Role로 키 없이 업로드 |
| 웹 대시보드 | S3 + CloudFront | 정적 파일 호스팅, 프리티어 범위 |

---

## 개발 우선순위

| 우선순위 | 기능 | 상태 |
|---|---|---|
| P0 | mss 멀티모니터 캡처 | ✅ 구현 |
| P0 | EasyOCR 에러 키워드 감지 | ✅ 구현 |
| P0 | Gemini Vision 분석 + 에러 해결 | ✅ 구현 |
| P0 | 키 프레임만 저장 | ✅ 구현 |
| P0 | Windows 트레이 알림 | ✅ 구현 |
| P0 | first_run.py 초기 설정 창 | ✅ 구현 |
| P1 | 창 전환 감지 | ✅ 구현 |
| P1 | 터미널 새 명령어 감지 | ✅ 구현 |
| P1 | 클립보드 변화 감지 | ✅ 구현 |
| P1 | 로컬 세션 인덱스 저장 | ✅ 구현 |
| P2 | 서버 세션 저장 | ✅ 구현 (uploader.py) |
| P2 | pgvector RAG | ✅ 구현 (ws_client.py) |
| P2 | WebSocket 채팅 | ✅ 구현 (ws_client.py) |
| P3 | gTTS 음성 브리핑 | ✅ 구현 (analyzer.py) |
| P3~P5 | 패닉 버튼, 팀 기능 등 | 🔜 추후 개발 |

---

## 알려진 한계 및 대응

| 한계 | 대응 방안 |
|---|---|
| EC2 t3.micro OOM | Swap 2GB 설정 (배포 가이드 참고) |
| PyInstaller Fork Bomb | `freeze_support()` main.py 최상단 |
| WebSocket workers 2+ 상태 불일치 | `--workers 1` 필수 |
| WebSocket NAT 타임아웃 | `ping_interval=20` Ping/Pong |
| JWT 토큰 만료 (30일) | uploader.py 401 감지 → 자동 재로그인 |
| EasyOCR 최초 로드 30~60초 | 트레이에 '로딩 중' 표시 (추후) |
| Python 3.13+ 미지원 | Python 3.12 필수 |
| Windows 전용 | win32gui/psutil Windows 의존 (졸업작품 범위) |

---

## 환경변수

`.env.example`을 복사하여 `.env`를 만들고 값을 채우세요.

```bash
cp .env.example .env
```

| 변수 | 설명 | 설정 방법 |
|---|---|---|
| `GEMINI_API_KEY` | Google Gemini API 키 | first_run.py에서 입력 |
| `API_BASE_URL` | 백엔드 서버 주소 | .env.example 참고 |
| `API_WS_URL` | WebSocket 서버 주소 | .env.example 참고 |
| `USER_TOKEN` | JWT 토큰 | first_run.py 로그인 후 자동 저장 |
| `USER_ID` | 사용자 ID | first_run.py 로그인 후 자동 저장 |

⚠ `.env` 파일은 절대 git에 올리지 마세요.

---

## 개발 일정

| # | 기간 | 작업 | 완료 기준 |
|---|---|---|---|
| 1 | 4월 1주 | mss 캡처 + EasyOCR 에러 감지 | 에러 코드 실행 시 콘솔에 에러 키워드 출력 |
| 2 | 4월 2주 | Gemini Vision 연동 + 알림 | 에러 감지 → Windows 알림 팝업 확인 |
| 3 | 4월 3주 | 창 전환/터미널/클립보드 감지 + 세션 인덱스 | index.json에 이벤트 정상 누적 |
| 4 | 4월 4주 | first_run.py + EC2 배포 | curl http://ec2-ip/docs 정상 응답 |
| 5 | 5월 1주 | JWT 인증 + 세션 저장 API | 에이전트 → 서버 세션 저장 확인 |
| 6 | 5월 2주 | WebSocket + RAG | 웹에서 질문 → 에이전트 처리 → 답변 반환 |
| 7 | 6월 | React 웹 대시보드 | 웹에서 전체 흐름 정상 동작 |
| 8 | 7월 | gTTS 음성 브리핑 + 패닉 버튼 | 에러 감지 → 음성 출력, 단축키 즉시 분석 |

---

## 비용

| 항목 | 부담 | 6개월 | 비고 |
|---|---|---|---|
| Gemini Vision API | 사용자 | 각자 부담 | 변화 감지 시에만 호출 |
| EC2 t3.micro | 운영자 | $0 | AWS 프리티어 12개월 |
| PostgreSQL + pgvector | 운영자 | $0 | EC2 내 운영 |
| S3 DB 백업 | 운영자 | $0.1 이하 | 30일 Lifecycle 적용 |
| 운영자 총합 | 운영자 | 약 $0.22 | AI 비용 제외 |

---

## .gitignore 필수 항목

```
.env
agent/output/
__pycache__/
*.pyc
dist/
*.spec
node_modules/
frontend/build/
```
