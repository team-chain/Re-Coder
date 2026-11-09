import os
import pytest
import aws_browser


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    for key in (*aws_browser.ENV_KEYS, 'AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AWS_SESSION_TOKEN', 'AWS_PROFILE', 'AWS_DEFAULT_PROFILE', 'AWS_REGION', 'AWS_DEFAULT_REGION'):
        monkeypatch.delenv(key, raising=False)


@pytest.mark.parametrize('endpoint', ['https://evil.example/credentials', 'http://localhost:1234/credentials', 'http://127.0.0.1:1234/credentials?secret=x', 'http://127.0.0.1:1234/other', 'http://user:pass@127.0.0.1:1234/credentials'])
def test_bridge_cannot_be_an_arbitrary_credential_endpoint(endpoint):
    with pytest.raises(ValueError):
        aws_browser.configure(endpoint, 'x' * 64, 'ap-northeast-2', '123456789012')
    assert not aws_browser.active()


def test_selected_account_disables_ambient_profile_and_checks_identity(monkeypatch):
    monkeypatch.setenv('AWS_PROFILE', 'other-account')
    monkeypatch.setenv('AWS_ACCESS_KEY_ID', 'old-key')
    aws_browser.configure('http://127.0.0.1:1234/credentials', 'x' * 64, 'ap-northeast-2', '123456789012')
    assert aws_browser.active()
    assert 'AWS_PROFILE' not in os.environ
    assert 'AWS_ACCESS_KEY_ID' not in os.environ
    assert os.environ['AWS_SHARED_CREDENTIALS_FILE'] == os.devnull
    aws_browser.validate_account('123456789012')
    with pytest.raises(ValueError):
        aws_browser.validate_account('999999999999')
    aws_browser.leave()
    assert not aws_browser.active()
    assert 'AWS_CONTAINER_AUTHORIZATION_TOKEN' not in os.environ
