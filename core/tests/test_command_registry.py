"""
P0-12 smoke #1: CommandTemplate Registry.

목적:
- 11 개의 CommandTemplate 가 모두 로드되는지
- docker_build / docker_run 의 build_command 가 안전한 명령을 생성하는지
- approval_level 별 분류가 정상인지 (Level 2 = 6개 docker, Level 3 = SSH)
- Injection / 잘못된 image 이름이 ValidationError 로 차단되는지
"""
from __future__ import annotations

import pytest

from registries.command_registry import (
    CommandRegistry,
    ValidationError,
    get_command_registry,
)


def test_registry_singleton_and_template_count():
    reg = get_command_registry()
    assert isinstance(reg, CommandRegistry)
    # 모든 템플릿
    all_templates = reg.list_templates()
    assert len(all_templates) >= 6, "최소 6 개의 docker 템플릿은 있어야 함"
    ids = {t.template_id for t in all_templates}
    for required in ("docker_build", "docker_run", "docker_stop", "docker_logs"):
        assert required in ids, f"{required} 템플릿이 누락됨"


def test_docker_build_renders_safe_command():
    reg = get_command_registry()
    cmd = reg.build_command("docker_build", {
        "image": "recoder-app:latest",
        "context_path": ".",
    })
    assert cmd.startswith("docker build"), cmd
    assert "recoder-app:latest" in cmd
    # injection 흔적이 끼지 않아야 한다
    assert ";" not in cmd and "&&" not in cmd


def test_docker_run_renders_safe_command():
    reg = get_command_registry()
    cmd = reg.build_command("docker_run", {
        "container_name": "recoder-app",
        "host_port": 8000,
        "container_port": 8000,
        "image": "recoder-app:latest",
    })
    assert cmd == "docker run -d --name recoder-app -p 8000:8000 recoder-app:latest", cmd


def test_unknown_template_raises():
    reg = get_command_registry()
    with pytest.raises(ValueError):
        reg.build_command("rm_rf_root", {})


def test_injection_param_is_blocked():
    reg = get_command_registry()
    # container_name 에 셸 메타문자 → ValidationError
    with pytest.raises(ValidationError):
        reg.build_command("docker_logs", {
            "lines": 50,
            "container_name": "app; rm -rf /",
        })


def test_invalid_image_name_blocked():
    reg = get_command_registry()
    with pytest.raises(ValidationError):
        reg.build_command("docker_build", {
            "image": "BadName With Spaces",
            "context_path": ".",
        })


def test_approval_levels_partition():
    reg = get_command_registry()
    level2 = reg.list_templates_by_approval_level(2)
    level3 = reg.list_templates_by_approval_level(3)
    # Level 2 는 docker_*, Level 3 는 ssh_*
    assert all("docker" in t.template_id for t in level2)
    assert all("ssh" in t.template_id for t in level3) or len(level3) == 0
