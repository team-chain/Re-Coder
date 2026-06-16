<div align="center">

# ReCoder

**개발부터 배포·운영까지, AI가 자동화하는 DevOps 도구**

![Python](https://img.shields.io/badge/Python-3.11+-3776AB?logo=python&logoColor=white)
![TypeScript](https://img.shields.io/badge/TypeScript-5.x-3178C6?logo=typescript&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white)
![React](https://img.shields.io/badge/React-18-61DAFB?logo=react&logoColor=black)
![AWS](https://img.shields.io/badge/AWS-Bedrock-FF9900?logo=amazonaws&logoColor=white)
![VS Code](https://img.shields.io/badge/VS_Code-Extension-007ACC?logo=visualstudiocode&logoColor=white)

[시연 영상](https://youtu.be/1VvfyR7i0tk) · [발표 자료](발표자료/ReCoder_기말발표.pptx) · [구현 설계서](docs/ReCoder_구현설계서_v1.md)

</div>

ReCoder는 VSCode(또는 Discord)에서 **AI가 코드 에러를 분석해 고치고, 배포 전 검증 → 안전한 배포 → 배포 후 감시·자동 롤백까지 한 번에 처리**하는 DevOps 도구입니다. 조직 단위에서는 권한·승인·감사를 통한 거버넌스도 지원합니다.

---

## 시연 영상

개발한 전체 기능을 하나의 영상에 담았습니다.

<div align="center">

[![ReCoder 데모 영상](https://img.youtube.com/vi/1VvfyR7i0tk/maxresdefault.jpg)](https://youtu.be/1VvfyR7i0tk)

**https://youtu.be/1VvfyR7i0tk**

</div>

---

## 발표 자료

| 자료 | 링크 |
|------|------|
| 프로젝트 제안서 발표 (중간) | [ReCoder_중간발표.pptx](발표자료/ReCoder_중간발표.pptx) |
| 프로젝트 최종 발표 (기말) | [ReCoder_기말발표.pptx](발표자료/ReCoder_기말발표.pptx) |

---

## 핵심 기능

| 기능 | 설명 |
|------|------|
| **AI 코드 에러 분석·패치** | 에러를 감지해 AI가 원인을 분석하고 수정 패치를 제안, 승인하면 자동 적용 |
| **배포 전 자동 검증** | 환경·코드·Dockerfile·보안 등을 점검하고, 임시 컨테이너로 실제 동작까지 확인 |
| **배포 문제 자동 수정 제안** | 검증에서 막힌 부분의 수정안을 자동 생성해 제안 |
| **원클릭 안전 배포** | 검증을 통과하면 클라우드(ECS / EKS)로 자동 배포 |
| **배포 후 감시·자동 롤백** | 배포 후 5분간 상태를 감시하고, 이상이 생기면 자동 롤백 제안 |
| **사고 학습·재발 대응** | 장애를 학습해, 같은 사고가 재발하면 과거 해결책을 자동 제안 |
| **장애 분석 자동화** | 장애 발생 시 원인분석·사후보고서를 자동 생성 |
| **조직 거버넌스** | 권한 관리, 위험 작업 2인 승인, 모든 작업 감사 로그 기록 |
| **멀티채널 지원** | VSCode와 Discord 양쪽에서 동일하게 사용 |

---

## 사용 흐름

1. **에러 분석** — 코드 에러가 나면 AI가 원인을 분석해 수정 패치를 제안하고, 승인하면 적용합니다.
2. **배포 검증** — 배포 전 환경·코드·보안을 자동 점검하고, 문제가 있으면 수정안을 제안합니다.
3. **배포** — 검증을 통과하면 클라우드(ECS / EKS)에 자동 배포합니다.
4. **감시·롤백** — 배포 후 5분간 상태를 감시하다 이상이 생기면 자동 롤백을 제안합니다.
5. **장애 대응** — 장애가 나면 원인분석·사후보고서를 자동 생성하고, 같은 사고 재발 시 과거 해결책을 제안합니다.

---

## 기술 스택

| 영역 | 기술 |
|------|------|
| **Local Core** | Python · FastAPI · Docker |
| **VSCode Extension** | TypeScript · React · Tailwind CSS |
| **Control Plane** | FastAPI · PostgreSQL · OPA |
| **Cloud / DevOps** | AWS Bedrock · ECS · EKS · ArgoCD |
| **Observability** | OpenTelemetry · Prometheus · Loki |

---

## 아키텍처

ReCoder는 세 개의 영역(Plane)으로 구성됩니다.

| 영역 | 역할 |
|------|------|
| **Local** | 코드 분석·패치, 로컬 검증·배포 (`core`, `extension`, `discord-bot`) |
| **Control** | 조직 단위 인증·권한·승인·감사 (`control_plane`) |
| **Cloud** | 클라우드 배포·GitOps·모니터링 (`core/agents`, `deploy`, `watchdog`) |

<div align="center">
<img width="824" alt="ReCoder Architecture" src="https://github.com/user-attachments/assets/18e3072e-0160-4ffe-841f-53ea0a1a598c" />
</div>

---

## 분기별 진척

| 단계 | 내용 | 상태 |
|------|------|:----:|
| **Q1** | AI 코드 분석·검증·자동 수정 기반 구축 | 완료 |
| **Q2** | 조직 거버넌스 (인증·권한·승인·감사) | 완료 |
| **Q3** | 클라우드 배포(ECS) + 보안 검사 | 완료 |
| **Q4** | GitOps·모니터링·장애 분석 자동화 | 완료 |

---

## 설치 · 실행

자세한 셋업은 [SETUP.md](SETUP.md)를 참고하세요.

```bash
# Local Core
cd core && pip install -r requirements.txt && python main.py

# VSCode Extension
cd extension && npm install && npm run build
```

---

## 문서

- [SETUP.md](SETUP.md) — 설치·실행 가이드
- [docs/ReCoder_구현설계서_v1.md](docs/ReCoder_구현설계서_v1.md) — 구현 설계서
- [docs/API_v10.md](docs/API_v10.md) — API 레퍼런스

---

## 담당

| 이름 | 역할 |
|------|------|
| **이동규** | 백엔드 · 인프라 |
| **윤세빈** | Extension · 코드 분석 |
