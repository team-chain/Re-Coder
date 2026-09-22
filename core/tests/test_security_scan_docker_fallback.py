"""보안 스캐너 Docker 폴백 — 바이너리가 없어도 docker 가 있으면 검사한다 (실기기 C3).

맥에서 로컬 Docker 배포는 `docker run aquasec/trivy` 로 잘 검사됐는데, ECS 경로의
이 모듈은 네이티브 바이너리만 찾아 trivy·gitleaks·hadolint 셋 다 "미설치" 로
배포를 막았다. 같은 컴퓨터에서 경로에 따라 검사 가능 여부가 갈리는 건 우리 문제다.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

import security_scan as ss


class _Capture:
    """`_run_cmd` 를 가로채 명령을 기록하고, 도구별 가짜 출력을 만든다."""

    def __init__(self, tmp_path: Path):
        self.calls: list[dict] = []
        self.tmp_path = tmp_path

    @staticmethod
    def _host_path(cmd, container_path: str) -> str:
        """컨테이너 안 경로(`/out/…`)를 호스트의 마운트 경로로 되돌린다.

        `-v "<호스트>:/out"` 에서 호스트 쪽을 떼어낼 때 `split(":")[0]` 을 쓰면
        **Windows 에서 드라이브 문자가 잘린다** — `C:/Temp/x:/out` 이 `"C"` 가
        되어 존재하지 않는 곳에 결과를 쓰고, 제품 코드는 보고서를 못 찾아
        `FileNotFoundError` → `<tool>_not_installed` 로 떨어진다. 폴백이 멀쩡히
        도는데도 "도구 미설치" 로 보였다(2026-09-22 실기기 Windows).
        드라이브 문자를 건드리지 않게 **마운트 접미사만** 잘라낸다.
        """
        if not container_path.startswith("/out/"):
            return container_path
        suffix = ":/out"
        host_dir = next(a[: -len(suffix)] for a in cmd if a.endswith(suffix))
        return os.path.join(host_dir, os.path.basename(container_path))

    async def __call__(self, cmd, allow_nonzero=False, stdin_data=None, timeout=None):
        self.calls.append({"cmd": list(cmd), "stdin": stdin_data, "timeout": timeout})
        joined = " ".join(cmd)
        # trivy: --output 뒤 경로(컨테이너면 /out/trivy.json → 호스트 마운트 경로로 변환)에 빈 결과를 쓴다
        if "--output" in cmd:
            out = self._host_path(cmd, cmd[cmd.index("--output") + 1])
            Path(out).write_text(json.dumps({"Results": []}))
            return ""
        if "--report-path" in cmd:
            out = self._host_path(cmd, cmd[cmd.index("--report-path") + 1])
            Path(out).write_text("[]")
            return ""
        if "hadolint" in joined:
            return "[]"
        return ""


def _tools(monkeypatch, *, native: set[str], docker: bool):
    def which(binary: str) -> bool:
        if binary == "docker":
            return docker
        return binary in native
    monkeypatch.setattr(ss, "_which", which)


def test_컨테이너_출력경로_되돌리기는_Windows_드라이브_문자를_안_자른다():
    """이 하네스가 `-v "<호스트>:/out"` 에서 호스트 경로를 되찾는 방식 자체를 고정.

    `split(":")[0]` 이던 시절 Windows 에서 `C:\\Temp\\x` 가 `"C"` 로 잘려,
    폴백이 정상인데도 결과 파일을 못 찾아 `<tool>_not_installed` 가 나왔다.
    하네스가 틀리면 제품 테스트 전체가 거짓 실패한다 — 여기서 먼저 막는다.
    """
    win = ["docker", "run", "--rm", "-v", r"C:\Users\dy981\Temp\recoder-trivy-1:/out",
           "aquasec/trivy:latest", "--output", "/out/trivy.json", "img"]
    assert _Capture._host_path(win, "/out/trivy.json") == os.path.join(
        r"C:\Users\dy981\Temp\recoder-trivy-1", "trivy.json")

    posix = ["docker", "run", "--rm", "-v", "/tmp/recoder-trivy-1:/out",
             "aquasec/trivy:latest", "--report-path", "/out/gitleaks.json", "img"]
    assert _Capture._host_path(posix, "/out/gitleaks.json") == os.path.join(
        "/tmp/recoder-trivy-1", "gitleaks.json")

    #: 네이티브 경로(컨테이너가 아님)는 그대로 둔다.
    assert _Capture._host_path([], "/home/me/out/trivy.json") == "/home/me/out/trivy.json"


def test_바이너리_없고_docker_있으면_trivy_를_컨테이너로_돌린다(monkeypatch, tmp_path):
    _tools(monkeypatch, native=set(), docker=True)
    cap = _Capture(tmp_path)
    monkeypatch.setattr(ss.SecurityScanner, "_run_cmd", staticmethod(cap))
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIA-test")
    monkeypatch.delenv("AWS_SESSION_TOKEN", raising=False)

    findings = asyncio.run(ss.SecurityScanner()._run_trivy("123.dkr.ecr.ap-northeast-2.amazonaws.com/app:v1"))

    cmd = cap.calls[0]["cmd"]
    assert cmd[:3] == ["docker", "run", "--rm"]
    assert "/var/run/docker.sock:/var/run/docker.sock" in cmd, "로컬 이미지를 못 본다"
    assert ss._DOCKER_IMAGES["trivy"] in cmd
    assert cmd[-1] == "123.dkr.ecr.ap-northeast-2.amazonaws.com/app:v1"
    # 자격증명은 값이 아니라 **이름만** 넘긴다
    assert "AWS_ACCESS_KEY_ID" in cmd and "AKIA-test" not in " ".join(cmd)
    assert "AWS_SESSION_TOKEN" not in cmd, "비어 있는 변수까지 넘기면 docker 가 빈 값으로 덮어쓴다"
    assert cap.calls[0]["timeout"] == ss._TRIVY_TIMEOUT, "DB 다운로드가 2분 안에 안 끝난다"
    assert not any(f.title.endswith("_not_installed") for f in findings)


def test_바이너리가_있으면_네이티브를_쓴다(monkeypatch, tmp_path):
    _tools(monkeypatch, native={"trivy", "hadolint", "gitleaks"}, docker=True)
    cap = _Capture(tmp_path)
    monkeypatch.setattr(ss.SecurityScanner, "_run_cmd", staticmethod(cap))

    asyncio.run(ss.SecurityScanner()._run_trivy("app:latest"))

    assert cap.calls[0]["cmd"][0] == "trivy"


def test_둘_다_없으면_예전처럼_미설치로_남겨_게이트가_막는다(monkeypatch, tmp_path):
    _tools(monkeypatch, native=set(), docker=False)

    async def _missing(*_a, **_k):
        raise FileNotFoundError("binary not installed")

    monkeypatch.setattr(ss.SecurityScanner, "_run_cmd", staticmethod(_missing))

    findings = asyncio.run(ss.SecurityScanner()._run_trivy("app:latest"))

    assert [f.title for f in findings] == ["trivy_not_installed"]


def test_hadolint_폴백은_Dockerfile_을_표준입력으로_넘긴다(monkeypatch, tmp_path):
    _tools(monkeypatch, native=set(), docker=True)
    cap = _Capture(tmp_path)
    monkeypatch.setattr(ss.SecurityScanner, "_run_cmd", staticmethod(cap))
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM alpine\n")

    findings = asyncio.run(ss.SecurityScanner()._run_hadolint(str(dockerfile)))

    call = cap.calls[0]
    assert call["cmd"][:4] == ["docker", "run", "--rm", "-i"]
    assert ss._DOCKER_IMAGES["hadolint"] in call["cmd"]
    assert call["cmd"][-1] == "-", "표준입력을 읽게 해야 한다"
    #: 줄끝은 플랫폼마다 다르다 — Windows 의 `write_text` 는 CRLF 로 쓴다.
    #: 제품은 `read_bytes()` 로 **파일을 그대로** 넘기므로, 기대값도 리터럴이
    #: 아니라 그 파일의 바이트여야 한다(2026-09-22 실기기 Windows).
    assert call["stdin"] == dockerfile.read_bytes()
    assert findings == []


def test_gitleaks_폴백은_저장소를_읽기전용으로_마운트한다(monkeypatch, tmp_path):
    _tools(monkeypatch, native=set(), docker=True)
    cap = _Capture(tmp_path)
    monkeypatch.setattr(ss.SecurityScanner, "_run_cmd", staticmethod(cap))
    repo = tmp_path / "repo"
    repo.mkdir()

    findings = asyncio.run(ss.SecurityScanner()._run_gitleaks(str(repo)))

    cmd = cap.calls[0]["cmd"]
    assert cmd[:3] == ["docker", "run", "--rm"]
    assert f"{repo}:/repo:ro" in cmd, "소스는 읽기 전용이어야 한다"
    assert ss._DOCKER_IMAGES["gitleaks"] in cmd
    assert "--source" in cmd and cmd[cmd.index("--source") + 1] == "/repo"
    assert "--quiet" not in cmd, "gitleaks 에 없는 플래그 — 검사가 통째로 실패한다"
    assert findings == []


def test_gitleaks_가_0이_아닌_코드로_끝나면_시크릿_없음으로_읽지_않는다(monkeypatch, tmp_path):
    """`--exit-code 0` 이라 시크릿이 있어도 0 이다. 0 이 아니면 검사 실패다."""
    _tools(monkeypatch, native={"gitleaks"}, docker=False)

    async def _boom(*_a, **_k):
        raise RuntimeError("gitleaks failed (rc=126): unknown flag")

    monkeypatch.setattr(ss.SecurityScanner, "_run_cmd", staticmethod(_boom))

    findings = asyncio.run(ss.SecurityScanner()._run_gitleaks(str(tmp_path)))

    assert [f.title for f in findings] == ["gitleaks_scan_failed"]
