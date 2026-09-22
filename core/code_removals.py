"""Advisory comparison of saved source and generated whole-file replacements.

This is not a semantic safety check: renames/moves are removal candidates too.
Python uses AST; JS/TS recognises common static declarations and literal routes.
No project code is executed and no extra model request is made.
"""
from __future__ import annotations

import ast
import re
from collections import Counter
from pathlib import Path

MAX_SOURCE_BYTES = 1_000_000
_METHODS = {"get", "post", "put", "patch", "delete", "head", "options", "all"}
_JS_EXTS = {".js", ".cjs", ".mjs", ".ts", ".cts", ".mts", ".jsx", ".tsx"}


def _symbol(kind: str, name: str, line: int) -> dict:
    return {"kind": kind, "name": name, "line": line}


def _python_symbols(source: str) -> list[dict]:
    result: list[dict] = []

    class Visitor(ast.NodeVisitor):
        def __init__(self):
            self.scope: list[str] = []

        def visit_ClassDef(self, node):
            self.scope.append(node.name)
            self.generic_visit(node)
            self.scope.pop()

        def visit_FunctionDef(self, node):
            result.append(_symbol("function", ".".join([*self.scope, node.name]), node.lineno))
            for dec in node.decorator_list:
                if not isinstance(dec, ast.Call) or not isinstance(dec.func, ast.Attribute):
                    continue
                method = dec.func.attr.lower()
                route = dec.args[0] if dec.args else next(
                    (kw.value for kw in dec.keywords if kw.arg in {"path", "rule"}), None)
                if not isinstance(route, ast.Constant) or not isinstance(route.value, str):
                    continue
                if method in _METHODS:
                    methods = [method.upper()]
                elif method in {"route", "api_route"}:
                    methods_node = next((kw.value for kw in dec.keywords if kw.arg == "methods"), None)
                    if methods_node is None:
                        methods = ["GET"]
                    elif isinstance(methods_node, (ast.List, ast.Tuple, ast.Set)):
                        methods = [m.value.upper() for m in methods_node.elts
                                   if isinstance(m, ast.Constant) and isinstance(m.value, str)]
                    else:
                        continue
                else:
                    continue
                for verb in methods:
                    result.append(_symbol("route", f"{verb} {route.value}", dec.lineno))
            self.scope.append(node.name)
            self.generic_visit(node)
            self.scope.pop()

        visit_AsyncFunctionDef = visit_FunctionDef

    Visitor().visit(ast.parse(source))
    return result


# Mask comments, quoted strings, templates and common regex literals before
# matching declarations. Keep offsets/newlines so warnings refer to saved lines.
_JS_NON_CODE = re.compile(
    r"//[^\n]*|/\*[\s\S]*?\*/"
    r"|'(?:\\[\s\S]|[^'\\])*'|\"(?:\\[\s\S]|[^\"\\])*\"|`(?:\\[\s\S]|[^`\\])*`"
    r"|(?<=[=(:,!;\n])\s*/(?![/*])(?:\\.|\[(?:\\.|[^\]\\])*\]|[^/\n\\])+/[a-z]*"
)
_IDENT = r"[A-Za-z_$][\w$]*"
_JS_FUNCTION = re.compile(rf"\bfunction\s*\*?\s+(?P<name>{_IDENT})\s*(?:<[^;{{}}]*>)?\s*\(")
_JS_ASSIGNED = re.compile(
    rf"\b(?:const|let|var)\s+(?P<name>{_IDENT})\s*(?::[^=;\n]+)?=\s*"
    rf"(?:async\s+)?(?:function\b|(?:{_IDENT}|(?:<[^;{{}}]*>\s*)?\([^;{{}}]*?\))\s*(?::[^=;{{}}]+)?=>)"
)
_JS_ROUTE = re.compile(
    rf"\b(?P<receiver>{_IDENT}(?:\s*\.\s*{_IDENT})*)\s*\.\s*"
    r"(?P<method>get|post|put|patch|delete|head|options|all|route)\s*\(\s*(?P<path>@)"
)


def _js_symbols(source: str) -> list[dict]:
    literals: dict[int, str] = {}

    def mask(match):
        raw = match.group()
        masked = re.sub(r"[^\n]", " ", raw)
        if raw[0] in "'\"`":
            # Interpolated templates and escape-heavy paths require manual diff.
            value = raw[1:-1]
            if "${" not in value and "\\" not in value:
                literals[match.start()] = value
            return "@" + masked[1:]
        return masked

    code = _JS_NON_CODE.sub(mask, source)
    stack = []
    pairs = {")": "(", "]": "[", "}": "{"}
    for char in code:
        if char in "'\"`":
            raise ValueError("unclosed literal")
        if char in "([{":
            stack.append(char)
        elif char in pairs and (not stack or stack.pop() != pairs[char]):
            raise ValueError("unbalanced source")
    if stack:
        raise ValueError("incomplete source")
    result: list[dict] = []
    function_positions: set[int] = set()
    for pattern in (_JS_FUNCTION, _JS_ASSIGNED):
        for match in pattern.finditer(code):
            # A named function expression belongs to its assigned variable.
            if pattern is _JS_FUNCTION and re.search(r"=\s*(?:async\s+)?$", code[:match.start()]):
                continue
            pos = match.start("name")
            if pos not in function_positions:
                result.append(_symbol("function", match.group("name"), source.count("\n", 0, pos) + 1))
                function_positions.add(pos)
    for match in _JS_ROUTE.finditer(code):
        receiver = re.sub(r"\s", "", match.group("receiver")).split(".")[-1].lower()
        if receiver not in {"app", "router", "server", "api"} and not receiver.endswith("router"):
            continue
        route = literals.get(match.start("path"))
        if route is None or not route.startswith("/"):
            continue
        method = match.group("method")
        line = source.count("\n", 0, match.start()) + 1
        if method == "route":
            # Express app.route('/todos').get(...).post(...). Stop at statement end.
            # Balance call parentheses so handler bodies containing .get don't count.
            cursor = match.end()
            depth = 1
            while cursor < len(code) and depth:
                depth += (code[cursor] == "(") - (code[cursor] == ")")
                cursor += 1
            while cursor < len(code):
                chained = re.match(r"\s*\.\s*(get|post|put|patch|delete|head|options|all)\s*\(", code[cursor:])
                if not chained:
                    break
                result.append(_symbol("route", f"{chained[1].upper()} {route}", line))
                cursor += chained.end()
                depth = 1
                while cursor < len(code) and depth:
                    depth += (code[cursor] == "(") - (code[cursor] == ")")
                    cursor += 1
        else:
            result.append(_symbol("route", f"{method.upper()} {route}", line))
    return result


def compare_sources(before: str, after: str, suffix: str) -> dict:
    """Return missing declarations, preserving multiplicity and original lines."""
    if suffix not in {".py", *_JS_EXTS}:
        return {"status": "unavailable", "removed": [], "reason": "이 파일 형식은 함수·라우트 삭제 검사를 지원하지 않습니다."}
    if max(len(before.encode("utf-8")), len(after.encode("utf-8"))) > MAX_SOURCE_BYTES:
        return {"status": "unavailable", "removed": [], "reason": "파일이 1 MB를 넘어 삭제 검사를 수행하지 못했습니다."}
    try:
        extract = _python_symbols if suffix == ".py" else _js_symbols
        old, new = extract(before), extract(after)
    except (SyntaxError, ValueError, RecursionError):
        return {"status": "unavailable", "removed": [], "reason": "코드 구문을 분석하지 못했습니다. 변경 보기에서 직접 확인하세요."}
    remaining = Counter((item["kind"], item["name"]) for item in new)
    removed = []
    for item in old:
        key = (item["kind"], item["name"])
        if remaining[key]:
            remaining[key] -= 1
        else:
            removed.append(item)
    return {"status": "checked", "removed": removed}


def annotate_removals(ops: list[dict], root: Path, target_folder: str = "") -> None:
    """Attach trusted reports, ignoring model-supplied action/report fields.

    Match the extension's write-path normalisation, including its stripping of
    '..' segments. Refuse symlinks outside the request root. Compare saved files
    (the left side of the extension diff), not potentially stale prompt context.
    """
    root = root.resolve()
    folder = target_folder.replace("\\", "/").strip("/")
    for op in ops:
        report = {"status": "unavailable", "removed": [], "reason": "기존 파일을 읽지 못했습니다. 변경 보기에서 직접 확인하세요."}
        try:
            name = str(op["file"]).replace("\\", "/").lstrip("/")
            combined = f"{folder}/{name}" if folder else name
            parts = [p for p in combined.split("/") if p and p != ".."]
            path = root.joinpath(*parts).resolve()
            if not parts or not path.is_relative_to(root):
                op["removal_check"] = report
                continue
            # Stat/read errors must not masquerade as a new file.
            try:
                size = path.stat().st_size
            except FileNotFoundError:
                report = {"status": "new_file", "removed": []}
            else:
                if size > MAX_SOURCE_BYTES:
                    report["reason"] = "파일이 1 MB를 넘어 삭제 검사를 수행하지 못했습니다."
                else:
                    with path.open("rb") as stream:
                        raw = stream.read(MAX_SOURCE_BYTES + 1)
                    if len(raw) > MAX_SOURCE_BYTES:
                        report["reason"] = "파일이 1 MB를 넘어 삭제 검사를 수행하지 못했습니다."
                    else:
                        report = compare_sources(raw.decode("utf-8-sig"), op["content"], path.suffix.lower())
        except (OSError, UnicodeError, ValueError, RuntimeError):
            pass
        op["removal_check"] = report
