# ReCoder — 코드 컨벤션 / 브랜치 / PR 정책 v10

본 문서는 4명 팀원(A/B/C/D)이 **충돌 없이 병렬 작업**하기 위한 강제 규칙.
변경 시 PR로 본 문서 갱신 + 다른 팀원 1명 리뷰.

---

## 1. Python 컨벤션 (core/, discord-bot/)

### 1.1 포매팅
- **Black** (line-length 100, target-version py311)
- **Ruff** (`E,F,W,I,N,UP,B,A,C4,T20,SIM`) — `T20` print 문 발견 시 경고만, 에러 X
- **isort** (Black 호환 profile)
- 모든 commit 전 `pre-commit run --all-files` 또는 수동으로 black + ruff 실행

### 1.2 명명 규칙
- 모듈/패키지: `snake_case` (예: `release_contract.py`)
- 클래스: `PascalCase` (예: `ReleaseContract`)
- 함수/변수: `snake_case` (예: `compute_quality_score`)
- 상수: `UPPER_SNAKE_CASE` (예: `DEFAULT_TIMEOUT_SEC`)
- 비공개 멤버: 앞에 `_` (예: `_internal_cache`)
- Pydantic 필드: `snake_case` (JSON 응답에서도 동일 — JS 측이 변환 책임)

### 1.3 타입 힌트
- **모든 public 함수는 타입 힌트 필수.**
- `from __future__ import annotations` 사용 (forward reference 자유롭게).
- `Optional[X]` 보다 `X | None` 권장 (Python 3.10+).
- `Any` 사용 시 주석으로 사유 명시.

### 1.4 docstring
- Google-style 또는 짧은 한 줄. 설계서 섹션 참조 의무 (예: `"""§29 Release Contract — recoder.yml..."""`).
- Public API 함수는 다음 형식:
  ```python
  def foo(bar: str) -> int:
      """간략 설명 (§ref).

      Args:
          bar: ...

      Returns:
          ...

      Raises:
          ValueError: when ...
      """
  ```

### 1.5 import 순서 (isort)
1. 표준 라이브러리
2. 서드파티 (`pydantic`, `fastapi`, `boto3`)
3. ReCoder 내부 (`from schemas import ...`)

### 1.6 에러 처리
- 예외는 `Exception` 광범위 catch 금지. 구체적 예외만.
- 응답 보낼 때는 항상 `ErrorResponse` 형태 (자세한 건 `SECURITY.md`).
- 예외 message 에 secret / 절대경로 / IAM ARN 포함 금지.

### 1.7 print vs logging
- `print` 는 main entry point (`if __name__ == "__main__"`) 와 CLI 도구만 허용.
- 그 외엔 `log = logging.getLogger(__name__)` 사용.
- 로그 레벨:
  - `DEBUG`: 변수 값, 흐름 추적
  - `INFO`: 상태 변화, 정상 흐름
  - `WARNING`: 비정상이나 복구 가능
  - `ERROR`: 처리 실패
  - `CRITICAL`: 시스템 중단

---

## 2. TypeScript 컨벤션 (extension/, discord-bot 내 ts)

### 2.1 포매팅
- **Prettier** (semi: true, single-quote: false, print-width: 100)
- **ESLint** (`@typescript-eslint/recommended` + 프로젝트 룰)

### 2.2 명명 규칙
- 파일: `PascalCase.ts` (클래스 / 타입 위주) 또는 `camelCase.ts` (유틸)
- 클래스: `PascalCase`
- 함수/변수: `camelCase`
- 상수: `UPPER_SNAKE_CASE`
- 타입/인터페이스: `PascalCase` (`I` 접두 금지)

### 2.3 타입
- `any` 금지. 불가피하면 `unknown` 후 명시적 narrowing.
- `interface` vs `type`: 객체 모양은 `interface`, union/intersection 은 `type`.
- 모든 export 함수는 명시 return type.

### 2.4 React (webview-src)
- 함수 컴포넌트만 (class 금지).
- Hook 규칙 준수 (`useState`, `useEffect`, `useCallback`).
- props 는 명시 interface (component 이름 + `Props`).
- side effect 는 `useEffect` 안에서만.

---

## 3. 명명 — 데이터 / 도메인 용어

| 한국어 | 영문 식별자 | 사용 위치 |
|--------|------------|----------|
| 배포 | `deployment` | DB / API |
| 검사 | `preflight` | DB / API |
| 수정 제안 | `remediation` | DB / API |
| 사고 | `incident` | DB / API |
| 학습 | `incident_memory` | DB / API |
| 운영 브리핑 | `standup` | DB / API |
| 일기예보 | `forecast` | DB / API |
| 차이 비교 | `diff` | DB / API |
| 영상 재생 | `replay` | DB / API |
| 명령 큐 | `command_queue` | DB / API |
| 알림 | `notification` | DB / API |

**사용자 표시는 한국어, 내부 식별자 / 로그 / DB / API path 는 모두 영문**.

---

## 4. 브랜치 전략

```
main              ← 배포 안정. 직접 push 금지. develop에서만 fast-forward merge.
develop           ← 통합 베이스. 모든 PR 여기로.
feat/<owner>/<topic>   ← 작업 브랜치. develop 에서 분기.
fix/<owner>/<topic>    ← 버그 fix 작업 브랜치.
hotfix/<topic>         ← main 직접 적용 (긴급). develop 에도 즉시 백포트.
```

### 4.1 브랜치 명명 규칙
- 형식: `<type>/<owner>/<topic>`
- `type`: `feat` | `fix` | `chore` | `docs` | `test` | `refactor` | `hotfix`
- `owner`: `A` | `B` | `C` | `D` (또는 팀원 영문 이름)
- `topic`: kebab-case, 영문, 최대 30자
- 예: `feat/A/release-contract`, `fix/B/runtime-preflight-timeout`

### 4.2 보호 규칙 (GitHub Branch Protection)
- `main`: PR 필수, 1명 이상 리뷰, status check 통과
- `develop`: PR 필수, 직접 push 금지 (단, 단일 작업자 합의 하에 예외 허용)

---

## 5. 커밋 메시지

### 5.1 형식 (Conventional Commits 가벼운 적용)
```
<type>(<scope>): <short summary>

<body — 변경 이유 / 영향 범위 / 관련 §ref>

<footer — BREAKING CHANGE / Fixes #N>
```

- `type`: `feat` | `fix` | `chore` | `docs` | `test` | `refactor` | `perf` | `security`
- `scope` (선택): `core/preflight`, `extension/sidebar`, `discord-bot`, `cloud-relay` 등
- short summary: 영문 / 한국어 혼용 허용. 50자 이내.

### 5.2 예시
```
feat(core/preflight): Static Preflight 12종 검사 구현

설계서 §30.1 12종 검사 모두 구현. PreflightRun 생성 + RemediationProposal 자동 생성.

Fixes #65
```

---

## 6. PR 정책

### 6.1 PR 크기
- **권장**: 변경 라인 500 미만, 변경 파일 10개 미만.
- 큰 작업은 stacked PR 또는 분할.
- 예외: schemas.py / package.json 같이 단일 파일에 모이는 작업은 한 번에 OK.

### 6.2 PR 템플릿 (.github/pull_request_template.md)
```markdown
## 변경 요약
(2~3줄)

## 설계서 참조
- §XX, §YY

## 변경 파일
- ...

## 테스트
- [ ] 단위 테스트 추가
- [ ] 수동 테스트 완료 (PowerShell 명령 첨부)
- [ ] schemas.py 변경 → docs/API_v10.md 동시 갱신

## 영향 범위
- 다른 팀원 영역 영향 (Y/N) → Y면 누구 알림

## 리뷰어
- @팀원
```

### 6.3 리뷰 SLA
- **24시간 이내 첫 응답** (간단한 코멘트 OK).
- 48시간 무응답 시 다른 팀원에게 재요청 가능.
- Approve 받기 전 머지 금지.

### 6.4 머지 전략
- **Squash and merge** 권장 (history 깔끔).
- main → develop 백포트는 `merge --ff-only`.
- rebase 권장 안 함 (충돌 위험).

### 6.5 충돌 빈도 높은 파일
- `core/schemas.py`
- `core/api/routes/__init__.py`
- `extension/package.json`
- `docs/API_v10.md`

→ 위 파일 수정 PR 은 가능한 한 작게 + 자주 merge.

---

## 7. 테스트 정책

### 7.1 위치
- 단위 테스트: `core/tests/unit/test_<module>.py`
- 통합 테스트: `core/tests/integration/test_<flow>.py`
- Eval Harness: `core/tests/eval_harness/<category>/*.json`
- TypeScript 테스트: `extension/src/test/<topic>.test.ts`

### 7.2 명명
- 함수: `test_<함수>__<상황>__<기대결과>()` (예: `test_compute_quality_score__no_traceback__low_score()`)
- 한국어 docstring 허용.

### 7.3 커버리지 목표
- 핵심 모듈 (context_gate, risk_validator, rollback_policy, registries): 80%+
- Pydantic 모델 (schemas.py): 100% (자동 검증)
- API 라우트: 60%+ (happy path + error path 각 1개)

### 7.4 실행
```bash
# 전체
pytest core/tests/

# 단위만
pytest core/tests/unit/

# Eval Harness
python -m core.tests.eval_harness.runner
```

### 7.5 CI Gate
- PR 머지 전 GitHub Actions 가:
  - Python: `ruff check`, `black --check`, `pytest`
  - TypeScript: `npm run lint`, `npm run compile`
  - Eval Harness 통과
  - Safety violation 0건

---

## 8. 의존성 관리

### 8.1 Python (core/requirements.txt)
- 정확한 버전 pin (`==`)
- 추가 시 PR에 사유 설명.
- security update 는 Dependabot PR 우선 머지.

### 8.2 Node (extension/package.json)
- caret range (`^`) 허용. lock file 동시 commit 필수.
- 추가 시 license 확인 (MIT/Apache-2.0/BSD 권장).

### 8.3 동시 수정 충돌 방지
- schemas.py / package.json / requirements.txt 수정하는 PR 은 **stage 직전에 pull** + 충돌 시 rebase.

---

## 9. 다국어 / 표시 정책

- **코드 내부**: 영문 (식별자, 로그, 에러 코드, API path).
- **사용자 표시**: 한국어 (Sidebar UI, Discord 메시지, Wizard 질문, 알림).
- 에러 메시지는 두 곳 모두 — `ErrorResponse.message` 는 한국어, `ErrorResponse.code` 는 영문 enum.

---

## 10. 변경 절차 (본 문서 포함)

본 문서나 API_v10.md / SECURITY.md / ENV_VARS.md 수정 시:

1. PR 작성 — 변경 이유 명시
2. 영향받는 팀원 alert (Slack/Discord/Issue)
3. 1명 이상 리뷰
4. 머지 후 모두에게 broadcast (변경 요약 공유)

---

## 변경 이력

- **2026-05-24**: 초안 (Phase A-1 잔여)
