"""Deterministic error-local code/test selection within the existing context budget."""

from __future__ import annotations

import re
from collections import Counter
from pathlib import PurePosixPath

CONTEXT_VERSION = "error-local-code-tests-v1"
CONFIGS = {
    "Dockerfile",
    ".dockerignore",
    "package.json",
    "docker-compose.yml",
    "compose.yml",
    "vite.config.js",
    "vite.config.ts",
    "tsconfig.json",
    "requirements.txt",
    "requirements.md",
    "pyproject.toml",
    "pytest.ini",
    "task-definition.json",
}
SUFFIXES = {
    ".js",
    ".cjs",
    ".mjs",
    ".jsx",
    ".ts",
    ".tsx",
    ".py",
    ".json",
    ".yaml",
    ".yml",
    ".c",
    ".txt",
}
MANIFESTS = {"package.json", "pyproject.toml", "requirements.txt"}


def _references(names: list[str], evidence: str) -> set[str]:
    remaining = evidence.replace("\\", "/")
    remaining = re.sub(
        r"(?:[^\s\"']*/)?(?:node_modules|site-packages|\.venv|venv|\.git)/[^\s\"']*",
        " ",
        remaining,
    )
    counts = Counter(PurePosixPath(name).name for name in names)
    found: set[str] = set()
    # Consume longer paths first: a nested path must not also select a root file
    # with the same basename. Ambiguous bare names are deliberately not guessed.
    for name in sorted(names, key=lambda value: (-len(value), value)):
        if "/" not in name and counts[name] > 1:
            continue
        prefix = r"(?<![\w.-])" if "/" in name else r"(?<![\w./-])(?:\./)?"
        pattern = re.compile(prefix + re.escape(name) + r"(?![\w./-])")
        if pattern.search(remaining):
            found.add(name)
            remaining = pattern.sub(" ", remaining)
    for name in names:
        base = PurePosixPath(name).name
        if counts[base] == 1 and re.search(
            r"(?<![\w./-])" + re.escape(base) + r"(?![\w./-])", remaining
        ):
            found.add(name)
    return found


def _test_stem(name: str) -> str | None:
    stem = PurePosixPath(name).stem
    if stem.startswith("test_"):
        return stem[5:]
    for suffix in (".test", ".spec", "_test"):
        if stem.endswith(suffix):
            return stem[: -len(suffix)]
    return None


def context_order(files: dict[str, str], evidence: str) -> list[str]:
    """Prefer referenced files, matching tests and their package's configuration."""
    names = sorted(files)
    references = _references(names, evidence)
    packages = {
        PurePosixPath(name).parent for name in names if PurePosixPath(name).name in MANIFESTS
    }

    def owner(name: str) -> PurePosixPath:
        return next((p for p in PurePosixPath(name).parents if p in packages), PurePosixPath("."))

    owners = {name: owner(name) for name in names}
    scopes = {owners[name] for name in references}
    # A root-level failure (e.g. Dockerfile) may involve any child package.
    scoped = bool(scopes) and PurePosixPath(".") not in scopes
    targets = {(owners[name], _test_stem(name) or PurePosixPath(name).stem) for name in references}

    def priority(name: str) -> tuple[int, str]:
        path = PurePosixPath(name)
        related = (owners[name], _test_stem(name) or path.stem) in targets
        if name in references:
            rank = 0
        elif related:
            rank = 1
        elif path.name in CONFIGS:
            rank = 2
        else:
            rank = 3
        return rank, name

    candidates = [
        name
        for name in names
        if (
            name in references
            or PurePosixPath(name).name in CONFIGS
            or PurePosixPath(name).suffix in SUFFIXES
        )
        and (
            not scoped
            or owners[name] in scopes
            or (
                PurePosixPath(name).parent == PurePosixPath(".")
                and PurePosixPath(name).name in CONFIGS
            )
        )
    ]
    return sorted(candidates, key=priority)
