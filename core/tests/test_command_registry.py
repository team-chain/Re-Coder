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
    """승인 레벨은 **위험도 순서**를 지켜야 한다.

    예전 테스트는 "레벨2=docker, 레벨3=ssh" 라고 못 박았는데, 그 뒤 레지스트리가
    커지며 레벨2 에 로컬 조회(ecs_describe_service)·SBOM(syft_sbom) 이,
    레벨3 에 docker_tag_ecr 가 들어와 그 전제가 깨졌다. 이름 매칭 대신 **실제
    위험도 불변식**으로 바꾼다.
    """
    reg = get_command_registry()
    by_level = {lvl: reg.list_templates_by_approval_level(lvl) for lvl in (1, 2, 3, 4)}
    all_templates = [t for lvl in (1, 2, 3, 4) for t in by_level[lvl]]

    def is_remote_exec(tid: str) -> bool:      # 원격 셸 실행
        return "ssh" in tid

    def is_remote_mutate(tid: str) -> bool:    # 원격 상태 변경(레지스트리 push / 서비스 갱신)
        return "push" in tid or tid.startswith("ecs_update")

    # 레벨 2 는 로컬·비파괴 작업만 — 원격 실행도, 원격 상태 변경도 없어야 한다.
    for t in by_level[2]:
        assert not is_remote_exec(t.template_id), t.template_id
        assert not is_remote_mutate(t.template_id), t.template_id

    # 원격 셸 실행(ssh)은 최소 레벨 3.
    ssh = [t for t in all_templates if is_remote_exec(t.template_id)]
    assert ssh, "ssh 템플릿이 사라졌다 — 테스트가 헛돈다"
    for t in ssh:
        assert t.approval_level >= 3, (t.template_id, t.approval_level)

    # 원격 상태 변경(레지스트리 push / 서비스 갱신)은 가장 높은 레벨 4.
    mutate = [t for t in all_templates if is_remote_mutate(t.template_id)]
    assert mutate, "원격 상태 변경 템플릿이 사라졌다 — 테스트가 헛돈다"
    for t in mutate:
        assert t.approval_level == 4, (t.template_id, t.approval_level)
