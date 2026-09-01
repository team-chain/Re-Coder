"""롤백 대상(rollback_image) 결정 규칙 — 회차4 E2E 통합 검증.

## 왜 이 테스트가 있나

`DeployAgent.create_plan()` 은 `rollback_image=None` 을 하드코딩했고, 저장소
어디에서도 그 값을 채우지 않았다. 그 값이 그대로 `DeploymentRecord.rollback_target`
이 되므로 **모든 로컬 Docker 배포가 되돌릴 수 없는 상태**였다 —
`/api/deploy/rollback` 은 언제나 422 를 냈다.

조각별 단위 테스트는 전부 초록이었다. 배포 라우트는 플랜을 잘 만들었고,
롤백 라우트는 "대상이 없으면 422" 라는 자기 규칙을 정확히 지켰다. 둘을
이어 보지 않아서 그 사이가 비어 있다는 걸 아무도 몰랐다. 그래서 여기서는
**이어진 상태**를 검사한다: 배포가 쌓였을 때 다음 플랜이 되돌릴 곳을 아는가.

## 같은 태그를 대상으로 삼지 않는 이유

`app:latest` → `app:latest` 로 재배포한 뒤 그 태그로 되돌리면, docker 는
**그 태그가 지금 가리키는 이미지**(= 방금 올린 것)를 다시 띄운다. 롤백했다는
표시만 남고 실제로는 아무것도 되돌아가지 않는다. 그런 값을 대상으로 넣으면
"롤백 가능"이라 표시해 놓고 정작 필요할 때 사용자를 못 구한다.
"""
from __future__ import annotations

import pytest

import api.routes.deploy as deploy_route
from schemas import ActionType, DeploymentPlan, DeploymentRecord, DeployMethod, DeployStatus


@pytest.fixture(autouse=True)
def clean_records():
    """배포 기록은 프로세스 전역이라 테스트끼리 샌다. 매번 비운다."""
    deploy_route._deployment_records.clear()
    yield
    deploy_route._deployment_records.clear()


def _record(
    container: str,
    image: str,
    status: DeployStatus = DeployStatus.SUCCESS,
    rollback_eligible: bool = True,
) -> DeploymentRecord:
    rec = DeploymentRecord(
        project_id="p",
        method=DeployMethod.LOCAL_DOCKER,
        image=image,
        container_name=container,
        status=status,
        rollback_eligible=rollback_eligible,
    )
    deploy_route._deployment_records[rec.deployment_id] = rec
    return rec


def test_첫_배포는_되돌릴_곳이_없고_이유를_알려준다():
    target, reason = deploy_route._previous_image_for("api", "api:v1")

    assert target is None
    assert "검증 완료" in reason, reason


def test_이전_성공_배포의_이미지가_롤백_대상이_된다():
    """DoD 의 핵심 — v1 배포 뒤 v2 플랜은 v1 을 되돌릴 곳으로 안다."""
    _record("api", "api:v1")

    target, reason = deploy_route._previous_image_for("api", "api:v2")

    assert target == "api:v1"
    assert "api:v1" in reason


def test_가장_최근_성공_배포를_고른다():
    import time

    _record("api", "api:v1")
    time.sleep(0.01)  # deployed_at 이 같은 마이크로초면 정렬이 흔들린다
    _record("api", "api:v2")

    target, _ = deploy_route._previous_image_for("api", "api:v3")

    assert target == "api:v2", "가장 최근 것이 아니라 더 옛날 배포를 골랐다"


def test_같은_태그로_재배포하면_대상이_없다():
    """되돌려도 방금 올린 이미지가 다시 뜬다 — 대상으로 삼으면 거짓말이 된다."""
    _record("api", "api:latest")

    target, reason = deploy_route._previous_image_for("api", "api:latest")

    assert target is None
    assert "태그가 같습니다" in reason, reason
    assert "다른 태그" in reason, "무엇을 해야 하는지가 사유에 없다"


def test_같은_태그가_섞여_있어도_더_앞의_다른_태그를_찾는다():
    """latest 로 여러 번 올리다 태그를 바꾼 경우, 되돌릴 곳은 남아 있다."""
    import time

    _record("api", "api:v1")
    time.sleep(0.01)
    _record("api", "api:latest")

    target, _ = deploy_route._previous_image_for("api", "api:latest")

    assert target == "api:v1"


def test_실패한_배포는_롤백_대상이_아니다():
    """돌아가지 않았던 이미지로 되돌리면 장애가 장애로 이어진다."""
    _record("api", "api:broken", status=DeployStatus.FAILED)

    target, reason = deploy_route._previous_image_for("api", "api:v2")

    assert target is None
    assert "검증 완료" in reason


def test_헬스_검증을_통과하지_못한_배포는_롤백_대상이_아니다():
    """docker run 이 성공했어도 앱이 응답하지 않으면 복구 버전이 될 수 없다."""
    _record("api", "api:crashes-after-start", rollback_eligible=False)

    target, reason = deploy_route._previous_image_for("api", "api:v2")

    assert target is None
    assert "검증 완료" in reason


def test_승인_대기_플랜은_실행_직전에_최신_검증_대상으로_갱신된다():
    """v2/v3 플랜이 동시에 있어도 v3은 v2로 되돌아가야 한다."""
    _record("api", "api:v1")
    plan = DeploymentPlan(
        method=DeployMethod.LOCAL_DOCKER,
        action=ActionType.DOCKER_RUN,
        image="api:v3",
        container_name="api",
        rollback_image="api:v1",  # v3 플랜을 만들었을 당시의 오래된 스냅샷
    )
    _record("api", "api:v2")

    target, _reason = deploy_route._refresh_rollback_target(plan)

    assert target == "api:v2"
    assert plan.rollback_image == "api:v2"


def test_다른_컨테이너의_배포는_섞이지_않는다():
    _record("worker", "worker:v1")

    target, _ = deploy_route._previous_image_for("api", "api:v2")

    assert target is None, "다른 컨테이너의 이미지를 롤백 대상으로 잡았다"


def test_컨테이너_이름이_비면_대상을_찾지_않는다():
    _record("api", "api:v1")

    target, reason = deploy_route._previous_image_for("", "api:v2")

    assert target is None
    assert "컨테이너 이름" in reason
