"""
Code Agent — Gemini Flash PatchProposal 생성 + base_sha256 검증 + 파일 적용 + 백업.
사용자 클릭 시 1회 호출. 자동 트리거 없음.
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

from schemas import FilePatch, PatchProposal, RiskLevel

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

# 모델 폴백 체인 — 404(사라짐) / 429(quota 0) 시 다음 후보로 자동 전환.
# 2025~2026 라인업 기준 free tier 가 살아있을 가능성이 높은 순서로 정렬.
# 사용자는 GEMINI_MODEL_FALLBACKS 환경변수(쉼표 구분)로 덮어쓸 수 있다.
_DEFAULT_MODEL_FALLBACKS = [
    'gemini-2.5-flash-lite',
    'gemini-2.5-flash',
    'gemini-2.0-flash-lite',
    'gemini-2.0-flash',
    'gemini-1.5-flash-8b',
    'gemini-1.5-flash-latest',
    'gemini-1.5-flash',
]

# ── Gemini 클라이언트 ─────────────────────────────────────────────────

_client = None

def _get_client():
    global _client
    if _client is None:
        api_key = os.getenv('GEMINI_API_KEY', '').strip()
        if not api_key:
            raise RuntimeError(
                "GEMINI_API_KEY 환경변수가 설정되어 있지 않습니다. "
                "agent/.env 를 확인하세요."
            )
        try:
            from google import genai
        except ImportError as e:
            raise RuntimeError(
                f"google-genai 패키지가 설치되지 않았습니다: {e}. "
                "pip install google-genai 후 다시 시도하세요."
            ) from e

        # SDK 버전에 따라 http_options 전달 방식이 다름 — 안전하게 시도
        timeout_ms = int(float(os.getenv('GEMINI_TIMEOUT_SEC', '60')) * 1000)
        try:
            from google.genai import types as _gt  # type: ignore
            _client = genai.Client(
                api_key=api_key,
                http_options=_gt.HttpOptions(timeout=timeout_ms),
            )
        except Exception:
            try:
                _client = genai.Client(
                    api_key=api_key,
                    http_options={"timeout": timeout_ms},
                )
            except Exception:
                _client = genai.Client(api_key=api_key)
    return _client


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
    with open(path, 'rb') as f:
        return hashlib.sha256(f.read()).hexdigest()


def _read_file_safe(path: Path, max_bytes: int = _MAX_FILE_BYTES) -> str:
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
    for m in re.finditer(
        r'([A-Za-z_][\w./\\-]*\.(?:py|js|ts|tsx|jsx|go|rs|java|kt|rb|php|cs|cpp|c|h))',
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


# ── 핵심 API ──────────────────────────────────────────────────────────

def _extract_json(raw: str) -> dict:
    """
    Gemini 응답에서 JSON 객체를 추출한다.
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
                        # 다음 닫는 괄호 후보로 계속 시도
                        depth = 1
                        continue

    if last_err:
        raise ValueError(f"JSON 파싱 실패: {last_err}\n원문: {raw[:300]}")
    raise ValueError(f"닫히지 않은 JSON 객체.\n원문: {raw[:300]}")


def _safe_risk(value: str) -> RiskLevel:
    try:
        return RiskLevel(value.lower())
    except ValueError:
        return RiskLevel.LOW


def _humanize_gemini_error(e: Exception, model: str) -> str:
    """
    Gemini SDK 예외를 사용자에게 보여줄 수 있는 한국어 메시지로 변환.
    특히 429(quota), 401/403(키), 503(서비스 다운) 을 분기.
    """
    msg = str(e)
    lower = msg.lower()

    # 429 RESOURCE_EXHAUSTED
    if '429' in msg or 'resource_exhausted' in lower or 'quota' in lower:
        # 'Please retry in 23.246888647s.' 같은 패턴에서 초만 추출
        retry_sec = None
        m = re.search(r'retry\s+in\s+([\d.]+)s', msg, re.IGNORECASE)
        if m:
            try:
                retry_sec = round(float(m.group(1)))
            except ValueError:
                pass
        if retry_sec is None:
            m = re.search(r'retryDelay[\'"]?\s*:\s*[\'"]?(\d+)s', msg)
            if m:
                retry_sec = int(m.group(1))

        # limit: 0 인지 → free tier 자체가 막힌 경우
        free_blocked = bool(re.search(r'limit\s*:\s*0\b', msg))

        parts = [f"[Gemini quota 초과] model={model}"]
        if free_blocked:
            parts.append(
                f"이 프로젝트의 무료 tier 에서 '{model}' 사용 한도가 0 입니다. "
                "→ .env 의 GEMINI_MODEL 을 'gemini-1.5-flash' 로 변경하거나, "
                "Google AI Studio → Billing 활성화 필요."
            )
        elif retry_sec is not None:
            parts.append(f"{retry_sec}초 후 자동 재시도 가능합니다.")
        else:
            parts.append("잠시 후 다시 시도하세요.")
        return " ".join(parts)

    # 401 / 403 / API_KEY
    if '401' in msg or '403' in msg or 'api key' in lower or 'permission' in lower:
        return f"[Gemini 인증 실패] model={model}. GEMINI_API_KEY 가 유효한지 확인하세요. ({msg[:200]})"

    # 503 service unavailable
    if '503' in msg or 'unavailable' in lower:
        return f"[Gemini 서비스 일시 장애] model={model}. 잠시 후 다시 시도하세요. ({msg[:200]})"

    # 그 외 — 원문 일부만
    return f"Gemini API 호출 실패 (model={model}): {msg[:400]}"


def _model_candidates() -> list[str]:
    """
    호출 시도할 모델 목록을 우선순위대로 반환.
    1) GEMINI_MODEL (env)
    2) GEMINI_MODEL_FALLBACKS (env, 쉼표 구분) 또는 _DEFAULT_MODEL_FALLBACKS
    중복 제거하면서 순서 유지.
    """
    primary = os.getenv('GEMINI_MODEL', '').strip()
    raw_fallbacks = os.getenv('GEMINI_MODEL_FALLBACKS', '').strip()
    if raw_fallbacks:
        fallbacks = [m.strip() for m in raw_fallbacks.split(',') if m.strip()]
    else:
        fallbacks = list(_DEFAULT_MODEL_FALLBACKS)

    seen: set[str] = set()
    ordered: list[str] = []
    for m in [primary, *fallbacks]:
        if m and m not in seen:
            seen.add(m)
            ordered.append(m)
    return ordered


def _is_retryable_with_other_model(err: Exception) -> bool:
    """
    이 모델만의 문제일 가능성이 큰 에러는 다른 모델로 폴백을 시도한다.
      - 404 NOT_FOUND        : 모델이 사라짐
      - 429 RESOURCE_EXHAUSTED: 이 모델 quota 0 / 분당 한도 초과
      - 503 UNAVAILABLE      : 이 모델 일시 과부하 (다른 모델은 살아있을 수 있음)
      - 500 INTERNAL         : 종종 모델 단위 일시 오류
    """
    s = str(err).lower()
    return (
        '404' in s or 'not_found' in s or 'is not found' in s
        or '429' in s or 'resource_exhausted' in s or 'quota' in s
        or '503' in s or 'unavailable' in s
        or '500' in s and 'internal' in s
    )


def _call_with_fallback(client, prompt: str) -> tuple[object, str]:
    """
    여러 모델 후보를 순서대로 시도해 첫 성공 응답을 돌려준다.
    Returns: (response, used_model)
    """
    candidates = _model_candidates()
    if not candidates:
        raise RuntimeError("호출할 Gemini 모델 후보가 없습니다.")

    last_err: Exception | None = None
    last_model = candidates[0]

    for idx, model in enumerate(candidates):
        print(
            f"[code_agent] Gemini 호출 시도 [{idx + 1}/{len(candidates)}]: "
            f"model={model}, prompt={len(prompt)}자"
        )
        try:
            response = client.models.generate_content(
                model    = model,
                contents = prompt,
            )
            return response, model
        except Exception as e:
            last_err = e
            last_model = model
            humanized = _humanize_gemini_error(e, model)
            if _is_retryable_with_other_model(e) and idx + 1 < len(candidates):
                print(f"[code_agent] ⚠ {humanized}  → 다음 모델로 폴백")
                continue
            # 인증/네트워크 등 모델 바꿔도 안 풀릴 에러는 즉시 중단
            raise RuntimeError(humanized) from e

    # 모든 후보 실패
    tried = ", ".join(candidates)
    raise RuntimeError(
        f"모든 Gemini 모델 시도 실패. 시도한 모델: {tried}. "
        f"마지막 에러: {_humanize_gemini_error(last_err, last_model) if last_err else '알 수 없음'}"
    )


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


def generate_patch_proposal(error_text: str, related_files: list[str]) -> PatchProposal:
    """Gemini Flash 호출 → PatchProposal 반환."""
    print(f"[code_agent] 수정안 생성 시작 | 에러: {error_text[:80]!r}")

    root = _project_root()
    print(f"[code_agent] 프로젝트 루트: {root}")

    files = _collect_related_files(related_files, error_text)
    print(
        f"[code_agent] 관련 파일 {len(files)}개: "
        f"{[f['path'] for f in files] or '(없음)'}"
    )

    prompt = _build_prompt(error_text, files)

    client = _get_client()
    response, used_model = _call_with_fallback(client, prompt)

    raw = (getattr(response, 'text', None) or "").strip()
    print(f"[code_agent] Gemini 응답 길이: {len(raw)}자 (model={used_model})")

    if not raw:
        # 빈 응답이면 finish_reason을 노출해서 진단 도와주기
        finish = None
        try:
            cands = getattr(response, 'candidates', None) or []
            if cands:
                finish = getattr(cands[0], 'finish_reason', None)
        except Exception:
            pass
        raise RuntimeError(
            f"Gemini가 빈 응답을 반환했습니다 (finish_reason={finish}). "
            f"모델/할당량/필터를 확인하세요."
        )

    try:
        data = _extract_json(raw)
    except ValueError as e:
        raise RuntimeError(f"Gemini 응답 JSON 추출 실패: {e}") from e

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
        ))

    if not patches:
        print("[code_agent] ⚠ Gemini 응답에 patches 가 없습니다. summary 만 반환합니다.")

    proposal = PatchProposal(
        proposal_id  = uuid.uuid4().hex,
        summary      = data.get('summary', '수정안이 생성되었습니다.'),
        risk         = _safe_risk(data.get('risk', 'low')),
        test_command = data.get('test_command', ''),
        patches      = patches,
    )
    print(f"[code_agent] 수정안 생성 완료 | 파일 {len(patches)}개")
    return proposal


def apply_patch_proposal(proposal: PatchProposal) -> list[dict]:
    """
    base_sha256 검증 → 백업 생성 → patch 적용.
    Returns: 파일별 적용 결과 목록
    """
    results = []
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
                    "message": "Patch target is outside the project root.",
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
            backup_dir = root / ".recoder" / "backups" / proposal.proposal_id
            backup_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime('%Y%m%d_%H%M%S')
            backup_path = backup_dir / f"{fp.name}.{ts}.bak"
            backup_path.write_bytes(fp.read_bytes())
            _ensure_gitignore_recoder(root)

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
                "message": "Patch applied.",
                "validation": validation,
                "backup_path": str(backup_path) if backup_path else "",
            })
        except Exception as e:
            if backup_path and backup_path.exists():
                fp.write_bytes(backup_path.read_bytes())
            results.append({"file": patch.file, "status": "error", "message": str(e)})

    if backup_records:
        _LAST_APPLY_BACKUPS[proposal.proposal_id] = backup_records

    return results


def _apply_unified_diff(original_text: str, diff_text: str) -> str:
    """Apply a unified diff with context validation."""
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
    if index >= len(lines):
        raise ValueError(f"Diff {kind} line is past end of file: {expected!r}")
    actual = lines[index]
    if actual != expected:
        raise ValueError(
            f"Diff {kind} mismatch at line {index + 1}: expected {expected!r}, got {actual!r}"
        )


def _diff_adds_trailing_newline(diff_text: str) -> bool:
    stripped = diff_text.rstrip("\n\r")
    return bool(stripped) and not stripped.endswith("\\ No newline at end of file")


def _validate_changed_file(path: Path) -> str:
    if path.suffix.lower() != ".py":
        return "unknown"
    try:
        source = path.read_text(encoding="utf-8")
        compile(source, str(path), "exec")
        return "syntax_ok"
    except SyntaxError as e:
        return f"syntax_error:{e.lineno}:{e.msg}"


def rollback_patch_proposal(proposal: PatchProposal) -> list[dict]:
    """Restore files from the most recent backups created for a proposal."""
    records = _LAST_APPLY_BACKUPS.get(proposal.proposal_id, [])
    if not records:
        return [{
            "proposal_id": proposal.proposal_id,
            "status": "no_backup",
            "message": "No backup is available for this proposal.",
        }]

    results: list[dict] = []
    for record in records:
        target = Path(record["target_path"])
        backup = Path(record["backup_path"])
        if not backup.exists():
            results.append({
                "file": record.get("file", str(target)),
                "status": "missing_backup",
                "message": str(backup),
            })
            continue
        target.write_bytes(backup.read_bytes())
        results.append({
            "file": record.get("file", str(target)),
            "status": "ok",
            "message": "Rollback complete.",
        })
    return results


def _ensure_gitignore_recoder(root: Path | None = None) -> None:
    root = root or _project_root()
    gi = root / '.gitignore'
    marker = '.recoder/'
    if gi.exists():
        content = gi.read_text(encoding='utf-8')
        if marker not in content:
            gi.write_text(content + f'\n# ReCoder backup directory\n{marker}\n', encoding='utf-8')
    else:
        gi.write_text(f'# ReCoder backup directory\n{marker}\n', encoding='utf-8')
