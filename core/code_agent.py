"""
Code Agent — LLM Provider(router) 를 통한 PatchProposal 생성 + base_sha256 검증 + 파일 적용 + 백업.
사용자 클릭 시 1회 호출. 자동 트리거 없음.

v6.4 변경사항:
- AnalyzeRequest 객체를 입력으로 받음
- PatchProposal에 approval_level=1 추가
- router를 통해서만 LLM 호출 (Gemini 직접 호출 제거)
"""

from __future__ import annotations

import difflib
import hashlib
import json
import logging
import os
import posixpath
import re
import uuid
from datetime import datetime
from pathlib import Path

from llm.base import LLMRequest, LLMError, LLMErrorType
from llm.router import get_router
from code_output import CODE_OUTPUT_SCHEMA, CodeOutputError, parse_code_output
from schemas import AnalyzeRequest, FilePatch, PatchProposal, RiskLevel

log = logging.getLogger(__name__)

try:  # main.py 스택(core 를 sys.path 로) / 패키지 실행 양쪽 지원
    from adr import (
        ADR_DIR,
        MAX_ALTERNATIVES,
        MAX_DECISIONS,
        MAX_FIELD_CHARS,
        MAX_LIST_ITEMS,
        RESERVED_ID_PREFIX,
        NormalizedDecision,
        build_adr_ops,
        canonical_key,
        normalize_decisions,
    )
except ImportError:  # pragma: no cover
    from core.adr import (
        ADR_DIR,
        MAX_ALTERNATIVES,
        MAX_DECISIONS,
        MAX_FIELD_CHARS,
        MAX_LIST_ITEMS,
        RESERVED_ID_PREFIX,
        NormalizedDecision,
        build_adr_ops,
        canonical_key,
        normalize_decisions,
    )

BACKUP_DIR = Path.home() / '.recoder' / 'backups'
_LAST_APPLY_BACKUPS: dict[str, list[dict]] = {}

# 재귀 탐색 시 건너뛸 디렉터리
_SKIP_DIRS = {
    '.git', '.hg', '.svn',
    'node_modules', 'venv', '.venv', 'env', '.env',
    '__pycache__', '.mypy_cache', '.pytest_cache', '.ruff_cache',
    'dist', 'build', '.next', '.nuxt', 'out', 'target',
    'coverage', '.coverage', 'htmlcov',
}

# 수집 대상 확장자
_SOURCE_EXTS = {
    '.py', '.js', '.ts', '.tsx', '.jsx',
    '.go', '.rs', '.java', '.kt', '.kts',
    '.rb', '.php', '.cs', '.cpp', '.c', '.h',
}

# 한 번 분석에 보낼 최대 파일 수 / 본문 길이
_MAX_FILES         = 5
_MAX_FILE_BYTES    = 50_000
_MAX_PROMPT_BYTES  = 4_000   # 파일 한 개당 프롬프트에 박을 최대 본문
#: 지금 편집 중인 파일은 수정 대상일 가능성이 가장 높다 — 잘린 채 "전체 내용"을 다시 쓰면 뒷부분이
#: 사라진다(검토: 11KB 파일이 4KB 로 덮어써짐). 더 넉넉히 보내고, 잘랐으면 모델에게 알린다.
_MAX_OPEN_FILE_BYTES = 24_000


def _prompt_body(content: str, limit: int) -> str:
    """프롬프트에 넣을 파일 본문. 잘랐으면 그 사실과 규칙을 붙인다."""
    text = content or ""
    if len(text) <= limit:
        return text
    return (text[:limit] + f"\n... [ReCoder: 이 파일은 {len(text)}자 중 앞 {limit}자만 보냈습니다. "
            "이 파일은 전체를 다시 쓰지 말고, 꼭 필요하면 새 파일로 분리하세요.]")
_MAX_TOTAL_PROMPT  = 18_000  # 전체 프롬프트 상한 (토큰 quota 보호)


# ── 프로젝트 루트 / 파일 탐색 ─────────────────────────────────────────

def _project_root() -> Path:
    """
    분석 대상 프로젝트 루트를 결정한다.
    우선순위:
      1) 환경변수 RECODER_PROJECT_ROOT
      2) cwd 부터 부모 방향으로 마커(.git, pyproject.toml 등) 탐색
      3) 그래도 없으면 cwd
    """
    env_root = os.environ.get('RECODER_PROJECT_ROOT', '').strip()
    if env_root:
        p = Path(env_root).expanduser()
        if p.exists() and p.is_dir():
            return p

    cwd = Path.cwd()
    local_markers = (
        'pyproject.toml', 'package.json', 'requirements.txt',
        'main.py', 'app.py', 'go.mod', 'Cargo.toml',
    )
    if any((cwd / m).exists() for m in local_markers):
        return cwd

    markers = ('.git', 'pyproject.toml', 'package.json', 'go.mod', 'Cargo.toml')
    for d in [cwd, *cwd.parents]:
        if any((d / m).exists() for m in markers):
            return d
    return cwd


def _resolve_root(explicit: str = "") -> Path:
    """요청별 워크스페이스 루트를 결정한다.

    `RECODER_PROJECT_ROOT` 는 **프로세스 전역**이라, 여러 VSCode 창이 동시에
    요청하면 서로 덮어쓴다. 요청 처리 중 스레드로 넘어가는 지점이 생기면
    나중 요청이 먼저 요청의 루트를 바꿔버려, A 창 요청인데 B 워크스페이스를
    기준으로 코드·ADR 이 만들어질 수 있다.
    그래서 호출자가 넘긴 명시 경로를 항상 우선한다(전역 상태 비의존).
    """
    if explicit:
        p = Path(str(explicit)).expanduser()
        if p.exists() and p.is_dir():
            return p
    return _project_root()


def compute_sha256(path: str | Path) -> str:
    """파일의 SHA256 해시를 계산한다."""
    with open(path, 'rb') as f:
        return hashlib.sha256(f.read()).hexdigest()


def _read_file_safe(path: Path, max_bytes: int = _MAX_FILE_BYTES) -> str:
    """파일을 안전하게 읽는다. 디코딩 에러는 replace로 처리."""
    try:
        content = path.read_bytes()[:max_bytes]
        return content.decode('utf-8', errors='replace')
    except Exception:
        return ""


def _iter_source_files(root: Path, max_files: int = 1500):
    """프로젝트 루트 아래 소스 파일을 재귀 탐색."""
    count = 0
    try:
        for p in root.rglob('*'):
            if count >= max_files:
                return
            try:
                if not p.is_file():
                    continue
            except OSError:
                continue
            if p.suffix.lower() not in _SOURCE_EXTS:
                continue
            if any(part in _SKIP_DIRS for part in p.parts):
                continue
            yield p
            count += 1
    except Exception:
        return


def _extract_keyword_hints(error_text: str) -> list[str]:
    """에러 텍스트에서 파일 매칭에 쓸 키워드 추출."""
    hints: set[str] = set()

    # HTTP path: GET /api/users → ['api', 'users']
    for m in re.finditer(
        r'(?:GET|POST|PUT|DELETE|PATCH)\s+(/\S+)',
        error_text, re.IGNORECASE,
    ):
        path = m.group(1).split('?')[0].split('#')[0]
        for seg in path.split('/'):
            seg = seg.strip(':{}<>')
            if len(seg) >= 3 and not seg.isdigit():
                hints.add(seg.lower())

    # 모듈/식별자: 파이썬 traceback의 "File "...", line ..., in <name>"
    for m in re.finditer(r'in\s+([A-Za-z_][\w]+)', error_text):
        hints.add(m.group(1).lower())

    # 따옴표로 감싼 식별자: 'users', "register"
    for m in re.finditer(r"['\"]([A-Za-z_][\w\-]{2,})['\"]", error_text):
        hints.add(m.group(1).lower())

    return [h for h in hints if h not in {'true', 'false', 'null', 'none'}]


# 흔한 HTTP path prefix — 키워드 매칭에서 제외 (너무 광범위)
_GENERIC_PATH_SEGMENTS = {
    'api', 'v1', 'v2', 'v3', 'app', 'web', 'public', 'static',
    'detail', 'data', 'list', 'item', 'items',
}

_BACKEND_DIR_HINTS  = ('routers', 'router', 'routes', 'route', 'api', 'controllers',
                       'controller', 'handlers', 'handler', 'views', 'endpoints',
                       'backend', 'server')
_FRONTEND_DIR_HINTS = ('frontend', 'client', 'web', 'ui', 'pages', 'components',
                       'src/components', 'src/pages')

# HTTP 에러 시 본문에서 잡을 라우트 데코레이터/호출 패턴
_ROUTE_PATTERNS = [
    re.compile(r'@(?:router|app)\.(?:get|post|put|delete|patch)\s*\(\s*[\'"]([^\'"]+)[\'"]', re.IGNORECASE),
    re.compile(r'@app\.route\s*\(\s*[\'"]([^\'"]+)[\'"]', re.IGNORECASE),
    re.compile(r'(?:app|router)\.(?:get|post|put|delete|patch)\s*\(\s*[\'"]([^\'"]+)[\'"]', re.IGNORECASE),
]


def _detect_http_signal(error_text: str) -> tuple[bool, list[str]]:
    """에러 텍스트가 HTTP 형태인지, 어떤 path들이 등장하는지."""
    paths: list[str] = []
    for m in re.finditer(
        r'(?:GET|POST|PUT|DELETE|PATCH)\s+(/\S+)',
        error_text, re.IGNORECASE,
    ):
        paths.append(m.group(1).split('?')[0].split('#')[0])
    is_http = bool(paths) or bool(re.search(r'\b[45]\d{2}\b', error_text))
    return is_http, paths


def _is_server_side_error(error_text: str) -> bool:
    """5xx 인지 — 서버측 코드만 검사하면 되므로 frontend 가중치를 낮춘다."""
    return bool(re.search(r'\b5\d{2}\b', error_text))


def _explicit_path_hints(error_text: str) -> list[str]:
    """에러 텍스트에 직접 등장하는 소스 파일 경로."""
    paths: list[str] = []
    suffixes = r'(?:py|js|ts|tsx|jsx|go|rs|java|kt|rb|php|cs|cpp|c|h)'

    # Python traceback: File "C:\project\app.py", line 10, in ...
    for m in re.finditer(rf'File\s+"([^"]+\.{suffixes})"', error_text):
        paths.append(m.group(1))

    # Windows absolute paths, including drive letters.
    for m in re.finditer(rf'([A-Za-z]:[^\s"\']+\.{suffixes})', error_text):
        paths.append(m.group(1))

    for m in re.finditer(
        rf'([A-Za-z_][\w./\\-]*\.{suffixes})',
        error_text,
    ):
        paths.append(m.group(1))
    return paths


def _collect_related_files(hints: list[str], error_text: str = "") -> list[dict]:
    """
    수정 대상 후보 파일을 수집한다.
      1) 호출자가 넘긴 hints 중 실제 파일
      2) 에러 텍스트에 등장하는 파일 경로
      3) 에러 키워드와 가장 잘 맞는 프로젝트 파일들 (관련도 점수)
    """
    root = _project_root()
    files: list[dict] = []
    seen_keys: set[str] = set()

    def _add(p: Path) -> bool:
        try:
            key = str(p.resolve())
        except OSError:
            return False
        if key in seen_keys:
            return False
        if not p.exists() or not p.is_file():
            return False
        seen_keys.add(key)
        try:
            display_path = str(p.resolve().relative_to(root.resolve()))
        except ValueError:
            display_path = str(p)
        files.append({"path": display_path, "content": _read_file_safe(p)})
        return True

    # (1) 명시 hints + (2) 에러 본문 속 파일 경로
    candidate_paths = list(hints) + _explicit_path_hints(error_text)
    for raw in candidate_paths:
        if not raw:
            continue
        for cand in (Path(raw), root / raw, Path.cwd() / raw):
            if _add(cand):
                break
        if len(files) >= _MAX_FILES:
            return files

    # (3) 키워드 점수 기반 매칭
    keywords = _extract_keyword_hints(error_text)
    is_http, http_paths = _detect_http_signal(error_text)
    server_side = _is_server_side_error(error_text)

    # 매칭에 '쓸모없는' 너무 일반적인 키워드 제거
    meaningful_kw = [kw for kw in keywords if kw not in _GENERIC_PATH_SEGMENTS]

    if not (meaningful_kw or is_http):
        return files

    scored: list[tuple[int, Path]] = []
    for fp in _iter_source_files(root):
        try:
            key = str(fp.resolve())
        except OSError:
            continue
        if key in seen_keys:
            continue

        score = 0
        name_lower = fp.name.lower()
        path_lower = str(fp).lower()

        # (a) 키워드 매칭
        for kw in meaningful_kw:
            if kw in name_lower:
                score += 10
            elif kw in path_lower:
                score += 3

        # (b) HTTP 에러 → 백엔드 라우터 디렉터리 가산
        if is_http:
            if any(seg in path_lower for seg in _BACKEND_DIR_HINTS):
                score += 15
            # 서버사이드 5xx → frontend 디스카운트
            if server_side and any(seg in path_lower for seg in _FRONTEND_DIR_HINTS):
                score -= 25

        # (c) 라우트 데코레이터 본문 매칭 — HTTP path가 정확히 등장하면 강한 신호
        if is_http and http_paths and fp.suffix.lower() in {'.py', '.js', '.ts'}:
            try:
                body = fp.read_text(encoding='utf-8', errors='ignore')[:30_000]
            except Exception:
                body = ''
            if body:
                for pat in _ROUTE_PATTERNS:
                    for m in pat.finditer(body):
                        route = m.group(1)
                        # 정확 일치 +25, prefix 일치 +12
                        for hp in http_paths:
                            if route == hp:
                                score += 25
                            elif hp.startswith(route) or route.startswith(hp.split('/')[1] if '/' in hp else hp):
                                score += 12

        if score > 0:
            scored.append((score, fp))

    scored.sort(key=lambda x: (-x[0], len(str(x[1]))))
    for _, fp in scored:
        if len(files) >= _MAX_FILES:
            break
        _add(fp)

    return files


def _build_prompt(error_text: str, files: list[dict]) -> str:
    """LLM 호출용 프롬프트를 구성한다."""
    if files:
        file_section = ""
        running_total = 0
        for f in files:
            if running_total >= _MAX_TOTAL_PROMPT:
                break
            remaining = _MAX_TOTAL_PROMPT - running_total
            content = f['content'][:min(_MAX_PROMPT_BYTES, remaining)]
            chunk = f"\n### {f['path']}\n```\n{content}\n```\n"
            file_section += chunk
            running_total += len(chunk)
    else:
        file_section = "\n(관련 소스 파일을 찾지 못했습니다. 에러 텍스트만으로 추정해주세요.)\n"

    return f"""다음 에러를 분석하고 코드 수정안을 JSON으로만 응답하세요.
마크다운 코드펜스(```) 없이 순수 JSON만 출력하세요.

## 에러
{error_text[:2000]}

## 관련 파일
{file_section}

## 응답 형식 (이 JSON만 출력)
{{
  "summary": "수정 요약 (한국어 1줄)",
  "risk": "low",
  "test_command": "pytest 또는 python -c 등",
  "patches": [
    {{
      "file": "관련 파일 섹션에 보인 경로 그대로",
      "unified_diff": "--- a/경로\\n+++ b/경로\\n@@ ... @@\\n- 기존줄\\n+ 수정줄"
    }}
  ]
}}

주의:
- file 은 위 '관련 파일' 섹션에 보인 프로젝트 루트 기준 상대경로를 그대로 사용
- unified_diff 는 실제로 적용 가능한 최소 변경만 포함
- 파일 전체 출력 금지
- 반드시 에러와 직접 관련된 수정만
- 정보가 부족해 확신할 수 없으면 patches 는 빈 배열로 두고 summary 에 이유를 적어주세요
"""


def _extract_json(raw: str) -> dict:
    """
    LLM 응답에서 JSON 객체를 추출한다.
    마크다운 코드펜스, 앞뒤 설명 텍스트, 문자열 안의 중괄호도 처리.
    """
    text = raw.strip()
    # 1) 코드펜스 제거 (앞/뒤 모두)
    text = re.sub(r'^\s*```(?:json|JSON)?\s*', '', text)
    text = re.sub(r'\s*```\s*$', '', text).strip()

    # 2) 전체가 JSON이면 바로 파싱
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 3) 문자열을 인식하면서 첫 번째 { ... } 블록 추출
    start = text.find('{')
    if start == -1:
        raise ValueError(f"JSON 객체를 찾지 못했습니다.\n원문: {raw[:300]}")

    depth     = 0
    in_str    = False
    escape    = False
    last_err: Exception | None = None

    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if escape:
                escape = False
            elif ch == '\\':
                escape = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    chunk = text[start:i + 1]
                    try:
                        return json.loads(chunk)
                    except json.JSONDecodeError as e:
                        last_err = e
                        depth = 1
                        continue

    if last_err:
        raise ValueError(f"JSON 파싱 실패: {last_err}\n원문: {raw[:300]}")
    raise ValueError(f"닫히지 않은 JSON 객체.\n원문: {raw[:300]}")


def _safe_risk(value: str) -> RiskLevel:
    """위험도 문자열을 RiskLevel로 변환. 기본값은 LOW."""
    try:
        return RiskLevel(value.lower())
    except ValueError:
        return RiskLevel.LOW


def _resolve_patch_path(file_path: str, root: Path) -> Path:
    """patch 의 file 값을 절대경로로 해석한다."""
    fp = Path(file_path)
    if fp.is_absolute():
        return fp
    candidate = root / fp
    if candidate.exists():
        return candidate
    # 폴백: cwd 기준
    cwd_candidate = Path.cwd() / fp
    if cwd_candidate.exists():
        return cwd_candidate
    return candidate  # 존재하지 않아도 base_sha256 = "" 로 처리


# ── 핵심 API ──────────────────────────────────────────────────────────

def generate_patch(request: AnalyzeRequest, session_id: str) -> PatchProposal:
    """
    AnalyzeRequest를 받아 LLM으로 분석 후 PatchProposal을 생성한다.

    인자:
        request: AnalyzeRequest 객체 (error_text, related_files 포함)
        session_id: 세션 ID (로깅/추적용)

    반환:
        PatchProposal: 수정안 제안 (approval_level=1)
    """
    error_text = request.error_text or ""
    related_files = request.related_files or []

    print(f"[code_agent] 수정안 생성 시작 | 세션: {session_id} | 에러: {error_text[:80]!r}")

    root = _project_root()
    print(f"[code_agent] 프로젝트 루트: {root}")

    files = _collect_related_files(related_files, error_text)
    print(
        f"[code_agent] 관련 파일 {len(files)}개: "
        f"{[f['path'] for f in files] or '(없음)'}"
    )

    prompt = _build_prompt(error_text, files)

    try:
        llm_resp = get_router().call(
            LLMRequest(prompt=prompt, max_tokens=4096, temperature=0.0),
            agent="code_agent",
            operation="generate_patch",
        )
    except Exception as e:
        raise RuntimeError(f"LLM 호출 실패: {e}") from e

    raw        = llm_resp.text.strip()
    used_model = llm_resp.model_used
    print(f"[code_agent] LLM 응답 길이: {len(raw)}자 (model={used_model})")

    if not raw:
        raise RuntimeError(
            f"LLM이 빈 응답을 반환했습니다. "
            f"모델/할당량/필터를 확인하세요."
        )

    try:
        data = _extract_json(raw)
    except ValueError as e:
        raise RuntimeError(f"LLM 응답 JSON 추출 실패: {e}") from e

    # base_sha256 계산
    patches: list[FilePatch] = []
    for p in data.get('patches', []) or []:
        file_path = (p.get('file') or '').strip()
        if not file_path:
            continue
        fp  = _resolve_patch_path(file_path, root)
        sha = compute_sha256(fp) if fp.exists() else ""
        patches.append(FilePatch(
            file         = file_path,
            base_sha256  = sha,
            unified_diff = p.get('unified_diff', ''),
            reason       = p.get('reason', 'stack_trace_match'),
        ))

    if not patches:
        print("[code_agent] 경고: LLM 응답에 patches 가 없습니다.")

    proposal = PatchProposal(
        proposal_id    = uuid.uuid4().hex,
        summary        = data.get('summary', '수정안이 생성되었습니다.'),
        risk_level     = _safe_risk(data.get('risk', 'low')),
        test_command   = data.get('test_command', ''),
        patches        = patches,
        risk_reasons   = [data.get('risk_reason', '')] if data.get('risk_reason') else [],
        approval_level = 1,  # v6.4 추가
    )
    print(f"[code_agent] 수정안 생성 완료 | 파일 {len(patches)}개 | approval_level={proposal.approval_level}")
    return proposal


def apply_patch(proposal: PatchProposal) -> dict:
    """
    PatchProposal을 적용한다.
    base_sha256 검증 → 백업 생성 → unified diff 적용

    반환:
        {"success": bool, "error": str, "applied_files": list}
    """
    results: list[dict] = []
    root = _project_root()
    backup_records: list[dict] = []

    for patch in proposal.patches:
        fp = _resolve_patch_path(patch.file, root)
        try:
            resolved_fp = fp.resolve()
            resolved_root = root.resolve()
            if resolved_root != resolved_fp and resolved_root not in resolved_fp.parents:
                results.append({
                    "file": patch.file,
                    "status": "outside_project",
                    "message": "패치 대상이 프로젝트 루트 밖에 있습니다.",
                })
                continue
        except OSError as e:
            results.append({"file": patch.file, "status": "path_error", "message": str(e)})
            continue

        if not patch.unified_diff.strip():
            results.append({
                "file":    patch.file,
                "status":  "empty_diff",
                "message": "unified_diff 가 비어 있어 건너뜀",
            })
            continue

        # base_sha256 검증
        if fp.exists() and patch.base_sha256:
            current_sha = compute_sha256(fp)
            if current_sha != patch.base_sha256:
                results.append({
                    "file":    patch.file,
                    "status":  "hash_mismatch",
                    "message": "파일이 변경되었습니다. 재분석이 필요합니다.",
                })
                continue

        # 백업 생성
        backup_path = None
        if fp.exists():
            backup_dir = BACKUP_DIR / proposal.proposal_id
            backup_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime('%Y%m%d_%H%M%S')
            try:
                safe_name = str(fp.resolve().relative_to(root.resolve()))
            except Exception:
                safe_name = fp.name
            safe_name = safe_name.replace("\\", "__").replace("/", "__")
            backup_path = backup_dir / f"{safe_name}.{ts}.bak"
            backup_path.write_bytes(fp.read_bytes())

        # diff 적용
        try:
            original_text = fp.read_text(encoding='utf-8') if fp.exists() else ""
            patched_text = _apply_unified_diff(original_text, patch.unified_diff)
            fp.parent.mkdir(parents=True, exist_ok=True)
            fp.write_text(patched_text, encoding='utf-8')
            validation = _validate_changed_file(fp)
            if backup_path is not None:
                backup_records.append({
                    "file": patch.file,
                    "target_path": str(fp),
                    "backup_path": str(backup_path),
                })
            results.append({
                "file": patch.file,
                "status": "ok",
                "message": "패치 적용 완료",
                "validation": validation,
                "backup_path": str(backup_path) if backup_path else "",
            })
        except Exception as e:
            if backup_path and backup_path.exists():
                fp.write_bytes(backup_path.read_bytes())
            results.append({"file": patch.file, "status": "error", "message": str(e)})

    if backup_records:
        _LAST_APPLY_BACKUPS[proposal.proposal_id] = backup_records

    success = all(r.get("status") == "ok" for r in results)
    error_msg = " | ".join([r.get("message", "") for r in results if r.get("status") != "ok"])

    return {
        "success": success,
        "error": error_msg,
        "applied_files": results,
    }


def rollback_patch(proposal_id: str) -> dict:
    """
    proposal_id에 해당하는 패치를 롤백한다.

    반환:
        {"success": bool, "error": str, "restored_files": list}
    """
    records = _LAST_APPLY_BACKUPS.get(proposal_id, [])
    if not records:
        return {
            "success": False,
            "error": "이 제안에 대한 백업이 없습니다.",
            "restored_files": [],
        }

    results: list[dict] = []
    for record in records:
        target = Path(record["target_path"])
        backup = Path(record["backup_path"])
        if not backup.exists():
            results.append({
                "file": record.get("file", str(target)),
                "status": "missing_backup",
                "message": f"백업 파일을 찾을 수 없음: {backup}",
            })
            continue
        try:
            target.write_bytes(backup.read_bytes())
            results.append({
                "file": record.get("file", str(target)),
                "status": "ok",
                "message": "롤백 완료",
            })
        except Exception as e:
            results.append({
                "file": record.get("file", str(target)),
                "status": "error",
                "message": str(e),
            })

    success = all(r.get("status") == "ok" for r in results)
    error_msg = " | ".join([r.get("message", "") for r in results if r.get("status") != "ok"])

    return {
        "success": success,
        "error": error_msg,
        "restored_files": results,
    }


def _apply_unified_diff(original_text: str, diff_text: str) -> str:
    """unified diff를 원본 텍스트에 적용한다."""
    result = original_text.splitlines()
    original_had_trailing_newline = original_text.endswith(("\n", "\r"))
    lines = diff_text.splitlines()

    i = 0
    offset = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith('@@'):
            # @@ -start,count +start,count @@
            m = re.match(r'@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@', line)
            if not m:
                raise ValueError(f"Invalid hunk header: {line}")

            old_start = int(m.group(1))
            pos = max(old_start - 1, 0) + offset
            i += 1

            while i < len(lines) and not lines[i].startswith('@@'):
                dl = lines[i]
                if dl.startswith('\\ No newline at end of file'):
                    i += 1
                    continue

                marker = dl[:1]
                value = dl[1:] if marker in {' ', '-', '+'} else dl

                if marker == ' ':
                    _expect_line(result, pos, value, "context")
                    pos += 1
                elif marker == '-':
                    _expect_line(result, pos, value, "removal")
                    result.pop(pos)
                    offset -= 1
                elif marker == '+':
                    result.insert(pos, value)
                    pos += 1
                    offset += 1
                else:
                    _expect_line(result, pos, value, "context")
                    pos += 1
                i += 1
        else:
            i += 1

    if not result:
        return ""
    text = "\n".join(result)
    if original_had_trailing_newline or _diff_adds_trailing_newline(diff_text):
        text += "\n"
    return text


def _expect_line(lines: list[str], index: int, expected: str, kind: str) -> None:
    """diff 라인 기댓값 검증."""
    if index >= len(lines):
        raise ValueError(f"Diff {kind} line is past end of file: {expected!r}")
    actual = lines[index]
    if actual != expected:
        raise ValueError(
            f"Diff {kind} mismatch at line {index + 1}: expected {expected!r}, got {actual!r}"
        )


def _diff_adds_trailing_newline(diff_text: str) -> bool:
    """diff가 파일 끝에 newline을 추가하는지 확인."""
    stripped = diff_text.rstrip("\n\r")
    return bool(stripped) and not stripped.endswith("\\ No newline at end of file")


def _validate_changed_file(path: Path) -> str:
    """변경된 파일의 유효성을 검증한다 (Python 파일만)."""
    if path.suffix.lower() != ".py":
        return "unknown"
    try:
        source = path.read_text(encoding="utf-8")
        compile(source, str(path), "exec")
        return "syntax_ok"
    except SyntaxError as e:
        return f"syntax_error:{e.lineno}:{e.msg}"


# ════════════════════════════════════════════════════════════════════════════
# 코드 생성 에이전트 (Build 탭 — Codex/Cowork 식 인터랙티브 파일 생성/수정)
#
# 에러 수정(generate_patch)과 별개의 진입점.
#   - 사용자가 자연어로 "~ 만들어줘 / ~ 고쳐줘" 요청
#   - LLM 이 파일 단위 작업(ops) 을 전체 내용으로 반환 (unified diff 아님 → 적용 안정성↑)
#   - 확장(Bridge/Sidebar)이 워크스페이스에 직접 쓰고 에디터로 열어줌
# 정체성 유지: 자동 트리거 없음. 사용자가 명시적으로 호출할 때만 1회.
# ════════════════════════════════════════════════════════════════════════════

_CODE_AGENT_MAX_TOKENS = 8192
_CODE_AGENT_TREE_LIMIT = 80   # 컨텍스트에 넣을 기존 파일 경로 최대 수


def _list_project_files(root: Path, limit: int = _CODE_AGENT_TREE_LIMIT) -> list[str]:
    """충돌 회피·맥락용 기존 파일 경로 목록(상대경로). 가벼운 트리."""
    out: list[str] = []
    if limit <= 0:
        return out
    try:
        # Prune before descent. Sorting rglob materialized the entire dependency
        # tree before the 80-file limit could take effect, stalling AI requests.
        for directory, dirs, files in os.walk(root, followlinks=False):
            dirs[:] = sorted(name for name in dirs if name not in _SKIP_DIRS)
            for name in sorted(files):
                p = Path(directory) / name
                if p.suffix.lower() in _SOURCE_EXTS or p.suffix.lower() in {
                    ".html", ".css", ".json", ".md", ".txt", ".yml", ".yaml", ".toml",
                }:
                    out.append(p.relative_to(root).as_posix())
                    if len(out) >= limit:
                        return out
    except Exception:
        pass
    return out


def _decisions_prompt_block(decisions: list[NormalizedDecision]) -> str:
    """정규화된 결정 목록 → 프롬프트에 주입할 텍스트.

    입력은 반드시 `adr.normalize_decisions` 를 거친 것이어야 한다
    (여기서 다시 파싱하지 않는다). "이 선택을 따르라"는 지시는 프롬프트
    규칙 항목에 이미 있으므로 여기서는 사실만 나열한다.
    """
    lines: list[str] = []
    for d in (decisions or []):
        if not isinstance(d, dict):
            continue
        # 정규화를 거치지 않은 원시 입력이 흘러들어와도 죽지 않도록 .get() 으로 접근.
        key = str(d.get("chosen_key") or "").strip()
        if not key:
            continue
        label = str(d.get("chosen_label") or key).strip()
        title = str(d.get("question") or d.get("id") or "설계 결정").strip()
        summary = str(d.get("chosen_summary") or "").strip()
        lines.append(
            f"- {title}: {label} ({key})" + (f" — {summary}" if summary else "")
        )
    if not lines:
        return ""
    return (
        "\n사용자가 확정한 설계 결정(반드시 이 선택을 반영):\n"
        + "\n".join(lines)
        + "\n"
    )


def _scrub_context(open_file, prior_files, context_files, secrets: dict | None = None):
    """열린 파일·참고 파일의 비밀(.env 본문, 키 문자열)을 AI 로 보내지 않는다."""
    try:
        from context_gate import scrub_file_entry
    except ImportError:  # pragma: no cover
        from core.context_gate import scrub_file_entry  # type: ignore
    return (scrub_file_entry(open_file, secrets) if open_file else open_file,
            [scrub_file_entry(f, secrets) for f in prior_files] if prior_files else prior_files,
            [scrub_file_entry(f, secrets) for f in context_files] if context_files else context_files)


def _restore_secrets_in_ops(ops: list[dict], secrets: dict) -> None:
    if not secrets:
        return
    try:
        from context_gate import restore_code_secrets
    except ImportError:  # pragma: no cover
        from core.context_gate import restore_code_secrets  # type: ignore
    try:
        from security_scan import is_doc_like
    except ImportError:  # pragma: no cover
        from core.security_scan import is_doc_like  # type: ignore
    for op in ops:
        #: 문서(README·.env.example)에는 원래 값을 되돌려 넣지 않는다 — 자리표시가 맞다(키가 문서로 새지 않게).
        if isinstance(op.get("content"), str) and not is_doc_like(str(op.get("file") or "")):
            op["content"] = restore_code_secrets(op["content"], secrets)


def _build_code_prompt(
    instruction: str,
    existing_files: list[str],
    open_file: dict | None,
    prior_files: list[dict] | None = None,
    context_files: list[dict] | None = None,
    target_folder: str = "",
    decisions: list[NormalizedDecision] | None = None,
) -> str:
    """코드 생성 에이전트용 프롬프트. JSON ops 형식을 강제한다.

    `decisions` 는 `adr.normalize_decisions` 를 거친 목록이어야 한다.
    """
    open_file, prior_files, context_files = _scrub_context(open_file, prior_files, context_files)
    tree = "\n".join(f"- {f}" for f in existing_files) or "(빈 프로젝트)"
    prior_block = ""
    for pf in (prior_files or [])[:4]:
        body = _prompt_body(pf.get("content") or "", _MAX_PROMPT_BYTES)
        if body.strip():
            prior_block += f"\n[직전 생성 파일] {pf.get('path','?')}\n```\n{body}\n```\n"
    if prior_block:
        prior_block = "\n직전 턴에서 만든 파일들(이어서 수정/확장할 수 있음):\n" + prior_block

    ctx_block = ""
    for cf in (context_files or [])[:6]:
        body = _prompt_body(cf.get("content") or "", _MAX_PROMPT_BYTES)
        if body.strip():
            ctx_block += f"\n[참고 파일] {cf.get('path','?')}\n```\n{body}\n```\n"
    if ctx_block:
        ctx_block = "\n사용자가 첨부한 참고 파일(이 내용을 활용/일관성 유지):\n" + ctx_block

    folder_block = ""
    if (target_folder or "").strip():
        folder_block = (
            f"\n[대상 폴더] 생성/수정 파일은 '{target_folder.strip().rstrip('/')}/' 안에 둔다. 경로는 **대상 폴더 기준** "
            f"상대경로로 쓴다(예: index.html, src/app.js — 앞에 '{target_folder.strip().rstrip('/')}/' 를 붙이지 않는다).\n"
        )

    open_block = ""
    if open_file and (open_file.get("content") or "").strip():
        body = _prompt_body(open_file.get("content") or "", _MAX_OPEN_FILE_BYTES)
        open_block = (
            f"\n현재 편집 중인 파일: {open_file.get('path', '(unknown)')}\n"
            f"```\n{body}\n```\n"
        )

    # 결정 파싱은 adr.normalize_decisions 한 곳에서만 수행한다.
    # (프롬프트가 말하는 결정과 ADR 이 기록한 결정이 어긋나지 않도록)
    decision_block = _decisions_prompt_block(decisions or [])

    return f"""당신은 VSCode 안에서 동작하는 코드 작성 에이전트입니다.
사용자의 요청을 읽고, 실제로 워크스페이스에 적용할 파일 작업(ops)을 만드세요.

규칙:
- 각 파일 작업은 그 파일의 "전체 최종 내용"을 담습니다 (부분 diff 아님).
- 새 파일이면 action="create", 기존 파일을 바꾸면 action="edit".
- 가능한 한 적은 수의 파일로, 즉시 실행/렌더 가능한 완결된 코드를 작성합니다.
- 사용자가 지정한 프레임워크와 승인된 설계를 따르고, 실행에 필요한 설정 파일도 포함합니다.
- 프레임워크 지정이나 기존 프로젝트 제약이 없을 때만 단일 HTML 등 간단한 구성을 선택합니다.
- TypeScript 코드는 tsconfig와 일치해야 합니다. jsx="react-jsx"에서는 사용하지 않는 React 기본 import를 넣지 마세요.
- 기존 파일 목록과 겹치지 않게 파일명을 정하되, 사용자가 파일명을 지정하면 그대로 따릅니다.
- 사용자가 확정한 설계 결정이 있으면 그 선택을 우선하고 임의로 다른 방식을 택하지 않습니다.
- package.json 의 scripts 는 실제로 존재하는 파일과 설치되는 도구만 참조합니다. 쓰지 않는 빌드 스크립트나 의존성(예: src/ 없는 react-scripts)을 넣지 마세요.
- 의존성은 알려진 치명적 취약점이 없는 최신 major 버전을 씁니다(예: sqlite3 는 ^6.0.1 — 5.x 는 배포 보안 검사에서 차단됩니다).
- 코드가 require/import 하는 외부 패키지는 모두 package.json(또는 requirements.txt)에 선언합니다.
- 서버는 포트를 환경변수로 받게 합니다(예: process.env.PORT || 3000). 가능하면 GET /health 가 200 을 돌려주게 합니다.
- 화면(React·Vue 등)과 API 서버를 함께 만들면 서버가 빌드된 화면 폴더를 정적으로 제공하고, 화면은 API 를 상대 경로(/api/…)로 부릅니다. localhost 주소를 코드에 넣지 마세요.
- Vite 프로젝트에서는 JSX 가 든 파일을 .jsx/.tsx 로 만들고, 브라우저 코드의 환경변수는 import.meta.env.VITE_* 만 씁니다(process.env 금지).
- PostgreSQL(pg)의 NUMERIC/DECIMAL 값은 문자열로 옵니다 — 서버에서 숫자로 바꾸거나 pg.types.setTypeParser(1700, parseFloat) 를 설정하세요.
- 파일을 나눠 만들 때 서로 부르는 함수·컴포넌트 이름과 export 방식을 정확히 맞추고, import 하는 파일(CSS 포함)은 반드시 함께 만듭니다.
- 회원가입/로그인이 있으면 실제 입력 화면·라우트·인증 상태 공급자·로그아웃까지 연결합니다. 권한은 서버에서 확인하고 관리자 기본 계정/비밀번호를 만들지 않습니다. 비밀 환경변수가 없으면 시작을 거절하며 기본 JWT 비밀값을 두지 않습니다. 로그인 시도 횟수 제한, 비밀번호 길이 검증, 일반화된 오류 응답을 구현합니다.
- 쇼핑몰/결제에서는 가격·합계·사용자 ID·권한을 클라이언트 입력으로 신뢰하지 않습니다. 서버 DB 가격으로 최소 화폐 단위 정수 금액을 계산하고, 수량은 양의 정수로 검증합니다. 주문과 재고 예약은 DB 트랜잭션/행 잠금으로 묶어 동시 주문의 초과 판매를 막고, 주문 재시도에는 사용자별 idempotency key를 사용합니다.
- 결제 완료는 서명 검증된 웹훅에서만 처리합니다. raw body 라우트를 JSON 파서보다 먼저 등록하고, 주문 소유자·저장된 payment ID·통화·금액을 대조하며 이벤트 ID를 DB에 UNIQUE로 저장해 중복 처리를 막습니다. 사용자가 paid/completed 상태를 직접 지정하는 API는 금지합니다. 실패/취소 시 재고는 정확히 한 번 복원합니다. 미결제 주문의 만료·취소 경로도 만듭니다.
- 결제 테스트는 명시적 테스트 환경에서만 별도 API 호스트/포트를 주입할 수 있게 합니다. 브라우저에서 테스트 성공을 누르는 것만으로 결제를 확정하거나 운영 환경에서 모의 결제를 허용하면 안 됩니다. 실제 결제사 설정이 필요한 부분은 README에 명시합니다.
- DB 스키마·초기화 명령·필요 환경변수 예시·root 실행/빌드 스크립트·Dockerfile·.dockerignore·README를 포함합니다. 빈 CSS나 가짜 성공 동작으로 기능을 대신하지 않습니다. DB TLS 인증서 검증을 끄지 않습니다. 인증 토큰 저장과 CORS/CSRF 정책을 일관되게 설계합니다.
- React Router 를 쓰면 페이지 이동은 <Link>/useNavigate 로 합니다(<a href> 는 상태를 잃습니다).
- 자리표시 이미지는 https://placehold.co/300x200?text=이름 형식을 씁니다(via.placeholder.com·placeimg.com 은 문을 닫아 이미지가 깨집니다).
- 화면·서버를 폴더로 나누면(frontend/·backend/ 등) 폴더마다 package.json(그 폴더 코드가 불러오는 패키지 전부)을 만듭니다. tsconfig 의 references 는 실제로 만드는 파일만 가리킵니다.
- TypeScript 는 strict 로 컴파일되게 씁니다 — process.env.X 는 string | undefined 이므로 시작 때 확인한 뒤 string 으로 좁혀 쓰고, 선언한 타입·인터페이스는 실제로 export 합니다.
- 커스텀 훅이 함수를 돌려주면 useCallback(또는 돌려주는 객체를 useMemo)으로 감쌉니다. 매번 새로 만들어지는 함수를 useEffect 의존성에 넣으면 요청이 끝없이 반복됩니다.
- Dockerfile: package-lock.json 을 만들지 않으면 `npm ci` 대신 `npm install`(운영 설치는 --omit=dev)을 씁니다. 실행 사용자는 숫자 ID 로 지정합니다(예: `RUN addgroup -g 1001 -S app && adduser -S -u 1001 -G app app` 뒤 `USER 1001`). OS 패키지(apk·apt)를 설치하면 바로 윗줄에 `# hadolint ignore=DL3018`(apt 는 DL3008)과 그 이유(기본 이미지 태그가 버전을 정함)를 주석으로 남깁니다. 서버가 화면 빌드 폴더를 제공하는 경로는 컨테이너 안의 실제 위치(/app/…)와 맞게 씁니다.

기존 파일 목록:
{tree}
{folder_block}{ctx_block}{prior_block}{open_block}{decision_block}
사용자 요청:
{instruction}

아래 JSON 형식으로만 응답하세요(설명 문장 금지):
{{
  "summary": "무엇을 만들었는지 한국어 한 줄",
  "ops": [
    {{
      "action": "create",
      "file": "상대경로/파일명.확장자",
      "language": "html|python|javascript|...",
      "content": "파일의 전체 내용",
      "rationale": "이 파일을 만든/고친 이유 한 줄"
    }}
  ]
}}"""


# ── AI-DLC: 코드 대신 "설계 결정" 제시 (/api/code/plan) ────────────────
#
# 회차1 (ADR-D3·D5·D6, ReCoder_AI-DLC_도입_설계서.md §3.2 1단계):
#   POST /api/code/plan — 코드 대신 "설계 결정 목록"을 반환한다.
#   (2단계 /api/code/generate 의 decisions 반영 + ADR 영속화(docs/adr/)는
#    별도 담당 범위라 이 파일에서는 다루지 않는다.)

#: 결정 3개 × 선택지 3~4개 × pros/cons 를 **한국어 JSON** 으로 쓰면 2048 에
#: 닿는다. 상한에 잘리면 JSON 이 중간에 끊겨 파싱이 실패하고, 사용자에게는
#: "설계 결정 생성 실패"만 남는다 — 보드 이슈 「AI-DLC 설계 결정이 제대로
#: 나오지 않음」의 원인 중 하나. 프롬프트가 요구하는 최대 분량이 여유 있게
#: 들어가는 크기로 올린다.
_PLAN_MAX_TOKENS = 4096

#: 설계 결정 응답 스키마 — 모델이 구조화 출력(tool use)으로 답하게 해 **항상 올바른 JSON** 을 받는다.
#: 자유 텍스트로 받으면 한국어 설명 안의 따옴표("운영" 같은)를 이스케이프하지 않아 JSON 이 깨지고
#: 두 번 다 "닫히지 않은 JSON 객체"로 실패했다(실기기 쇼핑몰 요청).
PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "decisions": {
            "type": "array", "maxItems": 3,
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "question": {"type": "string"},
                    "options": {
                        "type": "array", "minItems": 2, "maxItems": 4,
                        "items": {
                            "type": "object",
                            "properties": {
                                "key": {"type": "string"},
                                "label": {"type": "string"},
                                "summary": {"type": "string"},
                                "pros": {"type": "array", "items": {"type": "string"}, "maxItems": 3},
                                "cons": {"type": "array", "items": {"type": "string"}, "maxItems": 3},
                                "recommended": {"type": "boolean"},
                            },
                            "required": ["key", "label"],
                        },
                    },
                    "impact": {"type": "string"},
                },
                "required": ["id", "question", "options"],
            },
        },
    },
    "required": ["decisions"],
}


def _build_plan_prompt(
    instruction: str,
    existing_files: list[str],
    open_file: dict | None,
    target_folder: str = "",
    context_files: list[dict] | None = None,
    count_rule: str = "- 결정 개수는 보통 1~3개로 제한합니다 (과도하게 쪼개지 마세요).",
) -> str:
    """/api/code/plan 용 프롬프트 — 코드가 아니라 '설계 결정 선택지'를 요구한다."""
    open_file, _prior, context_files = _scrub_context(open_file, None, context_files)
    tree = "\n".join(f"- {f}" for f in existing_files) or "(빈 프로젝트)"

    folder_block = ""
    if (target_folder or "").strip():
        folder_block = f"\n[대상 폴더] {target_folder.strip().rstrip('/')}/\n"

    open_block = ""
    if open_file and (open_file.get("content") or "").strip():
        body = _prompt_body(open_file.get("content") or "", _MAX_OPEN_FILE_BYTES)
        open_block = (
            f"\n현재 편집 중인 파일: {open_file.get('path', '(unknown)')}\n"
            f"```\n{body}\n```\n"
        )

    ctx_block = ""
    for cf in (context_files or [])[:6]:
        body = _prompt_body(cf.get("content") or "", _MAX_PROMPT_BYTES)
        if body.strip():
            ctx_block += f"\n[참고 파일] {cf.get('path', '?')}\n```\n{body}\n```\n"
    if ctx_block:
        ctx_block = (
            "\n사용자가 첨부한 참고 파일입니다. 파일의 아키텍처 제약과 기존 선택을 "
            "반드시 반영하고, 이 내용과 모순되는 선택지는 추천하지 마세요:\n" + ctx_block
        )

    return f"""당신은 VSCode 안에서 동작하는 AI-DLC(AI 개발 라이프사이클) 설계 도우미입니다.
사용자 요청을 코드로 바로 구현하지 말고, 구현하기 전에 사람이 승인해야 할 "설계 결정"을 선택지로 제시하세요.

규칙:
- 이 요청을 구현하는 데 필요한 설계 결정을 식별하세요 (예: 데이터 저장 방식, 인증/권한 방식, 프레임워크/라이브러리 선택, API 형태 등).
- 결정할 것이 전혀 없는 사소한 요청(오타 수정, 이름 변경 등)이면 decisions 를 빈 배열로 반환하세요.
- 각 결정은 서로 다른 2~4개의 선택지를 제공합니다.
- 프로젝트 성격과 기존 파일을 근거로 가장 적합한 선택지 하나를 recommended: true 로 표시하세요.
- 각 선택지의 장단점(pros/cons)을 1~3개씩 간결하게 답니다.
{count_rule}

기존 파일 목록:
{tree}
{folder_block}{open_block}{ctx_block}
사용자 요청:
{instruction}

아래 JSON 형식으로만 응답하세요(설명 문장 금지):
{{
  "decisions": [
    {{
      "id": "storage",
      "question": "데이터를 어디에 저장할까요?",
      "options": [
        {{"key": "local", "label": "브라우저 로컬 저장", "summary": "서버 없이 바로 동작",
         "pros": ["간단", "설정 불필요"], "cons": ["기기 간 공유 불가"], "recommended": true}},
        {{"key": "file", "label": "파일(JSON)", "summary": "서버 파일시스템에 저장",
         "pros": ["영속성"], "cons": ["동시성 처리 필요"], "recommended": false}}
      ],
      "impact": "앱 구조에 영향"
    }}
  ]
}}"""


# 승인 판정 결과 (FR-02-05)
APPROVAL_APPROVED = "approved"
APPROVAL_CANCELLED = "cancelled"
APPROVAL_MISSING = "missing"
APPROVAL_INVALID = "invalid"

# 확인 카드에서 허용되는 선택 (그 외 값은 승인으로 인정하지 않는다)
CONFIRM_PROCEED_KEY = "proceed"
CONFIRM_CANCEL_KEY = "cancel"

# 결정 하나가 제시할 선택지 개수.
#  - 최소 2: 하나뿐이면 '고른다'가 성립하지 않는다(승인이 아니라 통과 의식).
#  - 최대 MAX_ALTERNATIVES+1: 기록 단계가 '검토한 대안'을 그만큼만 남기므로,
#    더 제시하면 사용자가 본 대안이 ADR 에서 말없이 사라진다.
MIN_OPTIONS_PER_DECISION = 2
MAX_OPTIONS_PER_DECISION = MAX_ALTERNATIVES + 1


def _chosen_key(decision: dict) -> str:
    """사용자가 고른 키 (웹뷰가 쓰는 세 가지 필드명 허용) — **정규형으로** 반환.

    기록 단계(normalize_decisions)와 같은 정규형을 써야 '게이트에서는 A 를
    골랐는데 ADR 에는 B 가 남는' 어긋남이 생기지 않는다.
    """
    return canonical_key(
        decision.get("chosen_key") or decision.get("choice") or decision.get("chosen") or ""
    )


def _decision_id(decision: dict) -> str:
    """결정 id — 정규형. 중복 판정도 이 값으로 한다."""
    return canonical_key(decision.get("id"))


def _offered_key_list(decision: dict) -> list[str]:
    """그 결정이 제시한 선택지 key 목록 — 정규형, 등장 순서 유지(중복 포함)."""
    opts = decision.get("options")
    if not isinstance(opts, list):
        return []
    out: list[str] = []
    for o in opts:
        if not isinstance(o, dict):
            continue
        key = canonical_key(o.get("key"))
        if key:
            out.append(key)
    return out


def _is_cancelled(decisions: list | None) -> bool:
    """확인 카드에서 '취소'가 선택됐는지 판정한다 (FR-02-05).

    사용자가 명시적으로 취소했는데 코드를 생성하면 '사람 승인' 전제가 깨진다.
    """
    return _approval_state(decisions) == APPROVAL_CANCELLED


def _clean_str_list(values: object) -> list[str]:
    """장점/단점 목록을 기록 단계와 같은 형태(정규형·같은 개수 상한)로 정리한다.

    여기서 맞춰두지 않으면 `_clean_list` 가 나중에 잘라내, 사용자가 카드에서
    본 항목이 ADR 에서 말없이 사라진다. 숫자·불리언이 섞여 와도 여기서 흡수한다.
    """
    if not isinstance(values, list):
        return []
    out: list[str] = []
    for v in values:
        cleaned = canonical_key(v)
        if cleaned:
            out.append(cleaned)
        if len(out) >= MAX_LIST_ITEMS:
            break
    return out


def _unique_canonical_id(base: str, seen: set[str]) -> str:
    """정규형 기준으로 유일한 결정 id 를 만든다.

    접미사를 그냥 이어 붙이면 길이 상한(MAX_FIELD_CHARS)을 넘어, 게이트가
    다시 정규화할 때 잘려 원래 id 로 되돌아간다. 그러면 웹뷰에는 서로 다른
    카드로 보이는데 게이트는 중복으로 판정해 **정상 승인이 거절**된다.
    그래서 접미사 자리를 미리 비워두고, 최종 정규형으로 유일성을 확인한다.
    """
    base = canonical_key(base)
    if base and base not in seen:
        return base
    suffix = 2
    while True:
        tail = f"-{suffix}"
        candidate = canonical_key(base[:MAX_FIELD_CHARS - len(tail)] + tail)
        if candidate and candidate not in seen:
            return candidate
        suffix += 1


def _decision_is_valid(d: object) -> bool:
    """제출된 카드가 **plan 이 발급할 수 있었던 모양**이고, 그 카드가 제시한
    선택지 중 정확히 하나를 골랐는가.

    "고른 키가 목록에 있는가"만 봐서는 부족하다. `generate_plan` 이 지키는
    구조 제약(선택지 2~5개, 장단점 5개 이하, 질문 존재)을 게이트가 다시
    확인하지 않으면, 낡거나 망가진 클라이언트가 그 제약을 벗어난 카드를
    보냈을 때 **정규화가 초과분을 말없이 잘라낸다**. 사용자가 봤다고 주장하는
    대안이 ADR 에서 사라지거나, 선택지가 하나뿐인 '고를 수 없는 카드'가
    승인으로 처리된다. 발급 쪽 제약과 검증 쪽 제약은 반드시 같아야 한다.
    """
    from adr import CONFIRM_DECISION_ID, MAX_LIST_ITEMS

    if not isinstance(d, dict):
        return False
    did = _decision_id(d)
    if not did:
        # id 가 없으면 정규화가 chosen_key 를 id 로 대체해 엉뚱한 ADR 이름이 된다.
        return False
    chosen = _chosen_key(d)
    if not chosen:
        # 카드는 제시됐는데 고르지 않음 — 정규화가 조용히 버리므로,
        # 승인받으라고 내놓은 결정이 반영도 기록도 되지 않은 채 코드가 나간다.
        return False
    if did == CONFIRM_DECISION_ID:
        # 확인 카드는 서버가 만든 것이므로 허용 키가 고정이다.
        return chosen.lower() in (CONFIRM_PROCEED_KEY, CONFIRM_CANCEL_KEY)

    if not canonical_key(d.get("question")):
        # 질문이 없으면 ADR 제목이 id 로 대체된다 — 사용자가 승인한 질문이
        # 기록에서 사라진다.
        return False

    offered = _offered_key_list(d)
    if len(set(offered)) != len(offered):
        # 정규형이 겹치는 선택지가 둘 이상 — 정규화는 뒤엣것으로 덮어써서
        # 사용자가 고르지 않은 선택지의 라벨·근거를 기록한다. 어느 쪽을 고른
        # 것인지 확정할 수 없으므로 거절한다.
        return False
    if not (MIN_OPTIONS_PER_DECISION <= len(offered) <= MAX_OPTIONS_PER_DECISION):
        # 1개면 고를 것이 없고(승인이 아니라 통과 의식),
        # 상한을 넘으면 정규화가 '검토한 대안'을 잘라 본 대안이 사라진다.
        return False

    options = d.get("options") if isinstance(d.get("options"), list) else []
    for opt in options:
        if not isinstance(opt, dict):
            continue
        for field in ("pros", "cons"):
            values = opt.get(field)
            if isinstance(values, list) and len(values) > MAX_LIST_ITEMS:
                # 초과분을 정규화가 잘라내 사용자가 본 근거가 기록에서 빠진다.
                return False

    return chosen in offered


def _approval_state(decisions: list | None) -> str:
    """생성 요청이 사람의 승인을 온전히 담고 있는지 판정한다 (FR-02-05).

    ## 규칙 (하나로 통일)

    **제출된 모든 결정 카드는 각자 제시한 선택지 중 정확히 하나를 골라야 한다.
    하나라도 어긋나면 요청 전체를 거절한다.**

    부분 통과를 허용하면 안 되는 이유는 한 가지로 수렴한다 — `normalize_decisions`
    는 규칙에 맞지 않는 항목을 **조용히 버리거나 엉뚱하게 해석**한다.
    그 상태로 생성이 진행되면 "승인받으라고 내놓은 결정"과 "실제로 코드·ADR 에
    반영된 결정"이 어긋난다. 즉 게이트를 느슨하게 둘수록 AI-DLC 의 전제가 깨진다.

    ### 거절하는 경우 (모두 같은 이유의 변형)

    | 입력 | 정규화가 하는 일 | 그래서 |
    | --- | --- | --- |
    | dict 가 아닌 항목 | 건너뜀 | 거절 |
    | `id` 없음 | `chosen_key` 를 id 로 대체 | 거절 |
    | `chosen_key` 없음(미선택 카드) | 건너뜀 | 거절 |
    | 제시되지 않은 `chosen_key` | 그 값을 선택 라벨로 씀 | 거절 |
    | 확인 카드에 proceed/cancel 이 아닌 키 | 확인 카드로 처리 | 거절 |
    | `id` 중복 | 어느 선택이 어느 카드인지 불분명 | 거절 |
    | 정규형이 겹치는 선택지 | 뒤엣것으로 덮어써서 안 고른 쪽을 기록 | 거절 |
    | `MAX_DECISIONS` 초과 | 초과분을 잘라냄 | 거절 |

    비교는 반드시 `adr.canonical_key` 를 거친다. 게이트가 원문으로, 정규화가
    정규형으로 비교하면 "게이트에선 서로 다른 두 키가 기록에선 같아지는"
    구멍이 생긴다 — 앞 200자가 같은 두 키, 내부 공백만 다른 두 키가 그렇다.

    한계(의도적): 선택지 목록은 클라이언트가 되돌려준 값이라 이 검증만으로는
    악의적 위조를 막지 못한다. 위조를 막으려면 plan 을 서버에 보관해 대조해야
    하는데, 그러려면 웹뷰가 plan 식별자를 되돌려줘야 한다(확장 변경 필요).
    다만 코어는 세션 토큰이 걸린 로컬호스트 전용이라 토큰을 가진 쪽은 이미
    어떤 요청이든 보낼 수 있다. 즉 이 검증의 실익은 위조 차단이 아니라
    **클라이언트 결함 차단**(낡은 확장·오타·누락 페이로드)이며, 그 목적에는
    이 수준이 맞다. 서버 보관 대조는 별도 태스크로 분리.

    반환값 (우선순위 순):
      APPROVAL_CANCELLED — 확인 카드에서 명시적으로 '취소'를 골랐다
      APPROVAL_INVALID   — 위 표의 어느 하나라도 해당된다
      APPROVAL_APPROVED  — 모든 카드가 규칙을 만족한다
      APPROVAL_MISSING   — 결정이 아예 실려오지 않았다
    """
    from adr import CONFIRM_DECISION_ID, MAX_DECISIONS

    items = list(decisions or [])

    # 명시적 취소가 최우선 — 나머지가 엉터리여도 '생성 안 함'이 옳은 결과다.
    for d in items:
        if (isinstance(d, dict)
                and _decision_id(d) == CONFIRM_DECISION_ID
                and _chosen_key(d).lower() == CONFIRM_CANCEL_KEY):
            return APPROVAL_CANCELLED

    if not items:
        return APPROVAL_MISSING

    if len(items) > MAX_DECISIONS:
        # 잘라내고 진행하면 초과분이 승인만 받고 반영되지 않는다.
        return APPROVAL_INVALID

    ids: list[str] = []
    for d in items:
        if not _decision_is_valid(d):
            return APPROVAL_INVALID
        ids.append(_decision_id(d))

    if len(set(ids)) != len(ids):
        return APPROVAL_INVALID

    return APPROVAL_APPROVED


def _is_payment_mode_decision(d: dict) -> bool:
    """AI 가 만든 '결제를 키 없이/모의로 시작할지' 결정인가(ReCoder 가 따로 묻는 결정과 겹친다)."""
    if d.get("id") == "commerce-payment":
        return True
    text = " ".join([str(d.get("question") or "")] + [str(o.get("label") or "") for o in d.get("options") or []])
    return bool(re.search(r"결제|payment|stripe", text, re.I) and re.search(r"모의|mock|테스트\s*키|test\s*key|키\s*없이|샌드박스|sandbox", text, re.I))


def _build_confirm_decision(instruction: str) -> dict:
    """설계 결정이 없는 요청용 최소 확인 카드.

    FR-02-05 는 "항상 선택지·사람 승인"이다. 오타 수정처럼 결정할 것이 없는
    요청이라고 해서 승인 단계를 건너뛰면, AI 가 혼자 판단해 코드를 만든 것이
    되어 AI-DLC 전제가 깨진다. 결정이 없을 때도 사람이 한 번 승인하도록
    이 카드를 대신 내보낸다.

    `plan` 결과 스키마를 그대로 따르므로 확장 코드 변경이 필요 없다.
    id 가 예약 접두사(`__`)로 시작해 ADR 로는 기록되지 않는다.
    """
    from adr import CONFIRM_DECISION_ID  # 지연 import (순환 방지 목적 아님, 가독성)

    preview = re.sub(r"\s+", " ", (instruction or "").strip())[:60]
    question = f'"{preview}" — 이대로 진행할까요?' if preview else "이대로 진행할까요?"
    return {
        "id": CONFIRM_DECISION_ID,
        "question": question,
        "options": [
            {
                "key": "proceed",
                "label": "진행",
                "summary": "설계상 갈림길이 없어 요청대로 바로 반영합니다.",
                "pros": ["추가 선택 불필요"],
                "cons": [],
                "recommended": True,
            },
            {
                "key": "cancel",
                "label": "취소",
                "summary": "생성하지 않고 중단합니다.",
                "pros": ["변경 없음"],
                "cons": ["요청이 처리되지 않음"],
                "recommended": False,
            },
        ],
        "impact": "설계 결정 없음 — 승인만 확인합니다.",
    }


#: 새 앱을 만들 때 묻는 설계 결정 수 — 사용자가 하나하나 골라 적용하는 것이 AI-DLC 의 장점이다(사용자 지적 2.0.6:
#: "설계가 왜 이렇게 적게 나와"). 결제 카드·시작 방식 카드와 합쳐 MAX_DECISIONS 를 넘지 않게 한다.
FULL_DESIGN_MIN = 5
FULL_DESIGN_MAX = 8
_TOPIC_SCHEMA = {
    "type": "object",
    "properties": {"topics": {"type": "array", "maxItems": FULL_DESIGN_MAX, "items": {
        "type": "object",
        "properties": {"id": {"type": "string"}, "question": {"type": "string"}, "impact": {"type": "string"}},
        "required": ["id", "question"]}}},
    "required": ["topics"],
}
#: 새 앱에서 사람이 고를 만한 설계 영역(AI 가 이 앱에 필요한 것만 고른다)
_DESIGN_AREAS = ("화면 기술(프레임워크·스타일링)", "서버 기술(프레임워크·언어)", "데이터 저장(DB 종류)", "DB 접근 방식(ORM·쿼리 빌더·SQL)",
                 "로그인·인증 방식(세션·JWT·소셜)", "권한·관리자 구분", "API 형태(REST·GraphQL·페이지 수)", "상태 관리(화면)",
                 "이미지·파일 저장 위치", "검색·목록(페이지 나누기·필터)", "알림(메일·문자)", "주문·재고 처리 규칙", "다국어·통화")


def _full_design_decisions(instruction: str, existing: list[str], open_file, context_files, target_folder: str,
                           want: int) -> list[dict] | None:
    """새 앱의 설계 결정을 **주제 → 카드** 두 단계로 받는다(응답 길이 한도에 걸리지 않게 카드는 3개씩 나눠, 동시에).

    한 번에 받으면 학생용 게이트웨이(출력 4096 토큰)에서 3개 넘게 받기 어려웠고, 스키마도 3개로 막혀 있었다.
    주제를 못 받으면 None — 부르는 쪽이 예전 방식(한 번에)으로 받는다.
    """
    from concurrent.futures import ThreadPoolExecutor
    want = max(FULL_DESIGN_MIN, min(FULL_DESIGN_MAX, want))
    areas = ", ".join(_DESIGN_AREAS)
    topic_prompt = _build_plan_prompt(
        instruction, existing, open_file, target_folder=target_folder, context_files=context_files,
        count_rule=f"- 지금은 선택지 없이 **결정 주제만** {FULL_DESIGN_MIN}~{want}개 고르세요. 이 앱을 실제로 만들 때 사용자가 직접 골라야 "
                   f"결과가 달라지는 것만(참고 영역: {areas}). 서로 겹치지 않게, 의존 순서대로(기술 → 데이터 → 인증 → 기능 규칙).")
    topic_prompt = topic_prompt.split("아래 JSON 형식으로만 응답하세요", 1)[0] + (
        '아래 JSON 형식으로만 응답하세요(설명 문장 금지):\n{"topics": [{"id": "storage", "question": "데이터를 어디에 저장할까요?", '
        '"impact": "DB 연결·배포 구성"}]}')
    try:
        resp = get_router().call(LLMRequest(prompt=topic_prompt, json_schema=_TOPIC_SCHEMA, max_tokens=1500, temperature=0.2),
                                 agent="code_agent", operation="generate_plan_topics")
        topics = [t for t in (_extract_json(resp.text or "").get("topics") or []) if isinstance(t, dict)
                  and str(t.get("id") or "").strip() and str(t.get("question") or "").strip()]
    except Exception as exc:  # noqa: BLE001 — 예전 방식으로 받는다
        print(f"[code_agent] 설계 주제 받기 실패 → 한 번에 받기: {exc}", flush=True)
        return None
    topics = topics[:want]
    if len(topics) < 2:
        return None
    print(f"[code_agent] 설계 주제 {len(topics)}개: {[t.get('id') for t in topics]}", flush=True)

    def cards(chunk: list[dict]) -> list[dict]:
        listing = "\n".join(f"- id={t['id']}: {t['question']}" + (f" (영향: {t.get('impact')})" if t.get("impact") else "")
                            for t in chunk)
        others = ", ".join(str(t["question"]) for t in topics if t not in chunk)
        p = _build_plan_prompt(
            instruction, existing, open_file, target_folder=target_folder, context_files=context_files,
            count_rule=f"- 아래 주제마다 결정 카드를 **하나씩, 주제의 id 그대로** 만드세요(다른 주제는 다른 에이전트가 만듭니다: {others}).\n"
                       f"{listing}\n- 선택지 summary 는 한 줄, pros·cons 는 각 1~2개로 짧게.")
        for attempt in range(2):
            try:
                r = get_router().call(LLMRequest(prompt=p, json_schema=PLAN_SCHEMA, max_tokens=_PLAN_MAX_TOKENS, temperature=0.2),
                                      agent="code_agent", operation="generate_plan")
                got = _extract_json(r.text or "").get("decisions") or []
                by_id = {canonical_key(d.get("id")): d for d in got if isinstance(d, dict)}
                out = []
                for i, t in enumerate(chunk):
                    d = by_id.get(canonical_key(t["id"])) or (got[i] if i < len(got) and isinstance(got[i], dict) else None)
                    if d:
                        out.append(dict(d, id=t["id"], question=d.get("question") or t["question"],
                                        impact=d.get("impact") or t.get("impact") or ""))
                return out
            except Exception as exc:  # noqa: BLE001
                print(f"[code_agent] 설계 카드 받기 실패({attempt + 1}/2): {exc}", flush=True)
                p += "\n\n[재시도] JSON 하나만, 문자열 안 큰따옴표 없이, 선택지는 주제마다 2~3개로 짧게."
        return []

    chunks = [topics[i:i + 3] for i in range(0, len(topics), 3)]
    with ThreadPoolExecutor(max_workers=min(3, len(chunks))) as pool:
        results = list(pool.map(cards, chunks))
    decisions = [d for part in results for d in part]
    return decisions or None


def generate_plan(
    instruction: str,
    session_id: str = "",
    open_file: dict | None = None,
    target_folder: str = "",
    context_files: list[dict] | None = None,
    project_root: str = "",
    after_starter: str = "",
) -> dict:
    """
    자연어 instruction → "설계 결정" 목록 (코드 아님).

    after_starter="custom": 쇼핑몰 시작 방식에서 "AI 자유 생성" 을 고른 뒤 이어서 묻는 결정을 만든다.
        고정 기반 카드는 건너뛰고, 이 앱의 기술 구성(데이터 저장·로그인·결제 등) 결정을 AI 에게 받는다.

    AI-DLC 1단계: 에이전트가 코드를 바로 뱉지 않고, 사람이 선택·승인할
    결정 카드 목록을 반환한다. 확장(webview)이 이를 팝업 카드로 렌더하고,
    사용자가 각 결정을 선택하면 그 결과(decisions: [{id, chosen_key}])를
    담아 /api/code/generate 를 다시 호출한다.

    project_root: 이 요청의 워크스페이스 루트. 비우면 전역 설정을 따른다.
        동시 요청이 서로의 루트를 덮어쓰지 않도록 호출자가 명시하는 것을 권장.

    반환:
        {"decisions": [{id, question, options: [...], impact}], "model": str}
    """
    instruction = (instruction or "").strip()
    if not instruction:
        raise ValueError("instruction 이 비어 있습니다.")

    root = _resolve_root(project_root)
    existing = _list_project_files(root)
    from commerce_starter import matches as commerce_matches, decision as commerce_decision
    from commerce_starter import followups as commerce_followups
    if not existing and commerce_matches(instruction) and after_starter != "custom":
        #: 시작 방식 하나만 묻고 끝나면 고를 이유가 없다 — 고른 쪽에 따라 이어서 물을 결정을 함께 알려 준다.
        return {"decisions": [commerce_decision()], "followups": commerce_followups(),
                "model": "reviewed-commerce-v1", "provider": "starter"}
    import payment_contract
    #: 결제가 들어가는 새 앱이면 "결제 시작 방식(모의 결제 / 실제 키)" 을 함께 묻는다 — 고른 값이 첫 로컬 배포를 정한다.
    #: AI 가 처음부터 만드는 앱도 쇼핑몰 기반과 같은 모의 결제 약속을 지키게 해, 키가 없어도 배포까지 이어진다.
    ask_payment = not existing and (after_starter == "custom" or payment_contract.applies(instruction))
    if after_starter == "custom":
        instruction = (instruction + "\n\n[이어서 묻는 결정] 사용자는 ReCoder 제공 쇼핑몰 기반 대신 AI 자유 생성을 골랐습니다. "
                       "이 앱의 기술 구성·기능 규칙 중 사용자가 직접 골라야 할 결정을 제시하세요. "
                       "각 결정의 선택지는 실제로 서로 다른 구현이어야 합니다.")
    if ask_payment:
        instruction += payment_contract.PLAN_NOTE
    print(f"[code_agent] 설계 결정 생성 시작 | 세션: {session_id} | 요청: {instruction[:80]!r} | 기존파일 {len(existing)}개")

    prompt = _build_plan_prompt(
        instruction,
        existing,
        open_file,
        context_files=context_files,
        target_folder=target_folder,
    )

    #: 깨진 JSON(응답 잘림·설명 문장 혼입)과 빈 응답은 **재시도로 살릴 수 있는
    #: 실패**다. 예전에는 한 번 깨지면 그대로 500 이었고 사용자에게는 "설계
    #: 결정 생성 실패"만 남았다. 한 번은 교정 지시를 붙여 다시 시도한다.
    llm_resp = None
    data: dict | None = None
    last_parse_error: Exception | None = None
    #: 새 앱(빈 프로젝트)은 설계를 넉넉히 — 주제를 먼저 받고 카드를 나눠 받는다. 기존 프로젝트 수정은 예전처럼 1~3개.
    if not existing:
        room = MAX_DECISIONS - (1 if ask_payment else 0)
        full = _full_design_decisions(instruction, existing, open_file, context_files, target_folder, min(FULL_DESIGN_MAX, room))
        if full:
            data = {"decisions": full}
            llm_resp = type("Planned", (), {"model_used": "design-topics", "provider": ""})()
    for attempt in range(0 if data is not None else 2):
        attempt_prompt = prompt if attempt == 0 else (
            prompt
            + "\n\n[재시도] 직전 응답이 올바른 JSON 이 아니었습니다. 설명 문장 없이 "
              "위 형식의 JSON 객체 하나만 반환하세요. 분량이 길어지면 결정 개수를 "
              "줄여서라도 JSON 을 완결하세요. 문자열 안에서는 큰따옴표(\")를 쓰지 말고 "
              "pros·cons 는 선택지마다 짧게 2개 이하로 쓰세요."
        )
        try:
            llm_resp = get_router().call(
                LLMRequest(prompt=attempt_prompt, json_schema=PLAN_SCHEMA,
                           max_tokens=_PLAN_MAX_TOKENS, temperature=0.2),
                agent="code_agent",
                operation="generate_plan",
            )
        except LLMError as e:
            if e.error_type == LLMErrorType.STRUCTURED_OUTPUT:
                last_parse_error = RuntimeError(str(e) or "모델 출력이 응답 길이 제한에서 잘렸습니다.")
                print(f"[code_agent] plan 구조화 출력 실패 (시도 {attempt + 1}/2): {e}", flush=True)
                continue
            from llm.failure import public_ai_failure_reason
            log.warning('Design generation provider failure: %s', e)
            guidance = ' 잠시 후 같은 요청으로 다시 시도해 주세요.' if e.retryable else ' AI 연결 설정을 확인해 주세요.'
            raise RuntimeError(f"LLM 호출 실패: {public_ai_failure_reason(e)}{guidance}") from e
        except Exception as e:
            raise RuntimeError(f"LLM 호출 실패: {e}") from e

        raw = (llm_resp.text or "").strip()
        if not raw:
            last_parse_error = RuntimeError("LLM 이 빈 응답을 반환했습니다.")
            print(f"[code_agent] plan 응답이 비어 있음 (시도 {attempt + 1}/2)")
            continue
        try:
            data = _extract_json(raw)
            break
        except Exception as e:  # noqa: BLE001 — ValueError / JSONDecodeError
            last_parse_error = e
            print(f"[code_agent] plan JSON 파싱 실패 (시도 {attempt + 1}/2): {e}")

    if data is None:
        raise RuntimeError(
            "LLM 응답을 JSON 으로 해석하지 못했습니다(2회 시도). "
            f"모델/할당량/응답 길이를 확인하세요. 마지막 오류: {last_parse_error}"
        )

    raw_decisions = data.get("decisions") or []
    if not isinstance(raw_decisions, list):
        raw_decisions = []
    decisions_out: list[dict] = []
    #: 여기서 **버려진 결정**은 지금까지 print 로그로만 남았다. 사용자 눈에는
    #: "AI 가 설계를 안 해준다"로 보인다 — 버린 이유를 응답에 실어 화면까지
    #: 보낸다(웹뷰가 결정 모달에 표시).
    dropped: list[str] = []
    seen_ids: set[str] = set()
    for d in raw_decisions:
        if not isinstance(d, dict):
            continue
        # 정규형으로 발급한다 — 게이트·기록이 쓰는 비교 형태와 처음부터 일치시켜
        # "제시한 키"와 "해석되는 키"가 갈라지지 않게 한다(adr.canonical_key).
        did = canonical_key(d.get("id"))
        # question 도 정규형으로 — 기록 단계가 어차피 같은 상한으로 자르므로,
        # 여기서 맞춰두지 않으면 "사용자가 본 질문"과 "ADR 제목"이 달라진다.
        question = canonical_key(d.get("question"))
        if not did or not question:
            dropped.append("형식이 불완전한 결정 1건 제외 (id 또는 question 누락)")
            continue
        # FR-02-05 — 예약 네임스페이스(`__`)는 내부 확인 카드 전용이다.
        # 모델이(또는 요청문에 유도되어) `__` 로 시작하는 id 를 만들어내면
        # 그 결정은 UI 에는 뜨지만 정규화 단계에서 걸러져, 사용자가 고른
        # 선택이 프롬프트에도 ADR 에도 반영되지 않는 채로 코드가 생성된다.
        # 조용히 사라지지 않도록 여기서 일반 id 로 옮겨 붙인다.
        if did.startswith(RESERVED_ID_PREFIX):
            did = f"d-{did.strip('_')}" if did.strip("_") else "decision"
        # id 는 웹뷰가 사용자의 선택을 저장하는 **키**다(selections[decision.id]).
        # 겹치면 뒤 카드가 앞 카드의 선택을 덮어써, 고르지도 않은 선택이
        # 코드 생성과 ADR 에 실린다. 모델이 같은 id 를 두 번 주는 경우와
        # 위 재배치가 기존 id 와 충돌하는 경우(`__auth` + `d-auth`) 둘 다
        # 여기서 걸러, plan 전체에서 id 유일성을 보장한다.
        did = _unique_canonical_id(did, seen_ids)
        seen_ids.add(did)
        raw_options = d.get("options") or []
        options_out: list[dict] = []
        seen_keys: set[str] = set()
        has_recommended = False
        for opt in raw_options:
            if not isinstance(opt, dict):
                continue
            key = canonical_key(opt.get("key"))
            if not key or key in seen_keys:
                # 정규형이 겹치는 선택지는 기록 단계에서 서로를 덮어써
                # 고르지 않은 쪽의 라벨·근거가 ADR 에 남는다. 제시 자체를 막는다.
                if key:
                    print(f"[code_agent] 선택지 key 중복으로 제외: {key[:40]!r}")
                continue
            seen_keys.add(key)
            recommended = bool(opt.get("recommended", False))
            has_recommended = has_recommended or recommended
            options_out.append({
                "key": key,
                # 라벨·요약·장단점도 기록 단계와 같은 형태로 맞춘다. 여기서
                # 자르지 않으면 사용자가 본 문구와 ADR 문구가 달라지고,
                # 숫자·불리언이 섞여 와도 여기서 문자열로 정리된다.
                "label": canonical_key(opt.get("label")) or key,
                "summary": canonical_key(opt.get("summary")),
                "pros": _clean_str_list(opt.get("pros")),
                "cons": _clean_str_list(opt.get("cons")),
                "recommended": recommended,
            })
            if len(options_out) >= MAX_OPTIONS_PER_DECISION:
                # 기록 단계는 '검토한 대안'을 MAX_ALTERNATIVES 개까지만 남긴다.
                # 그보다 많이 제시하면 사용자가 본 대안이 ADR 에서 말없이 사라진다.
                break
        if len(options_out) < MIN_OPTIONS_PER_DECISION:
            # 선택지가 하나뿐이면 '고를 수 있다'는 전제가 성립하지 않는다.
            # 사용자는 유일한 항목을 누를 수밖에 없고, 그건 승인이 아니라
            # 통과 의식이다. 정규형 충돌로 하나만 남은 경우도 여기서 걸린다.
            print(f"[code_agent] 선택지가 {len(options_out)}개뿐이라 결정 제외: {did[:40]!r}")
            dropped.append(
                f"「{question[:40]}」 — 유효한 선택지가 {len(options_out)}개뿐이라 제외"
            )
            continue
        if not has_recommended:
            # 모델이 recommended 를 하나도 표시하지 않았으면 첫 옵션을 기본 추천으로.
            options_out[0]["recommended"] = True
        decisions_out.append({
            "id": did,
            "question": question,
            "options": options_out,
            "impact": canonical_key(d.get("impact")),
        })
        # 정규화(normalize_decisions)는 MAX_DECISIONS 를 넘는 결정을 잘라낸다.
        # 그보다 많이 제시하면 사용자가 승인한 결정 중 일부가 코드에도 ADR 에도
        # 반영되지 않는다. 애초에 제시 단계에서 같은 상한을 지켜 그 상황을 막고,
        # 잘린 사실은 로그로 드러낸다(조용히 사라지지 않게).
        if len(decisions_out) >= MAX_DECISIONS:
            # 상한 때문에 못 본 것만 센다 (형식 불량으로 걸러진 것과 섞지 않는다).
            seen_so_far = raw_decisions.index(d) + 1
            over_cap = len(raw_decisions) - seen_so_far
            if over_cap > 0:
                print(f"[code_agent] 결정 {over_cap}개가 상한({MAX_DECISIONS})을 넘어 제외됨")
                dropped.append(f"결정 {over_cap}개가 상한({MAX_DECISIONS}개)을 넘어 제외")
            break

    # FR-02-05 (ADR-D5 항상 선택지 · D6 사람 승인)
    # 설계 결정이 없다고 판단된 요청이라도 **빈 목록을 돌려주지 않는다**.
    # 빈 목록이면 확장이 결정 카드를 건너뛰고 곧장 생성으로 넘어가, 사람이
    # 승인하지 않은 코드가 만들어진다(= AI 가 혼자 판단). 그래서 최소 한 장의
    # 확인 카드를 항상 보장한다. 이 카드는 설계 결정이 아니므로 예약 id 를
    # 사용해 ADR 로는 기록하지 않는다(adr.RESERVED_ID_PREFIX).
    if ask_payment:
        #: AI 가 결제 시작 방식을 스스로 물었으면(지시를 어김) 그 카드는 빼고 ReCoder 의 카드 하나만 남긴다.
        decisions_out = [d for d in decisions_out if not _is_payment_mode_decision(d)]
        if len(decisions_out) >= MAX_DECISIONS:
            decisions_out = decisions_out[:MAX_DECISIONS - 1]
        decisions_out.append(payment_contract.payment_decision())
    if not decisions_out:
        decisions_out.append(_build_confirm_decision(instruction))
        print("[code_agent] 설계 결정 없음 → 확인 카드로 대체 (항상 사람 승인)")

    result = {
        "decisions": decisions_out,
        #: 걸러진 결정의 사유 목록. 비어 있지 않으면 웹뷰가 결정 모달에
        #: 표시한다 — "설계를 안 해준다"가 아니라 "제시됐지만 형식 문제로
        #: 제외됐다"를 사용자가 알 수 있게.
        "dropped": dropped,
        "model": getattr(llm_resp, "model_used", ""),
        # 라이브 스모크처럼 특정 공급자(Bedrock)를 검증해야 하는 호출자가
        # 폴백 결과를 성공으로 오인하지 않도록 실제 공급자도 함께 돌려준다.
        "provider": getattr(llm_resp, "provider", ""),
    }
    print(f"[code_agent] 설계 결정 생성 완료 | 결정 {len(decisions_out)}개 | model={result['model']}")
    return result


def _relative_to_target(ops: list[dict], target_folder: str) -> list[dict]:
    """op 경로를 대상 폴더 기준으로 맞춘다. 모델이 'web/index.html' 처럼 대상 폴더를 붙여 돌려주면
    적용 단계에서 폴더가 한 번 더 붙어 web/web/index.html 이 생겼다(확장·ADR·삭제 경고는 대상 기준)."""
    folder = (target_folder or "").replace("\\", "/").strip().strip("/")
    if not folder or Path(folder).is_absolute() or re.match(r"^[A-Za-z]:", folder):
        return ops
    prefix = folder.lower() + "/"
    for op in ops:
        name = str(op.get("file") or "")
        if name.lower().startswith(prefix):
            op["file"] = name[len(prefix):]
    return ops


def _autofix_ops(root: Path, target_folder: str, ops: list[dict], _again: bool = True,
                 new_app: bool = False) -> tuple[list[dict], list[str]]:
    """AI 를 다시 부르지 않고 확실히 고칠 수 있는 것만 ops 에서 고친다.

    나눠서 만든 파일끼리 자주 어긋나는 것들(실기기 쇼핑몰): Vite 인데 JSX 가 든 .js,
    import 하지만 만들지 않은 CSS, CRA 식 process.env, 선언과 다른 패키지 이름.
    """
    try:
        from build_readiness import (add_missing_export, analyze, api_prefix_rewrite, dead_image_rewrite, jsx_reference_rewrite,
                                     pg_numeric_parser_rewrite, relative_api_rewrite, rename_package_import,
                                     router_link_rewrite, set_build_script, vite_env_rewrite, vite_out_dir_rewrite)
    except ImportError:  # pragma: no cover
        from core.build_readiness import (add_missing_export, analyze, api_prefix_rewrite, dead_image_rewrite,  # type: ignore
                                          jsx_reference_rewrite, pg_numeric_parser_rewrite, relative_api_rewrite,
                                          rename_package_import, router_link_rewrite, set_build_script,
                                          vite_env_rewrite, vite_out_dir_rewrite)
    _JS_EXTS = (".js", ".cjs", ".mjs")
    folder = (target_folder or "").replace("\\", "/").strip("/")
    base = (root / folder).resolve() if folder and not Path(folder).is_absolute() else (Path(folder) if folder else root.resolve())
    by_path = {str(op.get("file") or "").replace("\\", "/").lstrip("/"): op for op in ops}
    notes: list[str] = []
    manifests = ("package.json",)
    projects = sorted({posixpath.dirname(p) for p in by_path if posixpath.basename(p) in manifests})
    if (base / "package.json").is_file() or "package.json" in by_path:
        projects = [""]
    #: 한 번 고치면 다음 것이 보인다(내보내기를 붙여야 /api 중복 호출이 보인다) — 두 번 돈다.
    for project in [p for _ in range(2) for p in projects[:5]]:
        prefix = f"{project}/" if project else ""
        overlay = {p[len(prefix):]: op.get("content") or "" for p, op in by_path.items() if p.startswith(prefix)}
        try:
            data = analyze(base / project, overlay, dockerfile=None).fix_data
        except Exception as exc:  # noqa: BLE001 - 교정 실패는 원래 결과로 둔다
            print(f"[code_agent] 자동 교정 점검 생략: {exc}", flush=True)
            continue
        for importer, spec in data.get("missing_styles") or []:
            if prefix + importer not in by_path:
                continue
            rel = prefix + posixpath.normpath(posixpath.join(posixpath.dirname(importer), spec))
            if rel not in by_path and not (base / rel).exists():
                by_path[rel] = {"action": "create", "file": rel, "language": "css",
                                "content": "/* ReCoder: 코드가 불러오지만 AI 가 만들지 않은 스타일 파일 — 필요한 스타일을 채우세요. */\n",
                                "rationale": "import 하는 스타일 파일이 없어 빌드가 실패하지 않도록 빈 파일을 만들었습니다."}
                notes.append(f"{rel}: 빈 스타일 파일 추가")
        for rel in data.get("jsx_in_js") or []:
            op = by_path.pop(prefix + rel, None)
            if op is None:
                continue
            new_rel = prefix + rel[:-3] + ".jsx"
            op["file"] = new_rel
            by_path[new_rel] = op
            if (base / (prefix + rel)).exists():
                #: 디스크에 옛 .js 가 남는다(확장은 파일을 지우지 않는다). 이번 결과에 없는 파일이 './App' 처럼
                #: 확장자 없이 가져오면 Vite 는 .js 를 먼저 찾아 옛 코드를 쓴다 — 옛 파일을 새 .jsx 로 넘겨주는 파일로 바꾼다.
                content = op.get("content") or ""
                target = "./" + posixpath.basename(new_rel)
                stub = f"// ReCoder: {posixpath.basename(new_rel)} 로 옮겼습니다(JSX 는 .jsx 여야 Vite 가 읽습니다).\n"
                stub += f"export * from '{target}';\n"
                if re.search(r"\bexport\s+default\b|\bexport\s*\{[^}]*\bas\s+default\b", content):
                    stub += f"export {{ default }} from '{target}';\n"
                by_path[prefix + rel] = {"action": "edit", "file": prefix + rel, "language": "javascript", "content": stub,
                                         "rationale": f"내용을 {posixpath.basename(new_rel)} 로 옮기고 예전 경로는 그 파일을 다시 내보냅니다."}
            for other_path, other in by_path.items():
                if other_path.endswith((".html", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".vue")):
                    other["content"] = jsx_reference_rewrite(other.get("content") or "", other_path, prefix + rel, new_rel)
            notes.append(f"{prefix + rel} → {new_rel}(Vite JSX)")
        for rel in data.get("router_anchor_files") or []:
            op = by_path.get(prefix + rel)
            if op is not None:
                updated = router_link_rewrite(op.get("content") or "")
                if updated != op.get("content"):
                    op["content"] = updated
                    notes.append(f"{prefix + rel}: <a href> → <Link>")
        for rel in data.get("hardcoded_api") or []:
            op = by_path.get(prefix + rel)
            if op is not None:
                updated = relative_api_rewrite(op.get("content") or "")
                if updated != op.get("content"):
                    op["content"] = updated
                    notes.append(f"{prefix + rel}: localhost API 주소 → 상대 경로")
        for rel in data.get("dead_images") or []:
            op = by_path.get(prefix + rel)
            if op is not None:
                updated = dead_image_rewrite(op.get("content") or "")
                if updated != op.get("content"):
                    op["content"] = updated
                    notes.append(f"{prefix + rel}: 닫힌 이미지 서비스 주소 → placehold.co")
        for rel in data.get("pg_numeric_files") or []:
            op = by_path.get(prefix + rel)
            if op is not None:
                updated = pg_numeric_parser_rewrite(op.get("content") or "")
                if updated != op.get("content"):
                    op["content"] = updated
                    notes.append(f"{prefix + rel}: pg NUMERIC 숫자 파서 추가")
        for rel in data.get("process_env") or []:
            op = by_path.get(prefix + rel)
            if op is not None:
                op["content"] = vite_env_rewrite(op.get("content") or "")
                notes.append(f"{prefix + rel}: process.env → import.meta.env")
        for old_pkg, new_pkg in (data.get("package_typos") or {}).items():
            for op in by_path.values():
                updated = rename_package_import(op.get("content") or "", old_pkg, new_pkg)
                if updated != op.get("content"):
                    op["content"] = updated
                    notes.append(f"{op['file']}: {old_pkg} → {new_pkg}")
        #: 나눠 만든 서버 파일끼리 ESM(import)·CommonJS(require)가 섞이면 컨테이너가 시작 직후 죽는다(실기기 TEMP).
        #: 진입 파일 형식으로 통일한다. 이름을 바꿀 파일이 이번 결과에 없으면(디스크에만 있음) AI 교정에 맡긴다.
        fmt = data.get("module_format") or {}
        if fmt.get("auto") and all(prefix + old in by_path for old, _new in fmt.get("renames") or []):
            for old, new in fmt.get("renames") or []:
                op = by_path.pop(prefix + old)
                op["file"] = prefix + new
                by_path[prefix + new] = op
                notes.append(f"{prefix + old} → {prefix + new}(모듈 형식)")
            for rel, content in (fmt.get("writes") or {}).items():
                op = by_path.get(prefix + rel)
                if op is None:
                    op = {"action": "edit", "file": prefix + rel, "language": "javascript" if rel.endswith(_JS_EXTS) else "json",
                          "content": content, "rationale": "서버 파일들의 모듈 형식(ESM·CommonJS)을 하나로 맞췄습니다."}
                    by_path[prefix + rel] = op
                if op.get("content") != content:
                    op["content"] = content
                    notes.append(f"{prefix + rel}: 모듈 형식 통일")
        #: 나눠 만든 파일끼리 내보내기가 어긋남(선언만 하고 export 안 함, 기본 내보내기 없음, 객체 안의 함수를 이름으로 가져옴).
        for target, name, kind in data.get("missing_exports") or []:
            op = by_path.get(prefix + target)
            if op is None:
                continue
            updated = add_missing_export(op.get("content") or "", name, kind)
            if updated is not None and updated != op.get("content"):
                op["content"] = updated
                notes.append(f"{prefix + target}: {name.replace('default:', '기본 내보내기 ')} 내보내기")
        #: baseURL 이 /api 인 axios 인스턴스로 '/api/…' 를 또 부르면 /api/api/… 404
        for rel, names in (data.get("api_double_prefix") or {}).items():
            op = by_path.get(prefix + rel)
            if op is not None:
                updated = api_prefix_rewrite(op.get("content") or "", names)
                if updated != op.get("content"):
                    op["content"] = updated
                    notes.append(f"{prefix + rel}: /api 중복 제거")
        #: 서버가 제공하는 화면 폴더를 Docker 빌드(npm run build)가 만들도록 루트 build 를 잇는다.
        ui = data.get("frontend_build") or {}
        if ui.get("vite_config") and by_path.get(prefix + ui["vite_config"]) is not None:
            vite_op = by_path[prefix + ui["vite_config"]]
            updated = vite_out_dir_rewrite(vite_op.get("content") or "", ui["vite_out_to"])
            if updated is not None and updated != vite_op.get("content"):
                vite_op["content"] = updated
                notes.append(f"{prefix + ui['vite_config']}: outDir → {ui['vite_out_to']}(서버가 제공하는 폴더)")
        manifest_op = by_path.get(prefix + "package.json")
        if ui.get("build") and manifest_op is not None:
            try:
                updated = set_build_script(manifest_op.get("content") or "", ui["build"])
            except (ValueError, TypeError, AttributeError):
                updated = manifest_op.get("content")
            if updated != manifest_op.get("content"):
                manifest_op["content"] = updated
                notes.append(f"{prefix}package.json: build = {ui['build']}(화면 빌드 연결)")
    #: 점검이 고칠 내용을 미리 만들어 둔 것(빠진 하위 package.json·없는 tsconfig references·terser·프로젝트 밖 화면 경로·
    #: lock 없는 npm ci). 하나를 고치면 다음 것이 보이므로(package.json 이 생겨야 terser 가 보인다) 몇 번 되풀이한다.
    try:
        from build_readiness import FILE_WRITE_FIXES
    except ImportError:  # pragma: no cover
        from core.build_readiness import FILE_WRITE_FIXES  # type: ignore
    wrote_any = False
    for _pass in range(8):
        overlay = {p: op.get("content") or "" for p, op in by_path.items()}
        #: 루트와 하위 package.json 폴더를 각각 본다(루트 build 에 안 들어가는 하위 서버의 tsconfig 등).
        subs = sorted({posixpath.dirname(p) for p in by_path if posixpath.basename(p) == "package.json" and "/" in p})
        found_writes: list[tuple[str, str, str]] = []
        for project in [""] + subs[:6]:
            prefix = f"{project}/" if project else ""
            sub_overlay = {p[len(prefix):]: c for p, c in overlay.items() if p.startswith(prefix)}
            try:
                fixes = (analyze(base / project if project else base, sub_overlay,
                                 dockerfile="Dockerfile" if "Dockerfile" in sub_overlay else None)
                         .fix_data.get("file_writes") or {})
            except Exception as exc:  # noqa: BLE001
                print(f"[code_agent] 빌드 설정 자동 교정 생략({project or '루트'}): {exc}", flush=True)
                continue
            for code in FILE_WRITE_FIXES:
                if project and code in _ROOT_SCOPED:
                    continue
                for rel, content in (fixes.get(code) or {}).items():
                    found_writes.append((code, prefix + rel.replace("\\", "/").lstrip("/"), content))
        applied = 0
        touched: set[str] = set()
        for code, rel, content in found_writes:
            if rel in touched:
                continue  # 같은 파일의 다른 고침은 다음 점검에서(같은 원본에서 만든 두 고침이 서로 덮지 않게)
            op = by_path.get(rel)
            if op is not None and (op.get("content") == content or op.get("fixed")):
                continue  # 같거나, ReCoder 고정 파일(검증 Dockerfile·결제 모듈)
            if op is None and (base / rel).exists():
                continue  # 이번 결과에 없는 디스크 파일은 바꾸지 않는다(배포 준비 점검이 백업과 함께 고친다)
            by_path[rel] = dict(op or {"action": "create", "file": rel, "language": "json" if rel.endswith(".json") else ""},
                                content=content, rationale=(op or {}).get("rationale") or f"ReCoder 자동 교정({code})")
            notes.append(f"{rel}: {code}")
            touched.add(rel)
            applied += 1
        if not applied:
            break
        wrote_any = True
    #: 새로 만든 tsconfig — strict 는 두고, 동작과 무관한 린트성 검사(미사용 변수·인덱스 접근 undefined)만 끈다.
    #: AI 코드의 tsc 빌드 실패 대부분이 이것이었다(실기기 TEMP: 40건 중 30건).
    try:
        import node_fixups
        for rel, op in by_path.items():
            #: 새로 만드는 tsconfig — 사용자가 이미 가진 프로젝트의 tsconfig 는 바꾸지 않는다(새 앱 생성이면 다시 쓴 것도)
            if re.fullmatch(r"tsconfig(?:\.[\w-]+)?\.json", posixpath.basename(rel)) and op.get("content") and (
                    new_app or (op.get("action") == "create" and not (base / rel).exists())):
                relaxed = node_fixups.relax_generated_tsconfig(op.get("content") or "")
                if relaxed and relaxed != op.get("content"):
                    op["content"] = relaxed
                    notes.append(f"{rel}: 린트성 검사(미사용 변수 등)가 빌드를 막지 않게")
    except Exception as exc:  # noqa: BLE001
        print(f"[code_agent] tsconfig 교정 생략: {exc}", flush=True)
    # AI 가 Dockerfile 도 만들었으면 하위 폴더(client/·server/) 의존성 설치를 채운다.
    docker_op = by_path.get("Dockerfile")
    if docker_op is not None:
        try:
            from build_readiness import ProjectFiles, add_runtime_subproject_install, add_subproject_installs
        except ImportError:  # pragma: no cover
            from core.build_readiness import ProjectFiles, add_runtime_subproject_install, add_subproject_installs  # type: ignore
        overlay = {p: op.get("content") or "" for p, op in by_path.items()}
        try:
            readiness = analyze(base, overlay, dockerfile="Dockerfile")
            content = docker_op.get("content") or ""
            codes = {i.code for i in readiness.issues}
            if "DOCKERFILE_SUBPROJECT_DEPS_MISSING" in codes:
                content = add_subproject_installs(content, readiness.subprojects, ProjectFiles(base, overlay))
            if "DOCKERFILE_RUNTIME_SUBPROJECT_DEPS_MISSING" in codes and readiness.runtime_subproject:
                content = add_runtime_subproject_install(content, readiness.runtime_subproject)
            if content != docker_op.get("content"):
                docker_op["content"] = content
                notes.append("Dockerfile: 하위 폴더 의존성 설치 추가")
        except Exception as exc:  # noqa: BLE001
            print(f"[code_agent] Dockerfile 자동 교정 생략: {exc}", flush=True)
    if wrote_any and _again:
        #: 새 파일·import 를 넣으면 앞 단계가 고칠 것(내보내기·/api 중복 등)이 새로 보인다 — 한 번 더 돈다.
        more_ops, more_notes = _autofix_ops(root, target_folder, list(by_path.values()), _again=False, new_app=new_app)
        return more_ops, notes + [n for n in more_notes if n not in notes]
    return list(by_path.values()), notes


def _export_names(text: str) -> list[str]:
    names = re.findall(r"\bexport\s+(?:declare\s+)?(?:default\s+)?(?:async\s+)?(?:abstract\s+)?(?:const\s+enum|const|let|var|function\*?|class|interface|type|enum)\s+([\w$]+)", text)
    for group in re.findall(r"\bexport\s*\{([^}]*)\}", text):
        names += [p.split(" as ")[-1].strip() for p in group.split(",") if p.strip()]
    if re.search(r"\bexport\s+default\b", text):
        names.append("default")
    names += re.findall(r"\b(?:module\.)?exports\.([\w$]+)\s*=", text)
    return list(dict.fromkeys(n for n in names if n))


def _name_tokens(name: str) -> set[str]:
    return {t.lower() for t in re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+", name or "") if len(t) > 2}


def _name_mismatch_issues(files, details: list, prefix: str = "") -> list[dict]:
    """(불러오는 파일, 이름, 내보내는 파일) → 고칠 파일마다 한 건. 내보내는 쪽에 비슷한 이름이 있으면 쓰는 쪽을, 없으면
    내보내는 쪽(선언·export)을 지목하고, 판단에 필요한 사실(실제 export 목록·쓰는 줄)을 함께 담는다."""
    by_target: dict[str, list[tuple[str, str]]] = {}
    for importer, name, target in details:
        by_target.setdefault(target, []).append((importer, name))
    out: list[dict] = []
    per_file: dict[str, list[str]] = {}
    for target, uses in by_target.items():
        text = files.read(target) or ""
        exported = _export_names(text)
        shown = ", ".join(exported[:30]) or "(없음)"
        for importer, name in uses:
            similar = [e for e in exported if e != "default" and _name_tokens(e) & _name_tokens(name)] if name != "default" else \
                [e for e in exported if e != "default"]
            if similar:
                per_file.setdefault(importer, []).append(
                    f"{target} 에서 불러오는 `{name}` 이(가) 그 파일에 없습니다. {target} 가 실제로 내보내는 이름: {shown}. "
                    f"같은 일을 하는 이름({', '.join(similar[:4])})으로 바꿔 쓰세요(인자·반환 모양을 그 함수에 맞게)."
                    if name != "default" else
                    f"{target} 에는 기본 내보내기(default)가 없습니다. 실제로 내보내는 이름: {shown}. "
                    f"`import {{ {similar[0]} }} from …` 처럼 이름으로 불러오세요.")
            else:
                usage = [l.strip() for l in (files.read(importer) or "").splitlines()
                         if re.search(rf"\b{re.escape(name)}\b", l) and not l.lstrip().startswith("import")][:4]
                per_file.setdefault(target, []).append(
                    f"{importer} 가 이 파일에서 `{name}` 을(를) 불러오는데 없습니다(이 파일이 내보내는 이름: {shown}). "
                    f"쓰는 쪽에 맞게 선언하고 export 하세요." + (f" 쓰는 곳: {' / '.join(usage)}" if usage else ""))
    for rel, problems in per_file.items():
        out.append({"code": "NODE_IMPORT_NAME_MISSING", "severity": "error", "file": prefix + rel,
                    "message": " ".join(problems)[:1800],
                    "fix": "실제로 있는 이름으로 맞추거나 빠진 것을 구현해 export 하세요. 다른 파일을 지우거나 기능을 빼지 마세요."})
    return out


#: 프로젝트 전체를 봐야 판단할 수 있는 문제 — 루트를 점검할 때 하위 폴더 단독 점검의 같은 문제는 버린다.
_ROOT_SCOPED = {"NODE_STATIC_PATH_OUTSIDE_PROJECT", "NODE_STATIC_DIR_MISSING", "NODE_WORKSPACE_MANIFEST_MISSING"}


def _consistency_issues(root: Path, target_folder: str, ops: list[dict]) -> list[dict]:
    """ops 를 적용했을 때 **새로 생기는** 빌드·실행 문제(build_readiness)만 돌려준다.

    기존 프로젝트에 원래 있던 문제로 사용자 요청 범위를 넘는 수정을 요구하지 않도록
    적용 전/후를 비교한다. 매니페스트(package.json·requirements)가 있는 폴더만 본다.
    """
    try:
        from build_readiness import analyze
    except ImportError:  # pragma: no cover
        from core.build_readiness import analyze  # type: ignore
    folder = (target_folder or "").replace("\\", "/").strip("/")
    base = root.resolve()
    if folder and not Path(folder).is_absolute():
        base = (root / folder).resolve()
    elif folder:
        base = Path(folder).resolve()
    written: dict[str, str] = {}
    for op in ops:
        name = str(op.get("file") or "").replace("\\", "/").lstrip("/")
        parts = [p for p in name.split("/") if p and p not in {".", ".."}]
        if parts:
            written["/".join(parts)] = str(op.get("content") or "")
    manifests = ("package.json", "requirements.txt", "pyproject.toml")
    projects: set[str] = set()
    for rel in written:
        path = posixpath.split(rel)
        if path[1] in manifests:
            projects.add(path[0])
    if any((base / m).is_file() for m in manifests):
        projects.add("")
    found: list[dict] = []
    root_seen: set[tuple[str, str]] = set()
    name_seen: set[tuple[str, str]] = set()
    for project in sorted(projects)[:5]:
        prefix = f"{project}/" if project else ""
        overlay = {rel[len(prefix):]: content for rel, content in written.items() if rel.startswith(prefix)}
        try:
            before = {(i.code, i.message) for i in analyze(base / project, dockerfile=None).issues}
            #: 새로 쓰는 package.json 은 버전이 npm 에 실제로 있는지도 본다 — AI 가 없는 버전
            #: (jsonwebtoken@^9.1.2 등)을 적어 Docker 빌드가 ETARGET 으로 멈췄다(실기기).
            after = analyze(base / project, overlay, dockerfile="Dockerfile" if "Dockerfile" in overlay else None,
                            online="package.json" in overlay and os.environ.get("RECODER_TEST_MODE") != "1")
        except Exception as exc:  # noqa: BLE001 - 점검 실패가 생성을 막지 않는다
            print(f"[code_agent] 일관성 점검 생략: {exc}", flush=True)
            continue
        details = after.fix_data.get("missing_names_detail") or []
        if details and project and "" in projects:
            details_shown = True  # 루트 점검이 같은 파일들을 이미 파일별로 냈다
        else:
            details_shown = False
        if details and not details_shown:
            #: 이름이 어긋난 문제는 묶음 한 건이 아니라 **고칠 파일마다** 한 건씩, 그 파일이 실제로 내보내는 이름을 함께 —
            #: 묶음 한 건은 첫 파일만 지목해 나머지가 끝까지 남았다(실기기 TEMP 2.0.6: getOrdersByAdmin·apiClient).
            try:
                from build_readiness import ProjectFiles as _PF
            except ImportError:  # pragma: no cover
                from core.build_readiness import ProjectFiles as _PF  # type: ignore
            for item in _name_mismatch_issues(_PF(base / project if project else base, overlay), details, prefix):
                key = (item["file"], item["message"])
                if key not in name_seen:
                    name_seen.add(key)
                    found.append(item)
        for issue in after.issues:
            if (issue.code, issue.message) in before or issue.code == "DOCKERIGNORE_MISSING":
                continue
            if details and issue.code in ("NODE_IMPORT_NAME_MISSING", "NODE_IMPORT_NAME_UNDEFINED"):
                continue  # 위에서 파일별로 냈다
            if project and "" in projects and (issue.code in _ROOT_SCOPED or (issue.code, prefix + (issue.file or "")) in root_seen):
                #: 루트 프로젝트가 함께 점검한다 — 하위 폴더만 따로 보면 폴더 밖(../frontend/dist)을 잘못 문제 삼고, 같은 문제가 두 번 나온다.
                continue
            if not project:
                root_seen.add((issue.code, issue.file or ""))
            item = issue.to_dict()
            if project:
                item["message"] = f"[{project}] {item['message']}"
                if item.get("file"):
                    item["file"] = prefix + item["file"]
            found.append(item)
    return found


# ── 큰 요청: 대규모 생성 엔진(gen_engine)으로 나눠서 끝까지 만든다 ─────────────────
#
# "결제까지 되는 쇼핑몰 만들어줘" 처럼 큰 요청은 응답 한 번(게이트웨이 30초·4096 토큰)에 담기지
# 않는다. 예전에는 파일 36개에서 목록을 조용히 자르고, 파일 하나가 한도를 넘으면 실패했다.
# 지금은 gen_engine 이 설계(약속·목록, 상한 없이 이어 받기) → 기반 파일 순서대로 → 기능·화면 동시에
# → 큰 파일 이어 쓰기 → 파일마다 즉시 확인·교정 → 체크포인트(이어서 만들기) 로 끝까지 만든다.
#: 생성 결과의 빌드·실행 문제를 AI 에게 고치게 하는 최대 횟수(결정적 자동 교정은 매번 먼저 한다).
_CONSISTENCY_ROUNDS = 6
#: 확장이 응답 하나를 기다리는 예전 경로(/api/code/generate)용 교정 시간 예산.
#: 스트림 경로는 확장이 진행 이벤트를 받으므로 이 예산을 넉넉히 준다(generate_code(budget_seconds=)).
_GENERATION_BUDGET_SECONDS = int(os.environ.get("RECODER_GENERATION_BUDGET_SECONDS", "540"))


def _is_truncation(exc: Exception) -> bool:
    return isinstance(exc, LLMError) and exc.error_type == LLMErrorType.STRUCTURED_OUTPUT and "잘렸" in str(exc)


def _norm_op_path(path: str) -> str:
    out = posixpath.normpath(str(path or "").strip().replace("\\", "/"))
    while out.startswith("./"):
        out = out[2:]
    return out.lstrip("/").casefold()


_CODE_EXTS = (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".vue")


def _complete_fullstack_manifest(files: list[dict]) -> list[dict]:
    """A fresh frontend/backend project needs a runnable deployment root.

    코드가 든 화면·서버 폴더(frontend/·backend/ 등)에 package.json 이 목록에 없으면 함께 만든다 — AI 설계가 자주
    빠뜨렸고(실기기 TEMP 쇼핑몰), 그러면 의존성이 어디에도 선언되지 않아 설치·빌드가 시작부터 실패했다.
    """
    files = list(files)
    paths = {f["file"] for f in files}
    for folder in ("frontend", "client", "web", "backend", "server", "api"):
        code = [p for p in paths if p.startswith(folder + "/") and p.endswith(_CODE_EXTS)]
        if not code:
            continue
        ts = any(p.endswith((".ts", ".tsx")) for p in code)
        ui = folder in ("frontend", "client", "web")
        if folder + "/package.json" not in paths:
            files.append({"file": f"{folder}/package.json", "layer": 0, "size": "small",
                          "purpose": (f"{folder} dependencies (exactly the packages its code imports, stable versions) and scripts: "
                                      + ("dev=vite, build=vite build" if ui else
                                         ("build=tsc, start=node dist/<entry>.js, dev=tsx watch src/<entry>.ts" if ts else "start=node <entry>.js")))})
            paths.add(f"{folder}/package.json")
        if ts and folder + "/tsconfig.json" not in paths:
            files.append({"file": f"{folder}/tsconfig.json", "layer": 0, "size": "small",
                          "purpose": ("TypeScript config for the Vite React app (strict, jsx react-jsx, noEmit, no project references)" if ui else
                                      "TypeScript config for the Node server (strict, rootDir src, outDir dist, ESM module NodeNext)")})
            paths.add(f"{folder}/tsconfig.json")
    client = next((p for p in ("frontend", "client", "web") if p + "/package.json" in paths), None)
    server = next((p for p in ("backend", "server", "api") if p + "/package.json" in paths), None)
    if not client or not server:
        return files
    required = {
        "package.json": f"root build/start/init-db scripts that invoke {client} and {server}; install both projects",
        "Dockerfile": f"multi-stage build of {client}; non-root {server} runtime serving the built frontend on one port",
        ".dockerignore": "exclude .env, credentials, node_modules, .git, tests, local artifacts",
        #: 짧게 — 길게 쓰라고 하면 응답 길이 한도를 매번 넘겨 생성이 README 에서 멈췄다(실기기 2026-10-10).
        "README.md": "concise run guide: install, env var names with <placeholders>, DB init, start, Docker commands, payment mode note (a few lines each)",
    }
    return files + [{"file": p, "purpose": purpose} for p, purpose in required.items() if p not in paths]


def _generate_split(prompt: str, target_folder: str = "", *, new_project: bool = False,
                    job_id: str = "", fingerprint: str = "", resume: dict | None = None,
                    concurrency: int | None = None, skip_failed: bool = False,
                    plan_hook=None) -> tuple[dict, list[dict], object]:
    """큰 요청을 대규모 생성 엔진으로 만든다. 멈추면 GenerationPaused(이어서 만들기 가능)."""
    try:
        import gen_engine
        import generation_progress
    except ImportError:  # pragma: no cover - 패키지 실행
        from core import gen_engine  # type: ignore
        from core import generation_progress  # type: ignore
    engine = gen_engine.LargeGeneration(
        prompt, target_folder=target_folder, new_project=new_project, job_id=job_id,
        fingerprint=fingerprint, concurrency=concurrency, emit=generation_progress.current(),
        skip_failed=skip_failed, plan_hook=plan_hook,
    )
    engine.resume_from(resume)
    return engine.run()


def _completed_files_context(ops: list[dict], limit: int = 64_000) -> str:
    """Bound source context; omit whole files instead of presenting truncated code."""
    blocks, used = [], 0
    for op in ops:
        if not op.get("content") or str(op.get("file", "")).endswith((".css", ".md")):
            continue
        block = f"\n[이미 생성한 파일 — 이 API/스키마/export를 그대로 사용] {op['file']}\n```\n{op['content']}\n```\n"
        if used + len(block) <= limit:
            blocks.append(block)
            used += len(block)
    return "".join(blocks)


def _merge_ops(base: list[dict], updates: list[dict]) -> list[dict]:
    """교정 결과를 파일 경로 기준으로 덮어쓴다. 교정 응답에 없는 파일은 그대로 둔다.
    ReCoder 가 넣은 고정 파일(결제 모듈)은 AI 교정이 덮어쓰지 못한다."""
    index = {_norm_op_path(op["file"]): i for i, op in enumerate(base)}
    merged = list(base)
    for op in updates:
        key = _norm_op_path(op["file"])
        if key in index and merged[index[key]].get("fixed"):
            continue
        if key in index:
            merged[index[key]] = op
        else:
            index[key] = len(merged)
            merged.append(op)
    return merged


def _repair_context(ops: list[dict], issues: list[dict], limit: int = 80_000) -> str:
    """Issue files first, then manifests and dependencies; never cut a file body."""
    wanted = {_norm_op_path(i.get("file") or "") for i in issues if i.get("file")}
    messages = "\n".join(i.get("message", "") for i in issues)
    def priority(op):
        path = _norm_op_path(op["file"])
        return (0 if path in wanted or op["file"] in messages else
                1 if posixpath.basename(path) in {"package.json", "dockerfile", "tsconfig.json", "schema.sql"} else
                3 if path.endswith((".css", ".md")) else 2)
    blocks, used = [], 0
    for op in sorted(ops, key=priority):
        block = f"\n[현재 파일] {op['file']}\n```\n{op['content']}\n```\n"
        if used + len(block) <= limit:
            blocks.append(block)
            used += len(block)
    return "".join(blocks)


def _issue_weight(issues: list[dict]) -> int:
    # A broken manifest hides many downstream errors. Repairing it is progress
    # even when reading the now-valid manifest reveals more missing dependencies.
    keys = {(i['code'], i.get('file', '')) for i in issues if i['severity'] == 'error'}
    return sum(100 if code == 'NODE_PACKAGE_JSON_INVALID' else 1 for code, _ in keys)


def _apply_docker_kit(root: Path, target_folder: str, ops: list[dict]) -> tuple[list[dict], bool]:
    """새로 만든 화면·서버 폴더형 Node 앱이면 Dockerfile 을 ReCoder 검증본(고정 파일)으로 쓴다.

    AI 가 쓴 Dockerfile 이 매번 다른 방식으로 깨졌다(실기기 TEMP 2.0.6: 빌드 단계 --omit=dev → tsc 없음, 실행 단계에
    backend/package.json 없음 → No workspaces found). 구조를 확신할 수 없으면 AI 것을 그대로 둔다(정적 점검이 고친다).
    """
    try:
        import docker_kit
        from build_readiness import ProjectFiles
    except ImportError:  # pragma: no cover
        from core import docker_kit  # type: ignore
        from core.build_readiness import ProjectFiles  # type: ignore
    folder = (target_folder or "").replace("\\", "/").strip("/")
    base = (root / folder).resolve() if folder and not Path(folder).is_absolute() else (Path(folder) if folder else root.resolve())
    overlay = {str(op.get("file") or "").replace("\\", "/").lstrip("/"): op.get("content") or "" for op in ops if op.get("action") != "delete"}
    try:
        info = docker_kit.layout(ProjectFiles(base, overlay))
    except Exception as exc:  # noqa: BLE001
        print(f"[code_agent] Dockerfile 검증본 판단 생략: {exc}", flush=True)
        return ops, False
    if not info:
        return ops, False
    content = docker_kit.render(info)
    out = [op for op in ops if str(op.get("file") or "").replace("\\", "/").lstrip("/") not in ("Dockerfile",)]
    current = next((op for op in ops if str(op.get("file") or "").replace("\\", "/").lstrip("/") == "Dockerfile"), None)
    if current is not None and current.get("content") == content:
        current["fixed"] = True  # 점검의 자동 수정이 이미 검증본으로 바꿨다 — AI 교정이 덮지 않게
        return ops, False
    out.append({"action": "edit" if (base / "Dockerfile").exists() else "create", "file": "Dockerfile", "language": "dockerfile",
                "content": content, "fixed": True,
                "rationale": "ReCoder 검증 Dockerfile — 화면·서버 폴더를 각각 설치·빌드하고 실행 이미지에는 운영 의존성과 빌드 결과만"})
    ignore = next((op for op in out if str(op.get("file") or "").lstrip("/") == ".dockerignore"), None)
    if ignore is None or "node_modules" not in (ignore.get("content") or ""):
        out = [op for op in out if str(op.get("file") or "").lstrip("/") != ".dockerignore"]
        out.append({"action": "create", "file": ".dockerignore", "language": "", "content": docker_kit.DOCKERIGNORE,
                    "fixed": True, "rationale": "ReCoder: Docker 빌드에서 제외할 파일"})
    return out, True


def _build_failure_issues(verification: dict, ops: list[dict]) -> list[dict]:
    """컨테이너 빌드 실패 로그 → 소스 파일별 문제(tsc·vite 오류 줄). 못 뽑으면 빈 목록."""
    try:
        import node_fixups
        docker = next((op.get("content") or "" for op in ops if _norm_op_path(op.get("file", "")) == "dockerfile"), "")
        return node_fixups.build_log_issues(str(verification.get("log") or verification.get("output") or ""),
                                            [op["file"] for op in ops if op.get("file")], docker,
                                            {op["file"]: str(op.get("content") or "") for op in ops if op.get("file")})
    except Exception as exc:  # noqa: BLE001
        print(f"[code_agent] 빌드 로그 분석 생략: {exc}", flush=True)
        return []


def _verify_generated_build(root: Path, target_folder: str, ops: list[dict]) -> dict:
    from generated_validation import verify_proposal
    base = (root / target_folder).resolve() if target_folder else root.resolve()
    return verify_proposal(base, ops)


def generate_code(
    instruction: str,
    session_id: str = "",
    open_file: dict | None = None,
    prior_files: list[dict] | None = None,
    context_files: list[dict] | None = None,
    target_folder: str = "",
    decisions: list | None = None,
    project_root: str = "",
    mode: str = "auto",
    job_id: str = "",
    budget_seconds: int | None = None,
    agents: int | None = None,
    skip_failed: bool = False,
) -> dict:
    """
    자연어 instruction → 파일 작업(ops) 목록.

    mode: "auto"(작은 요청은 한 번에, 잘리면 대규모 엔진) | "team"(처음부터 대규모 엔진 — 여러 에이전트).
    job_id: 대규모 생성의 체크포인트 ID. 같은 요청을 같은 job_id 로 다시 보내면 멈춘 지점부터 이어 만든다.
    budget_seconds: 자동 교정 시간 예산(스트림 경로는 넉넉히).

    project_root: 이 요청의 워크스페이스 루트. 비우면 전역 설정을 따른다.
        전역 env 는 동시 요청 시 서로 덮어써 다른 워크스페이스를 가리킬 수
        있으므로, 호출자가 요청별로 명시하는 것을 권장.

    decisions: /api/code/plan 이 제시하고 사용자가 선택·승인한 설계 결정.
        주어지면 (1) 결정을 프롬프트에 주입해 코드가 그 결정을 따르게 하고,
        (2) 각 결정을 docs/adr/ADR-NNN-*.md 로 영속화해 ops 에 함께 담는다.
        None/빈 값이면 기존 동작과 동일(하위호환).
        비신뢰 JSON 이 그대로 들어오므로 타입은 list 로 받고 정규화에서 거른다.

    반환:
        {"summary": str, "ops": [{action, file, language, content, rationale}],
         "model": str, "adr"?: [파일경로]}
    """
    import time as _time
    #: 전체 생성 시간 예산 — 확장은 15분까지 기다린다. 예산을 넘기면 AI 교정을 더 하지 않고 결과를 돌려준다
    #: (남은 문제는 결과에 "확인 필요"로 남고 배포 준비 점검이 다시 잡는다).
    started_at = _time.monotonic()
    instruction = (instruction or "").strip()
    if not instruction:
        raise ValueError("instruction 이 비어 있습니다.")

    root = _resolve_root(project_root)
    existing = _list_project_files(root)
    print(f"[code_agent] 코드 생성 시작 | 세션: {session_id} | 요청: {instruction[:80]!r} | 기존파일 {len(existing)}개")
    #: 열린 파일·참고 파일의 비밀은 AI 로 보내지 않는다. 자리표시로 보낸 값은 결과에서 원래 값으로 되돌린다.
    context_secrets: dict = {}
    open_file, prior_files, context_files = _scrub_context(open_file, prior_files, context_files, context_secrets)

    # FR-02-05 — 승인 게이트. 라우트가 아니라 여기(생성 함수)에서 막는다.
    # 라우트에서만 막으면 server.py 의 구(舊) /api/code/generate 처럼 decisions 를
    # 아예 받지 않는 호출부가 게이트를 우회한다. 생성 경로는 하나뿐이므로
    # 이 지점을 지키면 어떤 호출자가 와도 승인 없이 코드가 나오지 않는다.
    approval = _approval_state(decisions)
    if approval == APPROVAL_CANCELLED:
        raise ValueError("사용자가 취소를 선택해 생성을 중단했습니다.")
    if approval == APPROVAL_INVALID:
        raise ValueError(
            "승인 내용이 온전하지 않아 생성을 중단했습니다. "
            "제시된 모든 결정 카드에 대해 그 카드의 options 중 하나의 key 를 "
            "chosen_key 로 보내야 합니다 (미선택·중복 id·목록에 없는 key 는 거절)."
        )
    if approval == APPROVAL_MISSING:
        raise ValueError(
            "승인된 선택이 없어 생성을 중단했습니다. "
            "/api/code/plan 으로 설계 결정을 먼저 받은 뒤, 사용자가 고른 결과를 "
            "decisions 에 담아 다시 요청하세요."
        )

    # 결정 파싱은 여기서 딱 한 번. 프롬프트와 ADR 이 같은 데이터를 본다.
    norm_decisions = normalize_decisions(decisions)
    if norm_decisions:
        print(f"[code_agent] 승인된 결정 {len(norm_decisions)}개 주입")

    prompt = _build_code_prompt(
        instruction, existing, open_file, prior_files or [], context_files or [],
        target_folder, norm_decisions,
    )
    #: 고른 결제 시작 방식 — AI 가 만드는 앱도 로컬 배포의 모의 결제 서버와 맞는 약속을 지키게 한다.
    import payment_contract
    from commerce_starter import selected as _commerce_selected
    payment_mode = payment_contract.payment_choice(norm_decisions)
    payment_ai = bool(payment_mode) and not _commerce_selected(norm_decisions)
    if payment_ai:
        prompt += payment_contract.code_contract(payment_mode)

    try:
        import generation_jobs as _jobs
        from generation_progress import report as _report
    except ImportError:  # pragma: no cover
        from core import generation_jobs as _jobs  # type: ignore
        from core.generation_progress import report as _report  # type: ignore
    budget = _GENERATION_BUDGET_SECONDS if budget_seconds is None else budget_seconds
    job_id = job_id if _jobs.valid_job_id(job_id) else _jobs.new_job_id()
    #: 같은 요청인지 — 요청문·승인한 결정·대상 폴더·프로젝트가 같아야 체크포인트를 이어 쓴다.
    fp = _jobs.fingerprint(instruction=instruction, decisions=norm_decisions, target=target_folder, root=str(root),
                           context=[f.get("path") for f in (context_files or []) if isinstance(f, dict)])
    saved = _jobs.load(job_id, fp)
    _jobs.prune()
    if saved and isinstance(saved.get("result"), dict):
        #: 이미 끝난 작업 — 확장이 결과를 받기 전에 연결이 끊겼던 경우. 다시 만들지 않고 결과를 돌려준다.
        _report({"step": "resumed", "message": "이미 끝난 작업입니다 — 저장된 결과를 불러옵니다"})
        return saved["result"]

    #: AI 가 만드는 결제 앱 — Stripe 연동은 ReCoder 결제 모듈(고정 파일)을 넣고 AI 는 불러 쓰기만 한다.
    import payment_kit
    kit_hook = payment_kit.plan_with_kit if (payment_ai and not existing) else None

    def _split() -> tuple:
        return _generate_split(prompt, target_folder, new_project=not existing,
                               job_id=job_id, fingerprint=fp, resume=saved, concurrency=agents,
                               skip_failed=skip_failed, plan_hook=kit_hook)

    # Request a native schema instead of relying only on prose instructions.
    # Reject incomplete batches as a whole, then give the model one bounded
    # correction attempt. Neither attempt writes project files.
    from commerce_starter import selected as commerce_selected, operations as commerce_operations
    foundation = ""
    split_mode = False
    if commerce_selected(norm_decisions):
        if existing or prior_files or context_files:
            raise ValueError("검증된 쇼핑몰 기반은 빈 프로젝트에서 시작하세요. 기존 파일은 변경하지 않았습니다.")
        from types import SimpleNamespace
        foundation = "commerce-v1"
        ops_out = commerce_operations()
        data = {"summary": "검증된 쇼핑몰 기반을 준비했습니다. React·Express·PostgreSQL·Stripe 구성의 상품·가입·장바구니·주문·재고·관리자 화면을 포함합니다. 실제 결제 키·상품·HTTPS·사업 정책을 설정한 뒤 업무 검증을 진행하세요."}
        llm_resp = SimpleNamespace(model_used="reviewed-commerce-v1", provider="starter")
    else:
        reason = ""
        split_mode = False
        split_mode_failed = False
        direct_split = bool(saved and saved.get("manifest")) or mode == "team"
        if direct_split:
            #: 팀 모드이거나 멈춘 작업을 이어 만드는 중 — 한 번에 만들어 보는 호출을 건너뛴다(잘릴 게 뻔한 30초 절약).
            try:
                data, ops_out, llm_resp = _split()
            except _jobs.GenerationPaused:
                raise
            except (LLMError, CodeOutputError, RuntimeError) as split_exc:
                raise RuntimeError(f"대규모 생성에 실패했습니다: {split_exc} 파일은 변경하지 않았습니다.") from split_exc
            split_mode = True
        else:
            _report({"step": "generating", "message": "AI 가 코드를 만드는 중…"})
            for attempt in range(2):
                attempt_prompt = prompt
                if attempt:
                    attempt_prompt += (
                        f"\n\n직전 응답의 문제: {reason}\n"
                        "같은 사용자 요청과 승인된 설계를 유지하여 다시 생성하세요. "
                        "summary와 비어 있지 않은 ops 배열을 포함한 JSON 하나를 완성하세요. "
                        "각 op에 file과 전체 content를 반드시 포함하세요. "
                        "기능이나 기존 코드를 생략하지 말고 장황한 설명과 반복 스타일을 줄여 출력 한도 안에서 완결하세요."
                    )
                try:
                    llm_resp = get_router().call(
                        LLMRequest(prompt=attempt_prompt, json_schema=CODE_OUTPUT_SCHEMA,
                                   max_tokens=_CODE_AGENT_MAX_TOKENS, temperature=0.2),
                        agent="code_agent", operation="generate_code",
                    )
                    data, ops_out = parse_code_output(llm_resp.text)
                    ops_out = _relative_to_target(ops_out, target_folder)
                    break
                except CodeOutputError as exc:
                    reason = str(exc)
                    if attempt == 1 and "완성되지 않았거나" in reason:
                        #: 교정 요청까지 완성되지 않은 JSON 이면 거의 언제나 길이 한도에서 잘린 것이다
                        #: (stop_reason 을 안 주는 제공자·게이트웨이 포함). 실패로 끝내지 않고 나눠서 만든다.
                        print("[code_agent] 응답 JSON 이 두 번 다 완성되지 않아 분할 생성으로 전환", flush=True)
                        _report({"step": "split", "message": "요청이 커서 여러 에이전트가 나눠 만드는 방식으로 전환합니다"})
                        try:
                            data, ops_out, llm_resp = _split()
                        except _jobs.GenerationPaused:
                            raise
                        except (LLMError, CodeOutputError, RuntimeError) as split_exc:
                            reason = f"나눠서 만들기도 실패했습니다: {split_exc}"
                            split_mode_failed = True
                            break
                        split_mode = True
                        break
                except LLMError as exc:
                    if exc.error_type != LLMErrorType.STRUCTURED_OUTPUT:
                        raise RuntimeError(f"LLM 호출 실패: {exc}") from exc
                    reason = "모델 출력이 응답 길이 제한에서 잘렸습니다."
                    if _is_truncation(exc):
                        #: 같은 요청을 다시 보내도 또 잘린다 — 파일 목록을 받아 나눠서 만든다.
                        print("[code_agent] 응답이 길이 한도에서 잘려 분할 생성으로 전환", flush=True)
                        _report({"step": "split", "message": "요청이 커서 여러 에이전트가 나눠 만드는 방식으로 전환합니다"})
                        try:
                            data, ops_out, llm_resp = _split()
                        except _jobs.GenerationPaused:
                            raise
                        except (LLMError, CodeOutputError, RuntimeError) as split_exc:
                            reason = f"나눠서 만들기도 실패했습니다: {split_exc}"
                            split_mode_failed = True
                            break
                        split_mode = True
                        break
                except Exception as exc:
                    raise RuntimeError(f"LLM 호출 실패: {exc}") from exc
                print(f"[code_agent] 생성 응답 검증 실패 ({attempt + 1}/2): {reason}", flush=True)
            else:
                raise RuntimeError(
                    f"AI가 완성된 파일 변경을 반환하지 못했습니다(2회 시도). {reason} "
                    "파일은 변경하지 않았습니다. 요청 범위를 나누어 다시 시도해 주세요."
                )
        if split_mode_failed:
            raise RuntimeError(
                f"AI가 완성된 파일 변경을 반환하지 못했습니다(2회 시도 뒤 나눠서 만들기도 실패). {reason} "
                "파일은 변경하지 않았습니다. 요청 범위를 나누어 다시 시도해 주세요."
            )

    # 생성 결과 일관성 — 이 ops 를 적용하면 새로 생기는 빌드·실행 문제(없는 파일을 가리키는
    # 스크립트, 선언 안 된 패키지 등)를 찾아 한 번 교정을 요청한다. 실기기에서 CRA 설정만 남은
    # Express 앱이 Docker 빌드에서 막혔다. 교정도 실패하면 결과에 경고로 남긴다.
    kit_now = next((op["file"] for op in ops_out if kit_hook and payment_kit.is_kit(op.get("file", ""))), None)
    if kit_now:
        #: 교정 단계의 AI 도 결제 모듈 약속을 알게 한다(설계 약속에만 있으면 교정 프롬프트에 빠진다).
        prompt += payment_kit.contract(kit_now)
    if kit_hook and not kit_now:
        #: 한 번에 만든 결과(설계 단계 없음) — 결제 모듈을 넣고, AI 가 따로 만든 모의 결제 서버는 뺀다.
        #: 서버가 모듈을 쓰지 않으면 결제 약속 점검이 오류로 잡아 교정한다.
        _files, kit_ops, _extra = payment_kit.plan_with_kit([{"file": op["file"]} for op in ops_out if op.get("file")])
        if kit_ops:
            ops_out = [op for op in ops_out if not payment_kit.is_ai_mock(op.get("file", ""))] + kit_ops
            prompt += _extra
    ops_out, autofix_notes = _autofix_ops(root, target_folder, ops_out, new_app=not existing)
    if not existing and not foundation:
        ops_out, kit_docker = _apply_docker_kit(root, target_folder, ops_out)
        if kit_docker:
            autofix_notes.append("Dockerfile: ReCoder 검증본")
    if autofix_notes:
        print(f"[code_agent] 자동 교정 {len(autofix_notes)}건: {autofix_notes[:5]}", flush=True)
    #: 사용자가 [이 파일 빼고 결과 받기]를 고른 파일 — 빠진 채로 적용하면 그 파일을 쓰는 곳이 동작하지 않으므로 남은 문제로 보인다.
    skipped_files = [f for f in ((data or {}).get("skipped") or []) if isinstance(f, dict)] if isinstance(data, dict) else []
    #: AI 가 끝내 못 써서 설계로 자동 작성한 문서 — 앱 동작과 무관하므로 경고로만 알린다.
    fallback_docs = [f for f in ((data or {}).get("fallback_docs") or []) if isinstance(f, dict)] if isinstance(data, dict) else []

    def _issues_for(candidate_ops: list[dict]) -> list[dict]:
        found = _consistency_issues(root, target_folder, candidate_ops)
        have = {_norm_op_path(op.get("file", "")) for op in candidate_ops}
        found = found + [{
            "code": "GENERATED_FILE_SKIPPED", "severity": "error", "file": f.get("file", ""),
            "message": f"{f.get('file')} 은(는) 만들지 못해 빼고 받았습니다({f.get('reason') or '실패'}). 이 파일을 쓰는 곳은 동작하지 않습니다.",
            "fix": "이 파일을 직접 만들거나, 같은 요청을 다시 보내 이 파일만 다시 쓰게 하세요.",
        } for f in skipped_files if _norm_op_path(f.get("file", "")) not in have]
        if payment_ai:
            #: 결제 약속(모의 결제 모드·서명 웹훅)이 빠졌으면 일관성 점검이 고칠 오류로 넣는다 — 빠진 채로 두면 키 없이 배포할 수 없다.
            found = found + payment_contract.issues(candidate_ops)
        if not existing and not foundation:
            #: 만들어 놓고 서버가 등록하지 않은 API 파일 — 그 기능이 동작하지 않으므로 서버 진입 파일을 고칠 오류로 넣는다.
            try:
                import unused_files
                found = found + unused_files.route_issues(unused_files.find(candidate_ops))
            except Exception as exc:  # noqa: BLE001 — 점검 실패가 생성을 막지 않는다
                print(f"[code_agent] 미등록 API 점검 생략: {exc}", flush=True)
        return found

    consistency = _issues_for(ops_out)
    verification = {"kind": "docker-build", "status": "blocked", "passed": False,
                    "output": "Static consistency errors must be repaired before building."}
    visited = set()
    #: 빌드 오류 수의 흐름 — 고쳐도 줄지 않으면 같은 일을 되풀이하지 않고 멈춘다("계속 고치기만 한다" — 사용자 지적 2.0.7)
    build_counts: list[int] = []
    for _round in range(_CONSISTENCY_ROUNDS):
        if _round and _time.monotonic() - started_at > budget:
            print(f"[code_agent] 생성 시간 예산({budget}s) 초과 — AI 교정을 멈추고 결과를 돌려줍니다", flush=True)
            break
        errors = [i for i in consistency if i["severity"] == "error"]
        if not existing and not foundation:
            #: 교정이 package.json·시작 스크립트를 바꿨을 수 있다 — 검증본 Dockerfile 을 지금 구조로 다시 쓴다.
            ops_out, _changed = _apply_docker_kit(root, target_folder, ops_out)
        if errors and _round >= 2 and split_mode:
            #: 정적 문제가 두 번 고쳐도 남으면 빌드도 함께 돌려 소스 오류(tsc·vite)를 한꺼번에 받는다 —
            #: 정적 문제만 고치다 빌드 검증을 한 번도 못 해 "빌드 검증 미통과" 로 끝났다(실기기 TEMP 2.0.6).
            _report({"step": "consistency", "round": _round + 1, "message": "남은 문제와 함께 컨테이너 빌드 오류도 확인하는 중"})
            built = _verify_generated_build(root, target_folder, ops_out)
            if built["status"] == "failed":
                seen = {(e.get("file"), e.get("code")) for e in errors}
                errors = errors + [e for e in _build_failure_issues(built, ops_out) if (e.get("file"), e.get("code")) not in seen]
        if not errors:
            _report({"step": "consistency", "round": _round + 1, "message": "컨테이너 빌드로 실제 동작을 확인하는 중"})
            verification = _verify_generated_build(root, target_folder, ops_out)
            if verification["status"] != "failed":
                break
            #: 빌드 로그의 오류를 파일별 문제로 — 부분 교정이 그 소스 파일을 고친다(예전엔 Dockerfile 만 지목해 못 고쳤다).
            errors = _build_failure_issues(verification, ops_out) or [
                {"code": "GENERATED_BUILD_FAILED", "severity": "error", "file": "Dockerfile",
                 "message": verification["output"],
                 "fix": "실제 빌드 로그의 원인을 고치세요. 검사를 제거하거나 기능을 생략하지 마세요."}]
            count = sum(len(re.findall(r"\d+행 ", e.get("message") or "")) or 1 for e in errors)
            if build_counts and count >= build_counts[-1]:
                print(f"[code_agent] 빌드 오류가 줄지 않아({build_counts[-1]} → {count}) 교정을 멈춥니다", flush=True)
                _report({"step": "consistency", "round": _round + 1,
                         "message": f"빌드 오류가 더 줄지 않아 교정을 멈췄습니다(남은 {count}건) — 결과에 파일별로 보여 드립니다"})
                consistency = errors
                break
            build_counts.append(count)
        if foundation:
            break  # A reviewed foundation is never silently rewritten by a model.
        signature = hashlib.sha256(json.dumps(ops_out, sort_keys=True).encode()).hexdigest()
        if signature in visited:
            break
        visited.add(signature)
        trend = (f" · 빌드 오류 {build_counts[-1]}건" + (f"(직전 {build_counts[-2]}건)" if len(build_counts) > 1 else "")) if build_counts else ""
        _report({"step": "consistency", "round": _round + 1,
                 "message": f"전체 점검 {_round + 1}/{_CONSISTENCY_ROUNDS}회차 — 고칠 곳 {len(errors)}개 파일{trend}"})
        if split_mode:
            #: 대규모 생성 결과는 바뀔 부분만 고친다(파일 전체를 다시 쓰면 응답 한도에서 잘려 교정이 생략됐다).
            try:
                try:
                    import gen_engine as _ge
                    import generation_progress as _gp
                except ImportError:  # pragma: no cover
                    from core import gen_engine as _ge  # type: ignore
                    from core import generation_progress as _gp  # type: ignore
                edited = _ge.edit_fix_round(prompt, ops_out, errors, max_files=12, emit=_gp.current())
            except Exception as exc:  # noqa: BLE001 — 교정 실패는 아래 파일 단위 교정으로 넘어간다
                print(f"[code_agent] 부분 교정 생략: {exc}", flush=True)
                edited = None
            if edited is not None:
                ops_e, _notes = _autofix_ops(root, target_folder, edited, new_app=not existing)
                remaining = _issues_for(ops_e)
                if _issue_weight(remaining) < _issue_weight(errors):
                    ops_out, consistency = ops_e, remaining
                    verification = {"kind": "docker-build", "status": "blocked", "passed": False,
                                    "output": "Updated proposal still requires verification."}
                    print(f"[code_agent] 부분 교정 적용({_round + 1}회) | 남은 문제 {len(remaining)}개", flush=True)
                    continue
        issue_lines = "\n".join(f"- {i['message']} (해결: {i['fix']})" for i in errors)
        listing = "\n".join(f"- {op['file']}" for op in ops_out)
        fix_prompt = prompt + (
            "\n\n생성한 파일 목록:\n" + listing + _repair_context(ops_out, errors)
            + "\n\n다음 문제를 고치세요:\n" + issue_lines
            + "\n바꿔야 하는 파일만 전체 내용으로 ops에 담으세요. 필요한 누락 파일은 추가하세요. "
              "정상 파일은 보존합니다. package.json은 유효한 JSON이어야 합니다. "
              "빌드 명령을 지우거나 타입 검사·인증·결제 검증을 비활성화해 통과시키지 마세요."
        )
        try:
            retry = get_router().call(
                LLMRequest(prompt=fix_prompt, json_schema=CODE_OUTPUT_SCHEMA,
                           max_tokens=_CODE_AGENT_MAX_TOKENS, temperature=0.2),
                agent="code_agent", operation="generate_code_consistency",
            )
            _data2, updates = parse_code_output(retry.text)
            updates = _relative_to_target(updates, target_folder)
            candidate = _merge_ops(ops_out, updates)
            candidate, more_notes = _autofix_ops(root, target_folder, candidate, new_app=not existing)
            remaining = _issues_for(candidate)
            if _issue_weight(remaining) > _issue_weight(consistency):
                break
            ops_out, llm_resp, consistency = candidate, retry, remaining
            verification = {"kind": "docker-build", "status": "blocked", "passed": False,
                            "output": "Updated proposal still requires verification."}
            print(f"[code_agent] 일관성 교정 적용({_round + 1}회) | 남은 문제 {len(remaining)}개", flush=True)
        except Exception as exc:
            print(f"[code_agent] 일관성 교정 실패: {exc}", flush=True)
            break
    #: 만들어 놓고 아무도 쓰지 않는 파일(아키텍처 맵의 "참조 0 · 고립"). 같은 API 를 직접 부르는 파일이 있으면
    #: 그 파일이 만들어 둔 모듈을 쓰게 한 번 고쳐 본다 — 새 오류가 생기면 고친 것을 버린다(동작이 먼저).
    unused: list[dict] = []
    if not existing and not foundation:
        try:
            import unused_files
            unused = [u for u in unused_files.find(ops_out) if u["kind"] == "module"]
            wiring = unused_files.wiring_issues(unused)
            in_budget = _time.monotonic() - started_at < budget
            if wiring and split_mode and in_budget and not any(i["severity"] == "error" for i in consistency):
                import gen_engine as _ge
                import generation_progress as _gp
                _report({"step": "consistency", "round": _CONSISTENCY_ROUNDS + 1,
                         "message": f"전체 점검 — 아무도 쓰지 않는 파일 {len(wiring)}개를 실제 화면에 연결하는 중"})
                edited = _ge.edit_fix_round(prompt, ops_out, wiring, emit=_gp.current())
                if edited is not None:
                    ops_w, _notes = _autofix_ops(root, target_folder, edited, new_app=not existing)
                    after = _issues_for(ops_w)
                    if _issue_weight(after) <= _issue_weight(consistency):
                        ops_out, consistency = ops_w, after
                        verification = {"kind": "docker-build", "status": "blocked", "passed": False,
                                        "output": "Updated proposal still requires verification."}
                        unused = [u for u in unused_files.find(ops_out) if u["kind"] == "module"]
                        print(f"[code_agent] 미사용 파일 연결 적용 | 남은 미사용 {len(unused)}개", flush=True)
                    else:
                        print("[code_agent] 미사용 파일 연결이 새 오류를 만들어 되돌림", flush=True)
        except Exception as exc:  # noqa: BLE001 — 미사용 파일 점검 실패가 생성을 막지 않는다
            print(f"[code_agent] 미사용 파일 점검 생략: {exc}", flush=True)
            unused = []
    if verification["status"] == "blocked":
        #: 정적 문제가 남아도 실제 빌드는 확인한다 — 결과에 "빌드 검증 미통과" 만 남고 무엇이 깨지는지 몰랐다(실기기 2.0.6).
        verification = _verify_generated_build(root, target_folder, ops_out)
    if verification["status"] == "failed":
        per_file = _build_failure_issues(verification, ops_out)
        seen = {(i.get("file"), i.get("code")) for i in consistency}
        consistency += [i for i in per_file if (i.get("file"), i.get("code")) not in seen] or [
            {"code": "GENERATED_BUILD_FAILED", "severity": "error", "file": "Dockerfile",
             "message": verification["output"], "fix": "실제 빌드 오류를 해결한 뒤 다시 검증하세요."}]
    if payment_mode:
        #: 고른 결제 시작 방식 — 이 폴더의 첫 로컬 배포가 데모(모의 결제)로 뜰지 정한다(배포 화면에서 바꿀 수 있음).
        try:
            import deploy_settings
            deploy_settings.remember_payment_choice(str((root / target_folder).resolve() if target_folder and not Path(target_folder).is_absolute() else root), payment_mode)
        except Exception as exc:  # noqa: BLE001 - 기록 실패가 생성을 막지 않는다(배포 화면에서 직접 고를 수 있음)
            print(f"[code_agent] 결제 시작 방식 기록 실패: {exc}")

    # ADR 영속화 — 승인된 결정을 docs/adr 에 구조화 기록으로 남긴다(코드와 동시 산출).
    # 시크릿 검사 '앞'에 넣어야 한다: ADR 본문에도 사용자 요청문이 들어가므로
    # 여기에 키가 섞여 있으면 경고 없이 파일로 굳어버린다.
    # 생성된 ADR 기록 파일(docs/adr/ADR-NNN-*.md)은 '사람이 승인한 결정'의 산물이다.
    # LLM 이 만든 코드 op 가 같은 경로를 쓰면 기록이 코드로 덮이거나 그 반대가 된다
    # (적용 순서에 따라 결과가 달라짐). 기록이 우선이므로 침범하는 op 는 걷어낸다.
    #
    # 디렉터리 전체가 아니라 **생성 기록 파일명만** 막는다. docs/adr/README.md 같은
    # 손으로 관리하는 문서까지 막으면 "ADR 규칙 문서 고쳐줘"가 아예 불가능해진다.
    # 비교 전에 경로를 **정규화**해야 한다. 글자 그대로 비교하면 같은 파일을
    # 가리키는 다른 표기가 전부 빠져나간다:
    #   - 대소문자: Windows·macOS 에서 `adr-001-x.md` 와 `ADR-001-x.md` 는 같은 파일.
    #     확장은 op 를 하나씩 독립적으로 쓰므로, 통과된 쪽이 나중에 쓰이면
    #     승인된 기록을 그대로 덮어쓴다 — 막으려던 사고가 그대로 난다.
    #   - 우회 경로: `docs/adr/../adr/ADR-001-x.md`, `./docs/adr/...` 도 같은 파일.
    def _overwrites_adr_record(op: dict) -> bool:
        raw = str(op.get("file") or "").replace("\\", "/")
        if not raw.strip():
            return False
        # normpath 로 `.` `..` 를 접고, casefold 로 대소문자를 통일한다.
        path = posixpath.normpath(raw).lstrip("/").casefold()
        prefix = f"{ADR_DIR}/".casefold()
        if not path.startswith(prefix):
            return False
        return bool(re.match(r"adr-\d+", path[len(prefix):]))

    intruders = [op for op in ops_out if _overwrites_adr_record(op)]
    if intruders:
        for op in intruders:
            print(f"[code_agent] ADR 기록을 덮어쓰려는 코드 op 제외: {op.get('file')!r}")
        ops_out = [op for op in ops_out if not _overwrites_adr_record(op)]
        if not ops_out:
            # 걸러내고 나니 적용할 것이 하나도 없다. 이대로 성공으로 반환하면
            # 확장은 "생성 완료 · 변경 0건"을 띄우고, 사용자는 요청이 처리된
            # 줄 안다. 실제로는 요청한 변경이 통째로 사라진 것이므로 실패로 알린다.
            raise ValueError(
                "요청한 변경이 모두 ADR 기록 파일(docs/adr/ADR-NNN-*.md)을 향해 있어 "
                "적용할 수 있는 것이 없습니다. ADR 은 승인된 설계 결정에서 자동 생성되며 "
                "직접 수정 대상이 아닙니다."
            )

    adr_ops = build_adr_ops(norm_decisions, instruction, root, target_folder)
    if adr_ops:
        ops_out.extend(adr_ops)

    # 적용 전 안전 검사 — 생성된 코드/ADR 에 시크릿이 박혀있으면 op 에 경고를 단다.
    # README·.env.example 같은 문서의 키 모양 예시 값은 자리표시(<STRIPE_SECRET_KEY>)로 바꾼다 — 배포 보안 검사(gitleaks)와
    # 같은 기준으로 잡으므로, 생성 때 통과한 문서가 배포 단계에서 "키 유출" 로 막히지 않는다.
    try:
        try:
            from security_scan import is_doc_like, redact_doc_secrets, scan_text_for_secrets
        except ImportError:
            from core.security_scan import is_doc_like, redact_doc_secrets, scan_text_for_secrets
        for op in ops_out:
            if is_doc_like(op["file"]) and isinstance(op.get("content"), str):
                op["content"], redacted = redact_doc_secrets(op["content"])
                if redacted:
                    print(f"[code_agent] {op['file']}: 문서의 예시 키 {redacted}줄을 자리표시로 바꿈")
            warns = scan_text_for_secrets(op["content"], op["file"])
            op["secret_warnings"] = warns
            if any(w.get("severity") in ("critical", "high") for w in warns):
                lines = ", ".join(str(w.get("line")) for w in warns[:5])
                consistency.append({
                    "code": "GENERATED_SECRET_IN_FILE", "severity": "error", "file": op["file"],
                    "message": f"{op['file']} {lines}번째 줄에 키처럼 보이는 값이 있습니다. 이대로 적용하면 배포 보안 검사가 막습니다.",
                    "fix": "값을 .env 로 옮기고 process.env 로 읽게 하세요(보안 탭의 자동 수정으로도 옮길 수 있습니다).",
                })
    except Exception as exc:
        print(f"[code_agent] 시크릿 사전검사 생략: {exc}")
        for op in ops_out:
            op.setdefault("secret_warnings", [])

    # 생성 모델의 자기 보고 대신, 실제 저장 파일과 전체 교체 내용을 비교한다.
    try:
        from code_removals import annotate_removals
    except ImportError:
        from core.code_removals import annotate_removals
    _restore_secrets_in_ops(ops_out, context_secrets)
    annotate_removals(ops_out, root, target_folder)

    summary = str(data.get("summary") or "코드를 생성했습니다.").strip()
    if fallback_docs:
        summary += "\n\n자동 작성한 문서: " + ", ".join(f.get("file", "") for f in fallback_docs) + " — AI 가 끝내 못 써서 설계에서 기본 내용으로 만들었습니다."
    if consistency:
        summary += "\n\n확인 필요: " + " / ".join(i["message"] for i in consistency[:3])
    result = {
        "summary": summary,
        "consistency_issues": consistency,
        #: 아무도 쓰지 않는 생성 파일 — 확장이 "모두 적용" 에서 기본으로 뺀다(사용자가 포함할 수 있음).
        "skipped_files": [f.get("file") for f in skipped_files],
        "fallback_docs": [f.get("file") for f in fallback_docs],
        "unused_files": [{"file": u["file"], "consumers": u.get("consumers", [])} for u in unused
                         if any(op.get("file") == u["file"] for op in ops_out)],
        "verification": {k: v for k, v in verification.items() if k != "log"},
        "foundation": foundation or None,
        "ops": ops_out,
        "model": getattr(llm_resp, "model_used", ""),
        "provider": getattr(llm_resp, "provider", ""),
    }
    if adr_ops:
        result["adr"] = [op["file"] for op in adr_ops]
        print(f"[code_agent] ADR {len(adr_ops)}건 영속화 예정: {result['adr']}")
    if split_mode:
        result["job_id"] = job_id
        #: 결과를 체크포인트 자리에 남겨 둔다 — 확장이 받기 전에 연결이 끊겨도 같은 작업 ID 로 바로 받는다.
        _jobs.save(job_id, {"fingerprint": fp, "result": result})
    print(f"[code_agent] 코드 생성 완료 | ops {len(ops_out)}개 | model={result['model']}")
    return result
