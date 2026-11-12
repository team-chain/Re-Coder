# 엔터프라이즈 하네스 (3.1, 1~3단계)

서비스·DB가 여럿인 앱을 개발 에이전트가 **생성 → 함께 띄워 검증 → 고침**까지 하게 만드는 장치입니다.
기준 브랜치: `codex/security-verified-sebin1103-20261110`.

## 흐름

```
요청 ("게이트웨이·주문·상품 마이크로서비스, PostgreSQL·MongoDB·Redis")
  │  architecture.wants() — 여러 서비스·여러 DB를 원하는 새 앱만
  ▼
0. 아키텍처 계약 (AI 1회 → architecture.normalize)
     서비스(api·worker·web, node·python, 포트, 의존), DB(엔진, 테이블), API(경로, 인증, 요청 예)
     이름·폴더(services/<이름>)·접속 변수(<DB>_URL, <서비스>_URL)·포트를 ReCoder가 확정
  ▼
1. 설계·생성 (gen_engine) — 모든 프롬프트에 계약 글(contract_text)
     AI가 목록에 넣은 Dockerfile·compose는 버리고 compose_kit.fixed_ops()로 교체(fixed)
  ▼
2. 전체 점검 루프 (code_agent)
     정적 점검 → compose_check.run() — 일회용 복사본 + 이번 실행만의 비밀값 + 127.0.0.1 임의 포트
       빌드 → 기동(--wait, DB healthcheck → 서비스 healthcheck) → 연결(A 안에서 B_URL/health)
       → 테이블(information_schema) → 계약 통합 테스트(architecture.test_cases)
     실패 → compose_check.issues() → 원인 파일(로그 경로 > 라우트 파일 > 진입 파일) → edit_fix_round
     실패가 줄지 않으면 멈춤(나빠졌으면 직전 상태로)
  ▼
결과: 프로젝트 + docker-compose.yml + contract-tests.json + 검증 결과(UI: DB·서비스·연결·테이블·통합 테스트 통과 수)
```

## 통합 테스트 규칙 (`architecture.test_cases`)

| 경우 | 기대 |
|---|---|
| 서비스마다 `GET /health`(web은 `/`) | 2xx |
| 로그인 필요 API를 토큰 없이 | 401 또는 403 (2xx면 "인증 없이 열림") |
| 응답 코드를 정한 API | 그 코드 |
| 그 밖의 API | 5xx 아님, 경로 변수가 없으면 404도 아님(라우트 존재) |

같은 규칙을 Core(파이썬)와 프로젝트의 `scripts/contract-test.mjs`(Node, 의존성 없음)가 씁니다.

## 검증한 것

| 항목 | 방법 | 결과 |
|---|---|---|
| 계약 정규화·compose·Dockerfile·.env | 단위 테스트 16개 + `docker compose config` | 통과 |
| 요청 한 줄 → 계약 → 생성 → compose 실패 → 교정 → 통과 | 가짜 AI·가짜 Docker E2E (`test_enterprise_e2e.py`) | 통과 |
| 판정 로직이 실제 HTTP·SQL에서 맞는가 | 실제 Postgres 16·Redis·uvicorn·node 프로세스로 fixture 실행(Docker 없이) | 정상판 13/13, 결함판에서 `GET /api/orders` 무인증 노출을 `services/orders/app/main.py`로 지목 |
| Node 실행기와 파이썬 판정이 같은가 | 실제 HTTP 서버 | 일치 |
| 계약 대조 | 단위 테스트 6개 + 생성 교정·배포 게이트 연동 시험 | 위반을 파일·줄로 지목, 위반 시 배포 차단 |
| 기존 기능 | Core 전체 · 확장 전체 테스트 | 기존과 같음(환경 문제 2건: AWS 클라이언트 기본값, Docker Hub 차단으로 인한 빌드 테스트) |

## 아직 검증하지 않은 것 (사용자 PC에서)

- **실제 컨테이너로 끝까지:** 개발 환경에서 Docker Hub가 막혀 이미지 빌드·기동은 못 돌렸습니다.
  `python scripts/verify-enterprise-live.py fixture` — 요금 없음, Docker Desktop과 인터넷 필요.
- **실제 AI로 생성 성공률:** `python scripts/verify-enterprise-live.py generate --budget 3` — AI 요금 발생.
  여러 번 돌려 성공률을 재기 전에는 "엔터프라이즈 앱을 완성한다"고 말할 수 없습니다.

## 한계 · 다음 단계

- 로컬 다중 서비스 배포(`compose_deploy`)는 구현했습니다: 서비스별 보안 게이트 → 빌드 → 이미지 검사 → 기동 → 검증 → 기록, 실패 시 자동 복구, 배포 이력에서 승인 롤백.
- AWS 다중 서비스 배포(compose → ECS 서비스 여러 개 + RDS + ElastiCache/DocumentDB)와 묶음 단위 감시는 4단계입니다. 그 전까지 다중 서비스 프로젝트의 ECS·S3 배포는 자원 생성 전에 안내와 함께 막습니다(`compose_deploy.aws_block`).
- 통합 테스트는 계약 수준(라우트·인증·서버 오류)이고, 업무 규칙은 명세 하네스(합격 조건 시험)가 다중 서비스에서도 봅니다. 화면 조건처럼 HTTP 로 확인할 수 없는 것은 "확인 불가"로 남습니다.
- 구조 분석(코드 맵)에 계약 대조를 붙였습니다(`contract_check`, `GET /api/architecture/check`): 서비스가 읽는 환경 변수·선언한 API·하드코딩된 호스트를 계약과 비교합니다. 정적 분석이라 동적으로 만든 변수 이름·URL은 못 봅니다. 실제 호출 확인은 compose 통합 테스트가 맡습니다.
- 서비스 언어는 Node·Python, DB는 PostgreSQL·MySQL·MongoDB·Redis입니다. 메시지 브로커(Kafka·RabbitMQ)는 아직 계약에 없습니다(Redis 대기열로 대체).
- 계약 단계는 요청에 여러 서비스·여러 DB를 뜻하는 말이 있을 때만 켜집니다(`RECODER_ARCHITECTURE=0`으로 끔).
