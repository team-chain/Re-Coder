# recoder/preflight-action@v1

> AI 기반 배포 전 안전성 검증 GitHub Action

## 사용법

```yaml
- name: ReCoder Preflight
  uses: team-chain/Re-Coder/marketplace-actions/recoder-preflight-action@v1
  with:
    api_url: ${{ secrets.RECODER_API_URL }}
    api_token: ${{ secrets.RECODER_API_TOKEN }}
```

## 전체 예시

```yaml
name: Deploy

on:
  push:
    branches: [main]

jobs:
  preflight:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: ReCoder Preflight
        id: preflight
        uses: team-chain/Re-Coder/marketplace-actions/recoder-preflight-action@v1
        with:
          api_url: ${{ secrets.RECODER_API_URL }}
          api_token: ${{ secrets.RECODER_API_TOKEN }}
          fail_on_blocker: "true"
          fail_on_score_below: "60"

      - name: 결과 출력
        run: |
          echo "점수: ${{ steps.preflight.outputs.risk_score }}"
          echo "통과: ${{ steps.preflight.outputs.passed }}"

  deploy:
    needs: preflight
    runs-on: ubuntu-latest
    steps:
      - name: 배포 실행
        run: echo "배포!"
```

## Inputs

| 이름 | 필수 | 기본값 | 설명 |
|------|------|--------|------|
| `api_url` | ✅ | — | ReCoder Core API 주소 |
| `api_token` | ✅ | — | 인증 토큰 (SESSION_TOKEN) |
| `project_path` | | `.` | 분석할 프로젝트 경로 |
| `fail_on_blocker` | | `true` | 블로커 발견 시 실패 처리 |
| `fail_on_score_below` | | `60` | 이 점수 미만이면 실패 (0이면 비활성화) |
| `timeout_seconds` | | `120` | 요청 타임아웃 (초) |

## Outputs

| 이름 | 설명 |
|------|------|
| `risk_score` | 리스크 점수 (0~100) |
| `blocker_count` | 블로커 수 |
| `warning_count` | 경고 수 |
| `passed` | 통과 여부 (`true`/`false`) |
| `summary` | 결과 요약 텍스트 |

## Secrets 설정

GitHub 저장소 → Settings → Secrets and variables → Actions:

| Secret | 값 |
|--------|----|
| `RECODER_API_URL` | `http://your-server:8000` |
| `RECODER_API_TOKEN` | `recoder-demo-token-2026` |
