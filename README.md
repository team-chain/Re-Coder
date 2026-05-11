# Re-Coder

> **From Error to Operation** — 에러 수정부터 운영 대응까지, VSCode 안에서 승인 기반으로.

ReCoder는 주니어 백엔드 개발자를 위한 AI DevOps 에이전트입니다.  
코드 에러 분석부터 Docker 빌드/배포, EC2 운영까지 VSCode Extension 하나로 처리합니다.

---

## 구성

```
VSCode Extension (TypeScript)
    ↕ HTTP REST (127.0.0.1:17894)
Local Core (Python + FastAPI)
    ↕ Docker / SSH / ECR
Local Docker / AWS EC2
```

---

## 3단계 기능

| Stage | 이름 | 내용 |
|---|---|---|
| Stage 1 | **Build** | 에러 자동 감지 → AI 분석 → 코드 패치 제안 → 승인 적용 |
| Stage 2 | **Ship** | Dockerfile 생성 → docker build/run → Health Check → GitHub 배포 |
| Stage 3 | **Operate** | EC2 SSH 배포 → ECR push → 운영 상태 조회 |

---

## 빠른 시작

### 사전 요구사항

- Python 3.11+
- Node.js 18+
- Docker Desktop
- AWS CLI (Bedrock 사용 시)

### Core 실행

```bash
cd core
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python main.py
```

### Extension 실행

```bash
cd extension
npm install
npm run compile
# VSCode에서 F5 → Extension Development Host 실행
```

---

## 환경 설정 (core/.env)

```env
# AWS Bedrock (AI 기능 필수)
AWS_PROFILE=default
BEDROCK_REGION=us-east-1
BEDROCK_PRIMARY_MODEL_IDENTIFIER=us.anthropic.claude-sonnet-4-6
BEDROCK_SECONDARY_MODEL_IDENTIFIER=us.anthropic.claude-sonnet-4-5-20250929-v1:0
BEDROCK_FAST_MODEL_IDENTIFIER=us.anthropic.claude-haiku-4-5-20251001-v1:0

# 개발 모드 (선택)
DEV_MODE=1
```

---

## 변경 이력

| 날짜 | 내용 |
|---|---|
| 2026-05-07 | v6.4-final 기준 초안 구현 (core 27개, extension 7개 파일) |
| 2026-05-08 | P0-1~P0-13 전항목 완료, pytest 15/15 통과 |
| 2026-05-10 | 실행 환경 버그 5종 수정 (venv 탐색, 환경변수명, CSP 등) |
| 2026-05-12 | 런타임 버그 6종 수정 (push 폴백, rollback, enum 역직렬화 등) + EC2 배포 기능 구현 + Bedrock 모델 업데이트 |

---

자세한 구현 현황은 [PROGRESS.md](./PROGRESS.md), 에이전트 인계 가이드는 [HANDOFF.md](./HANDOFF.md)를 참고하세요.
