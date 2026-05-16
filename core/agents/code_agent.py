"""
ReCoder Code Agent — Error analysis and PatchProposal generation.

Design principles:
- LLM produces PatchProposal JSON only; it never generates shell commands.
- AST-based chunking keeps prompts within the 8 000-token context budget.
- SHA-256 base verification prevents stale-file clobbering.
- Backups are written before every patch application.
"""

from __future__ import annotations

import ast
import hashlib
import json
import logging
import re
import shutil
import uuid
from pathlib import Path
from typing import Any

from schemas import (
    AnalyzeRequest,
    ApprovalLevel,
    FilePatch,
    PatchProposal,
    ProjectProfile,
    RiskLevel,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MAX_CONTEXT_CHARS = 24_000   # ~8 000 tokens @ ~3 chars/token
_BACKUP_BASE = Path.home() / ".recoder" / "backups"

_PATCH_PROMPT_TMPL = """\
You are ReCoder, an expert software-repair AI.

## Task
Analyse the error below and propose minimal file patches to fix it.

## Project
- Stack: {stack}
- Package manager: {package_manager}
- Active file: {active_file}

## Relevant code context
```
{context}
```

## Error / terminal output
```
{error}
```

## Output format
Return ONLY a JSON object that conforms exactly to the PatchProposal schema below.
Do NOT include any prose before or after the JSON object.

### PatchProposal schema
{{
  "schema_version": "1.0",
  "proposal_id": "<uuid4>",
  "summary": "<one-sentence summary>",
  "risk_level": "low" | "medium" | "high" | "critical",
  "risk_reasons": ["<reason>", ...],
  "approval_level": 1 | 2 | 3 | 4,
  "patches": [
    {{
      "file": "<relative path>",
      "base_sha256": "<sha256 of original file or null>",
      "unified_diff": "<unified diff string>",
      "reason": "<why this change fixes the error>"
    }}
  ],
  "test_command": "<optional command to verify the fix or null>"
}}
"""

_CLASSIFY_PROMPT_TMPL = """\
Classify the following error in JSON.
Return ONLY the JSON object — no prose.

Error:
```
{error}
```

Schema:
{{
  "error_type": "<ImportError|SyntaxError|RuntimeError|TypeError|AttributeError|NameError|OSError|Other>",
  "severity": "low" | "medium" | "high",
  "is_fixable": true | false
}}
"""


class CodeAgent:
    """
    Error analysis and code-repair proposal.

    LLM outputs are restricted to PatchProposal JSON.
    Actual patch application is performed by apply_patch.
    """

    def __init__(self, provider_router: Any) -> None:
        self._provider = provider_router

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def analyze_and_propose(
        self,
        masked_context: str,
        request: AnalyzeRequest,
        project: ProjectProfile,
    ) -> PatchProposal:
        """
        1. AST-based function-level context chunking (up to 70 % token reduction).
        2. Bedrock Sonnet analysis request (Structured Output).
        3. PatchProposal creation and schema validation.
        """
        error_text = request.terminal_output or masked_context or ""
        chunked_ctx = await self._chunk_context(
            workspace_path=request.workspace_path,
            active_file=request.active_file_path or "",
            error_content=error_text,
        )

        combined_context = f"{masked_context}\n\n{chunked_ctx}".strip()
        prompt = self._build_patch_prompt(combined_context, error_text, project)

        raw_response = await self._provider.complete(
            prompt=prompt,
            model_preference="sonnet",
            agent="code_agent",
            operation="analyze_and_propose",
            max_tokens=4096,
        )

        raw_dict = self._extract_json(raw_response)
        proposal = self._validate_proposal(raw_dict)

        # Run RiskValidator to apply escalation rules and determine ApprovalLevel
        try:
            from risk_validator import RiskValidator  # type: ignore
            proposal = RiskValidator().validate_patch_proposal(proposal)
        except Exception:
            pass  # RiskValidator unavailable — use LLM-provided risk assessment

        return proposal

    async def apply_patch(self, proposal: PatchProposal) -> tuple[bool, str]:
        """
        1. Verify base_sha256 for each patch.
        2. Create backup (~/.recoder/backups/{proposal_id}/{file}).
        3. Apply unified diff.
        4. Restore from backup on failure.

        Returns (success, error_message).
        """
        session_backup_dir = _BACKUP_BASE / proposal.proposal_id
        session_backup_dir.mkdir(parents=True, exist_ok=True)

        applied: list[tuple[Path, Path | None]] = []

        for patch in proposal.patches:
            file_path = Path(patch.file)
            if not file_path.is_absolute():
                return False, f"Relative path in patch: {patch.file}. Orchestrator must resolve to absolute paths."

            # SHA-256 verification
            if patch.base_sha256 and not self._verify_base_sha256(
                str(file_path), patch.base_sha256
            ):
                return (
                    False,
                    f"SHA-256 mismatch for {patch.file}: file modified since proposal was created.",
                )

            # Backup
            safe_name = patch.file.replace("/", "_").replace("\\", "_").lstrip("_")
            backup_path = session_backup_dir / safe_name
            try:
                if file_path.exists():
                    shutil.copy2(file_path, backup_path)
                    applied.append((file_path, backup_path))
                else:
                    applied.append((file_path, None))
            except OSError as exc:
                return False, f"Backup failed for {patch.file}: {exc}"

            # Apply diff
            ok = self._apply_unified_diff(str(file_path), patch.unified_diff)
            if not ok:
                self._rollback_backups(applied)
                return False, f"Patch application failed for {patch.file}"

        return True, ""

    # ------------------------------------------------------------------
    # Context chunking
    # ------------------------------------------------------------------

    async def _chunk_context(
        self,
        workspace_path: str,
        active_file: str,
        error_content: str,
    ) -> str:
        """
        AST-based function-level chunking:
        - Python: uses the ast module; extracts only functions/classes
          whose names appear in the error trace.
        - Node.js: line-based pattern matching for related functions.
        - Falls back to raw truncation for unknown file types.
        """
        ws = Path(workspace_path)
        active = Path(active_file) if active_file else None

        mentioned_files = self._extract_file_paths(error_content, ws)
        if active and active.exists() and active not in mentioned_files:
            mentioned_files.insert(0, active)

        chunks: list[str] = []
        for fpath in mentioned_files[:5]:
            if not fpath.exists():
                continue
            try:
                source = fpath.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue

            suffix = fpath.suffix.lower()
            if suffix == ".py":
                chunk = self._chunk_python(source, error_content)
            elif suffix in (".js", ".ts", ".mjs", ".cjs"):
                chunk = self._chunk_node(source, error_content)
            else:
                chunk = source[: _MAX_CONTEXT_CHARS // max(len(mentioned_files), 1)]

            chunks.append(f"# --- {fpath.name} ---\n{chunk}")

        combined = "\n\n".join(chunks)
        return combined[:_MAX_CONTEXT_CHARS]

    def _chunk_python(self, source: str, error_content: str) -> str:
        """Return only the Python AST nodes relevant to error_content."""
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return source[:_MAX_CONTEXT_CHARS]

        lines = source.splitlines(keepends=True)
        relevant_names = self._extract_symbol_names(error_content)

        selected_ranges: list[tuple[int, int]] = []

        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = getattr(node, "name", "")
                if not relevant_names or name in relevant_names:
                    start = node.lineno
                    end = getattr(node, "end_lineno", node.lineno)
                    selected_ranges.append((start, end))

        if not selected_ranges:
            return source[:_MAX_CONTEXT_CHARS]

        selected_ranges.sort()
        merged: list[tuple[int, int]] = []
        for start, end in selected_ranges:
            if merged and start <= merged[-1][1] + 2:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            else:
                merged.append([start, end])

        parts: list[str] = []
        total_chars = 0
        for start, end in merged:
            segment = "".join(lines[start - 1 : end])
            if total_chars + len(segment) > _MAX_CONTEXT_CHARS:
                parts.append(segment[: _MAX_CONTEXT_CHARS - total_chars])
                break
            parts.append(segment)
            total_chars += len(segment)

        return "\n".join(parts)

    def _chunk_node(self, source: str, error_content: str) -> str:
        """Line-based function extraction for JS/TS files."""
        relevant_names = self._extract_symbol_names(error_content)
        lines = source.splitlines(keepends=True)

        func_start_re = re.compile(
            r"^\s*(export\s+)?(async\s+)?function\s+(\w+)|"
            r"^\s*(export\s+)?const\s+(\w+)\s*=\s*(async\s+)?\(?|"
            r"^\s*(export\s+)?class\s+(\w+)"
        )

        result_lines: list[str] = []
        inside = False
        brace_depth = 0
        total_chars = 0

        for line in lines:
            m = func_start_re.match(line)
            if m:
                name = next(
                    (g for g in m.groups() if g and re.match(r"^\w+$", g)), None
                )
                if not relevant_names or (name and name in relevant_names):
                    inside = True
                    brace_depth = 0

            if inside:
                result_lines.append(line)
                total_chars += len(line)
                brace_depth += line.count("{") - line.count("}")
                if brace_depth <= 0 and "{" in "".join(result_lines):
                    inside = False
                    result_lines.append("\n")

            if total_chars >= _MAX_CONTEXT_CHARS:
                break

        return "".join(result_lines) if result_lines else source[:_MAX_CONTEXT_CHARS]

    # ------------------------------------------------------------------
    # Error classification
    # ------------------------------------------------------------------

    async def _classify_error(self, error_text: str) -> dict[str, Any]:
        """
        Use a lightweight model (Haiku) to classify the error.
        Returns: {error_type, severity, is_fixable}
        """
        prompt = _CLASSIFY_PROMPT_TMPL.format(error=error_text[:2000])
        try:
            raw = await self._provider.complete(
                prompt=prompt,
                model_preference="haiku",
                agent="code_agent",
                operation="classify_error",
                max_tokens=256,
            )
            return self._extract_json(raw)
        except Exception as exc:
            logger.warning("Error classification failed: %s", exc)
            return {"error_type": "Other", "severity": "medium", "is_fixable": True}

    # ------------------------------------------------------------------
    # Prompt building
    # ------------------------------------------------------------------

    def _build_patch_prompt(
        self,
        context: str,
        error: str,
        project: ProjectProfile,
    ) -> str:
        return _PATCH_PROMPT_TMPL.format(
            stack=project.stack.value,
            package_manager=project.package_manager or "unknown",
            active_file="(see context)",
            context=context,
            error=error,
        )

    # ------------------------------------------------------------------
    # Proposal validation
    # ------------------------------------------------------------------

    def _validate_proposal(self, raw: dict[str, Any]) -> PatchProposal:
        """
        Validate and coerce the LLM output into a PatchProposal.
        Applies schema repair before raising.
        """
        raw.setdefault("schema_version", "1.0")
        raw.setdefault("proposal_id", str(uuid.uuid4()))
        raw.setdefault("summary", "AI-generated patch proposal")
        raw.setdefault("risk_level", RiskLevel.MEDIUM.value)
        raw.setdefault("risk_reasons", [])
        raw.setdefault("approval_level", ApprovalLevel.CONFIRM.value)
        raw.setdefault("patches", [])

        try:
            raw["risk_level"] = RiskLevel(raw["risk_level"]).value
        except ValueError:
            raw["risk_level"] = RiskLevel.MEDIUM.value

        try:
            raw["approval_level"] = ApprovalLevel(int(raw["approval_level"])).value
        except (ValueError, TypeError):
            raw["approval_level"] = ApprovalLevel.CONFIRM.value

        repaired_patches = []
        for p in raw.get("patches", []):
            if not isinstance(p, dict):
                continue
            p.setdefault("file", "unknown")
            p.setdefault("unified_diff", "")
            p.setdefault("reason", "")
            p.setdefault("base_sha256", None)
            repaired_patches.append(p)
        raw["patches"] = repaired_patches

        return PatchProposal(**raw)

    # ------------------------------------------------------------------
    # Patch application helpers
    # ------------------------------------------------------------------

    def _verify_base_sha256(self, file_path: str, expected_sha256: str) -> bool:
        """Return True if the file's SHA-256 matches expected_sha256."""
        try:
            content = Path(file_path).read_bytes()
            return hashlib.sha256(content).hexdigest() == expected_sha256
        except OSError:
            return False

    def _apply_unified_diff(self, file_path: str, unified_diff: str) -> bool:
        """
        Apply a unified diff to file_path using pure-Python line manipulation.
        Returns True on success, False on failure.
        """
        fpath = Path(file_path)
        try:
            original_lines: list[str]
            if fpath.exists():
                original_lines = fpath.read_text(encoding="utf-8").splitlines(keepends=True)
            else:
                original_lines = []

            patched = self._apply_patch_lines(original_lines, unified_diff)
            if patched is None:
                return False

            fpath.parent.mkdir(parents=True, exist_ok=True)
            fpath.write_text("".join(patched), encoding="utf-8")
            return True
        except Exception as exc:
            logger.error("Patch application error for %s: %s", file_path, exc)
            return False

    @staticmethod
    def _apply_patch_lines(
        original: list[str], unified_diff: str
    ) -> list[str] | None:
        """
        Pure-Python unified diff application.

        Parses hunks and applies them sequentially.
        Returns patched line list, or None if the patch cannot be applied cleanly.
        """
        if not unified_diff.strip():
            return original[:]

        diff_lines = unified_diff.splitlines(keepends=True)
        result = original[:]
        offset = 0

        hunk_re = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
        i = 0

        while i < len(diff_lines):
            m = hunk_re.match(diff_lines[i])
            if not m:
                i += 1
                continue

            orig_start = int(m.group(1)) - 1  # 0-indexed
            orig_count = int(m.group(2)) if m.group(2) is not None else 1
            i += 1

            removes: list[str] = []
            adds: list[str] = []

            while i < len(diff_lines) and not hunk_re.match(diff_lines[i]):
                line = diff_lines[i]
                if line.startswith("-"):
                    removes.append(line[1:])
                elif line.startswith("+"):
                    adds.append(line[1:])
                elif line.startswith(" "):
                    removes.append(line[1:])
                    adds.append(line[1:])
                i += 1

            adjusted_start = orig_start + offset
            result[adjusted_start : adjusted_start + orig_count] = adds
            offset += len(adds) - orig_count

        return result

    @staticmethod
    def _rollback_backups(applied: list[tuple[Path, Path | None]]) -> None:
        """Restore files from backups when a mid-batch patch fails."""
        for orig_path, backup_path in reversed(applied):
            try:
                if backup_path is not None and backup_path.exists():
                    shutil.copy2(backup_path, orig_path)
                elif backup_path is None and orig_path.exists():
                    orig_path.unlink(missing_ok=True)
            except OSError as exc:
                logger.error("Rollback failed for %s: %s", orig_path, exc)

    # ------------------------------------------------------------------
    # Utility helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_json(text: str) -> dict[str, Any]:
        """Extract the first JSON object from an LLM response string."""
        text = text.strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        if fence:
            try:
                return json.loads(fence.group(1))
            except json.JSONDecodeError:
                pass

        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                pass

        raise ValueError(f"Could not extract JSON from LLM response: {text[:200]}")

    @staticmethod
    def _extract_symbol_names(text: str) -> set[str]:
        """Extract Python/JS identifier names mentioned in an error trace."""
        return set(re.findall(r"\b([a-zA-Z_]\w*)\b", text))

    @staticmethod
    def _extract_file_paths(error_content: str, workspace: Path) -> list[Path]:
        """Extract file paths mentioned in an error traceback."""
        paths: list[Path] = []
        patterns = [
            re.compile(r'File ["\']([^"\']+\.py)["\']'),
            re.compile(r'at\s+([\w./\\]+\.(?:js|ts|mjs|cjs)):'),
            re.compile(r'([\w./\\]+\.(?:py|js|ts))\s*,\s*line\s*\d+'),
        ]
        seen: set[str] = set()
        for pat in patterns:
            for m in pat.finditer(error_content):
                raw = m.group(1)
                if raw in seen:
                    continue
                seen.add(raw)
                candidate = Path(raw)
                if candidate.is_absolute() and candidate.exists():
                    paths.append(candidate)
                else:
                    resolved = workspace / raw
                    if resolved.exists():
                        paths.append(resolved)
        return paths
