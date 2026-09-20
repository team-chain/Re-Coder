"""
pytest 설정 — core 디렉터리를 import path 에 추가.

pytest 가 core/tests 에서 실행될 때 schemas, server, registries 등을
상대경로 없이 import 할 수 있게 한다.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

CORE_DIR = Path(__file__).resolve().parent.parent
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))

# **배포 기록 저장소를 개발자 홈에서 떼어 놓는다.**
#
# `api/routes/ecs.py` 는 **임포트 시점에** `_load_records()` 를 부른다. 기본
# 경로가 `~/.recoder/ecs_deployments.json` 이라, 막지 않으면 테스트가 개발자의
# 진짜 배포 기록을 읽고 쓴다. 남이 남긴 PENDING 기록 하나 때문에 라우트
# 테스트가 409 를 받아 **그 사람 히스토리에 따라 결과가 달라진다.**
#
# 개별 테스트에서 `monkeypatch.setenv` 로 덮는 것으로는 늦다 — 임포트가 먼저
# 일어난다. 세션이 시작되기 전에 깔아야 한다.
os.environ.setdefault(
    "RECODER_ECS_STORE",
    str(Path(tempfile.mkdtemp(prefix="recoder-test-store-")) / "ecs_deployments.json"),
)

# **ADR 번호 예약 장부도 같은 이유로 떼어 놓는다.**
#
# 기본 경로가 `~/.recoder/adr_reservations.json` 이라, 막지 않으면 테스트가
# 개발자의 진짜 장부에 번호를 적는다. 그러면 `build_adr_ops` 를 부르는
# 테스트가 실행될 때마다 번호가 올라가고, 번호를 단언하는 테스트는
# **몇 번째로 돌리느냐에 따라 결과가 달라진다.**
os.environ.setdefault(
    "RECODER_ADR_STORE",
    str(Path(tempfile.mkdtemp(prefix="recoder-test-adr-")) / "adr_reservations.json"),
)

# 테스트 중에는 실제 LLM 호출을 막기 위한 환경변수
os.environ.setdefault("RECODER_TEST_MODE", "1")
# infra_agent 의 LLM 커스터마이징도 비활성화 → 템플릿 그대로 반환되도록
os.environ.setdefault("RECODER_INFRA_AI_CUSTOMIZE", "0")

# **역할 환경변수도 세션 시작 전에 떼어 놓는다.**
#
# 개발자 PC 에는 실제 배포용으로 `ECS_EXECUTION_ROLE_ARN=...LabRole`
# (AWS Academy) 같은 값이 시스템 환경이나 셸 프로필에 남아 있을 수 있다.
# `aws_policy.build_policy()` 는 이 값을 읽어 PassRole 대상에 그 역할을
# 추가하므로, 안 막으면 권한표·온보딩 템플릿 드리프트 테스트가
# **어느 컴퓨터에서 돌리느냐에 따라** 통과/실패가 갈린다.
#
# `setdefault` 로는 못 막는다 — 이미 설정된 값을 지워야 한다. 이 값이
# 필요한 테스트는 전부 `monkeypatch.setenv` 로 직접 설정하므로 잃는 것이 없다.
for _role_var in ("ECS_EXECUTION_ROLE_ARN", "ECS_TASK_ROLE_ARN"):
    os.environ.pop(_role_var, None)
