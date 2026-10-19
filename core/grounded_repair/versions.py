"""Conservative declared dependency versions from the verified workspace snapshot."""

from __future__ import annotations

import json
import re
from pathlib import Path

from grounded_repair.workspace import safe_path

_SINGLE_MAJOR = re.compile(
    r"[~^]?v?(0|[1-9][0-9]*)(?:\.(?:[xX*]|(?:0|[1-9][0-9]*)" r"(?:\.(?:0|[1-9][0-9]*|[xX*]))?))?"
)


def declared_major(spec: object) -> int | None:
    """Accept only simple stable npm ranges confined to one major version."""
    match = _SINGLE_MAJOR.fullmatch(spec.strip()) if isinstance(spec, str) else None
    return int(match[1]) if match else None


def project_versions(root: Path, files: dict[str, str], packages: set[str]) -> dict[str, int]:
    """Return unambiguous majors; conflicting/unknown monorepo declarations stay unknown."""
    seen: dict[str, set[int | None]] = {}
    if not packages:
        return {}
    for name in sorted(files):
        if Path(name).name != "package.json":
            continue
        path = safe_path(root, name)
        # An unreadable manifest may contain a conflicting declaration.
        if path.stat().st_size > 128_000:
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (UnicodeError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        for field in (
            "dependencies",
            "devDependencies",
            "optionalDependencies",
            "peerDependencies",
        ):
            dependencies = data.get(field, {})
            if not isinstance(dependencies, dict):
                return {}
            for package, spec in dependencies.items():
                if package in packages:
                    seen.setdefault(package, set()).add(declared_major(spec))
    return {
        package: next(iter(majors))
        for package, majors in seen.items()
        if len(majors) == 1 and None not in majors
    }
