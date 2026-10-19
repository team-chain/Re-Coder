"""마을 공용 게시판 — 서버가 **실제로** 돌려주는 API 모양을 코드에서 뽑아 화면 담당에게 보여 준다 (2.0.24).

왜 필요한가
    대규모 생성은 화면(frontend)과 서버(backend) 파일을 동시에 만든다. 둘은 설계의 약속 문서만 보고 쓰기 때문에
    서버는 `{ orders: [...] }` 로 돌려주는데 화면은 `data.items` 를 꺼내는 식으로 어긋났고, 이것을 생성 뒤
    NODE_API_ENVELOPE_MISMATCH 같은 검사로 사후에 고쳐 왔다. 서버 라우트 파일이 완성되는 순간 그 파일에서
    경로·응답 최상위 키·인증 필요 여부를 뽑아 게시판에 붙이고, 화면의 API 호출 파일은 그 게시판을 보고 쓴다.

원칙
    - AI 호출 없음. 정규식과 괄호 짝 맞추기로 읽는다(근사치).
    - 못 읽으면(동적으로 만든 응답) "모양 모름" 으로 두고 약속 문서를 따르게 한다 — 지어내지 않는다.
"""
from __future__ import annotations

import posixpath
import re
from typing import Iterable, Optional

try:
    from runtime_check import _IMPORT, _MOUNT, _resolve, route_table  # type: ignore
except ImportError:  # pragma: no cover
    from core.runtime_check import _IMPORT, _MOUNT, _resolve, route_table  # type: ignore

_CODE = (".ts", ".js", ".mjs", ".cjs")
_ROUTE_CALL = re.compile(r"""\b(?:router|app|r|routes|api|[a-z]\w*Router)\.(get|post|put|patch|delete)\(\s*(['"`])(/[^'"`]*)\2""")
_USE_ALL = re.compile(r"""\b(?:router|r|routes|[a-z]\w*Router)\.use\(\s*([A-Za-z_$][\w$.]*)\s*\)""")
_AUTH_NAME = re.compile(r"(?i)^(?:auth\w*|\w*auth(?:enticate|orize|enticated|Required|Middleware)?\w*|protect\w*|require\w*(?:user|login|admin|auth)\w*|verify\w*token\w*|is(?:logged|admin|authenticated)\w*|ensure\w*(?:auth|admin|login)\w*|admin(?:only|required)?|jwt\w*)$")
_JSON_CALL = re.compile(r"""\.(?:json|send)\(\s*""")
_GENERIC_API = {"api", "client", "http", "axios", "index", "request", "requests", "fetcher", "apiclient", "httpclient", "base"}
_CLIENT_DIRS = re.compile(r"/(api|apis|services?|clients?|requests?|lib/api|utils/api)/")
_ROUTE_DIRS = re.compile(r"/(routes?|routers?|controllers?|handlers?)/")
_SERVER_SIDE_NAMES = {"backend", "server", "api"}


def _strip_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    return re.sub(r"(?m)(^|[^:\\])//.*$", r"\1", text)


def _matching(text: str, start: int, open_ch: str, close_ch: str) -> int:
    """text[start] 가 open_ch 일 때 짝이 맞는 close_ch 위치(문자열·템플릿 안은 건너뜀). 못 찾으면 -1."""
    depth, i, quote = 0, start, ""
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
        elif c == open_ch:
            depth += 1
        elif c == close_ch:
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _top_keys(obj_text: str) -> list[str]:
    """`{ a, b: 1, c: { d }, ...rest }` 의 최상위 키 → ['a', 'b', 'c', '...rest']."""
    inner = obj_text.strip()[1:-1]
    keys, depth, cur, quote = [], 0, "", ""
    for c in inner + ",":
        if quote:
            cur += c
            if c == quote:
                quote = ""
            continue
        if c in "'\"`":
            quote = c
        if c in "{[(":
            depth += 1
        elif c in "}])":
            depth -= 1
        if c == "," and depth == 0:
            part = cur.strip()
            if part:
                if part.startswith("..."):
                    keys.append(part.split()[0])
                else:
                    m = re.match(r"""['"]?([A-Za-z_$][\w$]*)['"]?\s*(?::|$|\()""", part)
                    if m:
                        keys.append(m.group(1))
            cur = ""
            continue
        cur += c
    return list(dict.fromkeys(keys))


def _shape_of(body: str) -> Optional[str]:
    """핸들러 본문에서 성공 응답(첫 2xx 로 보이는 json 호출)의 모양. 못 읽으면 None."""
    shapes: list[str] = []
    for m in _JSON_CALL.finditer(body):
        before = body[max(0, m.start() - 40):m.start()]
        status = re.search(r"\.status\(\s*(\d{3})\s*\)\s*$", before)
        if status and not status.group(1).startswith("2"):
            continue
        pos = m.end()
        if pos >= len(body):
            continue
        ch = body[pos]
        if ch == "{":
            end = _matching(body, pos, "{", "}")
            if end > 0:
                keys = _top_keys(body[pos:end + 1])
                if keys:
                    shapes.append("{ " + ", ".join(k if k.startswith("...") else k for k in keys) + " }")
                    continue
        elif ch == "[":
            shapes.append("[ … ]")
            continue
    return shapes[0] if shapes else None


def local_routes(content: str) -> list[dict]:
    """파일 안 라우트 [{method, sub, shape, auth}] — 마운트 접두사 없이."""
    text = _strip_comments(content or "")
    file_auth = any(_AUTH_NAME.match(n.split(".")[-1]) for n in _USE_ALL.findall(text))
    calls = list(_ROUTE_CALL.finditer(text))
    out = []
    for i, m in enumerate(calls):
        end = calls[i + 1].start() if i + 1 < len(calls) else len(text)
        segment = text[m.end():end]
        # 경로 뒤 인자들 중 함수 본문 앞까지의 이름들 = 미들웨어
        head = re.split(r"(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>|function\b", segment, maxsplit=1)[0]
        names = re.findall(r"[A-Za-z_$][\w$.]*", head)
        auth = file_auth or any(_AUTH_NAME.match(n.split(".")[-1]) for n in names)
        out.append({"method": m.group(1).upper(), "sub": m.group(3), "shape": _shape_of(segment), "auth": auth})
    return out


def is_route_file(path: str, content: str) -> bool:
    return path.endswith(_CODE) and not path.endswith(".d.ts") and bool(_ROUTE_CALL.search(_strip_comments(content or "")))


def _stem(path: str) -> str:
    stem = re.sub(r"\.(routes?|router|controller|api|service)$", "", re.sub(r"\.[^.]+$", "", posixpath.basename(path)).lower())
    return stem[:-1] if stem.endswith("s") and len(stem) > 3 else stem


def _mount_prefixes(files: dict[str, str], route_file: str, server: str) -> list[str]:
    """다른 완성 파일이 이 라우트 파일을 app.use('/api/x', router) 로 붙였으면 그 접두사들."""
    out = []
    for rel, text in files.items():
        if not rel.endswith(_CODE) or (server and not rel.startswith(server.rstrip("/") + "/")):
            continue
        imports = {}
        for m in _IMPORT.finditer(text):
            name, spec = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
            target = _resolve(files, rel, spec)
            if target:
                imports[name] = target
        for prefix, name in _MOUNT.findall(text):
            if imports.get(name) == route_file:
                out.append(prefix.rstrip("/"))
    return out


def board_entries(route_file: str, content: str, done: dict[str, str], server: str, contracts: str = "") -> list[dict]:
    """게시판 항목 [{method, path, shape, auth, file, guessed}].

    경로 접두사: 완성된 진입점이 이 파일을 붙였으면 그 접두사, 아니면 약속 문서에 나온 `/api/<이름>` 을 쓰고(추정 표시),
    둘 다 없으면 `/api/<파일 이름>` 으로 추정한다.
    """
    routes = local_routes(content)
    if not routes:
        return []
    prefixes = _mount_prefixes({**done, route_file: content}, route_file, server)
    guessed = not prefixes
    if guessed:
        stem = _stem(route_file)
        plural = re.compile(rf"/api(?:/v\d+)?/{re.escape(stem)}s?\b", re.I)
        found = plural.search(contracts or "")
        prefixes = [found.group(0)] if found else ([f"/api/{stem}s" if not stem.endswith("s") else f"/api/{stem}"]
                                                   if stem not in _GENERIC_API else ["/api"])
        if any(r["sub"].startswith("/api") for r in routes):
            prefixes = [""]
    out = []
    for r in routes:
        for p in prefixes[:1]:
            full = (p + ("" if r["sub"] == "/" else r["sub"])) or "/"
            out.append({"method": r["method"], "path": full, "shape": r["shape"], "auth": r["auth"],
                        "file": route_file, "guessed": guessed})
    return out


def format_board(board: dict[str, list[dict]], focus: Iterable[str] = (), limit: int = 6000) -> str:
    """프롬프트에 넣을 게시판 블록. focus(파일 이름 줄기) 와 관련된 항목을 먼저."""
    items = [e for entries in board.values() for e in entries]
    if not items:
        return ""
    stems = {_stem(f) for f in focus}
    generic = not stems or stems & _GENERIC_API
    items.sort(key=lambda e: (0 if generic or _stem(e["file"]) in stems else 1, e["path"], e["method"]))
    lines, used = [], 0
    for e in items:
        auth = " · 로그인 필요" if e["auth"] else ""
        shape = e["shape"] or "모양 모름 — 약속 문서를 따르세요"
        line = f"- {e['method']} {e['path']} → {shape}{auth}" + (" (경로 접두사 추정)" if e.get("guessed") else "") + "\n"
        if used + len(line) > limit:
            break
        lines.append(line)
        used += len(line)
    return ("\n[게시판 — 서버가 실제로 돌려주는 모양(이미 완성된 서버 코드에서 뽑음). API 를 부르는 코드는 이 경로·키를 글자 그대로 쓰세요."
            " 응답에서 없는 키를 꺼내지 마세요]\n" + "".join(lines))


def is_api_layer(path: str, server_sides: set[str], sides: frozenset) -> bool:
    """화면 쪽에서 서버를 부르는 파일(api.ts · services/*.ts · client.js …)."""
    p = "/" + path.replace("\\", "/").lower()
    parts = p.strip("/").split("/")
    side = parts[0] if len(parts) > 1 and parts[0] in sides else ""
    if side in server_sides or not path.endswith(_CODE + (".tsx", ".jsx")):
        return False
    stem = re.sub(r"\.[^.]+$", "", posixpath.basename(p))
    return bool(_CLIENT_DIRS.search(p)) or stem.replace("-", "").replace("_", "") in _GENERIC_API - {"index", "base"}


def is_server_route_path(path: str) -> bool:
    """서버 라우트 파일로 보이는 경로(routes/·controllers/ …, 또는 backend|server/ 아래 api/)."""
    p = "/" + path.replace("\\", "/").lower()
    if not path.endswith(_CODE) or p.endswith(".d.ts") or re.search(r"/(pages|components|hooks|app)/", p):
        return False
    if _ROUTE_DIRS.search(p):
        return not re.match(r"/(frontend|client|web|ui)/", p)
    head = p.strip("/").split("/")[0]
    return head in _SERVER_SIDE_NAMES and "/api/" in p


def api_waits_for(api_path: str, route_paths: list[str]) -> list[str]:
    """화면 API 파일이 기다릴 서버 라우트 파일 — 같은 이름(orders ↔ orders)만, 범용 클라이언트면 전부."""
    stem = _stem(api_path).replace("-", "").replace("_", "")
    if stem in _GENERIC_API:
        return list(route_paths)
    return [r for r in route_paths if _stem(r).replace("-", "").replace("_", "") == stem]
