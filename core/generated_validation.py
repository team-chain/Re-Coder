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


def verify_proposal(root: Path, ops: list[dict], *, timeout: int = 180) -> dict:
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
                target.write_text(scrub_build_source(op["content"]), encoding="utf-8")
            # Existing source can also contain inline credentials; preserve them only
            # in the original workspace, never in the disposable Docker context.
            for path in workspace.rglob("*"):
                if path.is_file():
                    try:
                        text = path.read_text(encoding="utf-8")
                    except UnicodeDecodeError:
                        continue
                    redacted = scrub_build_source(text)
                    if redacted != text:
                        path.write_text(redacted, encoding="utf-8")
            with tempfile.TemporaryFile() as output:
                process = subprocess.run(
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
            return {
                **base,
                "passed": process.returncode == 0,
                "status": (
                    "unavailable"
                    if unavailable
                    else "passed" if process.returncode == 0 else "failed"
                ),
                "exit_code": process.returncode,
                "output": summarize(log).text,
            }
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            return {**base, "output": mask_secrets(str(exc))[:2000]}
        finally:
            if image_file.is_file():
                image = image_file.read_text().strip()
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
