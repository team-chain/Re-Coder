import json, sys
sys.path.insert(0, "core")
from aws_policy import build_policy

policy = build_policy()
actions = sorted({a for s in policy["Statement"]
                  for a in (s["Action"] if isinstance(s["Action"], list) else [s["Action"]])})
print(f"액션 {len(actions)}개 / 문장 {len(policy['Statement'])}개 / "
      f"와일드카드 리소스 {sum(1 for s in policy['Statement'] if s.get('Resource') == '*')}개")
print()
print(json.dumps(policy, ensure_ascii=False, indent=2))
