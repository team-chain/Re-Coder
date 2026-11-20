"""Trivy JSON v2 → 화면이 쓰는 항목. 보안 화면은 제목·CWE 로 "무슨 일이 생길 수 있는지"를 고른다."""
from agents.infra_agent import InfraAgent


def test_filter_keeps_fields_the_security_screen_needs():
    raw = {"SchemaVersion": 2, "Results": [
        {"Target": "shop:latest (alpine 3.22.1)", "Class": "os-pkgs", "Type": "alpine", "Vulnerabilities": [
            {"VulnerabilityID": "CVE-1", "PkgName": "libssl3", "InstalledVersion": "3.5.1-r0", "FixedVersion": "3.5.4-r0",
             "Severity": "HIGH", "Title": "openssl: out-of-bounds read", "CweIDs": ["CWE-125", 7, "CWE-787"]},
            {"VulnerabilityID": "CVE-2", "PkgName": "busybox", "InstalledVersion": "1.37.0", "Severity": "CRITICAL", "Title": "busybox: tar"},
            {"VulnerabilityID": "CVE-3", "PkgName": "zlib", "InstalledVersion": "1", "Severity": "MEDIUM"}]},
        {"Target": "Node.js", "Class": "lang-pkgs", "Type": "node-pkg", "Vulnerabilities": [
            {"VulnerabilityID": "CVE-4", "PkgName": "axios", "PkgPath": "app/node_modules/axios/package.json", "InstalledVersion": "0.21.4",
             "FixedVersion": "0.30.0", "Severity": "HIGH", "Title": "axios: SSRF", "CweIDs": None}]}]}
    critical, high = InfraAgent._filter_trivy_results(raw)
    assert [c["package"] for c in critical] == ["busybox"] and critical[0]["fixed"] is None and critical[0]["cwe"] == []
    assert [h["package"] for h in high] == ["libssl3", "axios"], "MEDIUM 은 게이트 대상이 아니다"
    assert high[0]["cwe"] == ["CWE-125", "CWE-787"], "문자열 CWE 만"
    assert high[1]["class"] == "lang-pkgs" and high[1]["pkg_path"].startswith("app/") and high[1]["cwe"] == []
