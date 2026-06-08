"""AI 불이 왜 안 켜지는지 진단. Core 와 같은 자격증명/리전으로 Bedrock 을 직접 찔러본다.
실행:  cd C:\\ReCoder\\Re-Coder\\core ;  .venv\\Scripts\\python.exe check_ai.py
(.venv 가 없으면 python check_ai.py)
"""
import os

# .env 로드 (Core 와 동일 환경)
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

region = (os.getenv("BEDROCK_REGION") or os.getenv("AWS_REGION")
          or os.getenv("AWS_DEFAULT_REGION") or "ap-northeast-2")
print(f"[리전] {region}")

import boto3

sess = boto3.Session()
creds = sess.get_credentials()
if creds is None:
    print("❌ AWS 자격증명을 못 찾음. `aws configure` 가 안 돼 있거나 프로필이 비었음.")
    raise SystemExit(1)
frozen = creds.get_frozen_credentials()
print(f"[자격증명] AccessKeyId = {frozen.access_key[:8]}...  (있음)")

try:
    ident = sess.client("sts", region_name=region).get_caller_identity()
    print(f"[계정] {ident['Account']}  ARN={ident['Arn']}")
except Exception as e:
    print(f"[계정] STS 확인 실패: {e}")

rt = sess.client("bedrock-runtime", region_name=region)

def ping(mid):
    try:
        rt.converse(modelId=mid,
                    messages=[{"role": "user", "content": [{"text": "ping"}]}],
                    inferenceConfig={"maxTokens": 1})
        return True, ""
    except Exception as e:
        return False, str(e).split("\n")[0][:160]

candidates = [
    os.getenv("BEDROCK_PRIMARY_MODEL_IDENTIFIER", ""),
    "global.anthropic.claude-haiku-4-5-20251001-v1:0",
    "apac.anthropic.claude-3-5-sonnet-20241022-v2:0",
    "anthropic.claude-3-haiku-20240307-v1:0",
    "apac.anthropic.claude-sonnet-4-5-20250929-v1:0",
]
seen = set()
ok_any = False
print("\n[모델 핑 테스트]")
for mid in candidates:
    mid = mid.strip()
    if not mid or mid in seen:
        continue
    seen.add(mid)
    ok, err = ping(mid)
    if ok:
        print(f"  ✅ {mid}")
        ok_any = True
    else:
        print(f"  ❌ {mid}\n       → {err}")

print("\n" + ("✅ AI OK — 최소 1개 모델 호출 성공. 이게 되면 Core 재시작하면 AI 불 켜짐."
              if ok_any else
              "❌ AI FAIL — 호출되는 모델이 하나도 없음. 위 에러 메시지를 봐줘."))
