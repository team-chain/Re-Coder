# ReCoder 멘토링 데모 가이드

> **시나리오**: "v10 Backbone 완성도 + Bedrock E2E LLM 호출"
> **준비 시간**: 0시간 (이미 다 됨)
> **소요 시간**: 약 10~15분

---

## 0. 데모 직전 sanity check (1분)

데모 시작 전에 한 번 돌려서 모두 초록불인지 확인:

```powershell
cd C:\ReCoder\Re-Coder\core
.\.venv\Scripts\Activate.ps1
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONPATH = "."

# 1. 단위 테스트 225개 통과 확인
python -m pytest tests/unit/ -q

# 2. v10 백본 자동 평가 — Safety Gate 통과 확인
python -m eval.v10
```

기대 출력:
```
=========== 225 passed in ~75s ===========
[GATE PASS] weighted_pass_rate=100.00%
```

둘 다 OK면 데모 준비 끝.

---

## 1. 멘토에게 보여줄 큰 그림 (2분)

> **"ReCoder는 VSCode 안에서 동작하는 DevOps AI Agent예요. 사용자가 'deploy' 한 마디 하면 코드 검사 → 자동 수정 제안 → 배포 검증까지 자동으로 합니다."**

### v10 백본 5층 (이번 학기 구현)

| Layer | 역할 | 모듈 |
|-------|------|------|
| 1 | **Release Contract** — 프로젝트의 배포 계약 | `core/preflight/contract_loader.py` |
| 2 | **Static Preflight** — 12가지 정적 검사 (배포 가능?) | `core/preflight/checks/` |
| 3 | **Remediation** — 자동 수정 제안 (결정론적) | `core/remediation/` |
| 4 | **Runtime Preflight + CV** — 실제 컨테이너 검증 + 5분 감시 | `core/preflight/runtime.py`, `core/cv/` |
| 5 | **3-Layer Persistence + IncidentMemory** — 감사 + 학습 | `core/persistence/`, `core/incident_memory/` |

### 한 줄 요약

> "**같은 사고 두 번 안 나게**, **자동 제안은 매번 동일**하게 (결정론), **secret 절대 안 새게** (마스킹), **단 1건 보안 회귀도 머지 불가** (Safety Gate)."

---

## 2. 라이브 시연 — Eval Harness (3분)

```powershell
python -m eval.v10
```

### 보여줄 포인트

**(a) 6 카테고리 모두 100% 통과**:
- PREFLIGHT_ACCURACY: 12 검사가 정확히 blocker/warning 발생
- REMEDIATION_DETERMINISM: 12 코드 × 5회 반복 → 같은 proposal_id
- REMEDIATION_APPLY: 적용 후 preflight 재실행 → blocker 사라짐
- INCIDENT_FINGERPRINT: 같은 사고 시그니처 + 마스킹 검증
- INCIDENT_MATCH: 과거 fix 자동 매칭
- SAFETY_REGRESSIONS: curl|sh / AWS key / secret 노출 — 단 1건 실패 불가

**(b) Safety Gate**: `Exit code 0` = PR 머지 허용. `Exit code 1` = CI 차단.

**(c) 임팩트**:
- "다른 팀원이 backbone 깨는 코드 PR 올리면 Gate 가 자동 거부."
- "AWS key 같은 secret 이 코드/메시지에 노출되면 즉시 BLOCKED."

---

## 3. 결정론 시연 — RemediationProposal (2분)

```powershell
python -c "from remediation import generate_proposal_for_blocker; from schemas import PreflightBlocker, PreflightCheckCode, PreflightSeverity, ContractProjectMeta, ContractRuntime, ContractStack, ReleaseContract; from pathlib import Path; b = PreflightBlocker(code=PreflightCheckCode.MISSING_DOCKERFILE, message='x', severity=PreflightSeverity.HIGH); c = ReleaseContract(project=ContractProjectMeta(name='x', stack=ContractStack.PYTHON_FASTAPI), runtime=ContractRuntime(host_port=8080, app_port=8000), contract_hash='deadbeef'*8); ids = {generate_proposal_for_blocker(b, c, Path('.')).proposal_id for _ in range(5)}; print('5회 호출, unique id 개수:', len(ids), '| id:', ids)"
```

기대 출력:
```
5회 호출, unique id 개수: 1 | id: {'rem_abc12345'}
```

### 강조

> "LLM 답변은 매번 미세하게 다를 수 있지만, 우리 Remediation은 **결정론적 해시 (SHA256)** 기반이라 같은 입력 → 같은 proposal_id. CI 캐싱 / 중복 제안 제거 / 감사 추적 가능."

---

## 4. 보안 시연 — Secret 마스킹 (2분)

```powershell
python -c "from incident_memory import mask_for_fingerprint; print(mask_for_fingerprint('Error in deploy: AKIAIOSFODNN7EXAMPLE leaked at C:\\Users\\Alice\\.env line 42'))"
```

기대 출력:
```
Error in deploy: <SECRET> leaked at <WORKSPACE> line <NUM>
```

### 강조

> "AWS Access Key, GitHub PAT, OpenAI key, Stripe key, Slack token, bearer token 모두 fingerprint 단계에서 자동 마스킹. 로그/메시지/LLM prompt 어디에도 원문 노출 안 함."

---

## 5. AI 호출 E2E — Bedrock PatchProposal (3분)

> 이 부분은 5/24 커밋 `2df24b1 Fix: 1학기 시연 E2E + 보안 검토 반영` 의 흐름 그대로.

### 사전 확인

`.env` 에 AWS 자격 증명 설정돼 있는지:

```powershell
Get-Content C:\ReCoder\Re-Coder\core\.env
```

`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_REGION=ap-northeast-2` 있어야 함.

### 라이브

VSCode 에서 ReCoder 사이드바 → "분석" 버튼 → 의도적으로 에러 있는 Python 파일에 대해 분석 실행:
1. Context Gate 가 secret 마스킹
2. AWS Bedrock Claude Haiku 3 호출
3. PatchProposal 반환 (`patches[]` + `summary` + `risk_level`)
4. 사이드바에 카드로 표시

### 강조

> "**16-pattern Context Gate** 가 LLM 호출 전에 모든 secret 패턴 마스킹. 그 다음 Bedrock 호출. 응답은 Pydantic 모델로 검증. Quality Score < threshold 면 자동 차단."

---

## 6. 멘토에게 줄 수 있는 질문/답변 (2분)

### Q: "1학기에 어디까지 됐어요?"

> "v10 설계서의 백엔드 백본 6 단계 (A-1~A-6) + 인프라 핵심 2 단계 (B-1, B-2) 까지 완성. 코드 9,100줄 + 단위 테스트 225개 + 자동 평가 28 케이스 모두 통과. Bedrock Haiku 3 호출 + PatchProposal 반환까지 E2E 검증."

### Q: "2학기 남은 건 뭐예요?"

> "AWS 자원 셋업 (EC2 + DynamoDB + Lambda) 후 ① EC2 SSH 자동 배포 + Watchdog ② Hybrid Cloud Relay (PC 오프라인 시) 진행 예정. 코드 골격은 이번 학기 backbone 위에 자연스럽게 올라가도록 인터페이스 설계 완료."

### Q: "다른 팀원들은 뭐 하고 있어요?"

> "한 명은 Discord ChatOps (`discord` 브랜치에서 작업 중), 한 명은 VSCode UI/UX, 한 명은 First Run Wizard + 문서. 백엔드 backbone 이 통합 인터페이스라 각자 병렬로 진행 중."

### Q: "ReCoder만의 차별점은?"

> "**결정론적 Remediation** (같은 입력 → 같은 제안). 일반 LLM 코딩 도구는 비결정적이라 같은 에러를 다르게 답함. ReCoder 는 fingerprint 기반 캐시 + 템플릿 치환으로 항상 동일 결과 → 감사 가능, CI 캐싱 가능, 중복 제안 제거. **Safety Gate** 가 CI 단계에서 자동 회귀 차단."

### Q: "비용은?"

> "현재 단위 평가 28 케이스 + 백본 사용 시 LLM 호출 0회 (모두 결정론). Bedrock 은 PatchProposal 생성 시점에만 (Haiku 3 호출당 ~$0.01). IncidentMemory 가 같은 fingerprint 재발 시 캐시 hit → LLM 호출 0회."

---

## 7. 깃 상태 한 줄 보고

```powershell
cd C:\ReCoder\Re-Coder
git log --oneline main -10
```

```
c4e4e71d (HEAD -> main, origin/main, develop)  Phase B-2: Continuous Verification
70455304                                       Phase B-1: Runtime Preflight
c554b1e7                                       chore: gitignore eval harness output
e2eaa05c                                       Phase A-6: v10 Backbone Eval Harness + Safety CI Gate
9a66d365                                       Phase A-5: IncidentMemory 학습 시스템
be0b5de6                                       Phase A-4: 3-Layer 데이터 영속화 (SQLite)
e928e560                                       Phase A-3: RemediationProposal deterministic system
3b8c0a3f                                       Phase A-2: Static Preflight 12-check system
4405642f                                       Phase A-1: v10 PART II/III/IV 인터페이스 합의 산출물
9afe0528                                       W1 합의 마무리
```

> "이번 학기 8개 phase 커밋. 모두 main 머지 완료."

---

## 8. 멘토링 끝나고 (옵션)

데모 직후 멘토 피드백 받으면 즉시 메모. 다음 작업 우선순위 결정:
- D 역할 (UI/Wizard) 시작
- 보안 follow-ups (#59-#63)
- HANDOFF.md (팀원 공유)
- B-3/B-4 (AWS 인프라 셋업 후)
