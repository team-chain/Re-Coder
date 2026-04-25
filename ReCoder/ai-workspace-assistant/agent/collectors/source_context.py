"""활성 창 기반 소스 코드 컨텍스트 추출 유틸리티."""

from __future__ import annotations

import os
import re
from fnmatch import fnmatch
from pathlib import Path


MAX_SOURCE_FILE_SIZE = 100 * 1024
PROJECT_ROOT_MARKERS = ('.git', 'package.json', 'pyproject.toml', 'Cargo.toml')
SENSITIVE_PATTERNS = ('.env*', '*.pem', '*.key', '*secret*', 'id_rsa*')
SKIP_DIR_NAMES = {'.git', '__pycache__', '.venv', 'venv', 'node_modules', 'dist', 'build', 'target'}
_WINDOWS_ABS_RE = re.compile(r'^[A-Za-z]:[\\/].*')


def extract_file_path_from_window_title(title: str) -> str | None:
    """
    IDE/에디터 창 제목에서 현재 편집 중인 파일 경로를 추출한다.
    지원 패턴:
    - VS Code: "filename.py - project-name - Visual Studio Code"
    - VS Code (수정됨): "● filename.py - project-name - Visual Studio Code"
    - IntelliJ/PyCharm: "project-name - filename.py [path]"
    - Vim/Neovim: "filename.py (+) - VIM" 또는 터미널 제목
    - Sublime Text: "filename.py - project-name - Sublime Text"

    Returns:
        추출된 파일명 또는 None
    """
    raw = (title or '').strip()
    if not raw:
        return None

    normalized = raw.lstrip('●* ').strip()

    jetbrains = re.search(
        r' - (?P<file>[^\[\]]+\.[A-Za-z0-9_+-]+) \[(?P<path>[^\]]+)\]$',
        normalized,
    )
    if jetbrains:
        file_name = jetbrains.group('file').strip()
        base_path = jetbrains.group('path').strip()
        return str(Path(base_path) / file_name) if base_path else file_name

    suffix_patterns = (
        r'^(?P<file>.+?)\s[-—–]\s.+?\s[-—–]\sVisual Studio Code$',
        r'^(?P<file>.+?)\s[-—–]\s.+?\s[-—–]\sSublime Text$',
        r'^(?P<file>.+?)\s[-—–]\s.+?\s[-—–]\sCursor$',
        r'^(?P<file>.+?)\s[-—–]\s.+?\s[-—–]\sWindsurf$',
        r'^(?P<file>.+?)\s[-—–]\s(?:VIM|NVIM|Neovim)$',
    )
    for pattern in suffix_patterns:
        match = re.match(pattern, normalized)
        if match:
            candidate = match.group('file').strip()
            candidate = re.sub(r'\s+\(\+\)$', '', candidate).strip()
            if candidate:
                return candidate

    terminal_editor = re.search(
        r'(?P<file>(?:[A-Za-z]:[\\/]|\.{0,2}/|~?/)?[^\s\'"]+\.[A-Za-z0-9_+-]+)',
        normalized,
    )
    if terminal_editor:
        return terminal_editor.group('file').strip()

    return None


def find_project_root(file_hint: str, cwd: str | None = None) -> str | None:
    """
    파일명 힌트와 현재 작업 디렉토리를 기반으로
    프로젝트 루트 디렉토리를 찾는다.
    .git, package.json, pyproject.toml, Cargo.toml 등의 존재로 판단한다.
    """
    cwd_path = Path(cwd or os.getcwd()).expanduser()
    if cwd_path.is_file():
        cwd_path = cwd_path.parent

    hint_path = Path(file_hint).expanduser()
    candidates: list[Path] = [cwd_path]

    if _looks_like_absolute_path(file_hint):
        abs_hint = _path_from_hint(file_hint)
        if abs_hint is not None:
            candidates.insert(0, abs_hint.parent if abs_hint.is_file() else abs_hint)
    elif any(sep in file_hint for sep in ('/', '\\')):
        candidates.insert(0, (cwd_path / hint_path).parent)

    seen: set[Path] = set()
    for start in candidates:
        for current in (start, *start.parents):
            if current in seen:
                continue
            seen.add(current)
            if any((current / marker).exists() for marker in PROJECT_ROOT_MARKERS):
                return str(current)

    return str(cwd_path) if cwd_path.exists() else None


def resolve_source_file_path(file_hint: str, cwd: str | None = None) -> str | None:
    """파일 힌트와 cwd를 기반으로 실제 읽을 소스 파일 경로를 찾는다."""
    if not file_hint:
        return None

    cwd_path = Path(cwd or os.getcwd()).expanduser()
    if cwd_path.is_file():
        cwd_path = cwd_path.parent
    root_path = Path(find_project_root(file_hint, str(cwd_path)) or cwd_path)

    direct_candidates: list[Path] = []
    hinted_path = _path_from_hint(file_hint)
    if hinted_path is not None:
        direct_candidates.append(hinted_path)
    if any(sep in file_hint for sep in ('/', '\\')):
        direct_candidates.append((cwd_path / Path(file_hint)).expanduser())
        direct_candidates.append((root_path / Path(file_hint)).expanduser())

    checked: set[Path] = set()
    for candidate in direct_candidates:
        candidate = candidate.expanduser()
        if candidate in checked:
            continue
        checked.add(candidate)
        if candidate.exists() and candidate.is_file() and not _should_skip_file(candidate):
            return str(candidate.resolve())

    basename = Path(file_hint).name
    if not basename:
        return None

    matches = _search_file_by_name(root_path, basename)
    if not matches and cwd_path != root_path:
        matches = _search_file_by_name(cwd_path, basename)
    if not matches:
        return None

    matches.sort(key=lambda path: _candidate_sort_key(path, cwd_path, root_path))
    return str(matches[0].resolve())


def read_source_context(
    file_path: str,
    error_line: int | None = None,
    context_lines: int = 30,
    max_chars: int = 3000,
) -> str:
    """
    파일을 읽어 에러 주변 코드를 반환한다.
    error_line이 주어지면 해당 줄 +/- context_lines 범위만 반환한다.
    error_line이 None이면 파일 전체를 max_chars까지 반환한다.
    """
    if not file_path:
        return ''

    path = Path(file_path).expanduser()
    if not path.exists() or not path.is_file():
        return ''
    if _should_skip_file(path):
        return ''

    try:
        text = path.read_text(encoding='utf-8', errors='ignore')
    except Exception:
        return ''

    lines = text.splitlines()
    if error_line is not None and error_line > 0:
        start = max(1, error_line - context_lines)
        end = min(len(lines), error_line + context_lines)
        numbered = _format_numbered_lines(lines[start - 1:end], start)
    else:
        numbered = _format_numbered_lines(lines, 1)

    return _truncate_text(numbered, max_chars)


def _candidate_sort_key(path: Path, cwd_path: Path, root_path: Path) -> tuple[int, int, str]:
    try:
        relative_to_cwd = path.relative_to(cwd_path)
        cwd_score = len(relative_to_cwd.parts)
    except ValueError:
        cwd_score = 1000

    try:
        relative_to_root = path.relative_to(root_path)
        root_score = len(relative_to_root.parts)
    except ValueError:
        root_score = 1000

    return (cwd_score, root_score, str(path))


def _format_numbered_lines(lines: list[str], start_line: int) -> str:
    return '\n'.join(f'{line_no}: {line}' for line_no, line in enumerate(lines, start=start_line))


def _looks_like_absolute_path(value: str) -> bool:
    return value.startswith('/') or value.startswith('\\\\') or bool(_WINDOWS_ABS_RE.match(value))


def _path_from_hint(value: str) -> Path | None:
    if not value:
        return None
    if value.startswith('~/'):
        return Path(value).expanduser()
    if _looks_like_absolute_path(value):
        return Path(value).expanduser()
    return None


def _search_file_by_name(root_path: Path, basename: str, max_depth: int = 5) -> list[Path]:
    if not root_path.exists() or not root_path.is_dir():
        return []

    matches: list[Path] = []
    root_dir = str(root_path)
    for dirpath, dirnames, filenames in os.walk(root_path):
        depth = dirpath.replace(root_dir, '').count(os.sep)
        if depth > max_depth:
            dirnames[:] = []
            continue

        if depth >= max_depth:
            dirnames[:] = []

        dirnames[:] = [name for name in dirnames if name not in SKIP_DIR_NAMES]
        if basename not in filenames:
            continue
        candidate = Path(dirpath) / basename
        if not _should_skip_file(candidate):
            matches.append(candidate)
    return matches


def _should_skip_file(path: Path) -> bool:
    lowered_name = path.name.lower()
    lowered_path = path.as_posix().lower()

    if any(part == '.git' for part in path.parts):
        return True
    if '/.git/' in lowered_path:
        return True
    if any(fnmatch(lowered_name, pattern.lower()) for pattern in SENSITIVE_PATTERNS):
        return True
    if any(fnmatch(lowered_path, pattern.lower()) for pattern in SENSITIVE_PATTERNS):
        return True

    try:
        if path.stat().st_size > MAX_SOURCE_FILE_SIZE:
            return True
    except OSError:
        return True

    return _is_binary_file(path)


def _is_binary_file(path: Path) -> bool:
    try:
        with open(path, 'rb') as f:
            sample = f.read(4096)
    except OSError:
        return True

    if not sample:
        return False
    if b'\x00' in sample:
        return True
    try:
        sample.decode('utf-8')
        return False
    except UnicodeDecodeError:
        return True


def _truncate_text(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    note = '\n... [truncated]'
    return text[: max(0, max_chars - len(note))].rstrip() + note


def get_relevant_source(file_path: str, max_lines: int = 200) -> str:
    """지정된 파일의 내용을 읽어 반환합니다. max_lines를 초과하면 앞뒤 100줄만 포함합니다."""
    if not file_path:
        return ''
    
    path = Path(file_path).expanduser()
    if not path.exists() or not path.is_file():
        return ''
    if _should_skip_file(path):
        return ''
        
    try:
        text = path.read_text(encoding='utf-8', errors='ignore')
    except Exception:
        return ''
        
    lines = text.splitlines()
    if len(lines) <= max_lines:
        return text
        
    half_lines = max_lines // 2
    front = lines[:half_lines]
    back = lines[-half_lines:]
    
    return '\n'.join(front) + '\n\n... (중간 생략) ...\n\n' + '\n'.join(back)
