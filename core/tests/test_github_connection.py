import asyncio
import base64

import pytest

import github_agent as gh
from api.routes import github


@pytest.fixture
def agent(monkeypatch):
    monkeypatch.setattr(gh.GitHubAgent, '_load_token', lambda self: None)
    value = gh.GitHubAgent()
    value._token = 'fixture-token-not-a-real-key'
    value._user_cache = {'login': 'tester'}
    return value


@pytest.mark.parametrize('url', ['org/repo', 'https://github.com/org/repo.git', 'git@github.com:org/repo.git', 'ssh://git@github.com/org/repo.git'])
def test_remote_formats(url):
    assert gh._parse_owner_repo(url) == ('org', 'repo')


@pytest.mark.parametrize('url', ['https://evil.test/org/repo', 'https://github.com/../repo', 'git@evil.test:org/repo', 'org/../repo'])
def test_reject_other_hosts_and_invalid_paths(url):
    with pytest.raises(ValueError):
        gh._parse_owner_repo(url)


def test_new_repo_is_private_empty_and_does_not_run_git(agent, monkeypatch):
    calls = []
    def request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        return (404, {}) if method == 'GET' else (201, {})
    monkeypatch.setattr(gh, '_http', request)
    monkeypatch.setattr(agent, '_git', lambda *a, **kw: pytest.fail('Connecting must not commit/push'))
    assert agent.connect_repository('tester/new-repo', True)['status'] == 'ok'
    assert calls[1][1] == '/user/repos'
    assert calls[1][2]['payload'] == {'name': 'new-repo', 'private': True, 'auto_init': False}


@pytest.mark.parametrize('code,body,create', [(403, {}, True), (404, {}, False), (200, {'permissions': {'push': False}}, False), (200, {'permissions': {'push': True}, 'archived': True}, False), (200, {'permissions': {'push': True}}, True)])
def test_connection_failures_never_create_or_publish(agent, monkeypatch, code, body, create):
    def request(method, *a, **kw):
        assert method == 'GET'
        return code, body
    monkeypatch.setattr(gh, '_http', request)
    assert agent.connect_repository('tester/repo', create)['status'] == 'error'


def test_existing_repo_with_push_access(agent, monkeypatch):
    monkeypatch.setattr(gh, '_http', lambda *a, **kw: (200, {'permissions': {'push': True}}))
    assert agent.connect_repository('tester/repo')['status'] == 'ok'


@pytest.mark.parametrize('failure', [False, True])
def test_push_preserves_origin_and_never_writes_credentials(agent, monkeypatch, tmp_path, failure):
    (tmp_path / '.git').mkdir()
    calls = []
    def git(cwd, args, **kwargs):
        calls.append((args, kwargs))
        if args[:3] == ['remote', 'get-url', 'origin']:
            return 0, 'git@github.com:tester/repo.git', ''
        if 'push' in args:
            assert agent._token not in ' '.join(args)
            env = kwargs['env']
            assert env['GIT_CONFIG_KEY_0'] == 'http.https://github.com/.extraheader'
            assert base64.b64encode(f'x-access-token:{agent._token}'.encode()).decode() in env['GIT_CONFIG_VALUE_0']
            return (1, '', agent._token) if failure else (0, 'ok', '')
        pytest.fail(f'Unexpected git command: {args}')
    monkeypatch.setattr(agent, '_git', git)
    result = agent.push(str(tmp_path), 'main', auto_commit=False)
    assert result['status'] == ('error' if failure else 'ok')
    assert agent._token not in result['message']
    assert len(calls) == 2


def test_unauthenticated_push_is_an_actionable_error(agent, monkeypatch):
    agent._token = ''
    monkeypatch.setattr(gh, 'get_github_agent', lambda: agent)
    result = asyncio.run(github.git_push_route({'workspace_path': '/fixture', 'auto_commit': False}))
    assert result['status'] == 'error'
    assert 'GitHub 로그인' in result['message']
