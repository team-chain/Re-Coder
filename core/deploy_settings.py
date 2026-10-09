"""로컬 Docker 배포에 필요한 설정값(환경변수) — 배포 전에 찾고, 채우고, 실행할 때만 넘긴다.

왜 필요한가 (2026-10-09 실기기)
    검증된 쇼핑몰 기반은 JWT_SECRET·STRIPE_SECRET_KEY·STRIPE_WEBHOOK_SECRET 가 없으면
    시작하자마자 종료한다(운영 안전장치). 로컬 Docker 배포는 PC 의 .env 와 DB 접속 정보만
    넘겼기 때문에 이미지를 다 빌드한 뒤에야 "앱이 시작하자마자 오류로 종료됨" 으로 실패했고,
    안내는 "auth.js 코드를 고치라" 였다(실제 해결은 설정값 입력).

동작
    - 앱 코드에서 "없으면 종료/throw" 하는 환경변수를 찾는다(required_env).
    - 앱 내부 서명 키(JWT_SECRET 등)는 로컬 배포용으로 무작위 생성한다.
    - 외부 서비스 키(Stripe 등)는 배포 화면에서 입력받는다. 앱이 모의 결제를 지원하면
      "결제 없이 로컬 데모" 로 키 없이 실행할 수 있다(모의 결제 서버를 함께 띄운다).
    - 값은 ~/.recoder/deploy_settings/<컨테이너>.json 에만 둔다(사용자 전용 권한).
      계획·배포 기록·이미지·프로젝트 파일에는 남기지 않고, docker run 할 때만 -e 로 넘긴다.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
from pathlib import Path
from typing import Callable, Iterable, Optional

_HOME_ENV = "RECODER_DEPLOY_SETTINGS_DIR"
_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.\-]{0,100}$")
_ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]{1,63}$")
_MAX_VALUE = 4096
_MAX_FILES = 600
_MAX_FILE_BYTES = 400_000
_SKIP_DIRS = {"node_modules", ".git", "dist", "build", ".next", "coverage", "__pycache__", ".venv", "venv",
              ".recoder", "public", "frontend", "client", "web", "docs", "test", "tests", "__tests__"}
_CODE_EXT = (".js", ".mjs", ".cjs", ".ts", ".py")

#: 컨테이너·배포 도구가 정하는 값 — 사용자에게 묻지 않는다.
_RUNTIME_OWNED = {"PORT", "HOST", "NODE_ENV", "PATH", "HOME", "HOSTNAME", "TZ"}
#: 외부 서비스 이름 — 이 이름이 들어간 키는 무작위로 만들면 동작하지 않는다(입력받아야 한다).
_EXTERNAL = re.compile(r"STRIPE|TOSS|KAKAO|NAVER|GOOGLE|GITHUB|AWS|OPENAI|ANTHROPIC|GEMINI|SLACK|DISCORD|TWILIO|"
                       r"SENDGRID|MAILGUN|SMTP|PAYPAL|IAMPORT|PORTONE|FIREBASE|SUPABASE|CLOUDINARY|S3_|SENTRY|"
                       r"API_KEY|ACCESS_KEY|CLIENT_ID|CLIENT_SECRET|WEBHOOK")
#: 앱이 스스로 서명·암호화에 쓰는 키 — 로컬 배포에서는 무작위로 만들어도 된다.
_INTERNAL_SECRET = re.compile(r"(?:^|_)(?:JWT|SESSION|COOKIE|AUTH|APP|TOKEN|CSRF|ENCRYPTION|SIGNING|HMAC|NEXTAUTH|"
                              r"REFRESH|ACCESS_TOKEN|SECRET_KEY_BASE)(?:_|$).*(?:SECRET|KEY|SALT)$|^SECRET(?:_KEY)?$")

#: 외부 키 입력 안내(이름 → 설명). 모르는 키는 일반 안내를 쓴다.
_HINTS: dict[str, tuple[str, str]] = {
    "STRIPE_SECRET_KEY": ("Stripe 비밀 키", "Stripe 대시보드 → 개발자 → API 키의 테스트 비밀 키(sk_test_…)"),
    "STRIPE_WEBHOOK_SECRET": ("Stripe 웹훅 서명 키", "Stripe 대시보드 → 웹훅 또는 `stripe listen` 이 알려 주는 whsec_… 값"),
    "STRIPE_PUBLIC_KEY": ("Stripe 공개 키", "브라우저용 공개 키(pk_test_…)"),
    "STRIPE_PUBLISHABLE_KEY": ("Stripe 공개 키", "브라우저용 공개 키(pk_test_…)"),
}

# ── 모의 결제(로컬 데모) ────────────────────────────────────────────────────────
#: 앱이 아래 환경변수를 모두 읽으면 "모의 결제 서버로 실행" 을 지원한다고 본다(검증된 쇼핑몰 기반).
_DEMO_MARKERS = ("PAYMENT_MODE", "STRIPE_MOCK_HOST", "STRIPE_MOCK_PORT")
_MOCK_PORT = 19090
_DEMO_INIT_SCRIPTS = ("backend/init-db.js", "server/init-db.js", "init-db.js")

#: 앱 이미지의 node 로 띄우는 모의 결제 서버(외부 이미지를 내려받지 않는다).
#: 결제 의도를 만들면 잠시 뒤 서명한 결제 완료 웹훅을 앱에 보내 주문이 "결제 완료(테스트)" 가 된다.
MOCK_PAYMENT_JS = r"""
const http=require('http'),crypto=require('crypto');
const intents={},keys={};
const PORT=+process.env.MOCK_PORT||19090,HOOK=process.env.MOCK_WEBHOOK_URL||'',SECRET=process.env.MOCK_WEBHOOK_SECRET||'';
function reply(res,status,body){const d=Buffer.from(JSON.stringify(body));res.writeHead(status,{'Content-Type':'application/json','Content-Length':d.length});res.end(d);}
function hook(intent){if(!HOOK||!SECRET)return;const ev={id:'evt_demo_'+crypto.randomBytes(8).toString('hex'),type:'payment_intent.succeeded',object:'event',data:{object:intent}};
 const body=JSON.stringify(ev),t=Math.floor(Date.now()/1000),sig=crypto.createHmac('sha256',SECRET).update(t+'.'+body).digest('hex');
 const u=new URL(HOOK);const r=http.request({hostname:u.hostname,port:u.port||80,path:u.pathname,method:'POST',headers:{'Content-Type':'application/json','Content-Length':Buffer.byteLength(body),'Stripe-Signature':'t='+t+',v1='+sig}},x=>x.resume());
 r.on('error',()=>setTimeout(()=>hook(intent),3000));r.end(body);}
http.createServer((req,res)=>{let raw='';req.on('data',c=>raw+=c);req.on('end',()=>{
 if(req.method==='GET'&&req.url==='/health')return reply(res,200,{status:'ok',provider:'mock'});
 if(req.method==='GET'&&req.url.startsWith('/v1/payment_intents/'))return reply(res,200,intents[req.url.split('/').pop()]||{});
 if(req.method!=='POST')return reply(res,404,{});
 const form=new URLSearchParams(raw);
 if(req.url==='/v1/payment_intents'){const k=req.headers['idempotency-key'];if(k&&keys[k])return reply(res,200,intents[keys[k]]);
  const id='pi_demo_'+crypto.randomBytes(8).toString('hex'),meta={};for(const [a,b] of form)if(a.startsWith('metadata['))meta[a.slice(9,-1)]=b;
  const it={id,object:'payment_intent',amount:parseInt(form.get('amount')||'0',10),currency:form.get('currency')||'usd',status:'requires_payment_method',client_secret:id+'_secret_demo',metadata:meta};
  intents[id]=it;if(k)keys[k]=id;reply(res,200,it);
  setTimeout(()=>{if(intents[id].status==='canceled')return;intents[id]={...intents[id],status:'succeeded'};hook(intents[id]);},2500);return;}
 const parts=req.url.split('/'),id=parts[3]||'';
 if(intents[id]&&req.url.endsWith('/cancel')){intents[id].status='canceled';return reply(res,200,intents[id]);}
 reply(res,404,{error:{message:'Unsupported mock endpoint'}});});}).listen(PORT,'0.0.0.0');
""".strip()


def _home() -> Path:
    override = os.environ.get(_HOME_ENV)
    return Path(override) if override else Path.home() / ".recoder" / "deploy_settings"


def _file(container: str) -> Path:
    return _home() / f"{container}.json"


def load(container: str) -> dict:
    if not _NAME_RE.match(container or ""):
        return {}
    try:
        data = json.loads(_file(container).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(container: str, data: dict) -> None:
    if not _NAME_RE.match(container or ""):
        return
    path = _file(container)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


# ── 앱이 요구하는 설정 찾기 ─────────────────────────────────────────────────────

def _code_files(workspace: str) -> Iterable[Path]:
    root = Path(workspace)
    count = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
        for name in filenames:
            if not name.endswith(_CODE_EXT) or name.endswith((".test.js", ".spec.js", ".d.ts")):
                continue
            path = Path(dirpath) / name
            try:
                if path.stat().st_size > _MAX_FILE_BYTES:
                    continue
            except OSError:
                continue
            count += 1
            if count > _MAX_FILES:
                return
            yield path


_JS_GUARD = re.compile(r"!\s*process\.env\.([A-Z_][A-Z0-9_]*)\b|process\.env\.([A-Z_][A-Z0-9_]*)\s*(?:===?|==)\s*undefined")
_JS_ALIAS = re.compile(r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*process\.env\.([A-Z_][A-Z0-9_]*)\s*;")
_JS_LEN = re.compile(r"process\.env\.([A-Z_][A-Z0-9_]*)\.length\s*<\s*(\d{1,3})")
_FATAL = re.compile(r"\bthrow\b|process\.exit\(\s*[1-9]|\braise\b|sys\.exit\(\s*[1-9]")
_PY_INDEX = re.compile(r"os\.environ\[\s*['\"]([A-Z_][A-Z0-9_]*)['\"]\s*\]")
_PY_GUARD = re.compile(r"if\s+not\s+os\.(?:getenv|environ\.get)\(\s*['\"]([A-Z_][A-Z0-9_]*)['\"]")


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" \t"))


def _conditional(lines: list[str], i: int) -> bool:
    """검사 줄이 다른 if 안에 있으면(예: 모의 결제 모드일 때만 필요) 늘 필요한 값이 아니다."""
    depth = _indent(lines[i])
    if depth == 0:
        return False
    for back in range(i - 1, max(-1, i - 40), -1):
        text = lines[back].strip()
        if not text or text.startswith(("//", "#", "*")):
            continue
        if _indent(lines[back]) < depth:
            return bool(re.match(r"(?:\}\s*)?(?:if|else|elif)\b", text))
    return False


def required_env(workspace: str) -> dict[str, int]:
    """앱이 시작할 때 없으면 종료(throw·exit·raise)하는 환경변수 → 최소 길이(모르면 0)."""
    found: dict[str, int] = {}
    if not workspace or not Path(workspace).is_dir():
        return found
    for path in _code_files(workspace):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if "env" not in text:
            continue
        lines = text.splitlines()
        aliases = {m.group(1): m.group(2) for m in _JS_ALIAS.finditer(text)}
        for i, line in enumerate(lines):
            window = "\n".join(lines[i:i + 4])
            names = [a or b for a, b in _JS_GUARD.findall(line)]
            for alias, env in aliases.items():
                if re.search(rf"!\s*{re.escape(alias)}\b|\b{re.escape(alias)}\s*===?\s*undefined", line):
                    names.append(env)
            py = _PY_GUARD.findall(line)
            if (names or py) and re.search(r"\bif\b", line) and _FATAL.search(window) and not _conditional(lines, i):
                for name in names + py:
                    found.setdefault(name, 0)
            for name in _PY_INDEX.findall(line):
                found.setdefault(name, 0)
            lengths = list(_JS_LEN.findall(line))
            for alias, n in re.findall(r"\b([A-Za-z_$][\w$]*)\.length\s*<\s*(\d{1,3})", line):
                if alias in aliases:
                    lengths.append((aliases[alias], n))
            for name, n in lengths:
                if name in found or _FATAL.search(window):
                    found[name] = max(found.get(name, 0), int(n))
    return {k: v for k, v in found.items() if _ENV_NAME.match(k) and k not in _RUNTIME_OWNED}


def all_env_names(workspace: str) -> set[str]:
    names: set[str] = set()
    for path in _code_files(workspace) if workspace and Path(workspace).is_dir() else []:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        names.update(re.findall(r"process\.env\.([A-Z_][A-Z0-9_]*)", text))
        names.update(re.findall(r"os\.(?:getenv|environ\.get)\(\s*['\"]([A-Z_][A-Z0-9_]*)['\"]", text))
        names.update(_PY_INDEX.findall(text))
    return names


def kind_of(name: str) -> str:
    """generate(앱 내부 서명 키 — 무작위 생성) | input(외부 서비스 값 — 입력)."""
    upper = name.upper()
    if not _EXTERNAL.search(upper) and _INTERNAL_SECRET.search(upper):
        return "generate"
    return "input"


def demo_supported(workspace: str, names: Optional[set[str]] = None) -> bool:
    names = names if names is not None else all_env_names(workspace)
    return all(m in names for m in _DEMO_MARKERS) and (Path(workspace) / "package.json").is_file()


def _demo_secret(data: dict) -> str:
    value = data.get("demo_webhook_secret")
    if not isinstance(value, str) or not value:
        value = "whsec_local_demo_" + secrets.token_hex(16)
        data["demo_webhook_secret"] = value
    return value


def mock_container(container: str) -> str:
    return f"{container}-payment-mock"


def demo_env(container: str, data: dict, app_port: int) -> dict[str, str]:
    """결제 없이 로컬 데모로 실행할 때 앱에 넘기는 값(모의 결제 서버 주소 포함)."""
    return {
        "NODE_ENV": "test", "PAYMENT_MODE": "mock",
        "STRIPE_SECRET_KEY": "sk_test_local_demo_only", "STRIPE_WEBHOOK_SECRET": _demo_secret(data),
        "STRIPE_MOCK_HOST": mock_container(container), "STRIPE_MOCK_PORT": str(_MOCK_PORT),
        "SEED_DEMO": "true",
    }


#: 데모 모드가 대신 채우는 이름 — 이 이름은 입력받지 않는다.
_DEMO_COVERS = {"STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET", "PAYMENT_MODE", "STRIPE_MOCK_HOST", "STRIPE_MOCK_PORT"}


def evaluate(container: str, workspace: str, provided: Iterable[str], *, create: bool = True) -> dict:
    """화면에 보일 설정 목록. 값은 담지 않는다.

    반환: {"settings": [{name, label, hint, status, source, secret}], "missing": [...],
           "demo": {"available", "enabled", "label", "note"}}
    create=True 면 앱 내부 서명 키를 이 컨테이너용으로 한 번 만들어 보관한다.
    """
    provided = set(provided)
    data = load(container)
    values = data.get("values") if isinstance(data.get("values"), dict) else {}
    generated = data.get("generated") if isinstance(data.get("generated"), dict) else {}
    required = required_env(workspace)
    names = all_env_names(workspace)
    demo_ok = demo_supported(workspace, names)
    demo_on = bool(data.get("demo")) and demo_ok
    changed = False
    settings: list[dict] = []
    missing: list[str] = []
    for name in sorted(required):
        min_len = required[name]
        label, hint = _HINTS.get(name, (name, ""))
        row = {"name": name, "label": label, "hint": hint, "secret": True, "min_length": min_len}
        if name in provided:
            row.update(status="ready", source="provided")
        elif demo_on and name in _DEMO_COVERS:
            row.update(status="ready", source="demo")
        elif isinstance(values.get(name), str) and values[name] and len(values[name]) >= min_len:
            row.update(status="ready", source="saved")
        elif kind_of(name) == "generate":
            value = generated.get(name)
            if not (isinstance(value, str) and len(value) >= max(min_len, 32)):
                if create:
                    generated[name] = secrets.token_hex(max(32, (min_len + 1) // 2))
                    changed = True
                    row.update(status="ready", source="generated")
                else:
                    row.update(status="missing", source="")
                    missing.append(name)
            else:
                row.update(status="ready", source="generated")
        else:
            row.update(status="missing", source="",
                       hint=hint or "이 앱이 시작할 때 요구하는 값입니다. 프로젝트의 .env.example·README 를 참고하세요.")
            missing.append(name)
        settings.append(row)
    if changed:
        data["generated"] = generated
        _save(container, data)
    demo = {
        "available": demo_ok,
        "enabled": demo_on,
        "label": "키 없이 결제를 끈 로컬 데모로 실행",
        "note": ("모의 결제 서버를 함께 띄웁니다. 상품·장바구니·주문을 써 볼 수 있고 주문은 잠시 뒤 '결제 완료(테스트)' 가 됩니다. "
                 "실제 카드 결제·정산은 일어나지 않으며, 운영 배포에는 쓰지 않습니다.") if demo_ok else "",
    }
    return {"settings": settings, "missing": missing, "demo": demo}


def save_values(container: str, values: dict[str, str]) -> None:
    """입력받은 값을 보관한다. 빈 값은 지운다. 이름·길이·줄바꿈을 검사한다."""
    if not _NAME_RE.match(container or ""):
        raise ValueError("컨테이너 이름이 올바르지 않습니다.")
    data = load(container)
    stored = data.get("values") if isinstance(data.get("values"), dict) else {}
    for name, value in (values or {}).items():
        if not isinstance(name, str) or not _ENV_NAME.match(name):
            raise ValueError(f"설정 이름이 올바르지 않습니다: {name!r}")
        text = "" if value is None else str(value).strip()
        if len(text) > _MAX_VALUE or "\n" in text or "\r" in text or "\x00" in text:
            raise ValueError(f"{name} 값은 한 줄, {_MAX_VALUE}자 이하로 넣어 주세요.")
        if text:
            stored[name] = text
        else:
            stored.pop(name, None)
    data["values"] = stored
    _save(container, data)


def set_demo(container: str, enabled: bool) -> None:
    if not _NAME_RE.match(container or ""):
        raise ValueError("컨테이너 이름이 올바르지 않습니다.")
    data = load(container)
    data["demo"] = bool(enabled)
    if enabled:
        _demo_secret(data)
    _save(container, data)


def demo_enabled(container: str, workspace: str) -> bool:
    return bool(load(container).get("demo")) and demo_supported(workspace)


def runtime_env(container: str, workspace: str, provided: Iterable[str], app_port: int = 3001) -> dict[str, str]:
    """docker run 할 때 넘길 값. PC .env·DB 가 이미 준 이름은 건드리지 않는다(데모가 대신 채우는 이름은 예외)."""
    provided = set(provided)
    data = load(container)
    out: dict[str, str] = {}
    demo = bool(data.get("demo")) and (not workspace or demo_supported(workspace))
    if demo:
        out.update(demo_env(container, data, app_port))
        _save(container, data)
    for source in ("generated", "values"):
        stored = data.get(source) if isinstance(data.get(source), dict) else {}
        for name, value in stored.items():
            if not (_ENV_NAME.match(str(name)) and isinstance(value, str) and value):
                continue
            if name in provided or name in out:
                continue
            out[name] = value
    return out


def env_args(env: dict[str, str]) -> list[str]:
    args: list[str] = []
    for key, value in env.items():
        args.extend(["-e", f"{key}={value}"])
    return args


Runner = Callable[[list[str], int], subprocess.CompletedProcess]


def _run(args: list[str], timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(args, shell=False, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def ensure_demo(container: str, image: str, app_port: int, progress: Optional[Callable[[str], None]] = None,
                run: Runner = _run) -> list[str]:
    """모의 결제 서버를 앱 이미지의 node 로 띄운다. 앱 docker run 에 붙일 네트워크 인자를 돌려준다.

    실패하면 RuntimeError(사람이 읽을 원인).
    """
    import local_services
    data = load(container)
    secret = _demo_secret(data)
    _save(container, data)
    net = local_services.network_name(container)
    if run(["docker", "network", "inspect", net], 30).returncode != 0:
        created = run(["docker", "network", "create", net], 30)
        if created.returncode != 0 and "already exists" not in (created.stderr or ""):
            raise RuntimeError(f"Docker 네트워크 {net} 를 만들지 못했습니다: {(created.stderr or '').strip()[:200]}")
    name = mock_container(container)
    if progress:
        progress("결제 없이 쓰는 모의 결제 서버를 준비합니다")
    run(["docker", "rm", "-f", name], 60)
    started = run(["docker", "run", "-d", "--name", name, "--network", net, "--network-alias", name,
                   "--restart", "unless-stopped", "--no-healthcheck",
                   "-e", f"MOCK_PORT={_MOCK_PORT}",
                   "-e", f"MOCK_WEBHOOK_URL=http://{container}:{int(app_port)}/api/webhooks/stripe",
                   "-e", f"MOCK_WEBHOOK_SECRET={secret}",
                   "--entrypoint", "node", image, "-e", MOCK_PAYMENT_JS], 120)
    if started.returncode != 0:
        raise RuntimeError(f"모의 결제 서버를 시작하지 못했습니다: {(started.stderr or '').strip()[:300]}")
    return ["--network", net]


def remove_demo(container: str, run: Runner = _run) -> None:
    """실제 키로 바꾸면 남아 있던 모의 결제 서버를 내린다."""
    if _NAME_RE.match(container or ""):
        run(["docker", "rm", "-f", mock_container(container)], 60)


def demo_init_script(workspace: str) -> Optional[str]:
    """데모 상품을 넣는 초기화 스크립트(프로젝트에 있을 때만)."""
    for rel in _DEMO_INIT_SCRIPTS:
        if (Path(workspace) / rel).is_file():
            return rel
    return None


def run_demo_init(image: str, script: str, args: list[str], run: Runner = _run) -> tuple[bool, str]:
    """앱 이미지로 초기화 스크립트를 한 번 실행한다(데모 상품). 실패해도 배포는 계속한다."""
    done = run(["docker", "run", "--rm", *args, "--entrypoint", "node", image, script], 180)
    return done.returncode == 0, ((done.stderr or "") + (done.stdout or "")).strip()[-400:]
