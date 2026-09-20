# ReCoder 릴리스 런북 — 실 AWS 검증 · 운영 · 장애 대응

보드 카드 「E2E 통합 검증」 「롤백 1회 리허설 + 예산 알람」 「버그 버퍼 ·
문서/런북」의 실행 문서. 사람이 해야 하는 일은 전부 여기의 명령 몇 줄이다.

## 1. E2E 통합 검증 (개발→검사→배포→감시→롤백)

전제: Docker 실행 중, 코어 의존성 설치됨(`pip install -r core/requirements.txt`).

```bash
python3 scripts/e2e_verify.py           # 전 구간, 끝나고 정리
python3 scripts/e2e_verify.py --keep    # 실패 원인을 직접 볼 때
```

- AWS 없이 도는 사전 배선 검증: `python3 scripts/golden_path_smoke.py` (moto 기반, 이미 통과 확인됨).
- 성공 기준: 스크립트 종료코드 0. 실패 시 마지막 단계 로그가 원인 — `--keep` 으로 컨테이너를 남겨 확인.

## 2. 롤백 리허설 (1회, 실 AWS)

1. v1 배포: 확장 → Deploy 허브 → ECS 배포 (이미지 태그 `v1`).
2. v2 배포: 같은 서비스에 태그 `v2` 로 재배포 — 감시가 붙는 것 확인.
3. v2 를 일부러 죽인다(헬스체크 경로를 없는 값으로 바꿔 재배포하거나 컨테이너 정지).
4. 배포 센터에 **롤백 제안 카드**가 뜨는지 확인 → 「승인」.
5. 확인 기준: 서비스가 v1 이미지로 돌아오고, 배포 기록에 롤백이 남는다.
   (승인 없이 자동 롤백되면 버그다 — 폴링은 제안만 나른다. `rollbackApproval.test.js` 가 잠근 계약.)

## 3. 예산 알람 (Budgets)

```bash
python3 scripts/setup_budget.py --email <내메일> --limit 15
python3 scripts/setup_budget.py --email <내메일> --dry-run   # 페이로드 미리보기
```

본인 콘솔 계정으로 1회 실행. 80% / 100% 도달 시 이메일.

## 4. 운영 중 잘 나오는 문제

| 증상 | 원인 | 조치 |
|---|---|---|
| 화면 전부 "코어 연결 안 됨" | 로컬 코어 다운 | VSCode 재시작 또는 `cd core && python3 main.py`. 로그: 코어 터미널 출력 |
| 설계 카드가 비었다는 경고 | 모델이 형식을 어김 | 재시도 1회는 자동. 반복되면 `.env` 의 PRIMARY 를 Sonnet 급으로 (`core/.env.example` 주석) |
| "검증된 품질이 낮습니다" 배지 | 드문 스택/한국어 조합 | 정상 동작 — 결과를 더 주의 깊게 검토하라는 뜻 |
| 보안 검사 "확인하지 못했습니다" | Docker/Trivy 미실행 | Docker Desktop 시작 후 「다시 검사」. 미검증 상태로 배포하면 실행 직전 1회 스캔이 다시 돈다 |
| 배포 차단 (정책) | OPA 게이트 거부 | 사유가 화면에 있다. 규칙은 `policies/recoder/deploy.rego` — 완화는 코드 리뷰로만 |
| LLM 계속 폴백/실패 | 브레이커 열림 | 60초 후 자동 반개. 원인은 코어 로그의 `[CB] open` 직전 오류 |
| AWS 인증 실패 | 리전/키 불일치 | 확장 AWS 연결 화면에서 「권한 다시 점검」. 키가 없으면 「원클릭 IAM 셋업」 |

## 5. 릴리스 체크 순서 (요약)

1. `core: pytest tests/` · `extension: node --test webview-tests/*.test.js` · `npx tsc --noEmit` 전부 초록.
2. `python3 scripts/golden_path_smoke.py` 통과.
3. 위 1(E2E)·2(롤백 리허설) 실 AWS 1회.
4. `bash scripts/package-extension.sh` (플랫폼별) 또는 `--no-binary`.
5. `docs/mvp-acceptance-checklist.md` 전 항목 체크.
