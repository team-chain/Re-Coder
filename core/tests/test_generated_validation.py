import json
from pathlib import Path
from types import SimpleNamespace

import code_agent as ca
import generated_validation as gv
from build_readiness import analyze


def test_copy_sources_and_nested_compiled_entry(tmp_path):
    files = {'package.json': '{}', 'client/src/main.js': '', 'server/src/index.ts': '',
             'Dockerfile': 'FROM node:22\nCOPY client/public ./public\nCOPY ["client/src", "server/src", "/app/"]\nCOPY --from=builder /app/dist ./server/dist\nCMD ["node", "server/dist/index.js"]'}
    issues = analyze(tmp_path, files).issues
    assert [i.code for i in issues].count('DOCKERFILE_COPY_SOURCE_MISSING') == 1
    assert not any(i.code == 'DOCKERFILE_ENTRY_MISSING' for i in issues)


def test_repair_context_keeps_late_problem_files_and_whole_bodies():
    ops = [{'file': f'pkg{i}/package.json', 'content': '{}'} for i in range(5)]
    ops += [{'file': 'client/App.tsx', 'content': 'import Missing from "./Missing";'}]
    context = ca._repair_context(ops, [{'file': 'client/App.tsx', 'message': 'missing import'}], limit=150)
    assert 'import Missing from "./Missing";' in context
    assert context.index('client/App.tsx') < context.find('package.json') or 'package.json' not in context
    assert 'oversize' not in ca._repair_context([{'file': 'oversize', 'content': 'x'*1000}], [], limit=100)


def test_broken_manifest_repair_can_expose_new_errors():
    before = [{'code': 'NODE_PACKAGE_JSON_INVALID', 'severity': 'error', 'file': 'package.json'}]
    after = [{'code': 'NODE_LOCAL_IMPORT_MISSING', 'severity': 'error', 'file': 'a.js'},
             {'code': 'NODE_UNDECLARED_DEPENDENCY', 'severity': 'error', 'file': 'package.json'}]
    assert ca._issue_weight(after) < ca._issue_weight(before)


def test_proposal_build_is_disposable_and_secrets_are_excluded(tmp_path, monkeypatch):
    monkeypatch.delenv('RECODER_TEST_MODE', raising=False)
    monkeypatch.setattr(gv.shutil, 'which', lambda _: '/test/docker')
    (tmp_path / 'app.js').write_text('original')
    (tmp_path / '.env').write_text('PRIVATE_TOKEN=do-not-copy')
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        if command[1] == 'build':
            workspace = Path(kwargs['cwd'])
            assert workspace != tmp_path
            assert not (workspace / '.env').exists()
            assert not (workspace / '.env.production').exists()
            assert (workspace / 'app.js').read_text() == 'changed'
            Path(command[command.index('--iidfile') + 1]).write_text('sha256:fixture')
            kwargs['stdout'].write(b'build complete')
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(gv.subprocess, 'run', run)
    result = gv.verify_proposal(tmp_path, [
        {'file': 'Dockerfile', 'content': 'FROM node:22'},
        {'file': 'app.js', 'content': 'changed'},
        {'file': '.env.production', 'content': 'PRIVATE_TOKEN=do-not-copy'},
    ])
    assert result['status'] == 'passed'
    assert (tmp_path / 'app.js').read_text() == 'original'
    assert calls[-1] == ['docker', 'image', 'rm', 'sha256:fixture']


def test_build_unavailable_is_not_a_pass(tmp_path, monkeypatch):
    monkeypatch.delenv('RECODER_TEST_MODE', raising=False)
    monkeypatch.setattr(gv.shutil, 'which', lambda _: None)
    result = gv.verify_proposal(tmp_path, [])
    assert result['status'] == 'unavailable' and result['passed'] is False


def test_real_build_error_is_sent_back_to_repair_without_dropping_files(tmp_path, monkeypatch):
    base = [{'file': 'Dockerfile', 'content': 'FROM node:22'}, {'file': 'app.js', 'content': 'bad'}]
    seen = []
    class Router:
        def call(self, request, **kwargs):
            seen.append(request.prompt)
            ops = base if len(seen) == 1 else [{'file': 'app.js', 'content': 'fixed'}]
            return SimpleNamespace(text=json.dumps({'summary': 'app', 'ops': ops}), model_used='test', provider='test')
    monkeypatch.setattr(ca, 'get_router', Router)
    monkeypatch.setattr(ca, '_autofix_ops', lambda root, folder, ops: (ops, []))
    monkeypatch.setattr(ca, '_consistency_issues', lambda *args: [])
    def verify(root, folder, ops):
        good = next(o['content'] for o in ops if o['file'] == 'app.js') == 'fixed'
        return {'status': 'passed' if good else 'failed', 'passed': good, 'output': 'SyntaxError fixture'}
    monkeypatch.setattr(ca, '_verify_generated_build', verify)
    decisions = [{'id': 'stack', 'question': 'Stack?', 'chosen_key': 'yes',
                  'options': [{'key': 'yes', 'label': 'Proceed'}, {'key': 'no', 'label': 'Cancel'}]}]
    result = ca.generate_code('app', project_root=str(tmp_path), decisions=decisions)
    assert result['verification']['passed']
    assert any(o['file'] == 'Dockerfile' for o in result['ops'])
    assert 'SyntaxError fixture' in seen[1]


def test_build_redaction_preserves_expressions_messages_and_public_certificates():
    from context_gate import scrub_build_source
    source="newErrors.password='Password is required';\nconst token = result.token;\nconst key='AKIA' + 'not-a-key';"
    assert scrub_build_source(source)==source
    assert 'AKIAIOSFODNN7EXAMPLE' not in scrub_build_source("const key='AKIAIOSFODNN7EXAMPLE';")
    assert scrub_build_source('-----BEGIN CERTIFICATE-----\npublic\n-----END CERTIFICATE-----').startswith('-----BEGIN CERTIFICATE-----')
