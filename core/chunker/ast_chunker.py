"""
ReCoder Q1 — AST-based Code Chunker

Design (설계서 v5.0 §Q1):
- Python: `ast` 모듈로 FunctionDef / AsyncFunctionDef / ClassDef 단위 청크 생성
- Node.js / TypeScript: line-based chunk fallback (ADR-007)
- SyntaxError: 파일 전체를 단일 청크로 처리

Security policy (ADR-004):
- 인덱스에는 ChunkMetadata만 저장 (source text 미포함)
- source text는 LLM 전달 직전 파일 시스템에서 재읽음
- LLM 전달 직전 반드시 ContextGate 통과

Chunk constraints:
- chunk_id: SHA-256(file_path + name + str(start_line)) 앞 8자리
- 청크 길이 상한: 1500 토큰 (~6000 chars @ 4 chars/token)
- 청크 오버랩 없음
- .gitignore / .recoderignore 존중
- 민감 파일 패턴 기본 제외
"""

from __future__ import annotations

import ast
import fnmatch
import hashlib
import logging
import re
import time
from pathlib import Path
from typing import Iterator, Optional

from schemas import ChunkMetadata, NodeType

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# ~1500 tokens × 4 chars/token
_MAX_CHUNK_CHARS = 6_000

# Sensitive file patterns — always excluded regardless of .gitignore
_SENSITIVE_PATTERNS: list[str] = [
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*credential*",
    "*credentials*",
    "*secret*",
    "*.secrets",
    "id_rsa",
    "id_ed25519",
    "id_ecdsa",
    "*_token",
    "*.p8",
    "*.keystore",
    "service_account*.json",
    "gcp_*.json",
]

# Directories always excluded
_EXCLUDED_DIRS: set[str] = {
    ".git",
    ".venv",
    "venv",
    ".env",
    "node_modules",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "dist",
    "build",
    ".next",
    ".nuxt",
    "coverage",
    ".coverage",
    "htmlcov",
}


# ---------------------------------------------------------------------------
# Ignore rule loader
# ---------------------------------------------------------------------------


def _load_ignore_patterns(workspace: Path) -> list[str]:
    """Load patterns from .gitignore and .recoderignore."""
    patterns: list[str] = []
    for fname in (".gitignore", ".recoderignore"):
        ig_path = workspace / fname
        if not ig_path.exists():
            continue
        try:
            for line in ig_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    patterns.append(line)
        except OSError:
            pass
    return patterns


def _is_ignored(path: Path, workspace: Path, ignore_patterns: list[str]) -> bool:
    """Return True if path matches any ignore pattern (gitignore-style)."""
    try:
        rel = path.relative_to(workspace)
    except ValueError:
        return False

    rel_str = str(rel).replace("\\", "/")
    name = path.name

    # Check sensitive filename patterns first
    for pat in _SENSITIVE_PATTERNS:
        if fnmatch.fnmatch(name.lower(), pat.lower()):
            return True

    # Check ignore patterns
    for pat in ignore_patterns:
        pat = pat.lstrip("/")
        if fnmatch.fnmatch(rel_str, pat):
            return True
        if fnmatch.fnmatch(name, pat):
            return True
        # Directory pattern (trailing slash)
        if pat.endswith("/") and fnmatch.fnmatch(rel_str, pat.rstrip("/") + "*"):
            return True
    return False


# ---------------------------------------------------------------------------
# Chunk ID helper
# ---------------------------------------------------------------------------


def _make_chunk_id(file_path: str, name: str, start_line: int) -> str:
    """SHA-256 of (file_path + name + start_line) — first 8 hex chars."""
    raw = f"{file_path}::{name}::{start_line}"
    return hashlib.sha256(raw.encode()).hexdigest()[:8]


# ---------------------------------------------------------------------------
# Token estimator (rough: 4 chars ≈ 1 token)
# ---------------------------------------------------------------------------


def _estimate_tokens(source_slice: str) -> int:
    return max(1, len(source_slice) // 4)


# ---------------------------------------------------------------------------
# Python AST chunker
# ---------------------------------------------------------------------------


def _chunk_python(file_path: str, source: str) -> list[ChunkMetadata]:
    """
    Parse Python source with `ast` and produce one ChunkMetadata per
    top-level and class-level FunctionDef / AsyncFunctionDef / ClassDef.

    Falls back to a single module-level chunk on SyntaxError.
    If a chunk exceeds _MAX_CHUNK_CHARS, only the signature + docstring
    is included in the token_estimate (source is still re-read at query time).
    """
    chunks: list[ChunkMetadata] = []
    lines = source.splitlines(keepends=True)

    try:
        tree = ast.parse(source, filename=file_path)
    except SyntaxError:
        # Whole-file fallback
        return [
            ChunkMetadata(
                chunk_id=_make_chunk_id(file_path, "<module>", 1),
                file_path=file_path,
                node_type=NodeType.MODULE,
                name="<module>",
                start_line=1,
                end_line=len(lines) or 1,
                token_estimate=_estimate_tokens(source[:_MAX_CHUNK_CHARS]),
            )
        ]

    # Walk top-level + class-body nodes
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue

        name: str = node.name
        start: int = node.lineno
        end: int = getattr(node, "end_lineno", node.lineno)

        node_type = (
            NodeType.ASYNC_FUNCTION
            if isinstance(node, ast.AsyncFunctionDef)
            else (NodeType.CLASS if isinstance(node, ast.ClassDef) else NodeType.FUNCTION)
        )

        # Extract source slice for token estimation
        slice_lines = lines[start - 1 : end]
        slice_text = "".join(slice_lines)
        token_est = _estimate_tokens(slice_text)

        # If chunk too large: re-estimate using only signature + docstring
        if len(slice_text) > _MAX_CHUNK_CHARS:
            sig_lines = slice_lines[:min(20, len(slice_lines))]
            token_est = _estimate_tokens("".join(sig_lines))

        chunks.append(
            ChunkMetadata(
                chunk_id=_make_chunk_id(file_path, name, start),
                file_path=file_path,
                node_type=node_type,
                name=name,
                start_line=start,
                end_line=end,
                token_estimate=token_est,
            )
        )

    if not chunks:
        # No top-level symbols — treat as single module chunk
        chunks.append(
            ChunkMetadata(
                chunk_id=_make_chunk_id(file_path, "<module>", 1),
                file_path=file_path,
                node_type=NodeType.MODULE,
                name="<module>",
                start_line=1,
                end_line=len(lines) or 1,
                token_estimate=_estimate_tokens(source[:_MAX_CHUNK_CHARS]),
            )
        )

    return chunks


# ---------------------------------------------------------------------------
# Node.js / TypeScript line-based chunker (ADR-007)
# ---------------------------------------------------------------------------

_FUNC_START_RE = re.compile(
    r"^\s*(?:export\s+)?(?:async\s+)?function\s+(\w+)"
    r"|^\s*(?:export\s+)?const\s+(\w+)\s*=\s*(?:async\s+)?\("
    r"|^\s*(?:export\s+)?class\s+(\w+)",
    re.MULTILINE,
)


def _chunk_node(file_path: str, source: str) -> list[ChunkMetadata]:
    """Line-based chunker for JS/TS files (ADR-007 fallback)."""
    chunks: list[ChunkMetadata] = []
    lines = source.splitlines(keepends=True)
    n = len(lines)

    start_positions: list[tuple[int, str, NodeType]] = []

    for i, line in enumerate(lines, start=1):
        m = _FUNC_START_RE.match(line)
        if m:
            name = next((g for g in m.groups() if g), None)
            if not name:
                continue
            node_type = NodeType.CLASS if "class" in line else NodeType.FUNCTION
            start_positions.append((i, name, node_type))

    for idx, (start, name, node_type) in enumerate(start_positions):
        # End = start of next symbol - 1, or EOF
        end = (
            start_positions[idx + 1][0] - 1
            if idx + 1 < len(start_positions)
            else n
        )
        slice_text = "".join(lines[start - 1 : end])
        chunks.append(
            ChunkMetadata(
                chunk_id=_make_chunk_id(file_path, name, start),
                file_path=file_path,
                node_type=node_type,
                name=name,
                start_line=start,
                end_line=end,
                token_estimate=_estimate_tokens(slice_text[:_MAX_CHUNK_CHARS]),
            )
        )

    if not chunks:
        # Whole-file fallback
        chunks.append(
            ChunkMetadata(
                chunk_id=_make_chunk_id(file_path, "<module>", 1),
                file_path=file_path,
                node_type=NodeType.MODULE,
                name="<module>",
                start_line=1,
                end_line=n or 1,
                token_estimate=_estimate_tokens(source[:_MAX_CHUNK_CHARS]),
            )
        )

    return chunks


# ---------------------------------------------------------------------------
# Public API: ASTChunker
# ---------------------------------------------------------------------------


class ASTChunker:
    """
    Workspace-level AST chunker.

    Usage::

        chunker = ASTChunker(workspace_path="/path/to/project")
        index = chunker.build_index()          # full initial scan
        chunks = chunker.chunk_file("/path/to/project/app.py")  # single file

    The returned ChunkMetadata objects contain NO source text (ADR-004).
    Source is re-read from disk at query time after ContextGate masking.
    """

    def __init__(self, workspace_path: str) -> None:
        self._workspace = Path(workspace_path)
        self._ignore_patterns: list[str] = _load_ignore_patterns(self._workspace)

    # ------------------------------------------------------------------
    # Public methods
    # ------------------------------------------------------------------

    def chunk_file(self, file_path: str) -> list[ChunkMetadata]:
        """
        Chunk a single file.

        Returns [] if the file is ignored, unreadable, or unsupported.
        """
        fpath = Path(file_path)
        if not fpath.exists():
            return []
        if _is_ignored(fpath, self._workspace, self._ignore_patterns):
            logger.debug("Skipping ignored file: %s", file_path)
            return []

        try:
            source = fpath.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            logger.warning("Cannot read %s: %s", file_path, exc)
            return []

        suffix = fpath.suffix.lower()
        abs_path = str(fpath.resolve())

        if suffix == ".py":
            return _chunk_python(abs_path, source)
        elif suffix in (".js", ".ts", ".mjs", ".cjs", ".jsx", ".tsx"):
            return _chunk_node(abs_path, source)
        else:
            # Unsupported extension: skip
            return []

    def build_index(self) -> list[ChunkMetadata]:
        """
        Walk the workspace and build a full chunk index.

        Respects:
        - _EXCLUDED_DIRS
        - .gitignore / .recoderignore patterns
        - _SENSITIVE_PATTERNS
        - Only processes .py, .js, .ts, .mjs, .cjs, .jsx, .tsx files

        Target: completes within 3 seconds for typical projects (설계서 DoD).
        """
        t0 = time.monotonic()
        all_chunks: list[ChunkMetadata] = []

        for fpath in self._walk_files():
            chunks = self.chunk_file(str(fpath))
            all_chunks.extend(chunks)

        elapsed = time.monotonic() - t0
        if elapsed > 3.0:
            logger.warning(
                "Chunking index build took %.2fs (>3s DoD threshold) for %s",
                elapsed,
                self._workspace,
            )
        else:
            logger.info(
                "Chunking index built: %d chunks in %.2fs",
                len(all_chunks),
                elapsed,
            )

        return all_chunks

    def reload_ignore_patterns(self) -> None:
        """Re-read .gitignore / .recoderignore (call after file changes)."""
        self._ignore_patterns = _load_ignore_patterns(self._workspace)

    # ------------------------------------------------------------------
    # Source retrieval (called at query time, NOT at index time)
    # ------------------------------------------------------------------

    def read_chunk_source(self, chunk: ChunkMetadata) -> Optional[str]:
        """
        Read the actual source lines for a chunk from disk.

        This must be called immediately before LLM delivery and AFTER
        ContextGate masking. Never cache the returned text.

        Returns None if the file cannot be read.
        """
        fpath = Path(chunk.file_path)
        if not fpath.exists():
            return None
        try:
            lines = fpath.read_text(encoding="utf-8", errors="replace").splitlines(
                keepends=True
            )
            slice_lines = lines[chunk.start_line - 1 : chunk.end_line]
            text = "".join(slice_lines)
            # Cap at _MAX_CHUNK_CHARS to avoid oversized prompts
            return text[:_MAX_CHUNK_CHARS]
        except OSError as exc:
            logger.warning("Cannot read chunk source %s: %s", chunk.file_path, exc)
            return None

    def search_chunks(
        self,
        index: list[ChunkMetadata],
        query_names: set[str],
        *,
        file_path: Optional[str] = None,
        max_results: int = 10,
    ) -> list[ChunkMetadata]:
        """
        BM25-lite symbol search: return chunks whose name appears in query_names.

        Falls back to all chunks from file_path when query_names is empty.
        """
        results: list[ChunkMetadata] = []
        for chunk in index:
            # File filter
            if file_path and chunk.file_path != file_path:
                continue
            # Name match (exact or substring)
            if query_names:
                if not any(
                    q in chunk.name or chunk.name in q for q in query_names
                ):
                    continue
            results.append(chunk)
            if len(results) >= max_results:
                break
        return results

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _walk_files(self) -> Iterator[Path]:
        """Yield all indexable files under the workspace."""
        supported = {".py", ".js", ".ts", ".mjs", ".cjs", ".jsx", ".tsx"}
        for fpath in self._workspace.rglob("*"):
            if not fpath.is_file():
                continue
            # Skip excluded directories
            if any(part in _EXCLUDED_DIRS for part in fpath.parts):
                continue
            if fpath.suffix.lower() not in supported:
                continue
            if _is_ignored(fpath, self._workspace, self._ignore_patterns):
                continue
            yield fpath
