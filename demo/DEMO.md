# ReCoder Final Demo — 시나리오 가이드

> 설계서 §Final Demo Scope. ADR-008 에 따라 **Q3 데모(ECS) 와 Q4 데모(EKS) 는 분리된다.**

이 문서는 사람이 두 데모를 처음부터 끝까지 직접 시연할 수 있도록 단계별 작업을 정리한 가이드다.
대본이 아니라 체크리스트로 쓰일 수 있도록 각 단계마다 "확인 포인트" 를 함께 둔다.

---

## Final Demo A — Q3 배포 역량 데모 (AWS / ECS)

**목적**: 일반 AI 코딩 도구와 ReCoder Enterprise 의 차이는 *조직 정책 + 클라우드 실행* 임을 보인다.

| # | 단계 | 작업 | 확인 포인트 |
|---|------|------|-------------|
| 1 | 사전 점검 | `Cloud Preflight` (read-only IAM) 실행 | 누락 리소스 안내가 "가이드" 로만 표시되는지 |
| 2 | 코드 패치 | FastAPI sample 의 import 에러를 ReCoder 가 수정 | 다중 파일 패치 + SHA256 검증 |
| 3 | Dockerfile 생성 | FileTemplate Registry 기반 Dockerfile 생성 | LLM 직접 명령 생성 없음 — 템플릿만 |
| 4 | Trivy + Hadolint | OPA gate 통과 | critical 1건 발생 시 차단 동작 |
| 5 | SBOM 생성 | Syft (CycloneDX JSON) | DeploymentRecord 에 sbom_hash 기록 |
| 6 | ECR push | CommandTemplate 경유 | shell metacharacter 차단 |
| 7 | ECS Rolling Update | `update-service --force-new-deployment` | CloudWatch 폴링 진행률 표시 |
| 8 | Circuit Breaker | 의도적 health check 실패 주입 | 5분 내 50% 초과 시 자동 중단 |
| 9 | Rollback proposal | 이전 Task Definition 으로 되돌리는 Approval Level 3 카드 | 승인 누르기 전 실행되지 않음 |
| 10 | 정리 | 데모용 ECS 서비스 desiredCount=0 | 비용 0 확인 |

**비용 가드**: 데모 직후 `aws ecs update-service --desired-count 0` 와 `aws ecr batch-delete-image` 로 정리.

---

## Final Demo B — Q4 쐐기 시나리오 데모 (실제 EKS)

> ADR-009: k3d / kind 사용 금지. 실제 EKS 를 데모 2시간 전 생성하고 데모 직후 삭제한다.

### B-0. 사전 준비 (데모 2시간 전)

```bash
./demo/eks/create_demo_cluster.sh ap-northeast-2
```

스크립트가 출력하는 ArgoCD admin password 를 저장한다.

준비물 점검:
- FastAPI production-like service GitHub repo (helm chart 포함)
- ReCoder Control Plane(SaaS) 계정 — 2인 승인자 사전 등록
- Slack 채널 (Should 항목이지만 시연 효과 큼)
- 데모 전용 ECR repo + image (정상 image + 장애 유발 image 두 개)

### B-1. 시나리오 10단계 (설계서 §Final Demo Scope)

쐐기 시나리오를 사용자가 한 자리에서 끝까지 볼 수 있어야 한다.

| # | 단계 | 시간 | 시연 포인트 |
|---|------|------|-------------|
| 1 | 정상 배포 | 2분 | ReCoder Ship Mode → GitOps PR → 머지 → ArgoCD sync OK |
| 2 | 장애 유발 커밋 배포 | 3분 | 의도적으로 startup 에서 crash 하는 image 머지 |
| 3 | OTel 자동 감지 | 30초 | Prometheus error_rate spike 가 Sidebar 에 표시 |
| 4 | Incident Timeline 생성 | 1분 | `/api/incident/timeline` 호출 결과 표시 — DeploymentRecord + Watchdog + OTel 통합 |
| 5 | RCA 생성 | 1분 | "가능성 높은 원인 후보" 표현 확인. confidence score 표시 확인. |
| 6 | rollback PR 자동 생성 | 1분 | `/api/rollback-pr/create` — PR title / body / risk 노출 |
| 7 | Web UI 2인 승인 | 2분 | 두 승인자가 각각 승인 누르기. 거부 사유 입력 필수 검증. |
| 8 | ArgoCD 적용 | 2분 | PR 머지 후 ArgoCD sync 상태 폴링 — Sidebar 에 healthy 전환 |
| 9 | Health Check 회복 | 30초 | error_rate 정상으로 복귀. Sidebar 가 자동 closed 처리. |
| 10 | Postmortem skeleton 생성 | 30초 | `/api/postmortem/generate` — markdown skeleton 다운로드 |

총 시연 시간: 약 15분.

### B-2. 시연 후 정리

```bash
./demo/eks/destroy_demo_cluster.sh ap-northeast-2
```

- NAT GW / NLB 가 남아있지 않은지 AWS console 에서 추가 확인 (스크립트 마지막에 명령 안내됨)
- 데모 dryrun 비용 < $10 가 가이드라인

---

## 데모 중 실패 시 fallback 정책

| 컴포넌트 장애 | 데모 진행 방식 |
|--------------|---------------|
| OTel backend 미연결 | Incident Timeline 이 Watchdog + AuditLog fallback 으로 자동 전환 (`otel_available=false`) |
| ArgoCD API 미응답 | PR 생성까지만 시연, sync 상태는 "unknown" 라벨 |
| GitHub API rate limit | rollback PR 을 미리 잡아둔 staging 브랜치로 시연 |
| LLM provider 미응답 | RCA 가 deterministic 모드로 fallback — "관측 데이터 부족" 명시 |
| Web UI 2인 승인 실패 | Slack 버튼 fallback (Should 항목, 사전 활성화 필요) |

설계서의 핵심 메시지: **"AI 가 코드를 고쳐주는 도구" 가 아니라 "프로덕션 변경을 안전하게 실행하는 DevOps Execution Platform"** — 데모 어떤 단계에서 fallback 이 일어나도 메시지 자체는 유지된다.
