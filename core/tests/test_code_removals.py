"""A4: actual saved source -> generation -> removal report, without an LLM."""
import asyncio
import json
from pathlib import Path

import pytest

import code_agent as ca
from adr import CONFIRM_DECISION_ID
from code_removals import MAX_SOURCE_BYTES, annotate_removals, compare_sources


def removed(before, after, suffix=".js"):
    report = compare_sources(before, after, suffix)
    assert report["status"] == "checked", report
    return [(item["kind"], item["name"]) for item in report["removed"]]


JS = """const express = require('express');
const app = express();
function listTodos() { return []; }
const createTodo = async (todo) => todo;
app.get('/health', (req, res) => res.json({ok: true}));
app.get('/todos', (req, res) => res.json(listTodos()));
app.post('/todos', (req, res) => res.json(createTodo(req.body)));
"""
HEALTH_ONLY = "const app = express();\napp.get('/health', (req, res) => res.json({ok: true}));\n"


def test_express_removed_functions_and_routes():
    report = compare_sources(JS, HEALTH_ONLY, ".js")
    assert report == {"status": "checked", "removed": [
        {"kind": "function", "name": "listTodos", "line": 3},
        {"kind": "function", "name": "createTodo", "line": 4},
        {"kind": "route", "name": "GET /todos", "line": 6},
        {"kind": "route", "name": "POST /todos", "line": 7},
    ]}


def test_body_change_and_addition_are_not_removals():
    assert removed(JS, JS.replace("ok: true", "ok: false") + "\nfunction added() {}") == []


def test_method_change_detected_even_when_path_remains():
    assert removed("app.get('/todos', handler);", "app.post('/todos', handler);") == [("route", "GET /todos")]


def test_comments_strings_templates_regex_do_not_preserve_deleted_symbols():
    fake = '''// function listTodos() {}\n/* app.get('/todos', handler); */
const text = "function listTodos() {} app.post('/todos', handler);";
const template = `function createTodo() {}`;
const regex = /function listTodos\\(\\)/;
'''
    assert removed(JS, HEALTH_ONLY + fake) == removed(JS, HEALTH_ONLY)
    assert removed(fake, "") == []


def test_chain_multiline_and_literal_template_routes():
    before = """router.route('/todos')
        .get((req, res) => { cache.get('/not-route'); })
        .post(handler);
app.delete(`/todos/:id`, handler);"""
    after = "router.route('/todos').get(handler);"
    assert ("route", "POST /todos") in removed(before, after)
    assert ("route", "DELETE /todos/:id") in removed(before, after)
    assert ("route", "GET /not-route") not in removed(before, after)


def test_renaming_is_a_candidate_and_dynamic_routes_are_not_invented():
    assert removed("function oldName() {}", "function newName() {}") == [("function", "oldName")]
    assert removed("app.get(path, handler); app.get(`/items/${id}`, handler);", "") == []


def test_typescript_named_arrow_and_function_expression():
    before = """export async function fetchTodos<T>(id: string) { return []; }
export const saveTodo = async (id: string): Promise<string> => id;
const worker = function internalName() {};
const identity = value => value;"""
    assert set(removed(before, "", ".ts")) == {
        ("function", "fetchTodos"), ("function", "saveTodo"),
        ("function", "worker"), ("function", "identity"),
    }


def test_duplicate_declarations_are_counted():
    assert removed("function save() {}\nfunction save() {}", "function save() {}") == [("function", "save")]


def test_python_scopes_async_methods_and_decorated_routes():
    before = '''class A:
    def save(self): pass
class B:
    def save(self): pass
@router.get('/todos')
async def list_todos(): pass
@app.route('/todos', methods=['GET', 'POST'])
def todos(): pass
'''
    after = '''class B:
    def save(self): pass
@app.route('/todos', methods=['GET'])
def todos(): pass
'''
    assert set(removed(before, after, ".py")) == {
        ("function", "A.save"), ("function", "list_todos"),
        ("route", "GET /todos"), ("route", "POST /todos"),
    }


def test_python_keyword_route_and_comments():
    assert removed("@app.get(path='/health')\ndef health(): pass", "# def health(): pass\n", ".py") == [
        ("function", "health"), ("route", "GET /health"),
    ]


@pytest.mark.parametrize("suffix,source", [(".py", "def bad(:"), (".js", "function bad() {"), (".ts", 'const s = "bad;')])
def test_invalid_or_truncated_source_is_not_reported_as_safe(suffix, source):
    assert compare_sources("", source, suffix)["status"] == "unavailable"
    assert compare_sources(source, "", suffix)["status"] == "unavailable"


@pytest.mark.parametrize("suffix", [".go", ".md", ".json"])
def test_unsupported_source_is_explicit(suffix):
    assert compare_sources("before", "after", suffix)["status"] == "unavailable"


def test_size_limit_and_read_errors(tmp_path, monkeypatch):
    (tmp_path / "app.js").write_bytes(b" " * (MAX_SOURCE_BYTES + 1))
    ops = [{"file": "app.js", "content": ""}]
    annotate_removals(ops, tmp_path)
    assert ops[0]["removal_check"]["status"] == "unavailable"
    assert compare_sources("", " " * (MAX_SOURCE_BYTES + 1), ".js")["status"] == "unavailable"
    (tmp_path / "app.js").write_text(JS)
    def denied(*args, **kwargs):
        raise PermissionError("denied")
    monkeypatch.setattr(Path, "open", denied)
    annotate_removals(ops, tmp_path)
    assert ops[0]["removal_check"]["status"] == "unavailable"


def test_target_folder_and_request_root_override_action_and_model_report(tmp_path):
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "app.js").write_text(JS)
    (tmp_path / "app.js").write_text(HEALTH_ONLY)
    ops = [{"file": "app.js", "content": HEALTH_ONLY, "action": "create", "removal_check": {"status": "new_file"}}]
    annotate_removals(ops, tmp_path, "nested")
    assert len(ops[0]["removal_check"]["removed"]) == 4
    assert (tmp_path / "nested" / "app.js").read_text() == JS


def test_new_file_and_external_symlink(tmp_path):
    inside = tmp_path / "inside"
    inside.mkdir()
    (tmp_path / "secret.js").write_text(JS)
    try:
        (inside / "link.js").symlink_to(tmp_path / "secret.js")
    except OSError as error:
        if getattr(error, "winerror", None) == 1314:
            pytest.skip("Windows symbolic links require Developer Mode or link privilege")
        raise
    ops = [{"file": "new.js", "content": JS}, {"file": "link.js", "content": ""}]
    annotate_removals(ops, inside)
    assert ops[0]["removal_check"] == {"status": "new_file", "removed": []}
    assert ops[1]["removal_check"]["status"] == "unavailable"
    assert ops[1]["removal_check"]["removed"] == []


@pytest.mark.parametrize("name", ["./app.js", "/app.js", "..\\app.js"])
def test_matches_extension_write_path_normalisation(tmp_path, name):
    (tmp_path / "app.js").write_text(JS)
    ops = [{"file": name, "content": HEALTH_ONLY}]
    annotate_removals(ops, tmp_path)
    assert len(ops[0]["removal_check"]["removed"]) == 4


def test_generation_and_api_deliver_computed_warning_without_writing(tmp_path, monkeypatch):
    from api.routes.analyze import CodeGenerateRequest, generate_code_route

    (tmp_path / "app.js").write_text(JS)
    calls = []
    class Router:
        def call(self, *args, **kwargs):
            calls.append(kwargs)
            return type("Response", (), {"text": json.dumps({
                "summary": "health only", "ops": [{"file": "app.js", "action": "create", "content": HEALTH_ONLY,
                    "removal_check": {"status": "checked", "removed": []}}],
            }), "model_used": "fixture"})()
    monkeypatch.setattr(ca, "get_router", lambda: Router())
    result = asyncio.run(generate_code_route(CodeGenerateRequest(
        instruction="health만 남겨줘", workspace_path=str(tmp_path),
        decisions=[{"id": CONFIRM_DECISION_ID, "chosen_key": "proceed", "options": [{"key": "proceed"}]}],
    )))
    assert len(result["ops"][0]["removal_check"]["removed"]) == 4
    assert len(calls) == 1
    assert (tmp_path / "app.js").read_text() == JS
