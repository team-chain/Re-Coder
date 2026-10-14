"""보안 게이트 차단 시 '무엇을 어떻게 고치나' 안내 (vuln_advice) + 배포 준비 점검 연동."""
import json

import build_readiness as br
from vuln_advice import advise


def _f(pkg, installed, fixed, cls="lang-pkgs", path="", sev="CRITICAL", vid="CVE-X"):
    return {"severity": sev, "id": vid, "package": pkg, "installed": installed, "fixed": fixed,
            "class": cls, "pkg_path": path}


def _ws(tmp_path, deps, lock=False):
    (tmp_path / "package.json").write_text(json.dumps({"name": "a", "dependencies": deps}), encoding="utf-8")
    if lock:
        (tmp_path / "package-lock.json").write_text("{}", encoding="utf-8")
    return str(tmp_path)


def test_하위의존성_tar_는_sqlite3_업그레이드로_안내(tmp_path):
    ws = _ws(tmp_path, {"express": "^4.18.2", "sqlite3": "^5.1.6"})
    out = advise([_f("tar", "6.2.1", "7.5.21", path="app/node_modules/tar/package.json")], ws)
    item = out["items"][0]
    assert item["origin"] == "app"
    assert "sqlite3" in item["fix"] and "^6.0.1" in item["fix"] and "자동 수정" in item["fix"]
    assert out["diagnosis"]["code"] == "IMAGE_CRITICAL_CVE"
    assert "1건" in out["diagnosis"]["title"]


def test_lock_이_있으면_npm_install_명령으로_안내(tmp_path):
    ws = _ws(tmp_path, {"sqlite3": "^5.1.6"}, lock=True)
    item = advise([_f("tar", "6.2.1", "7.5.21")], ws)["items"][0]
    assert "npm install sqlite3@6.0.1" in item["fix"]


def test_직접_의존성은_버전_올리기(tmp_path):
    ws = _ws(tmp_path, {"lodash": "^4.17.0"})
    item = advise([_f("lodash", "4.17.0", "4.17.21")], ws)["items"][0]
    assert "package.json 의 lodash 를 4.17.21 이상" in item["fix"]


def test_베이스이미지_OS_패키지와_번들_npm_구분(tmp_path):
    ws = _ws(tmp_path, {})
    out = advise([
        _f("libssl3", "3.3.1-r0", "3.3.2-r0", cls="os-pkgs", vid="CVE-1"),
        _f("cross-spawn", "7.0.3", "7.0.5", path="usr/local/lib/node_modules/npm/node_modules/cross-spawn/package.json", vid="CVE-2"),
        _f("zlib", "1.3", "", cls="os-pkgs", vid="CVE-3"),
    ], ws)
    by = {i["package"]: i for i in out["items"]}
    assert by["libssl3"]["origin"] == "base_os" and "apk upgrade" in by["libssl3"]["fix"]
    assert by["cross-spawn"]["origin"] == "base_npm" and "rm -rf /usr/local/lib/node_modules/npm" in by["cross-spawn"]["fix"]
    assert "수정 버전이 없습니다" in by["zlib"]["fix"]
    assert any("[베이스 이미지]" in line for line in out["diagnosis"]["lines"])


def test_HIGH_는_제외하고_중복은_한번만(tmp_path):
    out = advise([_f("a", "1", "2"), _f("a", "1", "2"), _f("b", "1", "2", sev="HIGH")], None)
    assert [i["package"] for i in out["items"]] == ["a"]


def test_알수없는_하위의존성은_overrides_안내(tmp_path):
    ws = _ws(tmp_path, {"express": "^4"})
    item = advise([_f("form-data", "4.0.0", "4.0.4")], ws)["items"][0]
    assert "overrides" in item["fix"] and "4.0.4" in item["fix"]


def test_준비점검_sqlite3_5_는_오류이고_자동수정은_서식을_지킨다(tmp_path):
    text = '{\r\n    "name": "a",\r\n    "dependencies": {\r\n        "express": "^4.18.2",\r\n        "sqlite3": "^5.1.6"\r\n    }\r\n}\r\n'
    (tmp_path / "package.json").write_bytes(text.encode())
    issues = {i.code: i for i in br.analyze(tmp_path).issues}
    assert issues["NODE_VULNERABLE_DEPENDENCY"].severity == "error"
    assert issues["NODE_VULNERABLE_DEPENDENCY"].auto_fix
    out = br.apply_fix(tmp_path, "NODE_VULNERABLE_DEPENDENCY")
    assert out["applied"]
    raw = (tmp_path / "package.json").read_bytes().decode()
    assert '"sqlite3": "^6.0.1"' in raw and "\r\n" in raw and '        "express"' in raw
    assert "NODE_VULNERABLE_DEPENDENCY" not in {i["code"] for i in out["readiness"]["issues"]}
    assert list((tmp_path / ".recoder" / "backups").iterdir())


def test_준비점검_lock_이_있으면_자동수정_안함(tmp_path):
    _ws(tmp_path, {"sqlite3": "5.1.7"}, lock=True)
    issue = next(i for i in br.analyze(tmp_path).issues if i.code == "NODE_VULNERABLE_DEPENDENCY")
    assert not issue.auto_fix and "npm install sqlite3@6.0.1" in issue.fix


def test_준비점검_안전한_버전과_범위는_통과(tmp_path):
    for spec in ("^6.0.1", ">=5", "latest", "github:TryGhost/node-sqlite3"):
        _ws(tmp_path, {"sqlite3": spec})
        assert "NODE_VULNERABLE_DEPENDENCY" not in {i.code for i in br.analyze(tmp_path).issues}, spec
