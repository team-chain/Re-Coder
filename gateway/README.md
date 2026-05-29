# ReCoder Gateway (Phase 1)

학생이 **AWS 자격증명 없이** 운영자 계정의 Amazon Bedrock 을 쓰도록 중계하는 서버리스 게이트웨이.
전부 운영자 AWS 계정 안에서 동작하며(API Gateway + Lambda + DynamoDB), **HTTPS(TLS)는 API Gateway가 자동 종단**한다. 트라이얼 규모(20~30명·1주일)는 프리티어 한도 내라 인프라 비용은 사실상 $0이고, **Bedrock 추론만 크레딧을 차감**한다.

## 구성요소
| 파일 | 역할 |
|------|------|
| `template.yaml` | SAM 인프라 (HTTP API, Lambda×2, DynamoDB, Bedrock 전용 IAM) |
| `src/common.py` | 토큰·쿼터·DynamoDB·Bedrock 공통 로직 |
| `src/invoke.py` | `POST /llm/invoke` — 학생 토큰 인증 → 쿼터 → Bedrock 호출 |
| `src/admin.py` | `POST /admin` — 토큰 발급/폐기/쿼터/현황 (운영자 전용) |
| `scripts/issue_tokens.py` | 학생 토큰 일괄 발급 |

## 보안 모델
- 운영자 AWS 키는 **Lambda IAM Role 에만** 존재. 학생에겐 토큰만.
- IAM 은 `bedrock:InvokeModel` 계열 **전용**(그 외 권한 0) → 토큰이 새도 폭발 반경은 Bedrock 한정.
- 토큰 원문은 저장 안 함(**sha256 만 저장**), 7일 TTL 자동 만료.
- 모델 **allowlist**(기본 Haiku)로 비싼 모델 차단.
- per-student 총/일/분당 쿼터 + 학생 풀 전체 $ 캡(소프트 $18, 하드 $20).

## 사전 준비
1. AWS CLI + SAM CLI 설치, 운영자 계정 자격증명 구성.
2. **Bedrock 콘솔에서 사용할 모델(예: Claude 3 Haiku) 액세스 활성화**.
3. 리전 선택(모델 가용 리전, 예: us-east-1).

## 배포
```bash
cd gateway
sam build
sam deploy --guided \
  --parameter-overrides \
    AdminKey=<긴-랜덤-문자열> \
    EnrollCode=<반-공유-코드> MaxStudents=30 \
    AllowedModels=anthropic.claude-3-haiku-20240307-v1:0 \
    PoolCapUsd=20 PoolSoftUsd=18
```
배포 후 출력(Outputs)에서 `GatewayUrl` 과 `AdminEndpoint` 를 확인한다.

> 권장: 배포 후 `InvokeFunction` 의 Bedrock IAM `Resource: "*"` 를 실제 사용하는 **모델 ARN** 으로 좁힌다.

## 학생 토큰 발급
```bash
python scripts/issue_tokens.py \
  --endpoint <AdminEndpoint> \
  --admin-key <AdminKey> \
  --students s01,s02,s03 \
  --out tokens.csv
```
`tokens.csv` 의 토큰을 각 학생에게 개별 전달한다(원문은 발급 시 한 번만 표시).

## 학생 자가발급 (self-enroll) — 토큰이 절대 겹치지 않음

운영자가 일일이 발급하는 대신, 학생이 **반(class) 공유 코드**로 본인 토큰을 1회 자가발급할 수 있다.

```bash
curl -s -XPOST <EnrollEndpoint> -H "Content-Type: application/json"   -d '{"code":"<반 공유 enroll 코드>","name":"홍길동","discord_user_id":"<선택>"}'
# → { "student_id": "<난수>", "token": "rcdr_<id>_<secret>", ... }
```

충돌이 절대 나지 않는 이유(2중):
1. 토큰 secret 은 192bit 난수 → 추측·충돌 불가.
2. student_id 는 DynamoDB **조건부 삽입(attribute_not_exists)** 으로 원자적 고유성 보장. 혹시라도 겹치면 자동 재생성.

오남용 방지:
- `EnrollCode`(반 공유 코드) 없이는 발급 불가.
- 같은 `discord_user_id` 는 1회만(1:1 중복 거부).
- `MaxStudents`(정원) 초과 시 거부.

> 배포 시 `EnrollCode=<코드>` `MaxStudents=30` 파라미터를 넘기면 활성화된다. VSCode 확장 최초 실행에서 이 코드를 입력받아 `/enroll` 을 호출하고, 받은 토큰을 저장하면 "확장 설치 + 반 코드 입력"만으로 끝난다.

## 학생 클라이언트 설정 (VSCode Local Core)
학생 PC 의 Local Core 환경변수에 다음을 주입하면 Bedrock 직접호출 대신 게이트웨이를 사용한다(코드 변경 불필요):
```
RECODER_LLM_GATEWAY_URL=<GatewayUrl>
RECODER_STUDENT_TOKEN=<해당 학생 토큰>
```
> VSIX 패키징 시 `RECODER_LLM_GATEWAY_URL` 을 기본 주입하고, `RECODER_STUDENT_TOKEN` 만 최초 1회 입력받게 구성하면 "설치 + 토큰 입력"으로 끝난다.

## 비용 천장 (2중)
1. **게이트웨이 소프트 캡**(실시간): 풀 누적 비용이 `PoolSoftUsd`($18)에 닿으면 학생 호출 차단.
2. **AWS Budget Action**(백스톱): 콘솔에서 $20 예산 + 임계 도달 시 `InvokeFunction` Role 에 Bedrock deny 정책을 적용하도록 설정(Budgets 는 실시간이 아니므로 보조).

## 운영 명령 (admin)
```bash
# 현황(풀)
curl -s -XPOST <AdminEndpoint> -H "X-Admin-Key: <key>" -d '{"action":"status"}'
# 특정 학생 현황
curl -s -XPOST <AdminEndpoint> -H "X-Admin-Key: <key>" -d '{"action":"status","student_id":"s01"}'
# 학생 정지
curl -s -XPOST <AdminEndpoint> -H "X-Admin-Key: <key>" -d '{"action":"revoke","student_id":"s01"}'
# 쿼터 조정
curl -s -XPOST <AdminEndpoint> -H "X-Admin-Key: <key>" -d '{"action":"quota","student_id":"s01","max_total":800000}'
```

## 트라이얼 종료
토큰은 7일 TTL 로 자동 만료된다. 즉시 종료하려면 admin `revoke` 또는 DynamoDB 테이블 삭제.

## 다음 단계 (이 Phase 에 미포함)
- Phase 2: 릴레이(API GW WebSocket+Lambda+DynamoDB) — Discord→학생 VSCode 연동
- Phase 3: Discord 봇 배선 + Approval Level 게이트
