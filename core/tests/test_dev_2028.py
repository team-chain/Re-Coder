"""2.0.28 — AI 코드 보안 점수 · 배포 후 취약점 재검사."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import code_security as cs
import ops_alerts
import vuln_recheck as vr

CORE = Path(__file__).resolve().parents[1]


def _rules(files):
    return sorted({f.rule for f in cs.scan(files)[0]})


# ── 규칙별 양성·음성 ─────────────────────────────────────────────────────

def test_sql_injection_from_request_is_precise_and_parameterized_is_clean():
    bad = {"backend/src/routes/products.js": """
router.get('/search', async (req, res) => {
  const { q } = req.body;
  const rows = await pool.query(`SELECT * FROM products WHERE name LIKE '%${q}%'`);
  const more = await pool.query("SELECT * FROM products WHERE id = " + req.params.id);
  res.json(rows.rows);
});"""}
    found = [f for f in cs.scan(bad)[0] if f.rule == "SQL_INJECTION"]
    assert len(found) == 2 and all(f.precise and f.severity == "high" for f in found)
    assert any(i["severity"] == "error" for i in cs.issues(bad))
    good = {"backend/src/routes/products.js": """
router.get('/search', async (req, res) => {
  const { q } = req.body;
  const rows = await pool.query('SELECT * FROM products WHERE name ILIKE $1', [`%${q}%`]);
  const tagged = await prisma.$queryRaw`SELECT * FROM products WHERE id = ${q}`;
  res.json(rows.rows);
});"""}
    assert "SQL_INJECTION" not in _rules(good)
    dynamic = {"a.js": "const sets = keys.map((k, i) => `${k} = $${i + 1}`);\nawait pool.query(`UPDATE products SET ${sets.join(', ')} WHERE id = $${n}`, vals);"}
    f = [x for x in cs.scan(dynamic)[0] if x.rule == "SQL_INJECTION"][0]
    assert not f.precise and f.severity != "high"
    assert all(i["severity"] == "warning" for i in cs.issues(dynamic))
    unsafe = {"a.ts": "await prisma.$queryRawUnsafe(`SELECT * FROM t WHERE id = ${id}`);"}
    assert [x.precise for x in cs.scan(unsafe)[0]] == [True]


def test_jwt_rules():
    assert "JWT_UNSAFE" in _rules({"a.js": "jwt.verify(token, 'my-secret');"})
    assert "JWT_UNSAFE" in _rules({"a.js": "jwt.verify(token, secret, { algorithms: ['HS256', 'none'] });"})
    fallback = [f for f in cs.scan({"a.js": "jwt.sign(p, process.env.JWT_SECRET || 'dev');"})[0]]
    assert fallback and not fallback[0].precise
    assert "JWT_UNSAFE" not in _rules({"a.js": "jwt.verify(token, process.env.JWT_SECRET, { algorithms: ['HS256'] });"})


def test_cors_wildcard_with_credentials_only():
    assert _rules({"a.js": "app.use(cors({ origin: '*', credentials: true }));"}) == ["CORS_WILDCARD"]
    assert _rules({"a.js": "app.use(cors({ credentials: true, origin: true }));"}) == ["CORS_WILDCARD"]
    assert _rules({"a.js": "app.use(cors({ origin: process.env.CLIENT_URL, credentials: true }));"}) == []
    assert _rules({"a.js": "app.use(cors());"}) == []


def test_xss_and_error_leak():
    assert _rules({"a.jsx": "<div dangerouslySetInnerHTML={{ __html: post.body }} />"}) == ["XSS_HTML"]
    assert _rules({"a.jsx": "<div dangerouslySetInnerHTML={{ __html: DOMPurify.sanitize(post.body) }} />"}) == []
    leak = {"r.js": "router.get('/x', async (req, res) => { try {} catch (err) { res.status(500).json({ error: err.stack }); } });"}
    assert "ERROR_LEAK" in _rules(leak)
    ok = {"r.js": "router.get('/x', async (req, res) => { try {} catch (err) { res.status(500).json({ error: '오류' }); } });"}
    assert "ERROR_LEAK" not in _rules(ok)


AUTH_APP = {
    "backend/package.json": json.dumps({"dependencies": {"express": "4", "jsonwebtoken": "9"}}),
    "backend/src/routes/auth.js": """
router.post('/register', async (req, res) => {
  const { email, password } = req.body;
  await pool.query('INSERT INTO users (email, password) VALUES ($1, $2)', [email, password]);
  res.json({ ok: true });
});
router.post('/login', async (req, res) => { res.json({ token: jwt.sign({ id: 1 }, process.env.JWT_SECRET) }); });
""",
    "backend/src/routes/posts.js": """
router.post('/', requireAuth, async (req, res) => { res.json({}); });
router.delete('/:id', requireAuth, async (req, res) => { await pool.query('DELETE FROM posts WHERE id = $1', [req.params.id]); res.json({}); });
router.put('/:id', async (req, res) => { res.json({}); });
""",
}


def test_auth_owner_password_and_brute_force_rules():
    findings, applicable = cs.scan(AUTH_APP)
    rules = {f.rule for f in findings}
    assert {"MISSING_AUTH", "OWNER_CHECK", "PLAINTEXT_PASSWORD", "BRUTE_FORCE"} <= rules
    missing = [f for f in findings if f.rule == "MISSING_AUTH"]
    assert [f.message.split(" ")[1] for f in missing] == ["/:id"]           # 가입·로그인은 대상이 아니다
    pw = next(f for f in findings if f.rule == "PLAINTEXT_PASSWORD")
    assert pw.precise and pw.file == "backend/src/routes/auth.js"
    fixed = dict(AUTH_APP)
    fixed["backend/package.json"] = json.dumps({"dependencies": {"express": "4", "jsonwebtoken": "9", "bcryptjs": "2",
                                                                  "express-rate-limit": "7"}})
    fixed["backend/src/routes/posts.js"] = AUTH_APP["backend/src/routes/posts.js"].replace(
        "router.put('/:id', async", "router.put('/:id', requireAuth, async").replace(
        "DELETE FROM posts WHERE id = $1', [req.params.id]", "DELETE FROM posts WHERE id = $1 AND user_id = $2', [req.params.id, req.user.id]")
    assert _rules(fixed) == ["OWNER_CHECK"] or _rules(fixed) == []   # put 은 본문에서 주인 확인이 없으면 남는다
    assert "PLAINTEXT_PASSWORD" not in _rules(fixed) and "BRUTE_FORCE" not in _rules(fixed)


def test_router_mounted_with_auth_and_webhooks_are_not_flagged():
    files = {
        "backend/src/server.js": "import posts from './routes/posts.js';\nimport jwt from 'jsonwebtoken';\napp.use('/api/posts', authenticate, posts);",
        "backend/src/routes/posts.js": "router.post('/', async (req, res) => { res.json({ by: req.user.id }); });",
        "backend/src/routes/pay.js": "router.post('/', express.raw({type:'application/json'}), async (req, res) => { stripe.webhooks.constructEvent(req.body, req.headers['stripe-signature'], s); });",
    }
    assert "MISSING_AUTH" not in _rules(files)


def test_score_weights_and_no_score_without_rules():
    r = cs.report(AUTH_APP)
    assert 0 <= r["score"] < 100 and any(not x["passed"] for x in r["rules"])
    assert all(f["fix"] for f in r["findings"])
    assert cs.report({"index.html": "<h1>hi</h1>"})["score"] is None


def test_verified_commerce_starter_is_the_baseline_100():
    root = CORE / "starter_templates" / "commerce"
    files = {p.relative_to(root).as_posix(): p.read_text(encoding="utf-8", errors="replace") for p in root.rglob("*")
             if p.is_file() and "node_modules" not in p.parts and p.suffix in (".js", ".jsx", ".ts", ".tsx", ".json", ".mjs")}
    report = cs.report(files)
    assert report["score"] == 100, report["findings"]


def test_security_errors_join_the_generation_fix_loop():
    """code_agent 는 정확한 규칙의 심각한 발견만 교정 오류로 받는다."""
    errs = cs.issues({"a.js": "app.use(cors({ origin: '*', credentials: true }));\njwt.sign(p, process.env.S || 'x');"},
                     errors_only=True)
    assert [e["rule"] for e in errs] == ["CORS_WILDCARD"]
    assert "우회하지 말고" in errs[0]["fix"] and errs[0]["code"] == "GENERATED_SECURITY_ISSUE"
    src = (CORE / "code_agent.py").read_text(encoding="utf-8")
    assert "code_security.issues(code_security.ops_files(candidate_ops), errors_only=True)" in src
    assert '"security": security' in src


# ── 배포 후 취약점 재검사 ───────────────────────────────────────────────

def _sbom(tmp_path, comps):
    p = tmp_path / "sbom.json"
    p.write_text(json.dumps({"bomFormat": "CycloneDX", "components": comps}), encoding="utf-8")
    return str(p)


def _record(sbom, workspace="", status="succeeded"):
    return {"deployment_id": "d1", "status": status, "region": "us-east-1", "cluster": "c", "service": "shop",
            "sbom_path": sbom, "completed_at": "2026-10-14T00:00:00Z", "request": {"workspace_path": workspace}}


VULN = {"id": "GHSA-aaaa-bbbb-cccc", "summary": "Prototype pollution", "aliases": ["CVE-2026-1111"],
        "database_specific": {"severity": "HIGH"},
        "affected": [{"package": {"ecosystem": "npm", "name": "lodash"},
                      "ranges": [{"type": "SEMVER", "events": [{"introduced": "0"}, {"fixed": "4.17.21"}, {"introduced": "5.0.0"}, {"fixed": "5.0.1"}]}]}]}


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("RECODER_HOME", str(tmp_path / "home"))
    return tmp_path


def test_purls_only_language_packages(tmp_path):
    sbom = _sbom(tmp_path, [{"purl": "pkg:npm/lodash@4.17.15"}, {"purl": "pkg:npm/%40scope/x@1.0.0?arch=x"},
                            {"purl": "pkg:apk/alpine/musl@1.2.4"}, {"purl": "pkg:pypi/requests@2.0.0",
                                                                    "components": [{"purl": "pkg:npm/lodash@4.17.15"}]}])
    got = {(p["type"], p["name"], p["version"]) for p in vr.purls(sbom)}
    assert got == {("npm", "lodash", "4.17.15"), ("npm", "@scope/x", "1.0.0"), ("pypi", "requests", "2.0.0")}


def test_fixed_version_picks_the_nearest_fix():
    assert vr.fixed_version(VULN, {"type": "npm", "name": "lodash", "version": "4.17.15"}) == "4.17.21"
    assert vr.fixed_version(VULN, {"type": "npm", "name": "lodash", "version": "5.0.0"}) == "5.0.1"
    assert vr.fixed_version(VULN, {"type": "npm", "name": "other", "version": "1.0.0"}) == ""
    assert vr.severity(VULN) == "HIGH"


def test_recheck_alerts_only_new_vulns_and_records_fix_dates(home):
    tmp_path = home
    sbom = _sbom(tmp_path, [{"purl": "pkg:npm/lodash@4.17.15"}, {"purl": "pkg:npm/express@4.19.2"}])
    sent = []

    def post(url, body):
        sent.append(body)
        return {"results": [{"vulns": [{"id": VULN["id"]}]} if q["package"]["purl"].startswith("pkg:npm/lodash") else {}
                            for q in body["queries"]]}

    stored = []
    store = lambda a: stored.append(a) or a  # noqa: E731
    raised = vr.run([_record(sbom, workspace=str(tmp_path))], post=post, get=lambda url: VULN, store=store)
    assert len(raised) == 1 and raised[0]["kind"] == "vuln" and raised[0]["severity"] == "critical"
    assert "lodash 4.17.15 → 4.17.21" in raised[0]["detail"] and "CVE-2026-1111" in raised[0]["detail"]
    assert raised[0]["suggestions"][0]["value"] == {"ecosystem": "npm", "name": "lodash", "from": "4.17.15", "to": "4.17.21"}
    # 보낸 것은 패키지 식별자뿐
    assert set(json.dumps(sent)) and all(set(q) == {"package"} for q in sent[0]["queries"])
    # 두 번째 검사 — 같은 취약점은 다시 알리지 않는다
    assert vr.run([_record(sbom, workspace=str(tmp_path))], post=post, get=lambda url: VULN, store=store) == []
    # 다시 배포해 사라지면 고친 날을 남긴다
    sbom2 = _sbom(tmp_path, [{"purl": "pkg:npm/lodash@4.17.21"}])
    vr.run([_record(sbom2)], post=lambda u, b: {"results": [{}]}, get=lambda u: VULN, store=store)
    rec = next(iter(vr.history()["us-east-1/c/shop"]["known"].values()))
    assert rec["found_at"] and rec["notified_at"] and rec["fixed_at"]


def test_failed_or_sbomless_deployments_are_skipped(home):
    sbom = _sbom(home, [{"purl": "pkg:npm/lodash@4.17.15"}])
    assert vr.deployments([_record(sbom, status="failed"), _record("/nope.json")]) == []


def _workspace(tmp_path):
    ws = tmp_path / "ws"
    (ws / "backend").mkdir(parents=True)
    (ws / "backend" / "package.json").write_text(json.dumps({"dependencies": {"lodash": "^4.17.15", "express": "^4"}}), encoding="utf-8")
    (ws / "frontend").mkdir()
    (ws / "frontend" / "package.json").write_text(json.dumps({"dependencies": {"react": "^18"}}), encoding="utf-8")
    (ws / "frontend" / "package-lock.json").write_text("{}", encoding="utf-8")
    (ws / "requirements.txt").write_text("requests==2.0.0\nflask>=2\n", encoding="utf-8")
    return ws


def test_apply_fix_updates_direct_dependency_overrides_transitive_and_pins_python(home):
    ws = _workspace(home)
    alert = {"key": "us-east-1/c/shop", "workspace": str(ws)}
    out = vr.apply_fix(alert, {"ecosystem": "npm", "name": "lodash", "to": "4.17.21"}, npm_lock=False)
    assert out["applied"] and "backend/package.json" in out["message"]
    assert json.loads((ws / "backend" / "package.json").read_text())["dependencies"]["lodash"] == "^4.17.21"
    vr.apply_fix(alert, {"ecosystem": "npm", "name": "minimist", "to": "1.2.8"}, npm_lock=False)
    assert json.loads((ws / "frontend" / "package.json").read_text())["overrides"] == {"minimist": "^1.2.8"}
    vr.apply_fix(alert, {"ecosystem": "pypi", "name": "requests", "to": "2.32.3"})
    assert "requests>=2.32.3" in (ws / "requirements.txt").read_text()
    with pytest.raises(ValueError):
        vr.apply_fix({"workspace": ""}, {"ecosystem": "npm", "name": "lodash", "to": "1"})
    with pytest.raises(ValueError):
        vr.apply_fix(alert, {"ecosystem": "npm", "name": "lodash; rm -rf /", "to": "1"})


def test_ops_act_upgrade_only_what_was_offered(home):
    ws = _workspace(home)
    alert = ops_alerts._store({"key": "us-east-1/c/shop", "kind": "vuln", "region": "us-east-1", "cluster": "c",
                               "service": "shop", "workspace": str(ws),
                               "suggestions": [{"action": "upgrade", "value": {"ecosystem": "npm", "name": "lodash",
                                                                               "from": "4.17.15", "to": "4.17.21"}}]})
    never = lambda region: (_ for _ in ()).throw(AssertionError("AWS 를 부르면 안 된다"))  # noqa: E731
    with pytest.raises(ValueError):
        ops_alerts.act(alert["id"], "upgrade", {"ecosystem": "npm", "name": "express", "to": "9"}, approved=True,
                       session_factory=never)
    import vuln_recheck
    orig = vuln_recheck.apply_fix
    vuln_recheck.apply_fix = lambda a, v: orig(a, v, npm_lock=False)
    try:
        out = ops_alerts.act(alert["id"], "upgrade", {"ecosystem": "npm", "name": "lodash", "from": "4.17.15",
                                                     "to": "4.17.21"}, approved=True, session_factory=never)
    finally:
        vuln_recheck.apply_fix = orig
    assert out["applied"]
    assert next(a for a in ops_alerts.alerts() if a["id"] == alert["id"])["status"] == "applied"


def test_security_routes(home, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from api.routes import security_checks
    app = FastAPI()
    app.include_router(security_checks.router)
    c = TestClient(app)
    ws = _workspace(home)
    (ws / "backend" / "server.js").write_text("app.use(cors({ origin: '*', credentials: true }));", encoding="utf-8")
    (ws / "node_modules" / "x").mkdir(parents=True)
    (ws / "node_modules" / "x" / "index.js").write_text("app.use(cors({ origin: '*', credentials: true }));", encoding="utf-8")
    r = c.post("/api/security/code", json={"workspace_path": str(ws)}).json()
    assert [f["file"] for f in r["findings"]] == ["backend/server.js"]
    assert c.post("/api/security/code", json={"workspace_path": str(ws / "nope")}).status_code == 400
    monkeypatch.setattr(vuln_recheck_records := __import__("vuln_recheck"), "records_from_store", lambda: [])
    assert c.post("/api/security/vulns/check").json() == {"raised": 0}


def test_recheck_is_off_in_tests_and_when_disabled(monkeypatch):
    monkeypatch.setenv("RECODER_TEST_MODE", "1")
    assert not vr.enabled()
    monkeypatch.delenv("RECODER_TEST_MODE")
    monkeypatch.setenv("RECODER_VULN_RECHECK", "0")
    assert not vr.enabled()
