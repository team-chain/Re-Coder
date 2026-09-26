"""GitHub 푸시·저장소 생성의 자동 커밋이 민감한 파일을 올리지 않는다(보안 검토 P0)."""
import subprocess

import pytest

import github_agent as gh


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setattr(gh.GitHubAgent, '_load_token', lambda self: None)
    agent = gh.GitHubAgent()
    run = lambda *a: subprocess.run(['git', '-C', str(tmp_path), *a], capture_output=True, text=True, check=True)
    run('init', '-q', '-b', 'main')
    run('config', 'user.name', 't'); run('config', 'user.email', 't@example.test')
    (tmp_path / 'app.js').write_text('console.log(1)\n')
    (tmp_path / '.env.production').write_text('DB_PASSWORD=hunter2hunter2\n')
    (tmp_path / 'id_ed25519').write_text('-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n-----END OPENSSH PRIVATE KEY-----\n')
    (tmp_path / 'credentials.json').write_text('{}')
    (tmp_path / 'config.js').write_text('const key = "AKIA' + 'ABCDEFGHIJKLMNOP";\n')
    return agent, tmp_path, run


def test_안전한_스테이징은_민감한_이름과_시크릿_내용을_뺀다(repo):
    agent, ws, run = repo
    excluded = agent._stage_safely(ws)
    staged = set(run('diff', '--cached', '--name-only').stdout.split())
    assert staged == {'app.js'}
    assert {'.env.production', 'id_ed25519', 'config.js'} <= set(excluded)


def test_삭제된_추적_파일도_반영된다(repo):
    agent, ws, run = repo
    run('add', 'app.js'); run('commit', '-q', '-m', 'init')
    (ws / 'app.js').unlink()
    agent._stage_safely(ws)
    assert 'app.js' in run('diff', '--cached', '--name-only').stdout
