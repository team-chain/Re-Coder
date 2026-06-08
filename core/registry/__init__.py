"""
ReCoder Registry — CommandTemplateRegistry and FileTemplateRegistry

Loads command/file templates from JSON and template files respectively,
validates parameters (including shell-injection prevention), and renders
ready-to-execute command strings or file content.
"""

from __future__ import annotations

import json
import re
import shlex
from pathlib import Path
from typing import Any

import sys as _sys
import pathlib as _pathlib

# Ensure core package directory is on sys.path so bare `import schemas` works
# whether registry is loaded as core.registry or standalone.
_CORE_DIR = str(_pathlib.Path(__file__).parent.parent)
if _CORE_DIR not in _sys.path:
    _sys.path.insert(0, _CORE_DIR)

from schemas import ActionType, ApprovalLevel, CommandTemplate, FileTemplate, FileType, RiskLevel  # type: ignore  # noqa: E402

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_REGISTRY_DIR = Path(__file__).parent
_COMMAND_TEMPLATES_FILE = _REGISTRY_DIR / "command_templates.json"
_FILE_TEMPLATES_DIR = _REGISTRY_DIR / "file_templates"

# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

# Allowlist of characters safe for general parameter values (no shell metacharacters)
_SAFE_PARAM_RE = re.compile(r"^[a-zA-Z0-9_./:@=+\-]+$")

# Docker image name: registry/repo:tag or repo:tag
_IMAGE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._/\-]*:[a-z0-9._\-]+$")

# Simple alphanumeric tag
_TAG_RE = re.compile(r"^[a-z0-9._\-]+$")

# Relative path (no leading slash, no ..)
_REL_PATH_RE = re.compile(r"^(?!/)(?!.*\.\.)[\w./\-]+$")

# Valid port range
_PORT_MIN = 1
_PORT_MAX = 65535


class RegistryError(Exception):
    """Raised for missing templates or invalid parameters."""


# ---------------------------------------------------------------------------
# CommandTemplateRegistry
# ---------------------------------------------------------------------------


class CommandTemplateRegistry:
    """
    Loads CommandTemplates from command_templates.json and provides
    safe, validated command construction.
    """

    def __init__(self, templates_file: Path = _COMMAND_TEMPLATES_FILE) -> None:
        self._templates: dict[str, CommandTemplate] = {}
        self._load(templates_file)

    def _load(self, path: Path) -> None:
        if not path.exists():
            raise RegistryError(f"Command templates file not found: {path}")
        with path.open("r", encoding="utf-8") as fh:
            raw = json.load(fh)
        import logging as _logging
        for item in raw.get("templates", []):
            # 스키마와 안 맞는(레거시 포맷) 항목 하나가 레지스트리 전체 로딩을 깨뜨리지
            # 않도록, 검증 실패한 템플릿은 경고만 남기고 건너뛴다.
            try:
                tmpl = CommandTemplate(**item)
            except Exception as exc:  # noqa: BLE001
                _logging.getLogger(__name__).warning(
                    "Skipping malformed command template '%s': %s",
                    item.get("template_id", "<unknown>"), exc,
                )
                continue
            self._templates[tmpl.template_id] = tmpl

    # ------------------------------------------------------------------

    def get(self, template_id: str) -> CommandTemplate:
        """Return a CommandTemplate by ID, raising RegistryError if missing."""
        try:
            return self._templates[template_id]
        except KeyError:
            raise RegistryError(f"Unknown command template: '{template_id}'")

    def build_command(self, template_id: str, params: dict[str, Any]) -> str:
        """
        Validate *params* against the template's allowed_params spec and
        return the fully-substituted command string.

        Raises RegistryError on any validation failure.
        """
        tmpl = self.get(template_id)
        self._validate_params(tmpl, params)

        # Build safe substitution dict
        safe: dict[str, str] = {}
        for key, value in params.items():
            if isinstance(value, dict):
                # build_args style — flatten as --build-arg K=V pairs
                pairs = " ".join(
                    f"--build-arg {self._escape_shell(k)}={self._escape_shell(str(v))}"
                    for k, v in value.items()
                )
                safe[key] = pairs
            elif isinstance(value, int):
                safe[key] = str(value)
            else:
                safe[key] = self._escape_shell(str(value))

        try:
            return tmpl.command_pattern.format(**safe)
        except KeyError as exc:
            raise RegistryError(f"Missing parameter for template '{template_id}': {exc}") from exc

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _validate_params(self, template: CommandTemplate, params: dict[str, Any]) -> None:
        """
        Validate every supplied param against its spec and reject unknown keys.
        Raises RegistryError with a descriptive message on any violation.
        """
        allowed = template.allowed_params

        # Reject keys not in the allowlist
        for key in params:
            if key not in allowed:
                raise RegistryError(
                    f"Parameter '{key}' is not allowed for template '{template.template_id}'. "
                    f"Allowed: {list(allowed.keys())}"
                )

        # Validate each value
        for key, spec in allowed.items():
            if key not in params:
                continue  # Optional params may be absent
            value = params[key]
            param_type = spec.get("type", "string")

            if param_type == "string":
                if not isinstance(value, str):
                    raise RegistryError(f"Parameter '{key}' must be a string.")

                pattern = spec.get("pattern")
                if pattern and not re.fullmatch(pattern, value):
                    raise RegistryError(
                        f"Parameter '{key}' value '{value}' does not match pattern '{pattern}'."
                    )

                if spec.get("no_absolute") and value.startswith("/"):
                    raise RegistryError(
                        f"Parameter '{key}' must be a relative path, got absolute: '{value}'."
                    )

                if spec.get("no_absolute") and ".." in value:
                    raise RegistryError(
                        f"Parameter '{key}' must not contain path traversal sequences."
                    )

                # Generic shell-injection guard for unconstrained strings
                if not pattern and not self._is_safe_string(value):
                    raise RegistryError(
                        f"Parameter '{key}' contains unsafe characters: '{value}'."
                    )

            elif param_type == "integer":
                if not isinstance(value, int):
                    raise RegistryError(f"Parameter '{key}' must be an integer.")
                minimum = spec.get("minimum", _PORT_MIN)
                maximum = spec.get("maximum", _PORT_MAX)
                if not (minimum <= value <= maximum):
                    raise RegistryError(
                        f"Parameter '{key}' value {value} is out of range [{minimum}, {maximum}]."
                    )

            elif param_type == "object":
                if not isinstance(value, dict):
                    raise RegistryError(f"Parameter '{key}' must be an object/dict.")
                if spec.get("key_allowlist"):
                    for k, v in value.items():
                        if not re.fullmatch(r"[A-Z0-9_]+", k):
                            raise RegistryError(
                                f"Build arg key '{k}' must match [A-Z0-9_]."
                            )
                        if not self._is_safe_string(str(v)):
                            raise RegistryError(
                                f"Build arg value for '{k}' contains unsafe characters."
                            )

    @staticmethod
    def _validate_image_name(image: str) -> bool:
        """Return True if *image* is a valid Docker image reference."""
        return bool(_IMAGE_NAME_RE.fullmatch(image))

    @staticmethod
    def _validate_port(port: int) -> bool:
        """Return True if *port* is within the valid TCP port range."""
        return _PORT_MIN <= port <= _PORT_MAX

    @staticmethod
    def _escape_shell(value: str) -> str:
        """
        Return a shell-safe version of *value* using shlex quoting.
        Single-quoted strings prevent all shell expansion.
        """
        return shlex.quote(value)

    @staticmethod
    def _is_safe_string(value: str) -> bool:
        """
        Return True if *value* contains only characters safe for shell use
        without quoting (conservative allowlist).
        """
        return bool(_SAFE_PARAM_RE.fullmatch(value))


# ---------------------------------------------------------------------------
# FileTemplateRegistry
# ---------------------------------------------------------------------------


class FileTemplateRegistry:
    """
    Loads infrastructure file templates from the file_templates/ directory
    and renders them with user-supplied customizations.
    """

    def __init__(self, templates_dir: Path = _FILE_TEMPLATES_DIR) -> None:
        self._templates: dict[str, FileTemplate] = {}
        self._load(templates_dir)

    def _load(self, directory: Path) -> None:
        if not directory.exists():
            raise RegistryError(f"File templates directory not found: {directory}")

        for path in directory.iterdir():
            if path.suffix in (".py", ".pyc") or path.name == "__init__.py":
                continue
            if not path.is_file():
                continue

            template_id = path.name  # e.g. "Dockerfile.python-fastapi"
            content = path.read_text(encoding="utf-8")

            # Determine FileType from filename
            file_type = self._infer_file_type(path.name)

            tmpl = FileTemplate(
                template_id=template_id,
                file_type=file_type,
                base_content=content,
            )
            self._templates[template_id] = tmpl

    @staticmethod
    def _infer_file_type(filename: str) -> FileType:
        """Heuristically determine FileType from the template filename."""
        name_lower = filename.lower()
        if "dockerfile" in name_lower:
            return FileType.DOCKERFILE
        if "docker-compose" in name_lower or "compose" in name_lower:
            return FileType.DOCKER_COMPOSE
        if "github-actions" in name_lower or "workflow" in name_lower:
            return FileType.GITHUB_ACTIONS
        if "nginx" in name_lower:
            return FileType.NGINX_CONF
        if ".env" in name_lower:
            return FileType.ENV_FILE
        if "k8s" in name_lower or "kubernetes" in name_lower:
            return FileType.K8S_MANIFEST
        if "terraform" in name_lower or ".tf" in name_lower:
            return FileType.TERRAFORM
        return FileType.DOCKERFILE  # Default fallback

    # ------------------------------------------------------------------

    def get(self, template_id: str) -> FileTemplate:
        """Return a FileTemplate by ID, raising RegistryError if missing."""
        try:
            return self._templates[template_id]
        except KeyError:
            raise RegistryError(f"Unknown file template: '{template_id}'")

    def render(self, template_id: str, customizations: dict[str, str]) -> str:
        """
        Render the template by replacing {{PLACEHOLDER}} markers with values
        from *customizations*. Unmatched placeholders are left as-is.
        """
        tmpl = self.get(template_id)
        content = tmpl.base_content

        for key, value in customizations.items():
            placeholder = f"{{{{{key}}}}}"  # {{KEY}}
            content = content.replace(placeholder, value)

        return content

    def list_templates(self) -> list[str]:
        """Return a sorted list of all registered template IDs."""
        return sorted(self._templates.keys())
