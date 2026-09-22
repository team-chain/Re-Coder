"""A1: automatic checks must describe the final runtime and a real GET route."""

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.agents.ecs_health import configure_health_check, node_http_health_check, python_http_health_check
from core.schemas import ECSDeployRequest


def request_for(tmp_path, dockerfile="FROM node:22-alpine\n", **overrides):
    (tmp_path / "Dockerfile").write_text(dockerfile)
    fields = dict(project_id="p", cluster="c", service="s", workspace_path=str(tmp_path), container_port=3456)
    fields.update(overrides)
    return ECSDeployRequest(**fields)


@pytest.mark.parametrize("dockerfile", [
    "FROM node:22-alpine\n",
    "FROM --platform=linux/amd64 node:22 AS base\nFROM base AS runtime\n",
    "FROM python:3.12 AS builder\nFROM docker.io/library/node:22-slim AS runtime\n",
])
def test_express_check_uses_final_runtime_and_request_port(tmp_path, dockerfile):
    (tmp_path / "app.js").write_text("const app = express(); app.get('/health', (req,res)=>res.send('ok'));\n")
    req = request_for(tmp_path, dockerfile)
    assert configure_health_check(req) == ""
    assert req.health_check_command[:3] == ["CMD", "node", "-e"]
    assert "http://127.0.0.1:3456/health" in req.health_check_command[3]


def test_python_healthz_is_detected_without_curl(tmp_path):
    (tmp_path / "main.py").write_text("app = FastAPI()\n@app.get('/healthz')\ndef health():\n    return {'ok': True}\n")
    req = request_for(tmp_path, "FROM python:3.12-slim\n", container_port=8000)
    assert configure_health_check(req) == ""
    assert req.health_check_path == "/healthz"
    assert req.health_check_command[:3] == ["CMD", "python", "-c"]
    assert "http://127.0.0.1:8000/healthz" in req.health_check_command[3]


def test_next_api_route_is_used_instead_of_default_health(tmp_path):
    route = tmp_path / "src/app/api/health/route.ts"
    route.parent.mkdir(parents=True)
    route.write_text("export async function GET() { return Response.json({ok: true}); }")
    req = request_for(tmp_path)
    assert configure_health_check(req) == ""
    assert req.health_check_path == "/api/health"


@pytest.mark.parametrize("dockerfile", [
    "FROM node:22 AS builder\nFROM nginx:alpine\n",
    "FROM node:22 AS builder\nFROM scratch\n",
    "FROM node:22 AS builder\nFROM ${RUNTIME_IMAGE}\n",
    "FROM company/node:22\n",
])
def test_builder_or_package_json_does_not_prove_runtime(tmp_path, dockerfile):
    (tmp_path / "package.json").write_text('{"dependencies":{"express":"*"}}')
    (tmp_path / "app.js").write_text("app.get('/health', handler);")
    req = request_for(tmp_path, dockerfile)
    assert "런타임" in configure_health_check(req)
    assert req.health_check_command is None


@pytest.mark.parametrize("name,content,dockerfile", [
    ("app.js", "// app.get('/health', handler);", "FROM node:22\n"),
    ("app.js", "/* app.get('/health', handler); */", "FROM node:22\n"),
    ("app.js", "app.post('/health', handler);", "FROM node:22\n"),
    ("tests/app.js", "app.get('/health', handler);", "FROM node:22\n"),
    ("app/api/health/route.ts", "export function POST() {}", "FROM node:22\n"),
    ("main.py", "@app.route('/health', methods=['POST'])\ndef health(): pass", "FROM python:3.12\n"),
    ("routes.py", "router = APIRouter(prefix='/api')\n@router.get('/health')\ndef health(): pass", "FROM python:3.12\n"),
    ("routes.js", "const router=express.Router(); router.get('/health', handler);", "FROM node:22\n"),
])
def test_missing_get_route_is_reported_without_an_always_failing_check(tmp_path, name, content, dockerfile):
    source = tmp_path / name
    source.parent.mkdir(parents=True, exist_ok=True)
    setup = "app = FastAPI()\n" if source.suffix == ".py" else "const app = express();\n"
    source.write_text(setup + content)
    req = request_for(tmp_path, dockerfile)
    assert "헬스 경로" in configure_health_check(req)
    assert req.health_check_command is None


def test_explicit_path_and_command_are_preserved(tmp_path):
    req = request_for(tmp_path, health_check_path="/internal/ready")
    assert configure_health_check(req) == ""
    assert "/internal/ready" in req.health_check_command[3]
    command = ["CMD", "/app/check-health"]
    req = request_for(tmp_path, "FROM scratch\n", health_check_command=command)
    assert configure_health_check(req) == ""
    assert req.health_check_command == command


def test_image_only_deploy_keeps_a_visible_gap():
    req = ECSDeployRequest(project_id="p", cluster="c", service="s", image="example/app:v1")
    assert "작업 폴더" in configure_health_check(req)
    assert req.health_check_command is None


@pytest.mark.parametrize("path", ["health", "//example.com\n", "/health;echo", "/health$(id)"])
def test_bad_paths_are_rejected_for_both_api_shapes(path):
    from api.routes.deploy_ecs import ExtensionEcsDeployRequest
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        ECSDeployRequest(project_id="p", cluster="c", service="s", health_check_path=path)
    with pytest.raises(ValidationError):
        ExtensionEcsDeployRequest(health_check_path=path)


def test_extension_preserves_explicit_health_configuration():
    from api.routes.deploy_ecs import ExtensionEcsDeployRequest, to_core_request
    req = to_core_request(ExtensionEcsDeployRequest(health_check_path="/ready", health_check_command=["CMD", "/check"]))
    assert req.health_check_path == "/ready"
    assert req.health_check_command == ["CMD", "/check"]
    assert "health_check_path" not in to_core_request(ExtensionEcsDeployRequest()).model_fields_set


@pytest.mark.parametrize("status,exit_code", [(200, 0), (404, 1), (500, 1)])
def test_generated_python_probe_executes_and_checks_status(status, exit_code):
    command = python_http_health_check(3456)
    # Execute the actual generated program with a controlled HTTP response.
    setup = f"import urllib.request; urllib.request.urlopen=lambda *a,**k:type('R',(),{{'status':{status}}})();"
    result = subprocess.run([sys.executable, "-c", setup + command[3]], capture_output=True, timeout=5)
    assert command[:3] == ["CMD", "python", "-c"]
    assert result.returncode == exit_code, result.stderr


@pytest.mark.parametrize("event,status,exit_code", [("response", 200, 0), ("response", 404, 1), ("response", 500, 1), ("error", 0, 1), ("timeout", 0, 1)])
def test_generated_node_probe_executes_response_error_and_timeout(event, status, exit_code):
    import shutil
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required to execute the generated Node health check")
    command = node_http_health_check(3456, timeout=0.05)
    setup = (
        "require('http').get=(url,cb)=>{"
        + (f"setImmediate(()=>cb({{statusCode:{status}}}));" if event == "response" else "")
        + "return {on:(event,cb)=>{"
        + ("setImmediate(()=>cb(new Error('refused')));" if event == "error" else "")
        + "}};};"
    )
    result = subprocess.run([node, "-e", setup + command[3]], capture_output=True, timeout=5)
    assert result.returncode == exit_code, result.stderr
