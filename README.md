<div align="center">

# ReCoder

**AI DevOps Platform** — 코드 수정부터 클라우드 배포·운영까지, 결정론적이고 감사 가능한 흐름으로 자동화합니다.

![Python](https://img.shields.io/badge/Python-3.11+-3776AB?logo=python&logoColor=white)
![TypeScript](https://img.shields.io/badge/TypeScript-5.x-3178C6?logo=typescript&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white)
![React](https://img.shields.io/badge/React-18-61DAFB?logo=react&logoColor=black)
![AWS](https://img.shields.io/badge/AWS-Bedrock-FF9900?logo=amazonaws&logoColor=white)
![VS Code](https://img.shields.io/badge/VS_Code-Extension-007ACC?logo=visualstudiocode&logoColor=white)

[시연 영상](https://youtu.be/1VvfyR7i0tk) · [발표 자료](발표자료/ReCoder_기말발표.pptx) · [구현 설계서](docs/ReCoder_구현설계서_v1.md)

</div>

ReCoder 는 개발자가 VSCode 사이드바(또는 Discord)에서 **한 번의 클릭**으로 — AI 코드 패치 → Release Contract 기반 정적·런타임 검증 → 안전한 배포 → 5분 감시·자동 롤백까지 이어지는 DevOps 흐름을 자동화합니다. 조직 단위에서는 **Control Plane** 이 OIDC·RBAC·OPA 정책·2인 승인·불변 감사 로그로 거버넌스를 강제하고, **Cloud Execution Plane** 이 ECS Fargate 와 EKS + ArgoCD GitOps 를 자동화합니다.

---

## 시연 영상

개발한 전체 기능을 하나의 영상에 담았습니다. 썸네일을 클릭하면 YouTube 로 이동합니다.

<div align="center">

[![ReCoder 데모 영상](https://img.youtube.com/vi/1VvfyR7i0tk/maxresdefault.jpg)](https://youtu.be/1VvfyR7i0tk)

**https://youtu.be/1VvfyR7i0tk**

</div>

---

## 발표 자료

| 자료 | 설명 | 링크 |
|------|------|------|
| 프로젝트 제안서 발표 (중간발표) | 프로젝트 기획 · 목표 기능 제안 | _업로드 예정_ |
| 프로젝트 최종 발표 (기말발표) | 최종 구현 결과 · 시연 | [ReCoder_기말발표.pptx](발표자료/ReCoder_기말발표.pptx) |

---

## 3-Plane 아키텍처

ReCoder 는 책임에 따라 세 개의 Plane 으로 나뉩니다.

| Plane | 책임 | 주요 구성요소 |
|-------|------|--------------|
| **Local Execution** | 코드 분석·패치, 로컬 Preflight·배포, 사고 학습 | `core/` · `extension/` · `discord-bot/` |
| **Control Plane** | 조직 통제 — 인증·권한·정책·승인·감사 | `control_plane/` (FastAPI · PostgreSQL) |
| **Cloud Execution** | ECS / EKS 배포, GitOps, 관측성 | `core/agents/` · `deploy/` · `watchdog/` |

<div align="center">
<img width="824" alt="ReCoder Architecture" src="https://github.com/user-attachments/assets/18e3072e-0160-4ffe-841f-53ea0a1a598c" />
</div>

---

## 핵심 기능

| 기능 | 설명 |
|------|------|
| **Release Contract** (`recoder.yml`) | 배포 계약 — 스택·포트·health 경로·환경 변수·롤백 트리거를 명시. First Run Wizard 가 자동 생성 |
| **Static / Runtime Preflight** | 배포 전 정적 검사(env·code·Dockerfile·포트·의존성·secret) 후 0~100 점수화, 임시 컨테이너로 health·smoke 검증 |
| **Deterministic Remediation** | 같은 입력 → 같은 `proposal_id`(SHA256). 결정론적 템플릿 치환으로 재현성 보장 |
| **Plan-Execute-Verify** | LLM Planner → 결정론적 Executor(allowlist + 타임아웃) → Verifier(schema·sha256·test) 검증 |
| **3-Layer Audit** | Preflight → Remediation → DeploymentLedger 영속화. Layer 3 는 append-only 상태머신 |
| **IncidentMemory** | 사고 fingerprint 기반 매칭, 재발 시 과거 해결책 자동 제안(옵트인 학습) |
| **Continuous Verification** | 배포 후 5분 감시 — health·에러율·메모리 추적 → 자동 롤백 제안 |
| **Governance** | OIDC + Device 토큰 + RBAC, OPA 정책 7종, 위험 작업 2인 승인, hash-chain 불변 AuditLog |
| **Cloud Execution** | ECS Fargate Rolling Update(Circuit Breaker), EKS + ArgoCD GitOps, SBOM·보안스캔 |
| **Multi-channel** | VSCode 워크벤치 + Discord ChatOps(`/recoder deploy`, `/recoder rollback` 등) |

---

## 기술 스택

| 영역 | 기술 |
|------|------|
| **Local Core** | Python 3.11 · FastAPI · SQLite · Docker SDK |
| **VSCode Extension** | TypeScript · React · Tailwind CSS · Webpack |
| **Control Plane** | FastAPI · PostgreSQL (RLS) · SQLAlchemy · OPA (Rego) |
| **Cloud / DevOps** | AWS Bedrock · ECS Fargate · EKS · ArgoCD · SBOM(Syft) · Trivy |
| **Observability** | OpenTelemetry · Prometheus · Loki |
| **AI** | Amazon Bedrock (Claude) |

---

## 사용 흐름

1. **첫 실행** — First Run Wizard 가 Docker / AWS / Bedrock 연결을 진단하고 `recoder.yml` 자동 생성
2. **에러 분석** — 에러 감지 → Context Gate 마스킹 → Bedrock 호출 → `PatchProposal` 카드 → 승인 후 적용(SHA256 검증 + 백업)
3. **배포 검증(로컬)** — Static Preflight → 필요 시 자동 수정 제안 → Runtime Preflight → 배포 후 5분 CV 감시(STABLE / 자동 롤백 제안)
4. **클라우드 배포** — ECS Rolling Update(Circuit Breaker) 또는 EKS + ArgoCD sync, 롤백은 Git revert PR 자동 생성
5. **거버넌스** — 위험 작업은 OPA 평가 → 2인 승인 → 모든 행위 hash-chain AuditLog 기록
6. **장애 대응** — Incident → Timeline → RCA → Postmortem 자동 생성

---

## 분기별 진척

| 분기 | 범위 | 상태 |
|------|------|:----:|
| **Q1 — AI 품질 기반** | Plan-Execute-Verify, Eval Harness, Context Gate, Static/Runtime Preflight, Deterministic Remediation, 3-Layer Audit, IncidentMemory, Continuous Verification | 완료 |
| **Q2 — Control Plane + Governance** | FastAPI + PostgreSQL RLS, OIDC/Device/RBAC, hash-chain AuditLog, OPA 정책 7종, 2인 승인 흐름 | 완료 |
| **Q3 — Cloud Execution** | Cloud Preflight, ECS Rolling Update + Circuit Breaker, Rollback Proposal, SBOM, 보안스캔 | 완료 |
| **Q4 — GitOps + Observability + MCP** | ArgoCD 에이전트, Rollback PR 자동 생성, Incident·RCA·Postmortem, OpenTelemetry + Prometheus + Loki, MCP PoC | 완료 |

---

## 설치 · 실행

전체 셋업(Local Core · Gateway · Discord · Extension · Control Plane) 순서는 **[SETUP.md](SETUP.md)** 를 참고하세요.

```bash
# Local Core
cd core && pip install -r requirements.txt && python main.py     # → 127.0.0.1:17894

# VSCode Extension
cd extension && npm install && npm run build                     # VSCode 에서 F5 로 실행
```

---

## 문서

- [SETUP.md](SETUP.md) — 처음부터 끝까지 셋업 가이드
- [docs/ReCoder_구현설계서_v1.md](docs/ReCoder_구현설계서_v1.md) — 구현 설계서
- [docs/API_v10.md](docs/API_v10.md) — API 레퍼런스
- [docs/STUDENT_GUIDE.md](docs/STUDENT_GUIDE.md) — 학생 설치 가이드

---

## 담당

| 이름 | 역할 |
|------|------|
| **이동규** | 백엔드 · 인프라 (Local Core, Control Plane, Cloud Execution 에이전트) |
| **윤세빈** | Extension · 코드 분석 (VSCode UI, code_agent, MCP, workbench) |
