"""A stalled Docker Desktop info/plugin query must not block a working engine."""
import subprocess
import pytest
import docker_autostart as da


@pytest.mark.parametrize('exit_code,output,ready',[(0,b'29.3.1\n',True),(0,b'',False),(1,b'client-only',False)])
def test_probe_requires_an_actual_server_response(monkeypatch,exit_code,output,ready):
    def run(cmd,**kwargs):
        assert cmd==['docker','version','--format','{{.Server.Version}}']
        assert kwargs['timeout']==5.0
        return subprocess.CompletedProcess(cmd,exit_code,output,b'')
    monkeypatch.setattr(da.subprocess,'run',run)
    assert da.daemon_up() is ready


def test_engine_timeout_is_not_a_success(monkeypatch):
    def run(cmd,**kwargs):raise subprocess.TimeoutExpired(cmd,kwargs['timeout'])
    monkeypatch.setattr(da.subprocess,'run',run)
    assert da.daemon_up() is False
