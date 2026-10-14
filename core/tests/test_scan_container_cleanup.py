"""스캐너 컨테이너가 시간 초과·취소 뒤에도 남지 않는다."""
import asyncio

from agents import infra_agent


def test_docker_run_gets_a_removable_name():
    cmd, name = infra_agent._named_docker_run(["docker", "run", "--rm", "aquasec/trivy", "image", "x"])
    assert name and name.startswith("recoder-scan-")
    assert cmd[:4] == ["docker", "run", "--name", name]
    same, none = infra_agent._named_docker_run(["git", "status"])
    assert same == ["git", "status"] and none is None


def test_timeout_removes_named_container(monkeypatch):
    calls = []

    class FakeProc:
        returncode = None

        async def communicate(self, input=None):
            await asyncio.sleep(10)

        def kill(self):
            self.returncode = -9

        async def wait(self):
            return self.returncode

    class Done:
        returncode = 0

        async def wait(self):
            return 0

    async def fake_exec(*cmd, **kwargs):
        calls.append(list(cmd))
        return FakeProc() if cmd[1] == "run" else Done()

    monkeypatch.setattr(infra_agent.asyncio, "create_subprocess_exec", fake_exec)
    rc, _, err = asyncio.run(infra_agent.InfraAgent._run_subprocess(["docker", "run", "--rm", "img"], timeout=0.05))
    assert rc == -1 and "timed out" in err
    name = calls[0][3]
    assert calls[-1] == ["docker", "rm", "-f", name]


def test_outer_timeout_is_longer_than_scanner_timeout():
    from api.routes import deploy
    assert deploy._TRIVY_OUTER_TIMEOUT > infra_agent.TRIVY_TIMEOUT_SECONDS
