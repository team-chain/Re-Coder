# ReCoder Enterprise

> **DevOps Execution Platform** — AI가 코드를 고쳐주는 도구가 아니라,  
> 개발자의 DevOps 실행을 조직 정책·다중 승인·감사 로그·GitOps·관측성 데이터와 연결해  
> 프로덕션 변경을 안전하게 수행하는 플랫폼.

---

## 구성 (3개 Plane)

```
┌─────────────────────────────────────────┐
│  VSCode Extension (TypeScript)          │  ← 개발자 인터페이스
│  Sidebar Webview / Terminal 수집         │
└──────────────┬──────────────────────────┘
               │ HTTP REST (127.0.0.1)
┌──────────────▼──────────────────────────┐
│  Local Core (Python + FastAPI)          │  ← Local Execution Plane
│  Plan-Execute-Verify / AST Chunker      │
│  ContextGate (마스킹) / LLM Router      │
└──────────────┬──────────────────────────┘
               │
┌──────────────▼──────────────────────────┐
│  Control Plane (SaaS)                   │  ← 조직 통제 (Q2~)
│  OIDC / Device Token / RBAC / OPA       │
│  AuditLog / 2인 승인 / PolicyBundle     │
└──────────────┬──────────────────────────┘
               │
┌──────────────▼──────────────────────────┐
│  Cloud Execution Plane                  │  ← 배포·운영 (Q3~Q4)
│  ECS Fargate (Q3) / EKS + ArgoCD (Q4)  │
│  OpenTelemetry / Prometheus / Loki      │
└─────────────────────────────────────────┘
```

---

## 1년 로드맵

| 분기 | 목표 | 핵심 기능 |
|---|---|---|
| **Q1** | AI 품질 기반 | AST 청킹, Plan-Execute-Verify, Eval Harness |
| **Q2-A** | 조직 통제 골격 | OIDC, Device Token, RBAC, AuditLog |
| **Q2-B** | 정책·승인 | OPA 배포 차단, Web UI 2인 승인 |
| **Q3** | 클라우드 실행 | ECS Rolling Update, SBOM, Trivy 게이트 |
| **Q4** | GitOps + 관측성 | ArgoCD, Incident Timeline, RCA, Postmortem |

**Final Demo (Q4)**: 쐐기 시나리오 10단계 — 장애 감지 → RCA → rollback PR → 2인 승인 → ArgoCD → Postmortem

---

## 로컬 실행

### Core 서버

```bash
cd core
python -m venv .venv
.venv/Scripts/activate  # Windows
pip install -r requirements.txt
python main.py
```

### Extension

```bash
cd extension
npm install
npm run compile   # TypeScript 컴파일
# VSCode에서 F5 (Extension Development Host)
```

---

## 제품 패키징

| 플랜 | 내용 |
|---|---|
| **Free** | Local Core, 개인 프로젝트 3개, 로컬 Docker 배포 |
| **Pro** | SaaS Control Plane, 배포 이력, SBOM, GitHub Actions |
| **Team** | RBAC, Multi-Approver, AuditLog, PolicyBundle |
| **Enterprise** | SSO/SAML, Self-hosted (Q4+), 커스텀 정책, SLA |

**Open-core**: Local Core Apache 2.0 공개 예정, Control Plane 상용 운영

---

## 개발 진척

| 분기 | 상태 | 완료 일자 |
|------|------|-----------|
| Q1 Must-Core | ✅ 완료 | 2026-05-16 |
| Q2-A Control Plane Core | ✅ 완료 | 2026-05-16 |
| Q2-B Governance (OPA + 2인 승인) | ✅ 완료 | 2026-05-16 |
| Q3 ECS Fargate + SBOM + 보안스캔 | ✅ 완료 | 2026-05-16 |
| Q4 GitOps + OTel + MCP | ✅ 완료 | 2026-05-16 |

→ 상세 내용: [PROGRESS.md](PROGRESS.md) · [HANDOFF.md](HANDOFF.md)
