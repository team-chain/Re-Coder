"""Build proposed files in a disposable, secret-filtered copy; never edit the project."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from context_gate import mask_secrets, scrub_build_source
from grounded_repair.logs import summarize
from grounded_repair.workspace import manifest, safe_path, snapshot
try:
    import proc_tools  # type: ignore
except ImportError:  # pragma: no cover
    from core import proc_tools  # type: ignore


#: 프런트·백엔드를 함께 설치·빌드하는 앱은 첫 빌드(캐시 없음)가 3분을 넘는다 — 시간이 모자라 검증을 못 하면
#: 깨진 결과가 "검증 못 함" 으로 지나간다. 환경변수로 조정할 수 있다.
_BUILD_TIMEOUT = int(os.environ.get("RECODER_VERIFY_BUILD_TIMEOUT", "480"))


def verify_proposal(root: Path, ops: list[dict], *, timeout: int | None = None, run_app: bool = False) -> dict:
    timeout = timeout or _BUILD_TIMEOUT
    base = {"kind": "docker-build", "passed": False, "status": "unavailable"}
    if os.getenv("RECODER_TEST_MODE") == "1":
        return {**base, "output": "Build execution is disabled in unit tests."}
    if not shutil.which("docker"):
        return {**base, "output": "Docker is required for generated build verification."}
    if not any(op["file"] == "Dockerfile" for op in ops) and not (root / "Dockerfile").is_file():
        return {**base, "output": "No Dockerfile; generated code has not been build-verified."}
    with tempfile.TemporaryDirectory(prefix="recoder-generated-") as directory:
        workspace = Path(directory) / "project"
        workspace.mkdir()
        image_file = Path(directory) / "image-id"
        try:
            expected = manifest(root)
            snapshot(root, workspace, expected)
            for op in ops:
                # Never send a generated dotenv/credential file into a build context.
                if any(
                    part.startswith(".env") or part in {".aws", ".ssh", ".npmrc", ".netrc"}
                    for part in Path(op["file"]).parts
                ):
                    continue
                target = safe_path(workspace, op["file"])
                target.parent.mkdir(parents=True, exist_ok=True)
                #: Windows 에서도 LF 그대로 — CRLF 로 바뀐 셸 스크립트는 리눅스 컨테이너에서 실행되지 않는다(2.0.32).
                target.write_text(scrub_build_source(op["content"]), encoding="utf-8", newline="")
            # Existing source can also contain inline credentials; preserve them only
            # in the original workspace, never in the disposable Docker context.
            for path in workspace.rglob("*"):
                if path.is_file():
                    try:
                        text = path.read_bytes().decode("utf-8")
                    except (UnicodeDecodeError, OSError):
                        continue
                    redacted = scrub_build_source(text)
                    if redacted != text:
                        path.write_bytes(redacted.encode("utf-8"))
            #: 2.0.29 — AI 가 쓴 Dockerfile 의 위험한 RUN 은 검증 빌드도 하지 않는다(RUN 은 사용자 PC 에서 돈다).
            try:
                import dockerfile_guard
            except ImportError:  # pragma: no cover
                from core import dockerfile_guard  # type: ignore
            docker_path = workspace / "Dockerfile"
            unsafe = dockerfile_guard.check(docker_path.read_text(encoding="utf-8", errors="replace")) if docker_path.is_file() else []
            if unsafe:
                return {**base, "status": "failed", "passed": False, "exit_code": None,
                        "output": f"Dockerfile 안전 검사에서 막아 빌드하지 않았습니다: {dockerfile_guard.message(unsafe)}",
                        "guard": [v.__dict__ for v in unsafe]}
            with tempfile.TemporaryFile() as output:
                process = proc_tools.run(
                    ["docker", "build", "--progress=plain", "--iidfile", str(image_file), "."],
                    cwd=workspace,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    timeout=timeout,
                    shell=False,
                )
                length = output.tell()
                output.seek(max(0, length - 80_000))
                log = mask_secrets(output.read().decode("utf-8", "replace"))
            unavailable = any(
                marker in log.lower()
                for marker in (
                    "cannot connect to the docker daemon",
                    "is the docker daemon running",
                    "failed to update builder last activity",
                    "error during connect",
                )
            )
            runtime = None
            if run_app and process.returncode == 0 and image_file.is_file():
                #: 빌드가 통과하면 같은 이미지를 ECS(staging) 구성으로 잠깐 띄워 사용자 흐름 API 를 불러 본다(runtime_check).
                try:
                    import runtime_check
                    if runtime_check.enabled():
                        runtime = runtime_check.run(workspace, image_file.read_text(encoding="utf-8").strip())
                        for failure in runtime.get("failures") or []:
                            failure["lines"] = [mask_secrets(line) for line in failure.get("lines") or []]
                except Exception as exc:  # noqa: BLE001 - 실행 점검 실패가 빌드 판정을 바꾸지 않는다
                    runtime = {"status": "unavailable", "note": mask_secrets(str(exc))[:300]}
            return {
                **base,
                **({"runtime": runtime} if runtime else {}),
                "passed": process.returncode == 0,
                "status": (
                    "unavailable"
                    if unavailable
                    else "passed" if process.returncode == 0 else "failed"
                ),
                "exit_code": process.returncode,
                "output": summarize(log).text,
                #: 실패 로그 끝부분 — 파일별 문제(tsc·vite 오류 줄)를 뽑는 데만 쓰고 결과에서는 뺀다.
                **({"log": log[-40_000:]} if process.returncode != 0 else {}),
            }
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            return {**base, "output": mask_secrets(str(exc))[:2000]}
        finally:
            if image_file.is_file():
                image = image_file.read_text(encoding="utf-8").strip()
                if image.startswith("sha256:"):
                    try:
                        subprocess.run(
                            ["docker", "image", "rm", image],
                            capture_output=True,
                            timeout=30,
                            check=False,
                        )
                    except (OSError, subprocess.TimeoutExpired):
                        pass  # Build verdict remains valid even if cache cleanup fails.
