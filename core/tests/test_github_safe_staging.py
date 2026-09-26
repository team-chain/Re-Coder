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


def test_설정_견본과_토큰없는_npmrc_는_올린다(repo):
    agent, ws, run = repo
    (ws / '.env.example').write_text('DB_PASSWORD=\n')
    (ws / '.npmrc').write_text('legacy-peer-deps=true\n')
    (ws / 'sub').mkdir()
    (ws / 'sub' / '.npmrc').write_text('//registry.npmjs.org/:_authToken=npm_abcdefghijklmnop\n')
    (ws / 'jwt.test.js').write_text('const t = "eyJhbGciOiJIUzI1.eyJzdWIiOiIxMjM0NTY3.SflKxwRJSMeKKF2QT4f";\n')
    excluded = agent._stage_safely(ws)
    staged = set(run('diff', '--cached', '--name-only').stdout.split())
    assert {'.env.example', '.npmrc', 'jwt.test.js', 'app.js'} <= staged
    assert 'sub/.npmrc' in excluded


def test_pathspec_문법처럼_보이는_이름도_함께_올라간다(repo):
    agent, ws, run = repo
    (ws / ':memo.txt').write_text('memo\n')
    (ws / '한글 파일.txt').write_text('안녕\n')
    (ws / 'file[1].txt').write_text('x\n')
    agent._stage_safely(ws)
    staged = set(run('-c', 'core.quotepath=false', 'diff', '--cached', '--name-only', '-z').stdout.split('\0'))
    assert {':memo.txt', '한글 파일.txt', 'file[1].txt', 'app.js'} <= staged


def test_올릴_변경이_전부_제외되면_커밋없이_푸시한다(repo, monkeypatch):
    agent, ws, run = repo
    run('add', 'app.js'); run('commit', '-q', '-m', 'init')
    run('remote', 'add', 'origin', 'https://github.com/owner/repo.git')
    for name in ('app.js', 'config.js', 'credentials.json', 'id_ed25519'):
        p = ws / name
        if name != 'app.js':
            p.unlink()
    agent._token = 'ghp_fixture'
    real_git = agent._git
    pushed = []

    def fake_git(cwd, args, timeout=60, env=None):
        if 'push' in args:
            pushed.append(args)
            return 0, '', ''
        return real_git(cwd, args, timeout=timeout, env=env)

    monkeypatch.setattr(agent, '_git', fake_git)
    result = agent.push(str(ws), branch='main')
    assert result['status'] != 'error', result
    assert pushed
    assert '.env.production' in result.get('excluded_files', [])
