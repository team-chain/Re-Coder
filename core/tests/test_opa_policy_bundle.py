"""OPA 정책 번들 — 보드 태스크 「OPA 서버 정책 번들(.rego) 작성」.

왜 이 테스트가 필요한가
    배포 게이트 규칙이 **두 곳**에 있다.
      1. `policies/recoder/deploy.rego`     — OPA 서버가 있을 때
      2. `opa_gate._local_deploy_gate()`    — OPA 서버가 없을 때
    둘이 갈라지면 OPA 서버를 켰을 때와 껐을 때 결과가 달라진다. 그런 어긋남은
    아무도 눈치채지 못한 채 굳는다 — 정책은 평소에 조용하기 때문이다.

    여기서는 (a) 번들이 코어가 부르는 경로/응답 모양과 맞는지, (b) 두 구현이
    같은 규칙 집합을 갖는지, (c) opa 바이너리가 있으면 `opa test` 까지 돌린다.

한계 — 정직하게
    이 저장소의 CI/개발 컨테이너에 `opa` 바이너리가 없으면 rego **실행**
    검증은 건너뛴다(아래 skipif). 그 경우 이 파일은 구조·계약만 본다.
    실제 정책 동작은 `opa test policies/` 를 한 번 돌려 확인해야 한다.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_CORE = Path(__file__).resolve().parents[1]
_REPO = _CORE.parent
_POLICY_DIR = _REPO / "policies"
_DEPLOY_REGO = _POLICY_DIR / "recoder" / "deploy.rego"

if str(_CORE) not in sys.path:
    sys.path.insert(0, str(_CORE))


def test_번들_파일이_존재한다() -> None:
    assert _DEPLOY_REGO.is_file(), "배포 게이트 정책 번들이 없다"
    assert (_POLICY_DIR / "recoder" / "deploy_test.rego").is_file(), "정책 테스트가 없다"


def test_패키지_경로가_코어가_부르는_경로와_맞는다() -> None:
    """코어는 POST /v1/data/recoder/deploy/allow 로 질의한다.

    그 URL 은 `package recoder.deploy` + 규칙 이름 `allow` 에서 나온다.
    패키지명을 바꾸면 질의가 빈 결과를 받고, 코어는 그걸 deny 로 처리한다 —
    정책이 통과시키려 해도 전부 막히는, 원인 찾기 어려운 고장이 된다.
    """
    from opa_gate import OPAGate  # noqa: F401 — 경로 상수 확인용 import

    text = _DEPLOY_REGO.read_text(encoding="utf-8")
    assert "package recoder.deploy" in text
    assert "allow :=" in text or "allow contains" in text

    #: 코어 쪽 질의 경로가 바뀌면 여기서 잡힌다.
    source = (_CORE / "opa_gate.py").read_text(encoding="utf-8")
    assert '"recoder/deploy/allow"' in source


def test_응답이_코어가_읽는_키를_모두_담는다() -> None:
    """코어 `evaluate()` 는 result 에서 다섯 키를 읽는다.

    boolean 만 돌려주면 사용자는 "왜 막혔는지" 를 영영 못 본다 — 차단 사유는
    정책의 일부다.
    """
    text = _DEPLOY_REGO.read_text(encoding="utf-8")
    for key in ("decision", "reason", "fix_suggestion", "approval_level", "policy_bundle_version"):
        assert f'"{key}"' in text, f"응답에 {key} 가 없다 — 코어가 읽는 키다"


def test_두_구현이_같은_규칙_집합을_갖는다() -> None:
    """rego 와 로컬 폴백이 갈라지면 서버 유무에 따라 결과가 달라진다."""
    rego = _DEPLOY_REGO.read_text(encoding="utf-8")
    fallback = (_CORE / "opa_gate.py").read_text(encoding="utf-8")

    #: 각 규칙을 식별하는 표식. 문구가 바뀌어도 한쪽만 바뀌면 여기서 걸린다.
    for marker in ("SBOM", "critical", "secret", "Hadolint", "프로덕션"):
        assert marker in rego, f"rego 에 '{marker}' 규칙이 없다"
        assert marker in fallback, f"로컬 폴백에 '{marker}' 규칙이 없다"


def test_음성대조_trivy_high_는_차단하지_않는다() -> None:
    """high 까지 막으면 거의 모든 배포가 막힌다 — 설계상 경고지 차단이 아니다."""
    rego = _DEPLOY_REGO.read_text(encoding="utf-8")
    #: critical_count 로만 막아야 한다. high_count 로 deny 하는 규칙이 생기면
    #: 이 검사가 깨진다.
    deny_blocks = rego.split("deny contains msg if")
    for block in deny_blocks[1:]:
        body = block.split("}")[0]
        assert "high_count" not in body, "high 취약점으로 배포를 막고 있다"


@pytest.mark.skipif(shutil.which("opa") is None, reason="opa 바이너리 없음 — rego 실행 검증 생략")
def test_opa_test_가_통과한다() -> None:
    """opa 가 설치돼 있으면 정책을 실제로 실행해 검증한다."""
    proc = subprocess.run(
        ["opa", "test", str(_POLICY_DIR), "-v"],
        capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, f"opa test 실패:\n{proc.stdout}\n{proc.stderr}"
