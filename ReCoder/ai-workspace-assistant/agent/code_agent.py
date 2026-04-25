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

# 수집 제외 목록
_SKIP_PATTERNS = {'.env', '.git', 'node_modules', 'venv', '__pycache__', 'dist', 'build'}

# ── Gemini 클라이언트 ─────────────────────────────────────────────────

_client = None

def _get_client():
    global _client
    if _client is None:
        from google import genai
        _client = genai.Client(api_key=os.environ['GEMINI_API_KEY'])
    return _client


# ── 유틸 ──────────────────────────────────────────────────────────────

def compute_sha256(path: str | Path) -> str:
    with open(path, 'rb') as f:
        return hashlib.sha256(f.read()).hexdigest()


def _read_file_safe(path: Path, max_bytes: int = 50_000) -> str:
    try:
        content = path.read_bytes()[:max_bytes]
        return content.decode('utf-8', errors='replace')
    except Exception:
        return ""


def _collect_related_files(hints: list[str]) -> list[dict]:
    """에러 텍스트 힌트에서 파일 목록 수집."""
    files = []
    seen: set[str] = set()

    for hint in hints:
        p = Path(hint)
        if p.exists() and p.is_file():
            key = str(p.resolve())
            if key not in seen:
                seen.add(key)
                files.append({"path": str(p), "content": _read_file_safe(p)})

    # 현재 디렉터리에서 Python/JS 파일 추가 (최대 3개)
    for suffix in ('*.py', '*.js', '*.ts'):
        for fp in Path('.').glob(suffix):
            if any(skip in fp.parts for skip in _SKIP_PATTERNS):
                continue
            key = str(fp.resolve())
            if key not in seen and len(files) < 5:
                seen.add(key)
                files.append({"path": str(fp), "content": _read_file_safe(fp)})

    return files


def _build_prompt(error_text: str, files: list[dict]) -> str:
    file_section = ""
    for f in files:
        file_section += f"\n### {f['path']}\n```\n{f['content'][:3000]}\n```\n"

    return f"""다음 에러를 분석하고 코드 수정안을 JSON으로만 응답하세요.
마크다운 코드펜스(```) 없이 순수 JSON만 출력하세요.

## 에러
{error_text[:500]}

## 관련 파일
{file_section}

## 응답 형식 (이 JSON만 출력)
{{
  "summary": "수정 요약 (한국어 1줄)",
  "risk": "low",
  "test_command": "pytest 또는 python -c 등",
  "patches": [
    {{
      "file": "파일명.py",
      "unified_diff": "--- a/파일명.py\\n+++ b/파일명.py\\n@@ ... @@\\n- 기존줄\\n+ 수정줄"
    }}
  ]
}}

주의:
- unified_diff는 실제로 적용 가능한 최소 변경만 포함
- 파일 전체 출력 금지
- 반드시 에러와 직접 관련된 수정만
"""


# ── 핵심 API ──────────────────────────────────────────────────────────

def generate_patch_proposal(error_text: str, related_files: list[str]) -> PatchProposal:
    """Gemini Flash 호출 → PatchProposal 반환."""
    files = _collect_related_files(related_files)
    prompt = _build_prompt(error_text, files)

    client = _get_client()
    response = client.models.generate_content(
        model="gemini-2.0-flash",
        contents=prompt,
    )
    raw = response.text.strip()

    # JSON 파싱
    raw = re.sub(r'^```(?:json)?\s*', '', raw)
    raw = re.sub(r'\s*```$', '', raw)
    data = json.loads(raw)

    # base_sha256 계산
    patches: list[FilePatch] = []
    for p in data.get('patches', []):
        fp = Path(p['file'])
        sha = compute_sha256(fp) if fp.exists() else ""
        patches.append(FilePatch(
            file         = p['file'],
            base_sha256  = sha,
            unified_diff = p['unified_diff'],
        ))

    return PatchProposal(
        proposal_id  = uuid.uuid4().hex,
        summary      = data.get('summary', ''),
        risk         = RiskLevel(data.get('risk', 'low')),
        test_command = data.get('test_command', ''),
        patches      = patches,
    )


def apply_patch_proposal(proposal: PatchProposal) -> list[dict]:
    """
    base_sha256 검증 → 백업 생성 → patch 적용.
    Returns: 파일별 적용 결과 목록
    """
    session_id = uuid.uuid4().hex[:8]
    results = []

    for patch in proposal.patches:
        fp = Path(patch.file)

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
        if fp.exists():
            backup_dir = BACKUP_DIR / session_id
            backup_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime('%Y%m%d_%H%M%S')
            backup_path = backup_dir / f"{fp.name}.{ts}.bak"
            backup_path.write_bytes(fp.read_bytes())
            _ensure_gitignore_recoder()

        # diff 적용
        try:
            original_lines = fp.read_text(encoding='utf-8').splitlines(keepends=True) if fp.exists() else []
            patched = _apply_unified_diff(original_lines, patch.unified_diff)
            fp.write_text("".join(patched), encoding='utf-8')
            results.append({"file": patch.file, "status": "ok", "message": "적용 완료"})
        except Exception as e:
            results.append({"file": patch.file, "status": "error", "message": str(e)})

    return results


def _apply_unified_diff(original_lines: list[str], diff_text: str) -> list[str]:
    """unified diff를 원본에 적용해 결과 라인 목록 반환."""
    # difflib.restore는 복잡하므로 간단한 파서 사용
    result = list(original_lines)
    lines = diff_text.splitlines()

    i = 0
    offset = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith('@@'):
            # @@ -start,count +start,count @@
            m = re.match(r'@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@', line)
            if m:
                orig_start = int(m.group(1)) - 1 + offset
                i += 1
                removes: list[int] = []
                adds: list[str]    = []
                pos = orig_start
                while i < len(lines) and not lines[i].startswith('@@'):
                    dl = lines[i]
                    if dl.startswith('-'):
                        removes.append(pos)
                        pos += 1
                    elif dl.startswith('+'):
                        adds.append(dl[1:] + ('\n' if not dl[1:].endswith('\n') else ''))
                    else:
                        pos += 1
                    i += 1
                # 삭제 (역순)
                for idx in sorted(removes, reverse=True):
                    if 0 <= idx < len(result):
                        result.pop(idx)
                        offset -= 1
                # 삽입
                insert_at = orig_start
                for add_line in adds:
                    result.insert(insert_at, add_line)
                    insert_at += 1
                    offset += 1
        else:
            i += 1

    return result


def _ensure_gitignore_recoder() -> None:
    gi = Path('.gitignore')
    marker = '.recoder/'
    if gi.exists():
        content = gi.read_text(encoding='utf-8')
        if marker not in content:
            gi.write_text(content + f'\n# ReCoder backup directory\n{marker}\n', encoding='utf-8')
    else:
        gi.write_text(f'# ReCoder backup directory\n{marker}\n', encoding='utf-8')
