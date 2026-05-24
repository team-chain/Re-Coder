# ReCoder Preflight Action (`recoder/preflight-action@v1`)

> 설계서 §40 — GitHub Marketplace 공식 배포 Composite Action

ECS 배포 전에 AWS 리소스를 자동으로 점검하고, 실패 시 배포를 차단합니다.

## 사용 방법

```yaml
- name: ReCoder Preflight
  uses: recoder/preflight-action@v1
  with:
    cluster: my-cluster
    service: my-api-service
    region: ap-northeast-2
  env:
    AWS_ACCESS_KEY_ID: ${{ secrets.AWS_ACCESS_KEY_ID }}
    AWS_SECRET_ACCESS_KEY: ${{ secrets.AWS_SECRET_ACCESS_KEY }}
```

## 입력 파라미터

| 파라미터 | 필수 | 기본값 | 설명 |
|---------|------|-------|------|
| `cluster` | ✅ | — | ECS 클러스터 이름 |
| `service` | ✅ | — | ECS 서비스 이름 |
| `region` | ❌ | `ap-northeast-2` | AWS 리전 |
| `task_definition` | ❌ | — | ECS 태스크 정의 패밀리 |
| `ecr_repo` | ❌ | — | ECR 리포지토리 이름 |
| `log_group` | ❌ | — | CloudWatch Log Group |
| `fail_on_warning` | ❌ | `false` | MEDIUM 리스크도 실패 처리 |
| `output_format` | ❌ | `github` | 출력 형식 (text/json/github) |

## 출력 값

| 출력 | 설명 |
|------|------|
| `passed` | 전체 통과 여부 (`true`/`false`) |
| `risk_level` | 최고 리스크 레벨 |
| `report_json` | 전체 결과 JSON |
| `failed_checks` | 실패 항목 수 |

## 전체 파이프라인 예시

```yaml
name: Deploy to ECS

on:
  push:
    branches: [main]

jobs:
  preflight:
    runs-on: ubuntu-latest
    outputs:
      passed: ${{ steps.check.outputs.passed }}
    steps:
      - uses: actions/checkout@v4

      - name: ReCoder Preflight
        id: check
        uses: recoder/preflight-action@v1
        with:
          cluster: ${{ vars.ECS_CLUSTER }}
          service: ${{ vars.ECS_SERVICE }}
          region: ap-northeast-2
          fail_on_warning: true
        env:
          AWS_ACCESS_KEY_ID: ${{ secrets.AWS_ACCESS_KEY_ID }}
          AWS_SECRET_ACCESS_KEY: ${{ secrets.AWS_SECRET_ACCESS_KEY }}

  deploy:
    needs: preflight
    if: needs.preflight.outputs.passed == 'true'
    runs-on: ubuntu-latest
    steps:
      - name: ECS 배포
        run: echo "Preflight 통과 — 배포 진행"
```

## IAM 권한 (read-only)

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": [
      "ecr:DescribeRepositories",
      "ecs:DescribeClusters",
      "ecs:DescribeServices",
      "iam:GetRole",
      "logs:DescribeLogGroups",
      "elasticloadbalancing:DescribeLoadBalancers",
      "elasticloadbalancing:DescribeTargetGroups"
    ],
    "Resource": "*"
  }]
}
```

## 설계서 참조

- §40 GitHub Action 공식 배포
- §37.3 `/recoder preflight` Discord 커맨드와 동일한 점검 로직
- §Q3 Cloud Preflight Assistant
