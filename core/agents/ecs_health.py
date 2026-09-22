"""Resolve ECS HTTP health checks from the runtime image and declared routes."""

from __future__ import annotations

import ast
import json
import os
import re
from pathlib import Path

from core.schemas import ECSDeployRequest


def python_http_health_check(
    port: int, path: str = "/health", *, timeout: int = 4
) -> list[str]:
    url = f"http://127.0.0.1:{port}{path}"
    probe = (
        "import sys,urllib.request; "
        f"sys.exit(0 if urllib.request.urlopen({url!r}, timeout={timeout}).status == 200 else 1)"
    )
    return ["CMD", "python", "-c", probe]


def node_http_health_check(
    port: int, path: str = "/health", *, timeout: int = 4
) -> list[str]:
    url = json.dumps(f"http://127.0.0.1:{port}{path}")
    probe = (
        f"const timer=setTimeout(()=>process.exit(1),{timeout * 1000});"
        f"require('http').get({url},r=>{{clearTimeout(timer);"
        "process.exit(r.statusCode===200?0:1);})"
        ".on('error',()=>process.exit(1));"
    )
    return ["CMD", "node", "-e", probe]


def _runtime_family(dockerfile: Path) -> str:
    """Only trust a known final base, including aliases of earlier stages.

    package.json alone is insufficient: a Node builder may produce an nginx
    runtime. Unresolved ARGs and custom bases need an explicit command.
    """
    text = dockerfile.read_text(encoding="utf-8", errors="replace")
    text = re.sub(r"\\\r?\n", " ", text)
    stages: dict[str, str] = {}
    runtime = ""
    for line in text.splitlines():
        match = re.match(
            r"^\s*FROM\s+(?:--platform=\S+\s+)?(\S+)(?:\s+AS\s+(\S+))?\s*$",
            line, re.IGNORECASE,
        )
        if not match:
            # An unsupported FROM must not inherit the previous build stage.
            if re.match(r"^\s*FROM\s", line, re.IGNORECASE):
                runtime = ""
            continue
        base, alias = match.groups()
        image = base.split("@", 1)[0].split(":", 1)[0].lower()
        runtime = stages.get(base.lower(), "")
        for family in ("python", "node"):
            if image in {
                family, f"library/{family}", f"docker.io/{family}", f"docker.io/library/{family}",
                f"public.ecr.aws/docker/library/{family}",
            }:
                runtime = family
        if alias:
            stages[alias.lower()] = runtime
    return runtime


_EXCLUDED_DIRS = {
    ".git", ".venv", "venv", "node_modules", ".next", "dist", "build",
    "__pycache__", "tests", "test", "__tests__",
}


def _declared_health_path(workspace: Path, runtime: str, requested: str) -> str | None:
    """Find a literal health route; conventions alone do not prove it exists."""
    candidates = list(dict.fromkeys([requested, "/health", "/api/health", "/healthz", "/readyz"]))
    declared: set[str] = set()
    inspected = 0
    for directory, dirs, files in os.walk(workspace, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in _EXCLUDED_DIRS and not d.startswith("."))
        for name in sorted(files):
            source = Path(directory) / name
            suffixes = {".py"} if runtime == "python" else {".js", ".ts", ".mjs", ".cjs", ".tsx"}
            if source.suffix not in suffixes or source.is_symlink():
                continue
            inspected += 1
            if inspected > 1000:
                return next((p for p in candidates if p in declared), None)
            try:
                if source.stat().st_size > 1_000_000:
                    continue
                text = source.read_text(encoding="utf-8", errors="replace")
                if runtime == "python":
                    tree = ast.parse(text)
                    apps = {
                        target.id for node in ast.walk(tree)
                        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)
                        and isinstance(node.value.func, ast.Name) and node.value.func.id in {"FastAPI", "Flask"}
                        for target in node.targets if isinstance(target, ast.Name)
                    }
                    for node in ast.walk(tree):
                        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            continue
                        for decorator in node.decorator_list:
                            if (isinstance(decorator, ast.Call)
                                    and isinstance(decorator.func, ast.Attribute)
                                    and isinstance(decorator.func.value, ast.Name)
                                    and decorator.func.value.id in apps
                                    and decorator.func.attr in {"get", "route", "api_route"}
                                    and decorator.args
                                    and isinstance(decorator.args[0], ast.Constant)
                                    and isinstance(decorator.args[0].value, str)):
                                methods = next((kw.value for kw in decorator.keywords if kw.arg == "methods"), None)
                                if methods is not None:
                                    if not isinstance(methods, (ast.List, ast.Tuple)):
                                        continue
                                    if not any(isinstance(item, ast.Constant) and item.value == "GET" for item in methods.elts):
                                        continue
                                declared.add(decorator.args[0].value)
                else:
                    # Express literal GET routes, excluding standalone comment lines.
                    active = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
                    active = "\n".join(line for line in active.splitlines() if not line.lstrip().startswith("//"))
                    # Mounted routers may have a prefix; their local /health
                    # is not proof that the app serves /health at its root.
                    apps = re.findall(r"\b(?:const|let|var)\s+(\w+)\s*=\s*express\(\s*\)", active)
                    for app in apps:
                        declared.update(re.findall(rf"\b{re.escape(app)}\.get\s*\(\s*['\"]([^'\"]+)['\"]", active))
                    relative = source.relative_to(workspace).as_posix()
                    for path in candidates:
                        route = path.strip("/")
                        for prefix in ("", "src/"):
                            if (relative == f"{prefix}app/{route}/route{source.suffix}"
                                    and re.search(r"export\s+(?:(?:async\s+)?function\s+GET\b|(?:const|let)\s+GET\s*=)", active)):
                                declared.add(path)
                            if relative in {
                                f"{prefix}pages/{route}{source.suffix}",
                                f"{prefix}pages/{route}/index{source.suffix}",
                            }:
                                declared.add(path)
            except (OSError, SyntaxError, ValueError):
                continue
    return next((p for p in candidates if p in declared), None)


def configure_health_check(request: ECSDeployRequest) -> str:
    """Fill a missing command in the shared pipeline; return a user-facing gap."""
    if request.health_check_command:
        return ""
    if not request.workspace_path:
        return "작업 폴더가 없어 런타임과 헬스 경로를 확인하지 못했습니다. health_check_command를 지정하세요."
    workspace = Path(request.workspace_path)
    try:
        runtime = _runtime_family(workspace / request.dockerfile)
    except OSError:
        runtime = ""
    if not runtime:
        return "최종 Docker 이미지의 런타임을 확인하지 못했습니다. 이미지에서 실행 가능한 health_check_command를 지정하세요."

    # An explicit path is a caller's configuration. Otherwise require source
    # evidence, so adding ECS checks cannot turn an absent /health into a loop.
    if "health_check_path" not in request.model_fields_set:
        path = _declared_health_path(workspace, runtime, request.health_check_path)
        if not path:
            return "소스에서 헬스 경로를 찾지 못했습니다. GET /health를 구현하거나 health_check_path를 지정하세요."
        request.health_check_path = path

    factory = python_http_health_check if runtime == "python" else node_http_health_check
    request.health_check_command = factory(request.container_port, request.health_check_path)
    return ""
