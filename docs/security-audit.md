# 의존성 취약점 · 최소권한 감사 기록

보드 카드 「최소권한 정리 + 의존성 취약점(Dependabot) 해소」의 산출물.
DoD: 경고 0 또는 사유 문서화.

## 1. 의존성 취약점 — 2026-09-19 기준 전부 0건

| 대상 | 도구 | 결과 |
|---|---|---|
| extension (프로덕션 의존성) | npm audit --omit=dev (npm 10.9.7) | 취약점 0 |
| extension (dev 포함 전체) | npm audit | 취약점 0 |
| core/requirements.txt | pip-audit 2.10.1 (PyPI advisory DB) | 취약점 0 |
| core/requirements-dev.txt | pip-audit 2.10.1 | 취약점 0 |
| gateway/requirements.txt | pip-audit 2.10.1 | 취약점 0 |

재실행 방법:

```bash
cd extension && npm audit
pip install pip-audit
pip-audit -r core/requirements.txt
pip-audit -r core/requirements-dev.txt
pip-audit -r gateway/requirements.txt
```

Dependabot 대조: 위 감사는 로컬 lockfile/requirements 기준이다. GitHub
Security 탭의 Dependabot 알림이 위 결과와 다르면 — Dependabot 은 과거
커밋의 lockfile 이나 GitHub Actions 워크플로 의존성까지 보므로 — 알림의
대상 파일·버전을 확인하고 이 표에 사유를 추가할 것. 현재 로컬 기준으로는
해소할 경고가 없다.

## 2. 최소권한 (AWS)

원본은 `core/aws_policy.py` 하나다. 배포에 필요한 액션만 자원 범위를 좁혀
선언하며(53개 액션, `recoder-*` 자원 규칙), 다음 세 경로가 전부 이 원본을
쓴다:

- `GET /api/aws/policy` — 사용자가 콘솔에 붙여넣는 권한표
- `GET /api/aws/onboarding-link` + `infra/recoder-iam-quickcreate.json` —
  원클릭 IAM 셋업 (드리프트는 `core/tests/test_aws_onboarding.py` 가 차단)
- `POST /api/aws/permissions/check` — 연결된 키가 과하거나 부족한지 점검

원칙 (aws_policy.py 주석에 근거 있음):

- `task_role` 은 기본 비움 — 안 쓰는 배포에 PassRole 을 열지 않는다.
- 이름 와일드카드 입력은 400 거부 — `role/*` 정책을 뽑을 수 있으면
  이 기능의 존재 이유가 없다.
- 학교(AWS Academy) 계정은 역할 자동 치환 대신 환경변수 안내 —
  정책과 배포 경로가 갈라지는 것을 막는다.

## 3. 남은 항목

- GitHub Security 탭(Dependabot) 실측 확인은 저장소 관리자 권한 필요 —
  로컬 감사 0건이므로, 알림이 있다면 대부분 낡은 lockfile 스캔이다.
  확인 후 위 표에 한 줄 추가로 종결.
