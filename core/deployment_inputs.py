"""Stable Docker defaults derived from user workspace names."""
import hashlib
import os
import re
import subprocess
from pathlib import Path

#: 로컬 배포 컨테이너에 붙이는 라벨 — 어느 프로젝트 폴더의 컨테이너인지 구분한다.
WORKSPACE_LABEL = 'recoder.workspace'


def dockerfile_runtime_port(dockerfile: Path) -> int | None:
    """Use only literal TCP ports exposed by the final stage (including aliases)."""
    try:
        text = dockerfile.read_text(encoding='utf-8-sig')
    except (OSError, UnicodeError):
        return None
    return dockerfile_text_runtime_port(text)


def dockerfile_text_runtime_port(text: str) -> int | None:
    """`dockerfile_runtime_port` 와 같은 규칙으로 Dockerfile **본문**에서 포트를 읽는다."""
    stages: dict[str, list[int]] = {}
    ports: list[int] = []
    alias = None
    for line in re.sub(r'\\\r?\n', ' ', text).splitlines():
        words = line.split('#', 1)[0].split()
        if not words:
            continue
        if words[0].upper() == 'FROM':
            if alias:
                stages[alias] = ports[:]
            args = [word for word in words[1:] if not word.startswith('--')]
            ports = stages.get(args[0].lower(), [])[:] if args else []
            alias = args[2].lower() if len(args) == 3 and args[1].upper() == 'AS' else None
        elif words[0].upper() == 'EXPOSE':
            for value in words[1:]:
                if re.fullmatch(r'\d{1,5}(?:/tcp)?', value, re.IGNORECASE):
                    port = int(value.split('/')[0])
                    if 1 <= port <= 65535 and port not in ports:
                        ports.append(port)
    # Multiple ports require the user's selection, not a guess.
    return ports[0] if len(ports) == 1 else None


def workspace_container_name(workspace: str) -> str:
    name = Path(workspace).name.lower().replace(' ', '-') if workspace else 'app'
    clean = re.sub(r'[^a-z0-9_.-]', '-', name).strip('_.-')
    if clean != name or not clean:
        suffix = hashlib.sha256(name.encode('utf-8')).hexdigest()[:8]
        clean = f'{clean[:96] or "recoder-app"}-{suffix}'
    return clean[:120]


def workspace_fingerprint(workspace: str) -> str:
    """프로젝트 폴더의 절대 경로로 만든 짧은 식별자(대소문자·구분자 차이는 같은 폴더로 본다)."""
    try:
        resolved = str(Path(workspace).expanduser().resolve())
    except (OSError, RuntimeError):
        resolved = str(workspace)
    return hashlib.sha256(os.path.normcase(resolved).encode('utf-8')).hexdigest()[:8]


def _container_workspace_label(name: str) -> str | None:
    """컨테이너 `name` 의 프로젝트 라벨. 컨테이너가 없거나 docker 를 못 부르면 None, 라벨이 없으면 ''."""
    try:
        out = subprocess.run(
            ['docker', 'inspect', '-f', '{{index .Config.Labels "%s"}}' % WORKSPACE_LABEL, name],
            shell=False, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=8,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    label = (out.stdout or '').strip()
    return '' if label in ('', '<no value>') else label


def workspace_deploy_name(workspace: str) -> str:
    """로컬 배포의 컨테이너·이미지 이름.

    폴더 이름이 같은 **다른** 프로젝트(C:\\work\\shop 과 D:\\other\\shop)가 같은 컨테이너·DB 를 덮어쓰지 않도록,
    같은 이름의 컨테이너가 다른 폴더의 것이면 경로 식별자를 붙인다. 라벨이 없는 예전 컨테이너는 같은 프로젝트로 본다.
    """
    base = workspace_container_name(workspace)
    if not workspace:
        return base
    label = _container_workspace_label(base)
    fingerprint = workspace_fingerprint(workspace)
    if label and label != fingerprint:
        return f'{base[:111]}-{fingerprint}'
    return base
