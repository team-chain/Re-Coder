"""
ReCoder Core — AI 가 만든 코드의 보안 점검과 점수 (2.0.28)

AI 생성 코드는 동작은 하지만 보안 실수가 잦다(SQL 을 문자열로 이어 붙이기, 비밀번호 평문 저장, 로그인 무차별 대입 허용 …).
이 모듈은 AI 를 부르지 않고 정규식·간단한 구조 분석으로 생성 앱(Express·pg·Prisma·React 위주)을 본다.

  · 정확도가 확인된 규칙(precise)의 심각한 발견은 생성 교정 루프에 오류(GENERATED_SECURITY_ISSUE)로 넣어 AI 가 고치게 한다.
    "검사를 끄거나 우회하지 말고 안전한 방식으로" 고치라고 지시한다.
  · 나머지는 경고로만 보여 준다(오탐이 있을 수 있음).
  · 점수 = 적용되는 규칙 중 통과한 규칙의 가중치 합 / 전체 가중치(심각 3 · 보통 2 · 낮음 1).

검증된 쇼핑몰 기반(starter_templates/commerce)은 100점이어야 한다 — 그것이 기준선이다(테스트로 고정).
"""
from __future__ import annotations

import json
import posixpath
import re
from dataclasses import asdict, dataclass, field
from typing import Iterable, Optional

_CODE = (".js", ".ts", ".mjs", ".cjs", ".jsx", ".tsx")
_SKIP = re.compile(r"(^|/)(node_modules|dist|build|coverage|\.next|vendor)/|\.(test|spec)\.[jt]sx?$|(^|/)(tests?|__tests__)/")
WEIGHT = {"high": 3, "medium": 2, "low": 1}

#: 규칙 — (코드, 이름, 심각도, 교정 지시)
RULES: dict[str, dict] = {
    "SQL_INJECTION": {"label": "SQL 인젝션", "severity": "high",
                      "fix": "사용자 입력을 SQL 문자열에 넣지 말고 자리표시자($1, ?)와 값 배열로 넘기세요(Prisma 는 $queryRaw 태그 템플릿)."},
    "MISSING_AUTH": {"label": "인증 빠진 변경 API", "severity": "medium",
                     "fix": "데이터를 바꾸는 API(POST·PUT·PATCH·DELETE)에 로그인 확인 미들웨어를 붙이세요(가입·로그인·웹훅 제외)."},
    "OWNER_CHECK": {"label": "소유자 확인 누락", "severity": "medium",
                    "fix": "/:id 로 남의 데이터를 바꾸지 못하게 로그인한 사용자(req.user.id)와 데이터의 주인을 비교하세요."},
    "JWT_UNSAFE": {"label": "위험한 JWT 설정", "severity": "high",
                   "fix": "JWT 비밀값을 코드에 적지 말고 환경변수(JWT_SECRET)로 읽고, 'none' 알고리즘을 허용하지 마세요."},
    "PLAINTEXT_PASSWORD": {"label": "비밀번호 평문 저장", "severity": "high",
                           "fix": "비밀번호는 bcrypt(또는 argon2)로 해시해 저장하고, 로그인 때 bcrypt.compare 로 비교하세요."},
    "CORS_WILDCARD": {"label": "CORS 과다 허용", "severity": "high",
                      "fix": "쿠키·인증을 함께 쓰는 CORS 는 origin 을 '*' 나 true 로 두지 말고 화면 주소(환경변수)만 허용하세요."},
    "XSS_HTML": {"label": "XSS(HTML 그대로 넣기)", "severity": "medium",
                 "fix": "dangerouslySetInnerHTML 에 사용자 입력을 넣지 말고 텍스트로 렌더하거나 DOMPurify.sanitize 로 거르세요."},
    "BRUTE_FORCE": {"label": "로그인 무차별 대입", "severity": "medium",
                    "fix": "로그인·가입 API 에 시도 횟수 제한(express-rate-limit 등)을 붙이세요."},
    "ERROR_LEAK": {"label": "오류 내부 정보 노출", "severity": "low",
                   "fix": "응답에 err.stack·오류 객체를 그대로 보내지 말고, 서버 로그에만 남기고 사용자에게는 짧은 문장을 보내세요."},
}

_HASH_LIBS = ("bcrypt", "bcryptjs", "argon2", "@node-rs/argon2", "@node-rs/bcrypt", "scrypt-js")
_RATE_LIBS = ("express-rate-limit", "rate-limiter-flexible", "express-slow-down", "express-brute", "@nestjs/throttler")
_PUBLIC_PATH = re.compile(r"(?i)(login|log-in|signin|sign-in|signup|sign-up|register|auth|token|refresh|logout|password|reset|verify|"
                          r"webhook|health|contact|subscribe|newsletter|checkout/session|callback|oauth)")
_LOGIN_PATH = re.compile(r"(?i)(login|log-in|signin|sign-in|/auth/?$|/session)")
_SIGNUP_PATH = re.compile(r"(?i)(signup|sign-up|register|join)")


@dataclass
class Finding:
    rule: str
    file: str
    line: int
    message: str
    severity: str
    precise: bool = False

    @property
    def label(self) -> str:
        return RULES[self.rule]["label"]


def _line(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def _strip_comments(text: str) -> str:
    """주석을 같은 길이의 공백으로(줄 번호를 지키려고)."""
    def blank(m: re.Match) -> str:
        return re.sub(r"[^\n]", " ", m.group(0))
    text = re.sub(r"/\*.*?\*/", blank, text, flags=re.S)
    return re.sub(r"(?m)(?<![:\\'\"`])//[^\n]*", blank, text)


def code_files(files: dict[str, str]) -> dict[str, str]:
    return {p.replace("\\", "/"): c for p, c in files.items()
            if p.endswith(_CODE) and not p.endswith(".d.ts") and not _SKIP.search(p.replace("\\", "/")) and isinstance(c, str)}


def _deps(files: dict[str, str]) -> set[str]:
    out: set[str] = set()
    for path, text in files.items():
        if posixpath.basename(path.replace("\\", "/")) != "package.json" or "node_modules" in path:
            continue
        try:
            data = json.loads(text or "{}")
        except ValueError:
            continue
        for key in ("dependencies", "devDependencies"):
            if isinstance(data.get(key), dict):
                out |= set(data[key])
    return out


def _template_at(text: str, start: int) -> Optional[str]:
    """text[start] 가 ` 이면 짝 맞는 템플릿 문자열 전체."""
    if start >= len(text) or text[start] != "`":
        return None
    i, depth = start + 1, 0
    while i < len(text):
        c = text[i]
        if c == "\\":
            i += 2
            continue
        if c == "$" and text[i + 1:i + 2] == "{":
            depth += 1
            i += 2
            continue
        if c == "}" and depth:
            depth -= 1
        elif c == "`" and not depth:
            return text[start:i + 1]
        i += 1
    return None


_REQ_NAMES = re.compile(r"(?:const|let|var)\s*\{([^}]*)\}\s*=\s*req\.(?:body|query|params)")
_REQ_ONE = re.compile(r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*req\.(?:body|query|params)\b")
_SQL_CALL = re.compile(r"\.(query|execute|raw|\$queryRawUnsafe|\$executeRawUnsafe|unsafe)\(\s*")
_SQL_WORD = re.compile(r"(?i)\b(select|insert|update|delete|where|from|values|order\s+by)\b")


def _request_names(text: str) -> set[str]:
    names: set[str] = set()
    for m in _REQ_NAMES.finditer(text):
        for part in m.group(1).split(","):
            part = part.strip()
            if not part or part.startswith("..."):
                continue
            alias = part.split(":")[-1].split("=")[0].strip()
            if re.fullmatch(r"[A-Za-z_$][\w$]*", alias):
                names.add(alias)
    names |= set(_REQ_ONE.findall(text))
    return names


def _sql(path: str, text: str) -> list[Finding]:
    out: list[Finding] = []
    user = _request_names(text)
    for m in _SQL_CALL.finditer(text):
        arg = m.end()
        template = _template_at(text, arg)
        exprs: list[str] = []
        concat = False
        if template is not None:
            if not _SQL_WORD.search(template):
                continue
            exprs = re.findall(r"\$\{([^}]*)\}", template)
            if not exprs:
                continue
        else:
            head = text[arg:arg + 400]
            cm = re.match(r"""(['"])((?:\\.|(?!\1).)*)\1\s*\+\s*([^,;)]+)""", head)
            if not cm or not _SQL_WORD.search(cm.group(2)):
                continue
            exprs, concat = [cm.group(3)], True
        if m.group(1) in ("$queryRawUnsafe", "$executeRawUnsafe", "unsafe"):
            tainted = True
        else:
            tainted = any("req." in e or any(re.search(rf"(?<![\w$.]){re.escape(n)}(?![\w$])", e) for n in user) for e in exprs)
        placeholder_only = all(re.fullmatch(r"\s*[\w$.]+\s*", e) and not tainted for e in exprs) and not concat
        out.append(Finding(
            "SQL_INJECTION", path, _line(text, m.start()),
            ("사용자 입력이 SQL 문자열에 그대로 들어갑니다" if tainted else
             "SQL 을 문자열로 이어 만듭니다 — 넣는 값이 사용자 입력이 아닌지 확인하세요") + f" ({m.group(1)})",
            "high" if tainted else ("low" if placeholder_only else "medium"), precise=tainted))
    return out


_JWT_NONE = re.compile(r"""algorithms?\s*:\s*(\[[^\]]*['"]none['"][^\]]*\]|['"]none['"])""", re.I)
_JWT_CALL = re.compile(r"\bjwt\.(sign|verify)\(")


def _jwt(path: str, text: str) -> list[Finding]:
    out: list[Finding] = []
    for m in _JWT_NONE.finditer(text):
        out.append(Finding("JWT_UNSAFE", path, _line(text, m.start()), "JWT 에 'none' 알고리즘을 허용합니다 — 서명 없는 토큰이 통과합니다.",
                           "high", precise=True))
    for m in _JWT_CALL.finditer(text):
        end = _paren_end(text, m.end() - 1)
        args = _split_args(text[m.end():end])
        if len(args) < 2:
            continue
        secret = args[1].strip()
        if re.fullmatch(r"""(['"`])[^'"`]{1,200}\1""", secret):
            out.append(Finding("JWT_UNSAFE", path, _line(text, m.start()), "JWT 비밀값이 코드에 적혀 있습니다 — 누구나 토큰을 만들 수 있습니다.",
                               "high", precise=True))
        elif re.search(r"""\|\|\s*(['"`])[^'"`]+\1""", secret) or re.search(r"""\?\?\s*(['"`])[^'"`]+\1""", secret):
            out.append(Finding("JWT_UNSAFE", path, _line(text, m.start()),
                               "환경변수가 없으면 코드에 적힌 기본 비밀값을 씁니다 — 비밀값이 없으면 시작하지 않게 하세요.", "medium"))
    return out


def _paren_end(text: str, open_pos: int) -> int:
    depth, i, quote = 0, open_pos, ""
    while i < len(text):
        c = text[i]
        if quote:
            if c == "\\":
                i += 2
                continue
            if c == quote:
                quote = ""
        elif c in "'\"`":
            quote = c
        elif c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return len(text)


def _split_args(inner: str) -> list[str]:
    args, depth, cur, quote = [], 0, "", ""
    for c in inner:
        if quote:
            cur += c
            if c == quote:
                quote = ""
            continue
        if c in "'\"`":
            quote = c
        elif c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        if c == "," and depth == 0:
            args.append(cur)
            cur = ""
            continue
        cur += c
    if cur.strip():
        args.append(cur)
    return args


def _cors(path: str, text: str) -> list[Finding]:
    out: list[Finding] = []
    for m in re.finditer(r"\bcors\(\s*\{", text):
        end = _paren_end(text, m.end() - 1)
        obj = text[m.end() - 1:end + 1]
        wide = re.search(r"""\borigin\s*:\s*(['"]\*['"]|true\b)""", obj)
        creds = re.search(r"\bcredentials\s*:\s*true\b", obj)
        if wide and creds:
            out.append(Finding("CORS_WILDCARD", path, _line(text, m.start()),
                               "모든 사이트에 쿠키·인증 요청을 허용합니다(origin '*'/true + credentials).", "high", precise=True))
    return out


def _xss(path: str, text: str) -> list[Finding]:
    out: list[Finding] = []
    for m in re.finditer(r"dangerouslySetInnerHTML\s*=\s*\{\{\s*__html\s*:\s*([^}]+)\}\}", text):
        value = m.group(1).strip()
        if re.fullmatch(r"""(['"`])[^'"`$]*\1""", value) or re.search(r"(?i)sanitize|purify|escape", value):
            continue
        out.append(Finding("XSS_HTML", path, _line(text, m.start()), "HTML 을 걸러내지 않고 그대로 화면에 넣습니다.", "medium"))
    return out


def _errors(path: str, text: str) -> list[Finding]:
    out: list[Finding] = []
    for m in re.finditer(r"res\.(?:status\(\s*\d{3}\s*\)\s*\.)?(?:json|send)\(\s*([^;\n]{0,160})", text):
        arg = m.group(1)
        if re.search(r"\b(err|error|e|ex|exception)\.stack\b", arg) or re.fullmatch(r"\s*(err|error|e|ex)\s*\)?\s*", arg):
            out.append(Finding("ERROR_LEAK", path, _line(text, m.start()), "오류 내부 정보(스택·오류 객체)를 그대로 응답에 보냅니다.", "low"))
    return out


def _routes(files: dict[str, str]) -> list[dict]:
    try:
        from board import local_routes, is_route_file  # type: ignore
    except ImportError:  # pragma: no cover
        from core.board import local_routes, is_route_file  # type: ignore
    out = []
    for path, text in files.items():
        if not is_route_file(path, text):
            continue
        clean = _strip_comments(text)
        calls = list(re.finditer(r"""\b(?:router|app|r|routes|api|[a-z]\w*Router)\.(get|post|put|patch|delete)\(\s*(['"`])(/[^'"`]*)\2""", clean))
        for i, (route, m) in enumerate(zip(local_routes(text), calls)):
            end = calls[i + 1].start() if i + 1 < len(calls) else len(clean)
            out.append({**route, "file": path, "line": _line(clean, m.start()), "body": clean[m.end():end]})
    return out


def _mounted_with_auth(files: dict[str, str]) -> set[str]:
    """app.use('/x', auth, router) 처럼 붙일 때 인증을 거는 라우터 파일."""
    try:
        from board import _AUTH_NAME  # type: ignore
        from runtime_check import _IMPORT, _resolve  # type: ignore
    except ImportError:  # pragma: no cover
        from core.board import _AUTH_NAME  # type: ignore
        from core.runtime_check import _IMPORT, _resolve  # type: ignore
    out: set[str] = set()
    for rel, text in files.items():
        imports = {}
        for m in _IMPORT.finditer(text):
            name, spec = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
            target = _resolve(files, rel, spec)
            if target:
                imports[name] = target
        for m in re.finditer(r"""\.use\(\s*(['"`])[^'"`]*\1\s*,([^)]*)\)""", text):
            names = [n.strip() for n in m.group(2).split(",") if n.strip()]
            if len(names) >= 2 and names[-1] in imports and any(_AUTH_NAME.match(n.split("(")[0].split(".")[-1]) for n in names[:-1]):
                out.add(imports[names[-1]])
    return out


def scan(files: dict[str, str]) -> tuple[list[Finding], dict[str, bool]]:
    """(발견, 규칙별 적용 여부)."""
    code = code_files(files)
    clean = {p: _strip_comments(t) for p, t in code.items()}
    deps = _deps(files)
    everything = "\n".join(clean.values())
    findings: list[Finding] = []
    for path, text in clean.items():
        findings += _sql(path, text) + _jwt(path, text) + _cors(path, text) + _xss(path, text) + _errors(path, text)
    routes = _routes(code)
    has_auth = bool(re.search(r"\bjsonwebtoken\b|\bjwt\.(sign|verify)\(|passport|express-session|req\.user\b", everything)) \
        or any(r["auth"] for r in routes)
    protected = _mounted_with_auth(code)
    applicable = {k: False for k in RULES}
    applicable["SQL_INJECTION"] = bool(re.search(r"\.(query|execute|raw|\$queryRawUnsafe)\(", everything))
    applicable["JWT_UNSAFE"] = bool(_JWT_CALL.search(everything))
    applicable["CORS_WILDCARD"] = "cors(" in everything
    applicable["XSS_HTML"] = "dangerouslySetInnerHTML" in everything
    applicable["ERROR_LEAK"] = bool(routes)
    if has_auth:
        applicable["MISSING_AUTH"] = applicable["OWNER_CHECK"] = bool(routes)
        for r in routes:
            if r["method"] not in ("POST", "PUT", "PATCH", "DELETE"):
                continue
            authed = r["auth"] or r["file"] in protected
            #: 서명으로 확인하는 웹훅, 개발용 모의 서버는 로그인 대상이 아니다.
            signed = re.search(r"(?i)stripe-signature|constructEvent|webhook|x-hub-signature|verifySignature", r["body"])
            if not authed and not signed and not _PUBLIC_PATH.search(r["sub"]) and not _PUBLIC_PATH.search(r["file"]) \
                    and not re.search(r"(?i)mock", r["file"]):
                findings.append(Finding("MISSING_AUTH", r["file"], r["line"],
                                        f"{r['method']} {r['sub']} 에 로그인 확인이 없습니다.", "medium"))
            if authed and r["method"] in ("PUT", "PATCH", "DELETE") and "/:" in r["sub"] \
                    and not re.search(r"(?i)req\.(user|auth|session)|user_?id|owner|isAdmin|role|admin", r["body"]) \
                    and not re.search(r"(?i)admin", r["file"] + r["sub"]):
                findings.append(Finding("OWNER_CHECK", r["file"], r["line"],
                                        f"{r['method']} {r['sub']} 가 로그인한 사용자가 주인인지 확인하지 않습니다.", "medium"))
    signup = any(r["method"] == "POST" and _SIGNUP_PATH.search(r["sub"]) for r in routes)
    stores_password = bool(re.search(r"(?is)(insert\s+into\s+\w*users?\w*[^;`]{0,300}password|\.create\(\s*\{[^}]{0,300}password)", everything))
    if signup or stores_password:
        applicable["PLAINTEXT_PASSWORD"] = True
        hashed = any(d in deps for d in _HASH_LIBS) or re.search(
            r"\b(bcrypt|argon2|scrypt(Sync)?|pbkdf2(Sync)?)\b", everything)
        if not hashed:
            where = next((r for r in routes if r["method"] == "POST" and _SIGNUP_PATH.search(r["sub"])), None)
            findings.append(Finding("PLAINTEXT_PASSWORD", where["file"] if where else next(iter(clean), ""),
                                    where["line"] if where else 1,
                                    "비밀번호를 해시하지 않고 저장합니다(bcrypt·argon2 를 쓰지 않음).", "high", precise=True))
    login = [r for r in routes if r["method"] == "POST" and _LOGIN_PATH.search(r["sub"])]
    if login:
        applicable["BRUTE_FORCE"] = True
        #: 시도 제한 라이브러리 · 직접 만든 제한기 · 계정 잠금(실패 횟수) 중 하나면 통과.
        limited = any(d in deps for d in _RATE_LIBS) or re.search(
            r"(?i)rate_?limit\w*|\w*limiter\b|slow_?down\w*\(|throttl\w*|failed_?login|login_?attempts|failed_?attempts|lock_?out|locked_?until",
            everything)
        if not limited:
            findings.append(Finding("BRUTE_FORCE", login[0]["file"], login[0]["line"],
                                    f"로그인({login[0]['sub']})에 시도 횟수 제한이 없습니다"
                                    " — ECS 의 기본 방패가 IP 당 요청 수는 막지만 앱에도 두세요.", "medium"))
    for f in findings:
        applicable[f.rule] = True
    return findings, applicable


def report(files: dict[str, str]) -> dict:
    """점수와 규칙별 결과 — 화면·결과용."""
    findings, applicable = scan(files)
    rules = []
    total = passed = 0
    for code, meta in RULES.items():
        if not applicable.get(code):
            rules.append({"rule": code, "label": meta["label"], "applicable": False, "passed": True, "count": 0})
            continue
        count = sum(1 for f in findings if f.rule == code)
        weight = WEIGHT[meta["severity"]]
        total += weight
        passed += weight if count == 0 else 0
        rules.append({"rule": code, "label": meta["label"], "applicable": True, "passed": count == 0, "count": count,
                      "severity": meta["severity"]})
    #: 적용되는 규칙이 하나도 없으면(정적 화면만, 아직 지원하지 않는 언어) 점수를 매기지 않는다 — 100점처럼 보이면 오해한다.
    score = round(100 * passed / total) if total else None
    return {"score": score, "rules": rules,
            "findings": [{**asdict(f), "label": f.label, "fix": RULES[f.rule]["fix"]} for f in findings[:100]]}


def issues(files: dict[str, str], *, errors_only: bool = False) -> list[dict]:
    """교정 루프·점검 결과용 문제. 정확한 규칙의 심각한 발견만 error, 나머지는 warning."""
    findings, _ = scan(files)
    out = []
    for f in findings:
        severity = "error" if (f.precise and f.severity == "high") else "warning"
        if errors_only and severity != "error":
            continue
        out.append({"code": "GENERATED_SECURITY_ISSUE", "severity": severity, "file": f.file,
                    "message": f"보안({f.label}) {f.line}행: {f.message}",
                    "fix": RULES[f.rule]["fix"] + " 검사를 끄거나 우회하지 말고 안전한 방식으로 고치세요.",
                    "rule": f.rule})
    return out


def ops_files(ops: Iterable[dict]) -> dict[str, str]:
    return {str(op.get("file") or "").replace("\\", "/").lstrip("/"): str(op.get("content") or "")
            for op in ops if op.get("file") and op.get("action") != "delete"}
