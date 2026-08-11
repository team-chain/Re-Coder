"""
ReCoder Core — 컨테이너 이미지 빌드 + ECR 업로드

FR-05-04. 카드 「뭘 만들면 되나요」 1·2단계:
"샘플 앱 컨테이너 이미지 빌드", "사용자 계정 ECR 에 push".

이 로직은 원래 `core/ecs_deploy_agent.py` 에 있었지만 그 파일은
`core/server.py`(실행되지 않는 모놀리스)에서만 불린다. 실제 실행 경로인
`main.py → api/routes/ecs.py → agents/ecs_agent.py` 에는 빌드도 push 도
없어서 "이미지가 이미 ECR 에 있다"를 전제하고 있었다. 그래서 배포가
성립하지 않았다. 여기로 옮겨 살아있는 경로에 붙인다.

원본과 달라진 점 두 가지:

1. **ECR 로그인 토큰을 `aws` CLI 가 아니라 boto3 로 받는다.**
   원본은 `aws ecr get-login-password` 를 실행했다. 그러면 사용자 PC 에
   AWS CLI 가 깔려 있어야 하고, 확장이 쓰는 자격증명과 CLI 프로필이
   서로 다를 수 있다. boto3 를 쓰면 나머지 단계와 **같은 자격증명**을
   쓰는 것이 보장된다.

2. **실패가 사람이 읽을 수 있는 문장으로 나온다** (카드 DoD 3번).
   docker 미설치, Dockerfile 없음, 디스크 부족은 원인도 대처법도 다른데
   원본은 셋 다 stderr 를 그대로 흘렸다.
"""

from __future__ import annotations

import base64
import logging
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from core.aws_infra import InfraError, error_message

logger = logging.getLogger(__name__)

DOCKER_BUILD_TIMEOUT = 900   # 15분 — 첫 빌드는 베이스 이미지를 받느라 길다
DOCKER_PUSH_TIMEOUT = 900    # 15분
DOCKER_QUICK_TIMEOUT = 60

#: (returncode, stdout, stderr)
CommandResult = tuple[int, str, str]
Runner = Callable[..., CommandResult]


class BuildError(InfraError):
    """이미지 빌드/업로드 실패."""


@dataclass(frozen=True)
class PushResult:
    """빌드 + 업로드 결과."""

    #: ECR 에 올라간 최종 이미지 주소 (repositoryUri:tag)
    image_uri: str
    #: 로컬에서 만든 태그
    local_tag: str
    digest: Optional[str] = None


def run_command(
    args: Sequence[str],
    *,
    cwd: Optional[str] = None,
    timeout: int = DOCKER_QUICK_TIMEOUT,
    stdin_text: Optional[str] = None,
) -> CommandResult:
    """subprocess 래퍼. 예외를 던지지 않고 (rc, stdout, stderr) 로 돌려준다.

    **인코딩을 명시한다.** `text=True` 만 주면 파이썬이 시스템 로케일로
    디코딩하는데, 한국어 윈도우는 cp949 다. docker 는 진행 표시에 유니코드
    박스 문자(`─` 등)를 쓰기 때문에 cp949 로는 디코딩이 깨진다. 그러면
    subprocess 내부 리더 스레드가 `UnicodeDecodeError` 로 죽고 `proc.stdout`
    이 **None** 이 되어, 여기서 `None.strip()` 이 난다. 실제로 그 오류가
    사용자에게는 `'NoneType' object has no attribute 'strip'` 로 보였다 —
    원인이 인코딩이라는 걸 알아낼 방법이 없는 메시지다.

    `errors="replace"` 까지 두는 이유: 도구가 무슨 바이트를 뱉든 배포가
    그것 때문에 죽으면 안 된다. 읽을 수 없는 글자는 대체 문자로 두고
    나머지 로그를 살린다.
    """
    try:
        proc = subprocess.run(  # noqa: S603 - 인자는 호출부가 구성한다
            list(args),
            cwd=cwd,
            input=stdin_text,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        # 그래도 None 이 올 수 있는 경로(플랫폼별 차이)를 막는다.
        # 진단 메시지가 AttributeError 로 둔갑하는 일이 다시 없도록.
        return (
            proc.returncode,
            (proc.stdout or "").strip(),
            (proc.stderr or "").strip(),
        )
    except subprocess.TimeoutExpired:
        return -1, "", f"{timeout}초 안에 끝나지 않았습니다"
    except FileNotFoundError as exc:
        return -1, "", f"명령을 찾을 수 없습니다: {exc}"
    except OSError as exc:  # 권한, 디스크 등
        return -1, "", str(exc)


# ---------------------------------------------------------------------------
# 사전 점검
# ---------------------------------------------------------------------------


def ensure_docker_available(*, runner: Runner = run_command) -> str:
    """docker 가 설치돼 있고 데몬이 떠 있는지 확인하고 버전을 돌려준다.

    "설치 안 됨"과 "설치는 됐는데 데몬이 안 떠 있음"은 대처가 완전히
    다르다. 둘 다 `docker build` 단계에서 뭉뚱그려 실패하면 사용자는
    무엇을 해야 할지 알 수 없다.
    """
    if shutil.which("docker") is None:
        raise BuildError(
            "이 컴퓨터에서 docker 를 찾지 못했습니다.",
            remedy="Docker Desktop(또는 Docker Engine)을 설치한 뒤 다시 시도하세요.",
        )
    rc, out, err = runner(["docker", "info", "--format", "{{.ServerVersion}}"],
                          timeout=DOCKER_QUICK_TIMEOUT)
    if rc != 0:
        raise BuildError(
            "docker 는 설치돼 있지만 데몬에 연결하지 못했습니다.",
            detail=err or out,
            remedy="Docker Desktop 을 실행한 뒤 다시 시도하세요.",
        )
    return out or "unknown"


def ensure_dockerfile(workspace_path: str, dockerfile: str = "Dockerfile") -> Path:
    """Dockerfile 존재를 확인하고 경로를 돌려준다."""
    root = Path(workspace_path)
    if not root.is_dir():
        raise BuildError(
            f"작업 폴더를 찾을 수 없습니다: {workspace_path}",
            remedy="열려 있는 프로젝트 경로가 맞는지 확인하세요.",
        )
    path = root / dockerfile
    if not path.is_file():
        raise BuildError(
            f"{dockerfile} 이 없습니다: {path}",
            remedy="배포 카드에서 Dockerfile 생성을 먼저 실행하세요.",
        )
    return path


# ---------------------------------------------------------------------------
# 빌드
# ---------------------------------------------------------------------------


def build_image(
    workspace_path: str,
    local_tag: str,
    *,
    dockerfile: str = "Dockerfile",
    platform: str = "linux/amd64",
    runner: Runner = run_command,
) -> str:
    """`docker build` 를 실행하고 로컬 태그를 돌려준다.

    `platform` 을 명시하는 이유: Apple Silicon(arm64) 맥에서 그냥 빌드하면
    arm64 이미지가 나오는데, Fargate 는 기본이 X86_64 라 태스크가
    `exec format error` 로 죽는다. 이 오류는 로그에서 원인을 알아보기
    매우 어려워서, 애초에 플랫폼을 고정한다.
    """
    ensure_dockerfile(workspace_path, dockerfile)
    logger.info("docker build 시작: %s (platform=%s)", local_tag, platform)
    rc, out, err = runner(
        [
            "docker", "build",
            "--platform", platform,
            "-f", dockerfile,
            "-t", local_tag,
            ".",
        ],
        cwd=workspace_path,
        timeout=DOCKER_BUILD_TIMEOUT,
    )
    if rc != 0:
        raise BuildError(
            "컨테이너 이미지 빌드에 실패했습니다.",
            detail=(err or out)[-4000:],
            remedy="위 docker 출력의 마지막 오류 줄을 확인하세요. "
                   "의존성 설치 실패가 가장 흔한 원인입니다.",
        )
    logger.info("docker build 완료: %s", local_tag)
    return local_tag


# ---------------------------------------------------------------------------
# ECR 로그인 + push
# ---------------------------------------------------------------------------


def ecr_login(
    ecr: Any,
    *,
    runner: Runner = run_command,
) -> tuple[str, str]:
    """ECR 로그인 토큰을 받아 docker 에 로그인한다. (registry, username) 반환.

    `aws ecr get-login-password` 대신 boto3 를 쓴다 — 나머지 배포 단계와
    같은 자격증명이 쓰이는 것이 보장된다.
    """
    try:
        resp = ecr.get_authorization_token()
    except Exception as exc:  # noqa: BLE001
        raise BuildError(
            "ECR 로그인 토큰을 받지 못했습니다.",
            detail=error_message(exc),
            remedy="권한표의 ecr:GetAuthorizationToken 이 부여됐는지, "
                   "AWS 자격증명이 만료되지 않았는지 확인하세요.",
        ) from exc

    data = (resp.get("authorizationData") or [])
    if not data:
        raise BuildError(
            "ECR 로그인 토큰이 비어 있습니다.",
            remedy="AWS 자격증명이 올바른 계정을 가리키는지 확인하세요.",
        )
    entry = data[0]
    registry = str(entry.get("proxyEndpoint") or "").replace("https://", "")
    try:
        decoded = base64.b64decode(entry["authorizationToken"]).decode("utf-8")
        username, password = decoded.split(":", 1)
    except Exception as exc:  # noqa: BLE001
        raise BuildError(
            "ECR 로그인 토큰의 형식을 이해하지 못했습니다.",
            detail=str(exc),
        ) from exc

    rc, out, err = runner(
        ["docker", "login", "--username", username, "--password-stdin", registry],
        stdin_text=password,
        timeout=DOCKER_QUICK_TIMEOUT,
    )
    if rc != 0:
        raise BuildError(
            "docker 가 ECR 에 로그인하지 못했습니다.",
            detail=err or out,
            remedy="Docker 데몬이 떠 있는지, 네트워크가 ECR 에 닿는지 확인하세요.",
        )
    logger.info("ECR 로그인 성공: %s", registry)
    return registry, username


def push_image(
    local_tag: str,
    image_uri: str,
    *,
    runner: Runner = run_command,
) -> PushResult:
    """로컬 이미지를 ECR 주소로 태그해 push 한다."""
    rc, out, err = runner(
        ["docker", "tag", local_tag, image_uri], timeout=DOCKER_QUICK_TIMEOUT
    )
    if rc != 0:
        raise BuildError(
            "이미지에 ECR 주소 태그를 붙이지 못했습니다.",
            detail=err or out,
            remedy=f"로컬 이미지 '{local_tag}' 가 실제로 만들어졌는지 확인하세요.",
        )

    logger.info("docker push 시작: %s", image_uri)
    rc, out, err = runner(["docker", "push", image_uri], timeout=DOCKER_PUSH_TIMEOUT)
    if rc != 0:
        combined = f"{err}\n{out}".lower()
        # push 실패의 원인 중 사용자가 스스로 고칠 수 있는 두 가지는
        # 따로 짚어준다. 나머지는 원문을 그대로 보여준다.
        if "denied" in combined or "not authorized" in combined:
            remedy = ("권한표의 ecr:PutImage / ecr:UploadLayerPart 가 부여됐는지, "
                      "AWS 자격증명이 만료되지 않았는지 확인하세요. "
                      "학교 계정은 세션이 4시간마다 끊깁니다.")
        elif "no space left" in combined:
            remedy = "디스크 공간이 부족합니다. `docker system prune` 을 실행해 보세요."
        else:
            remedy = "위 docker 출력의 마지막 오류 줄을 확인하세요."
        raise BuildError(
            "ECR 로 이미지를 올리지 못했습니다.",
            detail=(err or out)[-4000:],
            remedy=remedy,
        )

    digest = _parse_digest(out)
    logger.info("docker push 완료: %s (digest=%s)", image_uri, digest or "?")
    return PushResult(image_uri=image_uri, local_tag=local_tag, digest=digest)


def _parse_digest(push_output: str) -> Optional[str]:
    """`docker push` 출력에서 이미지 다이제스트를 뽑는다. 없으면 None."""
    for line in push_output.splitlines():
        # 예: "latest: digest: sha256:abc... size: 1234"
        if "digest:" in line:
            for token in line.split():
                if token.startswith("sha256:"):
                    return token
    return None


def build_and_push(
    ecr: Any,
    *,
    workspace_path: str,
    repository_uri: str,
    tag: str,
    dockerfile: str = "Dockerfile",
    platform: str = "linux/amd64",
    runner: Runner = run_command,
) -> PushResult:
    """빌드 → 로그인 → push 를 한 번에. 최종 이미지 주소를 돌려준다.

    `repository_uri` 는 `aws_infra.ensure_ecr_repository` 가 돌려준 값이다
    (예: `1234.dkr.ecr.us-east-1.amazonaws.com/recoder-app`).
    """
    if not tag or ":" in tag or "/" in tag:
        raise BuildError(
            f"이미지 태그로 쓸 수 없는 값입니다: {tag!r}",
            remedy="태그에는 ':' 나 '/' 를 넣을 수 없습니다.",
        )
    ensure_docker_available(runner=runner)
    local_tag = f"{repository_uri.rsplit('/', 1)[-1]}:{tag}"
    build_image(
        workspace_path, local_tag, dockerfile=dockerfile,
        platform=platform, runner=runner,
    )
    ecr_login(ecr, runner=runner)
    return push_image(local_tag, f"{repository_uri}:{tag}", runner=runner)
