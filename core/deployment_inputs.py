"""Stable Docker defaults derived from user workspace names."""
import hashlib
import re
from pathlib import Path


def dockerfile_runtime_port(dockerfile: Path) -> int | None:
    """Use only literal TCP ports exposed by the final stage (including aliases)."""
    try:
        text = dockerfile.read_text(encoding='utf-8-sig')
    except (OSError, UnicodeError):
        return None
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
