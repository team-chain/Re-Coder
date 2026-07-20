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
import os
import re
import uuid
from datetime import datetime
from pathlib import Path

from llm.base import LLMRequest
from llm.router import get_router
from schemas import AnalyzeRequest, FilePatch, PatchProposal, RiskLevel

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
    try:
        for p in sorted(root.rglob("*")):
            if len(out) >= limit:
                break
            if p.is_dir():
                continue
            if any(part in _SKIP_DIRS for part in p.parts):
                continue
            if p.suffix.lower() in _SOURCE_EXTS or p.suffix.lower() in {
                ".html", ".css", ".json", ".md", ".txt", ".yml", ".yaml", ".toml",
            }:
                try:
                    out.append(str(p.relative_to(root)).replace("\\", "/"))
                except ValueError:
                    continue
    except Exception:
        pass
    return out


def _build_code_prompt(
    instruction: str,
    existing_files: list[str],
    open_file: dict | None,
    prior_files: list[dict] | None = None,
    context_files: list[dict] | None = None,
    target_folder: str = "",
) -> str:
    """코드 생성 에이전트용 프롬프트. JSON ops 형식을 강제한다."""
    tree = "\n".join(f"- {f}" for f in existing_files) or "(빈 프로젝트)"
    prior_block = ""
    for pf in (prior_files or [])[:4]:
        body = (pf.get("content") or "")[:_MAX_PROMPT_BYTES]
        if body.strip():
            prior_block += f"\n[직전 생성 파일] {pf.get('path','?')}\n```\n{body}\n```\n"
    if prior_block:
        prior_block = "\n직전 턴에서 만든 파일들(이어서 수정/확장할 수 있음):\n" + prior_block

    ctx_block = ""
    for cf in (context_files or [])[:6]:
        body = (cf.get("content") or "")[:_MAX_PROMPT_BYTES]
        if body.strip():
            ctx_block += f"\n[참고 파일] {cf.get('path','?')}\n```\n{body}\n```\n"
    if ctx_block:
        ctx_block = "\n사용자가 첨부한 참고 파일(이 내용을 활용/일관성 유지):\n" + ctx_block

    folder_block = ""
    if (target_folder or "").strip():
        folder_block = (
            f"\n[대상 폴더] 생성/수정 파일의 경로는 '{target_folder.strip().rstrip('/')}/' 아래 "
            f"상대경로로 정하라(예: {target_folder.strip().rstrip('/')}/index.html).\n"
        )

    open_block = ""
    if open_file and (open_file.get("content") or "").strip():
        body = (open_file.get("content") or "")[:_MAX_PROMPT_BYTES]
        open_block = (
            f"\n현재 편집 중인 파일: {open_file.get('path', '(unknown)')}\n"
            f"```\n{body}\n```\n"
        )

    return f"""당신은 VSCode 안에서 동작하는 코드 작성 에이전트입니다.
사용자의 요청을 읽고, 실제로 워크스페이스에 적용할 파일 작업(ops)을 만드세요.

규칙:
- 각 파일 작업은 그 파일의 "전체 최종 내용"을 담습니다 (부분 diff 아님).
- 새 파일이면 action="create", 기존 파일을 바꾸면 action="edit".
- 가능한 한 적은 수의 파일로, 즉시 실행/렌더 가능한 완결된 코드를 작성합니다.
- 외부 빌드 도구 없이 동작하도록 합니다(예: 단일 HTML 은 인라인 CSS/JS).
- 데이터 저장이 필요하면 외부 DB 대신 localStorage 를 사용합니다.
- 기존 파일 목록과 겹치지 않게 파일명을 정하되, 사용자가 파일명을 지정하면 그대로 따릅니다.

기존 파일 목록:
{tree}
{folder_block}{ctx_block}{prior_block}{open_block}
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

_PLAN_MAX_TOKENS = 2048


def _build_plan_prompt(
    instruction: str,
    existing_files: list[str],
    open_file: dict | None,
    target_folder: str = "",
) -> str:
    """/api/code/plan 용 프롬프트 — 코드가 아니라 '설계 결정 선택지'를 요구한다."""
    tree = "\n".join(f"- {f}" for f in existing_files) or "(빈 프로젝트)"

    folder_block = ""
    if (target_folder or "").strip():
        folder_block = f"\n[대상 폴더] {target_folder.strip().rstrip('/')}/\n"

    open_block = ""
    if open_file and (open_file.get("content") or "").strip():
        body = (open_file.get("content") or "")[:_MAX_PROMPT_BYTES]
        open_block = (
            f"\n현재 편집 중인 파일: {open_file.get('path', '(unknown)')}\n"
            f"```\n{body}\n```\n"
        )

    return f"""당신은 VSCode 안에서 동작하는 AI-DLC(AI 개발 라이프사이클) 설계 도우미입니다.
사용자 요청을 코드로 바로 구현하지 말고, 구현하기 전에 사람이 승인해야 할 "설계 결정"을 선택지로 제시하세요.

규칙:
- 이 요청을 구현하는 데 필요한 설계 결정을 식별하세요 (예: 데이터 저장 방식, 인증/권한 방식, 프레임워크/라이브러리 선택, API 형태 등).
- 결정할 것이 전혀 없는 사소한 요청(오타 수정, 이름 변경 등)이면 decisions 를 빈 배열로 반환하세요.
- 각 결정은 서로 다른 2~4개의 선택지를 제공합니다.
- 프로젝트 성격과 기존 파일을 근거로 가장 적합한 선택지 하나를 recommended: true 로 표시하세요.
- 각 선택지의 장단점(pros/cons)을 1~3개씩 간결하게 답니다.
- 결정 개수는 보통 1~3개로 제한합니다 (과도하게 쪼개지 마세요).

기존 파일 목록:
{tree}
{folder_block}{open_block}
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


def generate_plan(
    instruction: str,
    session_id: str = "",
    open_file: dict | None = None,
    target_folder: str = "",
) -> dict:
    """
    자연어 instruction → "설계 결정" 목록 (코드 아님).

    AI-DLC 1단계: 에이전트가 코드를 바로 뱉지 않고, 사람이 선택·승인할
    결정 카드 목록을 반환한다. 확장(webview)이 이를 팝업 카드로 렌더하고,
    사용자가 각 결정을 선택하면 그 결과(decisions: [{id, chosen_key}])를
    담아 /api/code/generate 를 다시 호출한다.

    반환:
        {"decisions": [{id, question, options: [...], impact}], "model": str}
    """
    instruction = (instruction or "").strip()
    if not instruction:
        raise ValueError("instruction 이 비어 있습니다.")

    root = _project_root()
    existing = _list_project_files(root)
    print(f"[code_agent] 설계 결정 생성 시작 | 세션: {session_id} | 요청: {instruction[:80]!r} | 기존파일 {len(existing)}개")

    prompt = _build_plan_prompt(instruction, existing, open_file, target_folder)

    try:
        llm_resp = get_router().call(
            LLMRequest(prompt=prompt, max_tokens=_PLAN_MAX_TOKENS, temperature=0.2),
            agent="code_agent",
            operation="generate_plan",
        )
    except Exception as e:
        raise RuntimeError(f"LLM 호출 실패: {e}") from e

    raw = (llm_resp.text or "").strip()
    if not raw:
        raise RuntimeError("LLM 이 빈 응답을 반환했습니다. 모델/할당량/필터를 확인하세요.")

    data = _extract_json(raw)

    decisions_out: list[dict] = []
    for d in (data.get("decisions") or []):
        if not isinstance(d, dict):
            continue
        did = (d.get("id") or "").strip()
        question = (d.get("question") or "").strip()
        if not did or not question:
            continue
        raw_options = d.get("options") or []
        options_out: list[dict] = []
        has_recommended = False
        for opt in raw_options:
            if not isinstance(opt, dict):
                continue
            key = (opt.get("key") or "").strip()
            if not key:
                continue
            recommended = bool(opt.get("recommended", False))
            has_recommended = has_recommended or recommended
            pros = opt.get("pros") or []
            cons = opt.get("cons") or []
            options_out.append({
                "key": key,
                "label": (opt.get("label") or key).strip(),
                "summary": (opt.get("summary") or "").strip(),
                "pros": [str(p) for p in pros if str(p).strip()],
                "cons": [str(c) for c in cons if str(c).strip()],
                "recommended": recommended,
            })
        if not options_out:
            continue
        if not has_recommended:
            # 모델이 recommended 를 하나도 표시하지 않았으면 첫 옵션을 기본 추천으로.
            options_out[0]["recommended"] = True
        decisions_out.append({
            "id": did,
            "question": question,
            "options": options_out,
            "impact": (d.get("impact") or "").strip(),
        })

    result = {
        "decisions": decisions_out,
        "model": getattr(llm_resp, "model_used", ""),
    }
    print(f"[code_agent] 설계 결정 생성 완료 | 결정 {len(decisions_out)}개 | model={result['model']}")
    return result


def generate_code(
    instruction: str,
    session_id: str = "",
    open_file: dict | None = None,
    prior_files: list[dict] | None = None,
    context_files: list[dict] | None = None,
    target_folder: str = "",
) -> dict:
    """
    자연어 instruction → 파일 작업(ops) 목록.

    반환:
        {"summary": str, "ops": [{action, file, language, content, rationale}], "model": str}
    """
    instruction = (instruction or "").strip()
    if not instruction:
        raise ValueError("instruction 이 비어 있습니다.")

    root = _project_root()
    existing = _list_project_files(root)
    print(f"[code_agent] 코드 생성 시작 | 세션: {session_id} | 요청: {instruction[:80]!r} | 기존파일 {len(existing)}개")

    prompt = _build_code_prompt(instruction, existing, open_file, prior_files or [], context_files or [], target_folder)

    try:
        llm_resp = get_router().call(
            LLMRequest(prompt=prompt, max_tokens=_CODE_AGENT_MAX_TOKENS, temperature=0.2),
            agent="code_agent",
            operation="generate_code",
        )
    except Exception as e:
        raise RuntimeError(f"LLM 호출 실패: {e}") from e

    raw = (llm_resp.text or "").strip()
    if not raw:
        raise RuntimeError("LLM 이 빈 응답을 반환했습니다. 모델/할당량/필터를 확인하세요.")

    data = _extract_json(raw)

    ops_out: list[dict] = []
    for op in (data.get("ops") or []):
        file_path = (op.get("file") or "").strip()
        content = op.get("content")
        if not file_path or content is None:
            continue
        action = (op.get("action") or "create").strip().lower()
        if action not in ("create", "edit"):
            action = "create"
        ops_out.append({
            "action": action,
            "file": file_path.replace("\\", "/"),
            "language": (op.get("language") or "").strip(),
            "content": str(content),
            "rationale": (op.get("rationale") or "").strip(),
        })

    if not ops_out:
        raise RuntimeError("LLM 응답에 적용할 ops 가 없습니다.")

    # 적용 전 안전 검사 — 생성된 코드에 시크릿이 박혀있으면 op 에 경고를 단다.
    try:
        try:
            from security_scan import scan_text_for_secrets
        except ImportError:
            from core.security_scan import scan_text_for_secrets
        for op in ops_out:
            warns = scan_text_for_secrets(op["content"], op["file"])
            op["secret_warnings"] = warns
    except Exception as exc:
        print(f"[code_agent] 시크릿 사전검사 생략: {exc}")
        for op in ops_out:
            op.setdefault("secret_warnings", [])

    result = {
        "summary": data.get("summary", "코드를 생성했습니다.").strip(),
        "ops": ops_out,
        "model": getattr(llm_resp, "model_used", ""),
    }
    print(f"[code_agent] 코드 생성 완료 | ops {len(ops_out)}개 | model={result['model']}")
    return result
