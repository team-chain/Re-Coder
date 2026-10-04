"""보안 검사 결과 → 결정론적 수정안(security_fix). 찾기만 하고 끝나지 않는다."""
from __future__ import annotations

import json
from pathlib import Path

import security_fix as sf


def _write(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8", newline="")
    return root


DOCKERFILE = """FROM node:20-alpine AS deps
WORKDIR /app
COPY package*.json ./
RUN npm ci --omit=dev

FROM node:20-alpine
WORKDIR app
ADD . .
COPY --from=deps /app/node_modules ./node_modules
USER node
EXPOSE 3000
CMD node server.js
"""


def _trivy(*items: dict) -> dict:
    return {"trivy": {"status": "ok", "findings": list(items)}}


def test_transitive_npm_vuln_becomes_an_override_and_direct_one_a_bump(tmp_path):
    root = _write(tmp_path, {
        "package.json": json.dumps({"name": "a", "dependencies": {"express": "^4.17.1", "axios": "^0.19.2"}}, indent=2) + "\n",
        "Dockerfile": DOCKERFILE,
    })
    reports = _trivy(
        {"package": "axios", "installed": "0.19.2", "fixed": "0.21.1", "class": "lang-pkgs", "type": "node-pkg",
         "pkg_path": "app/node_modules/axios/package.json", "severity": "HIGH"},
        {"package": "axios", "installed": "0.19.2", "fixed": "0.28.0, 1.6.0", "class": "lang-pkgs", "type": "node-pkg",
         "pkg_path": "app/node_modules/axios/package.json", "severity": "HIGH"},
        {"package": "path-to-regexp", "installed": "0.1.7", "fixed": "0.1.10", "class": "lang-pkgs", "type": "node-pkg",
         "pkg_path": "app/node_modules/path-to-regexp/package.json", "severity": "HIGH"},
    )
    props = {p.title.split(" ")[0]: p for p in sf.plan(str(root), reports) if p.tool == "trivy" and "→" in p.title}
    axios, ptr = props["axios"], props["path-to-regexp"]
    assert "0.28.0" in axios.title and not axios.risk, "같은 메이저 안에서 모두 고치는 버전을 골라야 한다"
    assert "overrides" in ptr.detail
    #: 하나만 골라도 그것만 바뀐다.
    result = sf.apply(str(root), reports, [ptr.id])
    data = json.loads((root / "package.json").read_text(encoding="utf-8"))
    assert data["overrides"] == {"path-to-regexp": "^0.1.10"} and data["dependencies"]["axios"] == "^0.19.2"
    assert result["applied"] == [ptr.id] and result["rebuild"]
    assert any(b.startswith(".recoder/backups/package.json") for b in result["backups"])
    #: 여러 수정이 같은 파일을 고쳐도 백업은 적용 한 번에 파일당 하나.
    both = sf.plan(str(root), reports)
    multi = sf.apply(str(root), reports, [p.id for p in both if p.auto and "→" in p.title])
    assert len([b for b in multi["backups"] if "package.json" in b]) <= 1
    sf.apply(str(root), reports, [axios.id])
    data = json.loads((root / "package.json").read_text(encoding="utf-8"))
    assert data["dependencies"]["axios"] == "^0.28.0" and data["overrides"]["path-to-regexp"] == "^0.1.10"
    #: 다시 계획하면 이미 고친 것은 나오지 않는다.
    assert not [p for p in sf.plan(str(root), reports) if "→" in p.title]


def test_major_bump_is_flagged_as_risk(tmp_path):
    root = _write(tmp_path, {"package.json": json.dumps({"dependencies": {"jsonwebtoken": "^8.5.1"}})})
    reports = _trivy({"package": "jsonwebtoken", "installed": "8.5.1", "fixed": "9.0.0", "class": "lang-pkgs",
                      "type": "node-pkg", "pkg_path": "app/node_modules/jsonwebtoken/package.json"})
    p = next(p for p in sf.plan(str(root), reports) if "jsonwebtoken" in p.title)
    assert p.risk and "메이저" in p.risk


def test_base_image_fixes_do_not_shift_hadolint_lines(tmp_path):
    root = _write(tmp_path, {"package.json": "{}", "Dockerfile": DOCKERFILE.replace("\n", "\r\n")})
    reports = {
        **_trivy({"package": "libcrypto3", "installed": "3.1.4-r0", "fixed": "3.1.4-r5", "class": "os-pkgs", "type": "alpine"},
                 {"package": "cross-spawn", "installed": "7.0.3", "fixed": "7.0.5", "class": "lang-pkgs", "type": "node-pkg",
                  "pkg_path": "usr/local/lib/node_modules/npm/node_modules/cross-spawn/package.json"}),
        "hadolint": {"findings": [
            {"line": 7, "code": "DL3000", "level": "error", "message": "Use absolute WORKDIR"},
            {"line": 8, "code": "DL3020", "level": "error", "message": "Use COPY instead of ADD for files and folders"},
            {"line": 12, "code": "DL3025", "level": "warning", "message": "Use arguments JSON notation for CMD"},
        ]},
    }
    props = sf.plan(str(root), reports)
    titles = [p.title for p in props]
    assert any("OS 패키지" in t for t in titles) and any("npm 제거" in t for t in titles)
    eol = next(p for p in props if "런타임" in p.title)
    assert eol.risk, "런타임 메이저 변경은 기본으로 고르지 않는다"
    pull = next(p for p in props if p.title == "베이스 이미지 새로 받기")
    assert ("", ("docker", "pull", "node:20-alpine")) in pull.post
    chosen = [p.id for p in props if p.auto and not p.risk and p.title != "베이스 이미지 새로 받기"]
    result = sf.apply(str(root), reports, chosen)
    assert len(result["applied"]) == len(chosen), result
    raw = (root / "Dockerfile").read_bytes().decode("utf-8")
    assert "\r\n" in raw and "\n" not in raw.replace("\r\n", ""), "CRLF 를 유지해야 한다"
    text = raw.replace("\r\n", "\n")
    assert "FROM node:20-alpine\n# ReCoder" in text and "RUN apk upgrade --no-cache" in text
    assert "WORKDIR /app\nCOPY . ." in text, "앞에서 줄을 끼워 넣어도 해당 줄을 고쳐야 한다"
    assert 'CMD ["node", "server.js"]' in text
    assert text.index("node_modules/npm") < text.index("USER node")
    assert text.count("ADD ") == 0


def test_npm_is_kept_when_the_app_starts_with_npm(tmp_path):
    root = _write(tmp_path, {"Dockerfile": "FROM node:22-alpine\nWORKDIR /app\nCOPY . .\nCMD [\"npm\", \"start\"]\n"})
    reports = _trivy({"package": "tar", "installed": "6.1.0", "fixed": "6.2.1", "class": "lang-pkgs", "type": "node-pkg",
                      "pkg_path": "usr/local/lib/node_modules/npm/node_modules/tar/package.json"})
    p = next(p for p in sf.plan(str(root), reports) if "npm" in p.title)
    assert not p.auto and "CMD" in p.detail


def test_secret_moves_to_env_and_is_masked_in_the_preview(tmp_path):
    secret = "sk_live_51HxYzAbCdEfGhIjKlMnOp"
    root = _write(tmp_path, {
        "package.json": json.dumps({"dependencies": {"express": "^4"}}),
        "server.js": f"const express = require('express');\nconst stripeKey = '{secret}';\napp.listen(3000);\n",
        "Dockerfile": "FROM node:22-alpine\nCOPY . .\nCMD [\"node\", \"server.js\"]\n",
    })
    reports = {"gitleaks": {"findings": [{"rule_id": "stripe-access-token", "file": "/repo/server.js", "line": 2}]}}
    p = next(p for p in sf.plan(str(root), reports) if p.tool == "gitleaks")
    assert p.auto and secret not in p.diff and "sk_l" in p.diff
    assert p.risk, "dotenv 없이 .env 로 옮기면 Docker 밖 실행에서 값이 빈다 — 기본 선택 안 함"
    assert secret not in json.dumps(p.public(), ensure_ascii=False)
    sf.apply(str(root), reports, [p.id])
    assert "process.env.STRIPE_KEY" in (root / "server.js").read_text(encoding="utf-8")
    assert f"STRIPE_KEY={secret}" in (root / ".env").read_text(encoding="utf-8")
    assert "STRIPE_KEY=\n" in (root / ".env.example").read_text(encoding="utf-8")
    assert ".env" in (root / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ".env" in (root / ".dockerignore").read_text(encoding="utf-8").splitlines()
    backups = list((root / ".recoder" / "backups").iterdir())
    assert not [b for b in backups if b.name.startswith(".env")], "키 값을 백업 폴더에 복사하지 않는다"
    assert all(secret not in b.read_text(encoding="utf-8") for b in backups), "백업 파일에 키가 남았다"
    assert any("[.env 로 옮김]" in b.read_text(encoding="utf-8") for b in backups if b.name.startswith("server.js"))
    #: 옮긴 뒤 .env 는 PC 에만 있는 키 — 유출로 세지 않는다.
    assert sf.local_env_only(root, ".env")


def test_python_secret_and_browser_code(tmp_path):
    root = _write(tmp_path, {
        "app.py": '#!/usr/bin/env python\nAPI_TOKEN = "ghp_abcdefghijklmnopqrstuvwxyz0123456789"\n',
        "client/src/api.tsx": 'const key = "AIzaSyD-abcdefghijklmnopqrstuvwxyz12345";\n',
        ".gitignore": ".env\n",
    })
    reports = {"gitleaks": {"findings": [{"rule_id": "github-pat", "file": "/repo/app.py", "line": 2},
                                         {"rule_id": "gcp-api-key", "file": "/repo/client/src/api.tsx", "line": 1}]}}
    props = sf.plan(str(root), reports)
    py = next(p for p in props if "app.py" in p.title)
    web = next(p for p in props if "api.tsx" in p.title)
    assert not web.auto and "브라우저" in web.detail
    sf.apply(str(root), reports, [py.id])
    out = (root / "app.py").read_text(encoding="utf-8")
    assert out.startswith("#!/usr/bin/env python\nimport os\n") and 'os.environ.get("API_TOKEN", "")' in out


def test_local_env_is_not_a_leak_but_an_unignored_one_gets_fixed(tmp_path):
    root = _write(tmp_path, {".env": "OPENAI_API_KEY=sk-proj-abc\n", ".gitignore": "node_modules\n.env\n",
                             ".dockerignore": ".env\n", "Dockerfile": "FROM node:22-alpine\n"})
    reports = {"gitleaks": {"findings": [{"rule_id": "openai", "file": "/repo/.env", "line": 1}]}}
    (p,) = sf.plan(str(root), reports)
    assert not p.auto and "유출 아님" in p.title
    (root / ".dockerignore").write_text("node_modules\n", encoding="utf-8")
    (p,) = sf.plan(str(root), reports)
    assert p.auto and p.files == [".dockerignore", ".gitignore"]
    sf.apply(str(root), reports, [p.id])
    gi = (root / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert gi.count(".env") == 1 and ".recoder/" in gi, "이미 있는 줄은 다시 쓰지 않고 빠진 줄만 덧붙인다"
    assert sf.local_env_only(root, ".env")


def test_requirements_pin(tmp_path):
    root = _write(tmp_path, {"requirements.txt": "flask==2.0.1  # web\nrequests>=2.20\n"})
    reports = _trivy({"package": "Flask", "installed": "2.0.1", "fixed": "2.2.5, 3.0.1", "type": "python-pkg", "class": "lang-pkgs", "pkg_path": "x"},
                     {"package": "urllib3", "installed": "1.26.5", "fixed": "1.26.18", "type": "python-pkg", "class": "lang-pkgs", "pkg_path": "x"})
    props = sf.plan(str(root), reports)
    sf.apply(str(root), reports, [p.id for p in props])
    text = (root / "requirements.txt").read_text(encoding="utf-8")
    assert "flask==2.2.5  # web" in text and "urllib3>=1.26.18" in text


def test_unknown_ids_and_manual_items_are_skipped(tmp_path):
    root = _write(tmp_path, {"Dockerfile": "FROM node:22-alpine\nUSER root\n"})
    reports = {"hadolint": {"findings": [{"line": 2, "code": "DL3002", "message": "Last USER should not be root"}]}}
    (p,) = sf.plan(str(root), reports)
    result = sf.apply(str(root), reports, [p.id, "nope"])
    assert result["applied"] == [] and {s["id"] for s in result["skipped"]} == {p.id, "nope"}


def test_api_plan_apply_and_gitleaks_local_env(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from api.routes import deploy as route

    app = FastAPI()
    app.include_router(route.router)
    client = TestClient(app)
    root = _write(tmp_path, {"Dockerfile": "FROM node:22-alpine\nWORKDIR app\nCMD [\"node\", \"index.js\"]\n"})
    reports = {"hadolint": {"findings": [{"line": 2, "code": "DL3000", "message": "Use absolute WORKDIR"}]}}
    body = client.post("/api/deploy/security/fixes", json={"workspace_path": str(root), "reports": reports}).json()
    (p,) = body["proposals"]
    assert set(p) == {"id", "tool", "title", "detail", "files", "diff", "auto", "risk", "note", "rebuild"}
    assert client.post("/api/deploy/security/fixes/apply", json={"workspace_path": str(root), "reports": reports}).status_code == 400
    out = client.post("/api/deploy/security/fixes/apply", json={"workspace_path": str(root), "reports": reports, "ids": [p["id"]]}).json()
    assert out["applied"] == [p["id"]] and "WORKDIR /app" in (root / "Dockerfile").read_text(encoding="utf-8")
    missing = client.post("/api/deploy/security/fixes", json={"workspace_path": str(tmp_path / "nope"), "reports": {}})
    assert missing.status_code == 409

    _write(root, {".env": "KEY=x\n", ".gitignore": ".env\n", ".dockerignore": ".env\n", "a.js": "x"})
    report = {"status": "ok", "critical_count": 2, "findings": [
        {"severity": "CRITICAL", "file": "/repo/.env", "line": 1}, {"severity": "CRITICAL", "file": "/repo/a.js", "line": 1}]}
    filtered = route._without_local_env_keys(report, str(root))
    assert filtered["critical_count"] == 1 and [f["file"] for f in filtered["findings"]] == ["/repo/a.js"]
    assert len(filtered["local_env"]) == 1 and "PC 에만 있는 키" in filtered["summary"]


def test_nothing_is_silently_dropped(tmp_path):
    root = _write(tmp_path, {"Dockerfile": "FROM alpine:3.19\nRUN apk add --no-cache curl\n"})
    reports = {
        "hadolint": {"findings": [{"line": 2, "code": "DL3018", "message": "Pin versions in apk add."}]},
        **_trivy({"package": "golang.org/x/net", "installed": "0.17.0", "fixed": "0.23.0", "class": "lang-pkgs", "type": "gobinary",
                  "pkg_path": "usr/local/bin/app"}),
    }
    props = sf.plan(str(root), reports)
    lint = next(p for p in props if p.tool == "hadolint")
    assert not lint.auto and "DL3018" in lint.title and "취약점은 아닙니다" in lint.detail
    go = next(p for p in props if "golang.org/x/net" in p.title)
    assert not go.auto and "0.23.0" in go.title


def test_recoder_backups_are_local_when_ignored(tmp_path):
    root = _write(tmp_path, {".gitignore": ".env\n.recoder/\n", ".dockerignore": ".env\n.recoder\n", "Dockerfile": "FROM node:22-alpine\n",
                             ".recoder/backups/server.js.1": "const k = 'sk_live_x';\n"})
    assert sf.local_env_only(root, ".recoder/backups/server.js.1")
    assert not sf.local_env_only(root, "server.js")
    (root / ".gitignore").write_text(".env\n", encoding="utf-8")
    assert not sf.local_env_only(root, ".recoder/backups/server.js.1"), "커밋될 수 있으면 유출로 센다"


def test_cd_and_shell_form_fixes_keep_the_same_behaviour(tmp_path):
    """2026-10-04 실기기: ReCoder 가 넣은 `RUN cd frontend && …` 와 HEALTHCHECK 셸 형식이 "직접 확인" 으로만 떴다."""
    text = ("FROM node:22-alpine AS builder\nWORKDIR /app\nCOPY . .\n"
            "RUN cd frontend && if [ -f package-lock.json ]; then npm ci; else npm install; fi\n"
            "FROM node:22-alpine\nWORKDIR /srv\n"
            "HEALTHCHECK --interval=30s \\\n    CMD curl -f http://localhost:5000/api/health || exit 1\n"
            "CMD node server.js && echo done\n")
    root = _write(tmp_path, {"Dockerfile": text})
    reports = {"hadolint": {"findings": [{"line": 4, "code": "DL3003", "message": "Use WORKDIR"},
                                         {"line": 7, "code": "DL3025", "message": "JSON"},
                                         {"line": 9, "code": "DL3025", "message": "JSON"}]}}
    props = sf.plan(str(root), reports)
    assert all(p.auto for p in props), [p.title for p in props]
    sf.apply(str(root), reports, [p.id for p in props])
    out = (root / "Dockerfile").read_text(encoding="utf-8")
    assert "WORKDIR /app/frontend\nRUN if [ -f package-lock.json ]; then npm ci; else npm install; fi\nWORKDIR /app\n" in out
    assert '    CMD ["/bin/sh", "-c", "curl -f http://localhost:5000/api/health || exit 1"]' in out
    assert 'CMD ["/bin/sh", "-c", "node server.js && echo done"]' in out
    #: 헬스 경로·포트는 그대로 읽힌다(배포 점검이 같은 값을 본다).
    import build_readiness as br
    facts = br._dockerfile_facts(out)
    assert facts["health_port"] == 5000 and facts["health_path"] == "/api/health"


def test_shell_form_is_left_alone_when_the_dockerfile_changes_the_shell(tmp_path):
    root = _write(tmp_path, {"Dockerfile": 'FROM mcr.microsoft.com/powershell\nSHELL ["pwsh", "-c"]\nCMD Write-Host $env:X\n'})
    reports = {"hadolint": {"findings": [{"line": 3, "code": "DL3025", "message": "JSON"}]}}
    (p,) = sf.plan(str(root), reports)
    assert not p.auto


def test_advisory_lint_rules_do_not_turn_the_gate_red(tmp_path):
    from api.routes import deploy as d
    raw = {"violations": [{"line": 3, "code": "DL3018", "level": "warning", "message": "pin"},
                          {"line": 9, "code": "DL3025", "level": "warning", "message": "json"},
                          {"line": 2, "code": "DL3000", "level": "error", "message": "abs"}]}
    rep = d._hadolint_headline(d._normalise_scan_result("hadolint", "Dockerfile", {**raw, "success": True, "summary": "AI text"}))
    assert (rep["critical_count"], rep["high_count"], rep["medium_count"]) == (1, 1, 1)
    assert rep["summary"].startswith("Dockerfile 검사 — 고칠 것 2건") and "apk 버전 미고정(3번째 줄)" in rep["summary"]
    assert rep["ai_summary"] == "AI text"
    assert d._hadolint_headline({"findings": []})["summary"] == "Dockerfile 검사 — 규칙 위반 없음"


def _apply_lint(root: Path, findings: list[dict]) -> tuple[list, str]:
    reports = {"hadolint": {"findings": findings}}
    props = sf.plan(str(root), reports)
    auto = [p.id for p in props if p.auto]
    if auto:
        sf.apply(str(root), reports, auto)
    return props, (root / "Dockerfile").read_bytes().decode("utf-8")


def test_named_user_becomes_the_same_numeric_id(tmp_path):
    """DL3066: USER appuser → USER 1001 (같은 단계에서 만든 ID). Windows 줄바꿈은 그대로 둔다."""
    text = ("FROM node:22-alpine AS runtime\r\n"
            "RUN addgroup -g 1001 appgroup && \\\r\n"
            "    adduser -u 1001 -G appgroup -s /bin/sh -D appuser\r\n"
            "USER appuser\r\n"
            "CMD [\"node\", \"server.js\"]\r\n")
    root = _write(tmp_path, {"Dockerfile": text})
    props, out = _apply_lint(root, [{"code": "DL3066", "line": 4, "message": "Non-numeric user-id"}])
    assert [p.title for p in props] == ["USER 를 숫자 ID 로(같은 사용자) (DL3066)"]
    assert out == text.replace("USER appuser\r\n", "USER 1001\r\n")


def test_numeric_id_fix_covers_useradd_and_root(tmp_path):
    text = ("FROM python:3.11-slim\n"
            "RUN groupadd --gid 1001 appgroup && \\\n"
            "    useradd --uid 1001 --gid appgroup --shell /bin/sh --create-home appuser\n"
            "USER root\n"
            "RUN chown appuser /app\n"
            "USER appuser\n")
    root = _write(tmp_path, {"Dockerfile": text})
    _, out = _apply_lint(root, [{"code": "DL3066", "line": 4}, {"code": "DL3066", "line": 6}])
    assert out.splitlines()[3] == "USER 0" and out.splitlines()[5] == "USER 1001"


def test_numeric_id_is_left_to_the_user_when_unknown(tmp_path):
    # 베이스 이미지에 있던 사용자(ID 를 Dockerfile 이 모름), 다른 단계에서 만든 사용자, 나중에 ID 를 바꾸는 경우.
    for text, line in (
        ("FROM node:22-alpine\nUSER node\n", 2),
        ("FROM node:22-alpine AS a\nRUN adduser -u 1001 -D appuser\nFROM node:22-alpine\nUSER appuser\n", 4),
        ("FROM node:22-alpine\nRUN adduser -u 1001 -D appuser && usermod -u 2000 appuser\nUSER appuser\n", 3),
        ("FROM node:22-alpine\nRUN adduser -D appuser\nUSER appuser\n", 3),
    ):
        root = _write(tmp_path / str(line) / str(abs(hash(text))), {"Dockerfile": text})
        props, out = _apply_lint(root, [{"code": "DL3066", "line": line, "message": "Non-numeric user-id"}])
        assert out == text
        assert [p.auto for p in props] == [False] and "권고, 배포에 영향 없음" in props[0].title


def test_advisory_lists_match_the_gate():
    from api.routes.deploy import _HADOLINT_ADVISORY
    assert sf._ADVISORY_LINT == frozenset(_HADOLINT_ADVISORY)
