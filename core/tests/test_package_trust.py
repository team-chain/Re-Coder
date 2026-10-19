"""2.0.23 — 슬롭스쿼팅·타이포스쿼팅 방어 (core/package_trust.py + NODE_PACKAGE_UNTRUSTED)."""
import json
import time
from pathlib import Path

import pytest

import build_readiness as br
import npm_registry
import package_trust as pt

POP = {"express": 145_000_000, "express-validator": 1_500_000, "react": 188_000_000, "lodash": 160_000_000,
       "axios": 119_000_000, "bcrypt": 6_000_000, "@types/node": 300_000_000}
NOW = time.time()


def _iso(days_ago: float) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(NOW - days_ago * 86400, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def info(days=900, weekly=50_000, script=False, repo=True, deprecated=False):
    return {"exists": True, "created": _iso(days), "weekly": weekly, "install_script": script,
            "repository": repo, "deprecated": deprecated}


# ── 이름 비교 ─────────────────────────────────────────────────────────────

def test_distance_counts_swaps_as_one():
    assert pt.distance("axois", "axios") == 1
    assert pt.distance("lodahs", "lodash") == 1
    assert pt.distance("express", "express") == 0
    assert pt.distance("abc", "xyz", limit=2) == 2


def test_look_alike_finds_popular_neighbour_but_not_itself():
    assert pt.look_alike("exprees-validator", POP) == "express-validator"
    assert pt.look_alike("express_validator", POP) == "express-validator"
    assert pt.look_alike("axois", POP) == "axios"
    assert pt.look_alike("express", POP) is None
    assert pt.look_alike("react-cart-hooks-pro", POP) is None
    # 스코프가 다르면 비교하지 않는다
    assert pt.look_alike("@evil/node", POP) is None


def test_bundled_popular_list_is_loaded():
    pop = pt.popular()
    assert len(pop) > 5000
    for name in ("express", "react", "jsonwebtoken", "bcryptjs", "vite", "@prisma/client"):
        assert name in pop, name


# ── 판정 규칙 ─────────────────────────────────────────────────────────────

def test_popular_package_is_ok_without_lookup():
    assert pt.judge("express", None, pop=POP).level == "ok"


def test_typo_with_low_usage_is_blocked_with_replacement():
    v = pt.judge("exprees-validator", info(weekly=40), pop=POP, now=NOW)
    assert v.level == "block" and "typo" in v.kinds and v.replacement == "express-validator"


def test_similar_name_with_real_usage_is_not_a_typo():
    # 비슷하지만 자기 사용자층이 큰 패키지(유명 목록 밖일 뿐)는 짝퉁으로 보지 않는다
    v = pt.judge("axois", info(weekly=5_000_000), pop=POP, now=NOW)
    assert "typo" not in v.kinds


def test_young_and_unknown_package_is_blocked():
    v = pt.judge("tiny-pay-sdk-kr", info(days=12, weekly=80), pop=POP, now=NOW)
    assert v.level == "block" and v.kinds == ["young"]
    assert "12일" in v.reasons[0]


def test_young_but_already_used_is_not_blocked():
    assert pt.judge("new-but-popular", info(days=5, weekly=90_000), pop=POP, now=NOW).level == "ok"


def test_install_script_and_missing_repo_warn():
    v = pt.judge("some-native-lib", info(script=True, repo=False), pop=POP, now=NOW)
    assert v.level == "warn" and set(v.kinds) == {"install_script", "no_repo"}


def test_deprecated_warns():
    assert pt.judge("old-lib", info(deprecated=True), pop=POP, now=NOW).kinds == ["deprecated"]


def test_network_failure_is_unknown_and_404_is_left_to_version_check():
    assert pt.judge("x-lib", None, pop=POP).level == "unknown"
    assert pt.judge("x-lib", {"exists": False}, pop=POP).level == "ok"


# ── 묶음 조회 ─────────────────────────────────────────────────────────────

def test_assess_skips_popular_and_trusted_scopes(monkeypatch):
    monkeypatch.setattr(pt, "popular", lambda: POP)
    monkeypatch.setenv("RECODER_TRUSTED_SCOPES", "@mycompany")
    asked = []

    def fetch(name):
        asked.append(name)
        return info(days=3, weekly=10)

    out = pt.assess(["express", "@mycompany/ui", "evil-new-thing"], fetch=fetch, now=NOW)
    assert asked == ["evil-new-thing"]
    assert out["express"].level == "ok" and out["@mycompany/ui"].level == "ok"
    assert out["evil-new-thing"].level == "block"


def test_assess_does_not_touch_network_in_tests_or_private_registry(monkeypatch):
    monkeypatch.setattr(pt, "popular", lambda: POP)
    monkeypatch.setattr(pt, "fetch_info", lambda name: pytest.fail("network"))
    assert pt.assess(["unknown-pkg"]) == {}
    monkeypatch.setenv("RECODER_NPM_REGISTRY", "1")
    monkeypatch.setenv("NPM_CONFIG_REGISTRY", "https://npm.mycompany.internal")
    assert pt.assess(["unknown-pkg"]) == {}


def test_cache_is_reused_for_a_day(monkeypatch, tmp_path):
    monkeypatch.setenv("RECODER_HOME", str(tmp_path))
    calls = []
    monkeypatch.setattr(pt, "fetch_info", lambda name: calls.append(name) or info())
    pt._cached_fetch("cached-lib")
    pt._cached_fetch("cached-lib")
    assert calls == ["cached-lib"]
    assert (tmp_path / "cache" / "package_trust.json").exists()


def test_rename_dependency_keeps_formatting():
    text = '{\n  "dependencies": {\n    "exprees-validator": "^7.0.0",\n    "express": "^4.18.2"\n  }\n}\n'
    out = pt.rename_dependency(text, "dependencies", "exprees-validator", "express-validator", "^7.2.1")
    assert '"express-validator": "^7.2.1"' in out and out.endswith("}\n") and "exprees" not in out


# ── 배포 준비 점검 연결 ───────────────────────────────────────────────────

def _project(root: Path, deps: dict) -> None:
    (root / "src").mkdir(parents=True)
    (root / "package.json").write_text(json.dumps({"name": "app", "main": "src/server.js",
                                                    "dependencies": deps}, indent=2), encoding="utf-8")
    (root / "src" / "server.js").write_text(
        "const express = require('express');\nconst { body } = require('exprees-validator');\n"
        "const app = express();\napp.get('/health', (q, r) => r.json({ok: true}));\napp.listen(process.env.PORT || 3000);\n",
        encoding="utf-8")


@pytest.fixture
def fake_registry(monkeypatch):
    monkeypatch.setattr(pt, "popular", lambda: POP)
    monkeypatch.setenv("RECODER_NPM_REGISTRY", "1")
    infos = {"exprees-validator": info(weekly=30), "tiny-pay-sdk-kr": info(days=9, weekly=20),
             "odd-native": info(script=True)}
    monkeypatch.setattr(pt, "_cached_fetch", lambda name: infos.get(name, info()))
    monkeypatch.setattr(npm_registry, "fetch_versions", lambda name: ["7.0.0", "7.2.1"] if name == "express-validator" else ["4.21.2"])
    return infos


def test_typo_package_is_reported_and_auto_fixed(tmp_path, fake_registry):
    _project(tmp_path, {"express": "^4.21.2", "exprees-validator": "^7.0.0"})
    issue = next(i for i in br.analyze(tmp_path, online=True).issues if i.code == "NODE_PACKAGE_UNTRUSTED")
    assert issue.severity == br.ERROR and issue.auto_fix and "express-validator" in issue.fix
    result = br.apply_fix(tmp_path, "NODE_PACKAGE_UNTRUSTED")
    assert result["applied"]
    pkg = json.loads((tmp_path / "package.json").read_text(encoding="utf-8"))
    assert pkg["dependencies"] == {"express": "^4.21.2", "express-validator": "^7.2.1"}
    assert "require('express-validator')" in (tmp_path / "src" / "server.js").read_text(encoding="utf-8")
    assert not any(i.code == "NODE_PACKAGE_UNTRUSTED" for i in br.analyze(tmp_path, online=True).issues)


def test_young_package_is_an_error_for_ai_repair(tmp_path, fake_registry):
    _project(tmp_path, {"express": "^4.21.2", "tiny-pay-sdk-kr": "^1.0.0"})
    issue = next(i for i in br.analyze(tmp_path, online=True).issues if i.code == "NODE_PACKAGE_UNTRUSTED")
    assert issue.severity == br.ERROR and not issue.auto_fix
    assert "우회하지 마세요" in issue.fix and "tiny-pay-sdk-kr" in issue.message


def test_warning_only_package_does_not_block(tmp_path, fake_registry):
    _project(tmp_path, {"express": "^4.21.2", "odd-native": "^1.0.0"})
    issue = next(i for i in br.analyze(tmp_path, online=True).issues if i.code == "NODE_PACKAGE_UNTRUSTED")
    assert issue.severity == br.WARNING


def test_offline_analysis_never_runs_trust_check(tmp_path, monkeypatch):
    monkeypatch.setattr(pt, "assess", lambda *a, **k: pytest.fail("offline analysis must not assess"))
    _project(tmp_path, {"express": "^4.21.2", "exprees-validator": "^7.0.0"})
    br.analyze(tmp_path, online=False)


def test_short_or_two_edit_names_are_not_flagged():
    pop = {**POP, "jwt-decode": 9_000_000, "ora": 30_000_000, "redux-form": 400_000}
    assert pt.look_alike("orm", pop) is None
    assert pt.look_alike("jwt-js-decode", pop) is None
    assert pt.look_alike("redux-orm", pop) == "redux-form"  # 한 글자 차이는 후보 — 판정은 사용량으로
    assert pt.judge("redux-orm", info(weekly=13_000), pop=pop, now=NOW).level == "ok"


def test_bundle_ships_popular_list_and_module():
    import release_check
    assert "package_trust" in release_check.RUNTIME_IMPORTS
    spec = (Path(__file__).resolve().parents[1] / "recoder-core.spec").read_text(encoding="utf-8")
    assert "popular_npm.json" in spec
