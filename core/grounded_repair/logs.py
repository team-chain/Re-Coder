"""Bounded, secret-masked evidence extraction without an LLM."""
from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass

from build_failure import diagnose
from context_gate import mask_secrets

SIGNAL = re.compile(
    r"ETARGET|E[A-Z][A-Z_0-9]{3,}|AccessDenied\w*|CannotPullContainerError|"
    r"ResourceInitializationError|stoppedReason|stopCode|health.?check|"
    r"not authorized|essential container|error|failed|exception|denied", re.I
)
CODE = re.compile(r"\b(?:E[A-Z][A-Z_0-9]{3,}|AccessDenied\w*|CannotPullContainerError|ResourceInitializationError)\b")
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


@dataclass
class Evidence:
    code: str
    lines: list[str]
    keywords: list[str]
    original_chars: int
    fingerprint: str

    def to_dict(self):
        return asdict(self)

    @property
    def text(self):
        return "\n".join(self.lines)


def summarize(log: str, stage: str = "build", limit: int = 4000) -> Evidence:
    # Scan the entire bounded request, so an early root cause survives long tails.
    clean = mask_secrets(ANSI.sub("", log))
    diagnosis = diagnose(clean, stage=stage)
    lines = clean.splitlines()
    selected = list(diagnosis.lines)
    for i, line in enumerate(lines):
        if SIGNAL.search(line):
            selected.extend(lines[max(0, i - 1):i + 2])
    # Specific codes and ECS reasons outrank generic BuildKit wrapper failures.
    selected = sorted(dict.fromkeys(selected), key=lambda s: not (
        CODE.search(s) or re.search(r"stoppedReason|stopCode|not authorized", s, re.I)))
    picked, size = [], 0
    for line in selected:
        line = re.sub(r"^#\d+\s+\d+\.\d+\s*", "", line).strip()[:600]
        if not line or line in picked:
            continue
        if size + len(line) + 1 > limit or len(picked) >= 16:
            break
        picked.append(line)
        size += len(line) + 1
    text = "\n".join(picked)
    keywords = sorted(set(CODE.findall(text)))[:16]
    digest = hashlib.sha256((stage + "\n" + text).encode()).hexdigest()
    return Evidence(diagnosis.code, picked, keywords, len(log), digest)
