"""
Command Safety Layer (설계서 v5 §10.4)

배포·인프라 명령 실행 전에 위험 패턴을 차단하는 화이트리스트 + 블랙리스트 검증기.
AI가 직접 생성한 명령은 절대 실행하지 않고, ReCoder 템플릿이 만든 안전한 명령만 통과시킨다.

사용 예:
    from command_safety import assert_safe_docker_run, assert_safe_command, CommandSafetyError

    args = ["docker", "run", "-d", "-p", "8000:8000", "--name", "app", "myimage:latest"]
    assert_safe_docker_run(args)            # 위험 시 CommandSafetyError 발생

    assert_safe_command("ssh ec2-user@host 'docker load < image.tar.gz'")
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass


class CommandSafetyError(RuntimeError):
    """명령이 안전성 검사를 통과하지 못했을 때 발생."""
    def __init__(self, reason: str, *, pattern: str = "", command: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.pattern = pattern
        self.command = command


@dataclass(frozen=True)
class _Rule:
    name: str
    pattern: re.Pattern
    reason: str


@dataclass(frozen=True)
class CommandSafetyResult:
    allowed: bool
    reason: str = ""
    pattern: str = ""


# 설계 §10.4 — 차단 패턴 (모든 docker / shell 명령에 공통 적용)
_FORBIDDEN_RULES: list[_Rule] = [
    _Rule(
        "privileged",
        re.compile(r'(?<!\w)--privileged(?!\w)'),
        "컨테이너에 모든 호스트 권한을 부여합니다 (--privileged).",
    ),
    _Rule(
        "host_mount",
        # -v /호스트경로:/컨테이너 형태. 상대 경로(./...)는 허용.
        re.compile(r'(?:^|\s)(?:-v|--volume)\s+(?:/[^:\s]+|[A-Z]:\\[^:\s]+):'),
        "호스트 파일시스템을 컨테이너에 직접 마운트합니다 (-v /호스트경로).",
    ),
    _Rule(
        "host_network",
        re.compile(r'(?<!\w)--network[\s=]host\b'),
        "호스트 네트워크 네임스페이스를 공유합니다 (--network host).",
    ),
    _Rule(
        "env_file_dotenv",
        re.compile(r'(?<!\w)--env-file\s+(?:\./?)?\.env(?:\s|$)'),
        ".env 파일을 직접 주입합니다 (--env-file .env).",
    ),
    _Rule(
        "cap_sys_admin",
        re.compile(r'(?<!\w)--cap-add[\s=](?:SYS_ADMIN|ALL)\b'),
        "관리자 권한 capability를 추가합니다 (--cap-add SYS_ADMIN/ALL).",
    ),
    _Rule(
        "rm_rf_root",
        re.compile(r'\brm\s+(?:-[a-zA-Z]*r[a-zA-Z]*\s+|-r\s+|-rf\s+|-fr\s+)/(?!\w)'),
        "시스템 루트 파일 삭제 명령이 포함되어 있습니다 (rm -rf /).",
    ),
    _Rule(
        "fork_bomb",
        re.compile(r':\(\)\s*\{\s*:\|:&\s*\};?:'),
        "fork bomb 패턴이 감지되었습니다.",
    ),
    _Rule(
        "system_prune",
        # 사용자 승인 없이 자동 실행되면 위험. assert_safe_*는 자동 모드용이므로 차단.
        re.compile(r'\bdocker\s+system\s+prune\b'),
        "docker system prune은 자동 실행 금지입니다 (사용자 승인 후에만).",
    ),
    _Rule(
        "shutdown",
        re.compile(r'\b(?:shutdown|reboot|halt|poweroff)\b'),
        "호스트 시스템 종료 명령이 포함되어 있습니다.",
    ),
    _Rule(
        "curl_pipe_sh",
        re.compile(r'curl[^|]*\|\s*(?:sh|bash|zsh)\b'),
        "원격 스크립트를 검증 없이 실행합니다 (curl | sh).",
    ),
    _Rule(
        "wget_pipe_sh",
        re.compile(r'wget[^|]*\|\s*(?:sh|bash|zsh)\b'),
        "원격 스크립트를 검증 없이 실행합니다 (wget | sh).",
    ),
]


def _to_command_string(args) -> str:
    """list[str] 또는 str을 검사용 문자열로 정규화."""
    if isinstance(args, str):
        return args
    if isinstance(args, (list, tuple)):
        try:
            return shlex.join(str(a) for a in args)
        except Exception:
            return " ".join(str(a) for a in args)
    raise TypeError(f"unsupported command type: {type(args).__name__}")


def check_command(args) -> list[_Rule]:
    """위반된 규칙 목록을 반환한다 (빈 리스트면 안전)."""
    text = _to_command_string(args)
    return [rule for rule in _FORBIDDEN_RULES if rule.pattern.search(text)]


def validate_command(args, *, docker: bool = False) -> CommandSafetyResult:
    """예외를 던지지 않고 명령 안전성 결과를 반환한다."""
    try:
        if docker:
            assert_safe_docker_run(args)
        else:
            assert_safe_command(args)
        return CommandSafetyResult(True)
    except CommandSafetyError as exc:
        return CommandSafetyResult(False, exc.reason, exc.pattern)


def assert_safe_command(args) -> None:
    """명령이 안전한지 검사하고, 위반 시 CommandSafetyError 발생."""
    violations = check_command(args)
    if not violations:
        return
    rule = violations[0]
    raise CommandSafetyError(
        rule.reason,
        pattern=rule.name,
        command=_to_command_string(args)[:500],
    )


# ── docker 전용 화이트리스트 ──────────────────────────────────────────
# AI가 생성한 docker 명령은 신뢰하지 않으므로, 사용 가능한 동사를 명시적으로 제한.
_ALLOWED_DOCKER_VERBS = frozenset({
    "build", "run", "stop", "start", "rm", "ps", "logs", "exec",
    "pull", "push", "tag", "save", "load", "image", "compose",
    "inspect", "version", "info", "network", "volume",
})


def assert_safe_docker_run(args) -> None:
    """docker 명령에 대해 (1) 동사 화이트리스트, (2) 공통 위험 패턴 모두 검사."""
    if isinstance(args, str):
        try:
            tokens = shlex.split(args)
        except ValueError:
            raise CommandSafetyError("명령 토큰화 실패", command=args[:200])
    else:
        tokens = [str(a) for a in args]

    # docker 명령인지 확인
    cmd_idx = next((i for i, t in enumerate(tokens) if t.endswith("docker")), -1)
    if cmd_idx == -1:
        # docker 명령이 아니면 일반 검사만
        assert_safe_command(args)
        return

    # 동사 검사
    if cmd_idx + 1 >= len(tokens):
        raise CommandSafetyError("docker 명령에 동사가 없습니다.", command=" ".join(tokens))
    verb = tokens[cmd_idx + 1]
    # docker compose / docker image 같은 서브커맨드도 1차 동사로 통과
    if verb not in _ALLOWED_DOCKER_VERBS:
        raise CommandSafetyError(
            f"docker {verb}은 허용되지 않는 동사입니다.",
            pattern="docker_verb",
            command=" ".join(tokens)[:500],
        )

    assert_safe_command(args)
