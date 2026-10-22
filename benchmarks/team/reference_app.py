"""팀 벤치마크 판정 시험(oracle)을 검증하는 기준 구현 — 사람이 tasks.json 의 요구대로 쓴 작은 서버.

쓰임: 판정 시험이 (1) 요구를 지킨 구현에서는 모두 통과하고 (2) 일부러 넣은 버그(BUGS)는 정확히 그 조건에서 잡는지
확인한다(core/tests/test_team_benchmark.py). 벤치마크 실행 때는 쓰지 않는다 — 생성한 앱을 컨테이너로 띄워 시험한다.

python reference_app.py T03-likes 8080   # 손으로 띄워 보기
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

#: 일부러 넣을 수 있는 버그 — {과제: {버그 이름: 잡아야 할 조건(들)}}. 판정 시험이 버그를 놓치거나 상관없는 조건까지
#: 실패시키면 테스트가 실패한다. 여러 조건이 걸린 것은 앞 기능이 깨지면 뒤 조건도 확인할 수 없는 경우다(예: 완료 표시가 안 되면 거르기도 틀린다).
BUGS = {
    "T01-notes": {"oldest_first": "AC-3", "delete_noop": "AC-4", "allow_empty_title": "AC-1"},
    "T02-todos": {"no_filter": "AC-4", "patch_ignores": ("AC-3", "AC-4")},
    "T03-likes": {"count_every_click": "AC-2", "detail_stale": "AC-3"},
    "T04-auth": {"leak_password": "AC-1", "no_duplicate_check": "AC-2", "me_without_token": "AC-4"},
    "T05-pages": {"no_cap": "AC-3", "accept_bad_limit": "AC-4", "ignore_limit": ("AC-1", "AC-2")},
    "T06-comments": {"newest_first": "AC-2", "comment_any_post": "AC-3"},
    "T07-stock": {"no_decrement": "AC-2", "oversell": "AC-3"},
    "T08-links": {"hits_not_saved": "AC-3", "accept_ftp": "AC-1"},
    "T09-books": {"case_sensitive": ("AC-2", "AC-3"), "title_only": "AC-3"},
    "T10-users": {"float_age": "AC-2", "no_unique_email": "AC-4"},
}

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _int(v: Any) -> Optional[int]:
    return v if isinstance(v, int) and not isinstance(v, bool) else None


class App:
    def __init__(self, task: str, bugs: frozenset[str] = frozenset()):
        self.task, self.bugs = task, bugs
        self.lock = threading.Lock()
        self.rows: dict[str, list[dict]] = {}
        self.seq = 0
        self.likes: dict[int, set] = {}
        self.tokens: dict[str, int] = {}

    def new_id(self) -> int:
        self.seq += 1
        return self.seq

    def table(self, name: str) -> list[dict]:
        return self.rows.setdefault(name, [])

    def find(self, name: str, ident: Any) -> Optional[dict]:
        return next((r for r in self.table(name) if str(r.get("id", r.get("code"))) == str(ident)), None)

    # (상태, 본문) — 본문 None 이면 빈 응답
    def handle(self, method: str, path: str, query: dict, body: Any, auth: str) -> tuple[int, Any]:
        with self.lock:
            fn = getattr(self, "t_" + self.task.split("-")[0].lower())
            return fn(method, path.rstrip("/") or "/", query, body if isinstance(body, dict) else {}, auth)

    def t_t01(self, m, p, q, b, auth):
        if p == "/api/notes" and m == "POST":
            if not isinstance(b.get("title"), str) or (not b["title"] and "allow_empty_title" not in self.bugs):
                return 400, {"error": "title"}
            row = {"id": self.new_id(), "title": b["title"], "body": b.get("body", "")}
            self.table("notes").append(row)
            return 201, row
        if p == "/api/notes" and m == "GET":
            rows = list(self.table("notes"))
            return 200, rows if "oldest_first" in self.bugs else rows[::-1]
        mm = re.fullmatch(r"/api/notes/(\d+)", p)
        if mm:
            row = self.find("notes", mm.group(1))
            if m == "GET":
                return (200, row) if row else (404, {"error": "없음"})
            if m == "DELETE":
                if not row:
                    return 404, {"error": "없음"}
                if "delete_noop" not in self.bugs:
                    self.table("notes").remove(row)
                return 204, None
        return 404, {"error": "경로"}

    def t_t02(self, m, p, q, b, auth):
        if p == "/api/todos" and m == "POST":
            if not isinstance(b.get("text"), str) or not b["text"]:
                return 400, {"error": "text"}
            row = {"id": self.new_id(), "text": b["text"], "done": False}
            self.table("todos").append(row)
            return 201, row
        if p == "/api/todos" and m == "GET":
            rows = self.table("todos")[::-1]
            if "done" in q and "no_filter" not in self.bugs:
                want = q["done"][0] == "true"
                rows = [r for r in rows if r["done"] == want]
            return 200, rows
        mm = re.fullmatch(r"/api/todos/(\d+)", p)
        if mm and m == "PATCH":
            row = self.find("todos", mm.group(1))
            if not row:
                return 404, {"error": "없음"}
            if isinstance(b.get("done"), bool) and "patch_ignores" not in self.bugs:
                row["done"] = b["done"]
            return 200, row
        return 404, {"error": "경로"}

    def t_t03(self, m, p, q, b, auth):
        if p == "/api/posts" and m == "POST":
            row = {"id": self.new_id(), "title": str(b.get("title", "")), "likes": 0}
            self.table("posts").append(row)
            self.likes[row["id"]] = set()
            return 201, row
        mm = re.fullmatch(r"/api/posts/(\d+)(/likes)?", p)
        if mm:
            row = self.find("posts", mm.group(1))
            if mm.group(2) and m == "POST":
                if not row:
                    return 404, {"error": "없음"}
                if not isinstance(b.get("user"), str) or not b["user"]:
                    return 400, {"error": "user"}
                if "count_every_click" in self.bugs:
                    row["likes"] += 1
                    return 200, {"likes": row["likes"]}
                self.likes[row["id"]].add(b["user"])
                count = len(self.likes[row["id"]])
                if "detail_stale" not in self.bugs:  # 버그: 응답만 맞고 글에는 저장하지 않는다
                    row["likes"] = count
                return 200, {"likes": count}
            if not mm.group(2) and m == "GET":
                return (200, row) if row else (404, {"error": "없음"})
        return 404, {"error": "경로"}

    def t_t04(self, m, p, q, b, auth):
        users = self.table("users")
        if p == "/api/auth/register" and m == "POST":
            email, pw = b.get("email"), b.get("password")
            if not isinstance(email, str) or not _EMAIL.match(email) or not isinstance(pw, str) or len(pw) < 8:
                return 400, {"error": "입력"}
            if any(u["email"] == email for u in users) and "no_duplicate_check" not in self.bugs:
                return 409, {"error": "중복"}
            salt = secrets.token_hex(8)
            row = {"id": self.new_id(), "email": email, "salt": salt, "hash": hashlib.sha256((salt + pw).encode()).hexdigest()}
            users.append(row)
            out = {"id": row["id"], "email": email}
            if "leak_password" in self.bugs:
                out["password"] = pw
            return 201, out
        if p == "/api/auth/login" and m == "POST":
            for u in users:
                if u["email"] == b.get("email") and u["hash"] == hashlib.sha256((u["salt"] + str(b.get("password"))).encode()).hexdigest():
                    tok = secrets.token_hex(16)
                    self.tokens[tok] = u["id"]
                    return 200, {"token": tok}
            return 401, {"error": "로그인"}
        if p == "/api/me" and m == "GET":
            tok = auth[7:] if auth.startswith("Bearer ") else ""
            uid = self.tokens.get(tok)
            if uid is None and "me_without_token" in self.bugs and users:
                uid = users[-1]["id"]
            u = next((x for x in users if x["id"] == uid), None)
            return (200, {"id": u["id"], "email": u["email"]}) if u else (401, {"error": "토큰"})
        return 404, {"error": "경로"}

    def t_t05(self, m, p, q, b, auth):
        if p == "/api/items" and m == "POST":
            if not isinstance(b.get("name"), str) or not b["name"]:
                return 400, {"error": "name"}
            row = {"id": self.new_id(), "name": b["name"]}
            self.table("items").append(row)
            return 201, row
        if p == "/api/items" and m == "GET":
            def num(key: str, default: int) -> Optional[int]:
                raw = q.get(key, [str(default)])[0]
                if not re.fullmatch(r"\d+", raw):
                    return 1 if "accept_bad_limit" in self.bugs else None
                v = int(raw)
                return v if v >= 1 or "accept_bad_limit" in self.bugs else None
            page, limit = num("page", 1), num("limit", 10)
            if page is None or limit is None:
                return 400, {"error": "page·limit"}
            page, limit = max(page, 1), max(limit, 1)
            if "no_cap" not in self.bugs:
                limit = min(limit, 50)
            rows = self.table("items")
            shown = rows if "ignore_limit" in self.bugs else rows[(page - 1) * limit: page * limit]
            return 200, {"items": shown, "total": len(rows), "page": page, "limit": limit}
        return 404, {"error": "경로"}

    def t_t06(self, m, p, q, b, auth):
        if p == "/api/posts" and m == "POST":
            row = {"id": self.new_id(), "title": str(b.get("title", ""))}
            self.table("posts").append(row)
            return 201, row
        if p == "/api/posts" and m == "GET":
            return 200, self.table("posts")
        mm = re.fullmatch(r"/api/posts/(\d+)/comments", p)
        if mm:
            post = self.find("posts", mm.group(1))
            if not post and "comment_any_post" not in self.bugs:
                return 404, {"error": "없음"}
            pid = int(mm.group(1))
            if m == "POST":
                if not isinstance(b.get("text"), str) or not b["text"]:
                    return 400, {"error": "text"}
                row = {"id": self.new_id(), "postId": pid, "text": b["text"]}
                self.table("comments").append(row)
                return 201, row
            if m == "GET":
                rows = [c for c in self.table("comments") if c["postId"] == pid]
                return 200, rows[::-1] if "newest_first" in self.bugs else rows
        return 404, {"error": "경로"}

    def t_t07(self, m, p, q, b, auth):
        if p == "/api/products" and m == "POST":
            stock = _int(b.get("stock"))
            if stock is None or stock < 0:
                return 400, {"error": "stock"}
            row = {"id": self.new_id(), "name": str(b.get("name", "")), "stock": stock}
            self.table("products").append(row)
            return 201, row
        mm = re.fullmatch(r"/api/products/(\d+)", p)
        if mm and m == "GET":
            row = self.find("products", mm.group(1))
            return (200, row) if row else (404, {"error": "없음"})
        if p == "/api/orders" and m == "POST":
            qty = _int(b.get("qty"))
            prod = self.find("products", b.get("productId"))
            if not prod:
                return 404, {"error": "상품"}
            if qty is None or qty < 1:
                return 400, {"error": "qty"}
            if qty > prod["stock"] and "oversell" not in self.bugs:
                return 409, {"error": "재고"}
            if "no_decrement" not in self.bugs:
                prod["stock"] -= qty
            row = {"id": self.new_id(), "productId": prod["id"], "qty": qty}
            self.table("orders").append(row)
            return 201, row
        return 404, {"error": "경로"}

    def t_t08(self, m, p, q, b, auth):
        if p == "/api/links" and m == "POST":
            url = b.get("url")
            if not isinstance(url, str) or not (re.match(r"^https?://", url) or ("accept_ftp" in self.bugs and "://" in url)):
                return 400, {"error": "url"}
            row = {"code": secrets.token_urlsafe(5), "url": url, "hits": 0}
            self.table("links").append(row)
            return 201, row
        mm = re.fullmatch(r"/api/links/([\w-]+)(/visit)?", p)
        if mm:
            row = self.find("links", mm.group(1))
            if not row:
                return 404, {"error": "없음"}
            if mm.group(2) and m == "POST":
                hits = row["hits"] + 1
                if "hits_not_saved" not in self.bugs:
                    row["hits"] = hits
                return 200, {"url": row["url"], "hits": hits}
            if m == "GET":
                return 200, row
        return 404, {"error": "경로"}

    def t_t09(self, m, p, q, b, auth):
        if p == "/api/books" and m == "POST":
            if not all(isinstance(b.get(k), str) and b[k] for k in ("title", "author")):
                return 400, {"error": "title·author"}
            row = {"id": self.new_id(), "title": b["title"], "author": b["author"]}
            self.table("books").append(row)
            return 201, row
        if p == "/api/books" and m == "GET":
            rows = self.table("books")
            if "q" in q:
                term = q["q"][0]
                norm = (lambda s: s) if "case_sensitive" in self.bugs else (lambda s: s.lower())
                fields = ("title",) if "title_only" in self.bugs else ("title", "author")
                rows = [r for r in rows if any(norm(term) in norm(r[f]) for f in fields)]
            return 200, rows
        return 404, {"error": "경로"}

    def t_t10(self, m, p, q, b, auth):
        users = self.table("users")
        if p == "/api/users" and m == "POST":
            name, email, age = b.get("name"), b.get("email"), b.get("age")
            if "float_age" in self.bugs and isinstance(age, float):
                age = int(age)
            if not (isinstance(name, str) and 1 <= len(name) <= 50) or not (isinstance(email, str) and _EMAIL.match(email)) \
                    or _int(age) is None or not 0 <= age <= 150:
                return 400, {"error": "입력"}
            if any(u["email"] == email for u in users) and "no_unique_email" not in self.bugs:
                return 409, {"error": "중복"}
            row = {"id": self.new_id(), "name": name, "email": email, "age": age}
            users.append(row)
            return 201, row
        mm = re.fullmatch(r"/api/users/(\d+)", p)
        if mm and m == "GET":
            row = self.find("users", mm.group(1))
            return (200, row) if row else (404, {"error": "없음"})
        return 404, {"error": "경로"}


def serve(task: str, bugs: frozenset[str] = frozenset(), port: int = 0) -> ThreadingHTTPServer:
    app = App(task, bugs)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # noqa: D401 — 조용히
            pass

        def _do(self):
            url = urlparse(self.path)
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b""
            try:
                body = json.loads(raw) if raw else {}
            except ValueError:
                body = {}
            status, data = app.handle(self.command, url.path, parse_qs(url.query), body, self.headers.get("Authorization") or "")
            out = b"" if data is None else json.dumps(data, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        do_GET = do_POST = do_PATCH = do_PUT = do_DELETE = _do

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


if __name__ == "__main__":
    srv = serve(sys.argv[1], port=int(sys.argv[2]) if len(sys.argv) > 2 else 8080)
    print(f"http://127.0.0.1:{srv.server_address[1]}  ({sys.argv[1]})")
    threading.Event().wait()
