"""
정책 Rego 생성기 — Codex P1 2건 회귀 테스트 (실제 OPA 평가).

1. complete(`deny_reasons := set()`)와 partial(`deny_reasons["…"]`)이
   공존하면 Rego 컴파일이 거부돼 **모든 정책 번들 로드가 불능**이었다.
2. decision 이 complete rule 인데 "deny" 와 "deny_with_fix_suggestion" 이
   동시에 참이 되어 evaluation conflict — 호출자는 OPA 불능으로 오판.

OPA 바이너리가 있으면 컴파일+평가까지, 없으면 텍스트 불변식만 검사한다.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
os.environ.setdefault("CONTROL_PLANE_DATABASE_URL", "sqlite+aiosqlite://")

from control_plane.models.schemas import PolicyPresetConfig, PolicyPresetKey  # noqa: E402
from control_plane.services.policy_service import _generate_rego  # noqa: E402

_OPA = shutil.which("opa") or ("/tmp/opa" if Path("/tmp/opa").exists() else None)

_ALL_PRESETS = [PolicyPresetConfig(key=k, enabled=True) for k in PolicyPresetKey]


def _eval(rego: str, input_doc: dict) -> dict:
    """opa eval 로 data.recoder.policy 전체를 평가한다."""
    with tempfile.TemporaryDirectory() as td:
        mod = Path(td) / "policy.rego"
        mod.write_text(rego, encoding="utf-8")
        inp = Path(td) / "input.json"
        inp.write_text(json.dumps(input_doc), encoding="utf-8")
        proc = subprocess.run(
            [_OPA, "eval", "-d", str(mod), "-i", str(inp),
             "--format", "json", "data.recoder.policy"],
            capture_output=True, text=True, timeout=30)
        assert proc.returncode == 0, f"opa eval 실패:\n{proc.stderr}\n--- rego ---\n{rego}"
        out = json.loads(proc.stdout)
        return out["result"][0]["expressions"][0]["value"]


# ── 텍스트 불변식 (OPA 없이도 검사) ────────────────────────────────────

def test_no_complete_partial_conflict_when_presets_enabled():
    """[Codex P1 회귀] deny preset 이 켜지면 빈 complete 정의를 내보내지 않는다."""
    rego = _generate_rego(_ALL_PRESETS)
    assert 'deny_reasons["' in rego, "partial deny 규칙이 없다"
    assert "deny_reasons := set()" not in rego, (
        "complete + partial 공존 — Rego 컴파일이 거부된다"
    )


def test_empty_bundle_still_defines_deny_reasons():
    """[음성 대조] preset 이 하나도 없으면 빈 집합 정의가 필요하다 —
    없으면 count(deny_reasons)가 undefined 로 allow 까지 무너진다."""
    rego = _generate_rego([])
    assert "deny_reasons := set()" in rego


# ── 실제 OPA 평가 ──────────────────────────────────────────────────────

needs_opa = pytest.mark.skipif(_OPA is None, reason="opa 바이너리 없음")


@needs_opa
def test_all_presets_compile_and_load():
    """[Codex P1 본판] 모든 preset 을 켠 번들이 컴파일·평가된다."""
    rego = _generate_rego(_ALL_PRESETS)
    value = _eval(rego, {"level": 1, "context": {"branch": "main", "environment": "staging",
                                                 "generate_sbom": True}})
    assert value.get("decision") == "allow", value


@needs_opa
@pytest.mark.parametrize("ctx,level,expected,label", [
    ({"trivy_critical_count": 3, "branch": "main", "environment": "staging",
      "generate_sbom": True}, 1,
     "deny_with_fix_suggestion", "Trivy 거부 → 수정 제안 (충돌 없이 단일 값)"),
    ({"branch": "develop", "environment": "production", "generate_sbom": True}, 1,
     "deny", "브랜치 거부 → 일반 deny"),
    ({"trivy_critical_count": 1, "branch": "develop", "environment": "production",
      "generate_sbom": True}, 1,
     "deny_with_fix_suggestion", "혼합 거부 → 수정 제안 우선"),
    ({"branch": "main", "environment": "staging", "generate_sbom": True,
      "env_keys": ["DB_PASSWORD"]}, 1,
     "escalate_to_security", "시크릿 env → 격상 (deny 와 충돌 없이)"),
    ({"trivy_critical_count": 1, "branch": "main", "environment": "staging",
      "generate_sbom": True, "env_keys": ["API_TOKEN"]}, 1,
     "escalate_to_security", "격상 + trivy 동시 → 격상 단일 값"),
    ({"branch": "main", "environment": "staging", "generate_sbom": True}, 3,
     "allow_with_approval", "Level 3 → 2인 승인"),
])
def test_decision_is_single_valued(ctx, level, expected, label):
    """[Codex P1 회귀] 어떤 입력에서도 decision 은 **정확히 하나의 값**이다.

    complete rule 두 몸통이 다른 값을 동시에 내면 opa eval 자체가
    eval_conflict_error 로 실패한다 — _eval 의 returncode 검사가 그걸 잡는다.
    """
    rego = _generate_rego(_ALL_PRESETS)
    value = _eval(rego, {"level": level, "context": ctx})
    assert value.get("decision") == expected, f"{label}: {value}"
