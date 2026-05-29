# AWS 계정 교체 가이드

현재 계정이 만료되어 **새 AWS 계정**으로 갈아탈 때, 아래 한 흐름으로 끝납니다.
자격증명(access key/secret)은 코드/.env 에 저장하지 않습니다 — `aws configure` 로만 관리합니다.

## 한 번에 (권장)
```powershell
cd C:\ReCoder\Re-Coder\core
# 1) 새 계정 자격증명 등록
aws configure                       # 또는: aws configure --profile recoder
# 2) ReCoder 설정 한 번에 교체 (리전 3종 + 진단 캐시 삭제)
.\.venv\Scripts\python.exe switch_aws.py --region us-east-1
#    프로필을 쓰면:  switch_aws.py --region us-east-1 --profile recoder
# 3) 새 계정 Bedrock 콘솔에서 사용할 모델 액세스 활성화 (위 리전 기준)
# 4) Local Core 재시작
.\.venv\Scripts\python.exe main.py
```

## switch_aws.py 가 해주는 것
- `core/.env` 의 `AWS_REGION` / `AWS_DEFAULT_REGION` / `BEDROCK_REGION` 을 동일 값으로 설정
  (모듈마다 읽는 변수명이 달라 셋 다 맞춰야 리전이 일관됨)
- (선택) `AWS_PROFILE`, Bedrock 모델 ID 갱신
- `~/.recoder/diagnostics.json` 삭제 → 새 계정으로 재진단

## 수동 체크리스트 (스크립트 없이 할 때)
1. `aws configure` 로 새 계정 키 등록 (또는 `~/.aws/credentials` 편집).
2. `core/.env` 에 `AWS_REGION` / `AWS_DEFAULT_REGION` / `BEDROCK_REGION` 동일 값 설정.
3. 새 계정 Bedrock 콘솔에서 모델 액세스 활성화.
4. `~/.recoder/diagnostics.json` 삭제.
5. Local Core 재시작.

## 게이트웨이(학생 배포)를 쓰는 경우 추가
게이트웨이는 별도 계정 리소스라 재배포가 필요합니다.
```powershell
cd C:\ReCoder\Re-Coder\gateway
sam deploy            # 새 계정 자격증명으로
```
- 새 계정에서도 Bedrock 모델 액세스 활성화 + IAM(Bedrock 전용) 확인.
- 새 엔드포인트 URL 이 바뀌면 학생 클라이언트의 `RECODER_LLM_GATEWAY_URL` 갱신.

## 코드에 박힌 계정 의존 값 (실배포 시에만)
- `core/agents/ecs_agent.py` 의 `arn:aws:iam::000000000000:role/...` 플레이스홀더 → ECS 실배포 시 실제 계정 ARN 으로 교체.
