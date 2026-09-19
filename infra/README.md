# infra/ — 온보딩 IAM 템플릿

## recoder-iam-quickcreate.json

ReCoder 배포용 최소권한 IAM 사용자(`recoder-deploy`)를 만드는 CloudFormation
템플릿. 확장의 「원클릭 IAM 셋업」 버튼이 이 템플릿을 쓴다.

- **원본은 `core/aws_policy.py` 다.** 이 파일을 손으로 고치지 말 것.
  정책이 바뀌면 `python3 scripts/generate_iam_template.py` 로 재생성해 커밋한다.
  어긋나면 `core/tests/test_aws_onboarding.py` 드리프트 검사가 실패한다.
- 계정 ID·리전·파티션은 `${AWS::AccountId}` 등 CloudFormation 내장 변수가
  스택 생성 시점에 채운다 — 사용자별 템플릿 생성이 필요 없다.

## 호스팅 (quick-create 링크 활성화)

CloudFormation quick-create 링크의 `templateURL` 은 **S3 HTTPS URL 만** 받는다.
팀 계정에서 한 번만 하면 된다:

```bash
aws s3 mb s3://recoder-assets --region ap-northeast-2
aws s3 cp infra/recoder-iam-quickcreate.json s3://recoder-assets/ \
  --content-type application/json
# 이 객체 하나만 공개 읽기 (버킷 전체 공개 아님)
aws s3api put-object-acl --bucket recoder-assets \
  --key recoder-iam-quickcreate.json --acl public-read
```

그 뒤 코어 `.env` 에:

```
RECODER_IAM_TEMPLATE_URL=https://recoder-assets.s3.ap-northeast-2.amazonaws.com/recoder-iam-quickcreate.json
```

설정 전에는 확장이 콘솔 「템플릿 업로드」 플로우로 폴백한다(템플릿 본문은
클립보드에 자동 복사됨). 템플릿을 재생성했으면 S3 객체도 다시 올릴 것.

## 보안 메모

스택 Outputs 에 SecretAccessKey 가 노출된다 — 스택을 볼 수 있는 사람에게
보인다. 개인 계정 온보딩용 절충이며, 조직 계정 사용자는 키를 콘솔에서 따로
발급하라고 Outputs 안내문(GuidanceKo)에 적어 두었다.
