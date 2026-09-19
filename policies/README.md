# ReCoder OPA 정책 번들

배포 게이트(설계서 §Q3 Preset Policy)를 OPA 정책으로 정의한 것이다.

## 구조

```
policies/
└── recoder/
    ├── deploy.rego        배포 차단 규칙 5개
    └── deploy_test.rego   규칙 검증
```

## 코어와의 계약

코어(`core/opa_gate.py`)는 배포 직전에 이렇게 질의한다.

```
POST {OPA_URL}/v1/data/recoder/deploy/allow
{"input": {image_uri, environment, branch, sbom{}, trivy{}, gitleaks{}, hadolint{}}}
```

응답 `result` 는 **객체**여야 한다 — boolean 을 돌려주면 사용자는 왜
막혔는지 알 수 없다.

| 키 | 뜻 |
|---|---|
| `decision` | `"allow"` \| `"deny"` |
| `reason` | 사용자에게 보여 줄 사유(여러 건이면 ` / ` 로 이어 붙임) |
| `fix_suggestion` | 다음에 무엇을 하면 되는지 |
| `approval_level` | 통과 3, 차단 4 |
| `policy_bundle_version` | 어떤 번들이 판단했는지 추적용 |

## 규칙 (5개)

1. **SBOM 필수** — SBOM 없는 이미지는 배포 불가
2. **Trivy critical 차단** — high 는 경고일 뿐 막지 않는다(막으면 거의 모든 배포가 막힌다)
3. **gitleaks 시크릿 차단** — 감지된 자격증명은 제거 후 **회전**까지 해야 한다
4. **Hadolint error 차단**
5. **프로덕션 브랜치 제한** — `production_branches` 에 있는 브랜치에서만.
   브랜치를 **모르는** 경우도 막는다(확인하지 못한 것을 괜찮다로 바꾸지 않는다)

## 실행

```bash
# 서버 기동 — 코어의 OPA_URL 기본값이 http://localhost:8181 이다
opa run --server --addr localhost:8181 policies/

# 정책 검증
opa test policies/ -v
```

## OPA 서버가 없으면

코어가 같은 규칙을 로컬 폴백(`opa_gate._local_deploy_gate`)으로 적용한다.
**두 구현은 항상 같은 판단을 해야 한다.** 규칙을 고칠 때는 양쪽을 함께
고치고, `core/tests/test_opa_policy_bundle.py` 가 그 어긋남을 감시한다.

Level 3~4 작업에서 OPA 서버에 연결하지 못하면 코어는 fail-closed 로
차단한다 — 정책을 확인하지 못한 것을 "허용"으로 바꾸지 않는다.
