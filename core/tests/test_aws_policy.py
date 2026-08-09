"""
FR-04-02 최소권한 정책 검증.

이 테스트의 목적은 "JSON 이 잘 만들어지나"가 아니다. **정책과 실제 코드가
어긋나는 것**을 막는 것이다. 권한표는 어긋나도 조용하다 — 사용자가 배포하다
막혀야 비로소 드러나고, 그때는 원인을 찾기 어렵다. 그래서

  - 코드가 부르는데 정책에 없으면 → 사용자 배포 실패
  - 정책에 있는데 아무 근거도 없으면 → 필요 없는 권한을 열어준 것

두 방향을 모두 잡는다.

## 이전 판이 왜 뚫렸나 (반드시 읽을 것)

이전 판은 정규식으로 소스를 훑고, 손으로 적은 대조표(`_CALL_TO_ACTION`)에서
찾았다. 그런데 **대조표에 없는 호출을 만나면 조용히 건너뛰었다.** 즉 새로
추가된 호출일수록 그냥 통과했다. 소스에 없던 호출 두 개를 일부러 넣고
돌렸더니 15개 테스트가 전부 통과했다.

게다가 그걸 잡으라고 만든 테스트는 docstring 에 "새 호출이 생기면 알려준다"고
써놓고 실제로는 **반대 방향**만 검사하고 있었다. 설명문이 코드보다 앞서 나간
것이다. 3차 리뷰 때 지적받은 것과 같은 실수다.

**교훈: 음성 대조(일부러 부수기)를 안 한 주장은 하지 않는다.** 이 파일의
대조 테스트들은 전부 부숴서 확인했다.

## 지금 판이 다른 점

  - `aws_calls.scan_source()` 가 **문법 트리**를 읽는다. 변수 이름에 좌우되지
    않고, AWS 와 무관한 `client.get(...)` 을 AWS 호출로 착각하지 않으며,
    `["aws", "ecr", ...]` CLI 경로와 인자로 넘어간 클라이언트까지 따라간다.
  - **모르는 호출은 조용히 넘기지 않고 실패**시킨다.
  - 정책에만 있는 권한은 `PINNED`(정적 분석으로 안 보이지만 지금 필요) 또는
    `PLANNED`(아직 안 쓰지만 곧 씀) 에 **근거를 적어야** 통과한다.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

import aws_calls as ac
import aws_policy as ap


CORE_DIR = Path(__file__).resolve().parent.parent


# ── 기본 형태 ────────────────────────────────────────────────────────

def test_policy_is_valid_iam_document():
    p = ap.build_policy(account_id="123456789012", region="ap-northeast-2")
    assert p["Version"] == "2012-10-17"
    assert p["Statement"], "빈 정책"
    for stmt in p["Statement"]:
        assert stmt["Effect"] == "Allow"
        assert stmt["Sid"] and re.fullmatch(r"[A-Za-z0-9]+", stmt["Sid"]), \
            f"Sid 는 영숫자만 허용된다: {stmt.get('Sid')!r}"
        assert stmt["Action"], f"{stmt['Sid']}: Action 이 비었다"
        assert stmt["Resource"], f"{stmt['Sid']}: Resource 가 비었다"


def test_sid_values_are_unique():
    p = ap.build_policy()
    sids = [s["Sid"] for s in p["Statement"]]
    assert len(set(sids)) == len(sids), f"Sid 중복: {sids}"


def test_same_targets_always_produce_identical_json():
    """대상 조합이 같으면 순서가 달라도 결과가 같아야 한다.

    사용자가 정책을 다시 발급받았을 때 이전 것과 비교할 수 있어야 한다.
    """
    a = ap.policy_json(["s3", "ecs"], "123456789012", "ap-northeast-2")
    b = ap.policy_json(["ecs", "s3"], "123456789012", "ap-northeast-2")
    assert a == b


# ── 자리표시자 ───────────────────────────────────────────────────────

def test_placeholders_remain_when_account_unknown():
    p = ap.build_policy()
    assert ap.has_placeholder(p), "계정을 모르면 사용자가 채우도록 표시가 남아야 한다"
    assert ap.ACCOUNT_PLACEHOLDER in json.dumps(p)


def test_placeholders_filled_when_account_known():
    p = ap.build_policy(account_id="123456789012", region="ap-northeast-2")
    assert not ap.has_placeholder(p)
    text = json.dumps(p)
    assert "123456789012" in text and "ap-northeast-2" in text


# ── 권한 범위 ────────────────────────────────────────────────────────

def test_no_administrator_level_permissions():
    """서비스 전체 와일드카드(`ecs:*` 등)를 열지 않는다."""
    for action in ap.used_actions(ap.build_policy()):
        assert not action.endswith(":*"), f"서비스 전체를 열고 있다: {action}"
        assert action != "*", "전체 권한을 열고 있다"


def test_wildcard_resources_are_limited_to_actions_that_require_it():
    """`Resource: "*"` 는 AWS 가 리소스 단위 제한을 지원하지 않는 액션에만 허용.

    여기 목록에 없는 액션이 `*` 로 열리면 범위가 넓어진 것이므로 실패시킨다.
    """
    allowed_wildcard = {
        "sts:GetCallerIdentity":        "계정 단위 조회 — 리소스가 없다",
        "ecr:GetAuthorizationToken":    "계정 단위 토큰 발급",
        "ecs:RegisterTaskDefinition":   "AWS 가 리소스 단위 제한 미지원",
        "ecs:DescribeTaskDefinition":   "AWS 가 리소스 단위 제한 미지원",
        "logs:DescribeLogGroups":       "AWS 가 리소스 단위 제한 미지원",
        "bedrock:ListFoundationModels": "계정 단위 조회",
    }
    for stmt in ap.build_policy()["Statement"]:
        if stmt["Resource"] != "*":
            continue
        for action in stmt["Action"]:
            assert action in allowed_wildcard, \
                f"{action} 이 근거 없이 Resource '*' 로 열려 있다 ({stmt['Sid']})"


def test_passrole_is_restricted_to_ecs_tasks():
    """PassRole 에 조건이 없으면 이 키로 아무 서비스에나 역할을 넘길 수 있다."""
    stmt = next(s for s in ap.build_policy()["Statement"] if "iam:PassRole" in s["Action"])
    condition = stmt.get("Condition", {})
    assert condition.get("StringEquals", {}).get("iam:PassedToService") == "ecs-tasks.amazonaws.com", \
        "PassRole 이 ECS 작업 외의 서비스로도 넘어갈 수 있다 (권한 상승 위험)"


def test_resource_scoped_to_recoder_prefix():
    """ECR 리포지토리·S3 버킷은 접두사로 좁혀 사용자의 다른 자원을 건드리지 못하게 한다."""
    for stmt in ap.build_policy(account_id="1", region="us-east-1")["Statement"]:
        resources = stmt["Resource"]
        for res in ([resources] if isinstance(resources, str) else resources):
            if res.startswith("arn:aws:s3:::") or ":repository/" in res:
                assert ap.RESOURCE_PREFIX in res, f"범위가 좁혀지지 않음: {res}"


# ── 대상 선택 ────────────────────────────────────────────────────────

def test_target_selection_changes_scope():
    ecs_only = ap.used_actions(ap.build_policy(["ecs"]))
    assert any(a.startswith("ecs:") for a in ecs_only)
    assert not any(a.startswith("s3:") for a in ecs_only)
    assert not any(a.startswith("bedrock:") for a in ecs_only)
    # 자격증명 확인은 대상과 무관하게 항상 필요하다.
    assert "sts:GetCallerIdentity" in ecs_only


def test_unknown_target_is_rejected_with_a_helpful_message():
    with pytest.raises(ValueError, match="알 수 없는 배포 대상"):
        ap.build_policy(["lambda"])


# ── AWS Academy (학교 계정) 지원 ─────────────────────────────────────
#
# 학교 계정은 IAM 역할을 만들 수 없고 미리 만들어진 `LabRole` 만 준다.
# 실행 역할 이름이 고정돼 있으면 학교 계정에서 개발 자체가 막힌다.

def test_task_execution_role_is_configurable_for_academy():
    """학교 계정은 역할을 못 만들므로 **실행 역할·태스크 역할 둘 다** LabRole 이다.

    한쪽만 바꾸면 나머지 하나가 존재하지 않는 역할(`ecsTaskRole`)을 가리켜,
    그 역할에 PassRole 을 주는 무의미한 정책이 나온다.
    """
    p = ap.build_policy(["ecs"], "123456789012", "us-east-1",
                        ap.ACADEMY_TASK_EXECUTION_ROLE,
                        task_role=ap.ACADEMY_TASK_EXECUTION_ROLE)
    role_arns = [
        r
        for s in p["Statement"]
        for r in ([s["Resource"]] if isinstance(s["Resource"], str) else s["Resource"])
        if ":role/" in r
    ]
    assert role_arns, "역할 ARN 이 하나도 없다"
    for arn in role_arns:
        assert arn.endswith(f"role/{ap.ACADEMY_TASK_EXECUTION_ROLE}"), \
            f"학교 계정용인데 LabRole 이 아니다: {arn}"


def test_default_role_is_the_standard_one():
    """인자를 안 주면 일반 계정 기준이어야 한다 (사용자용 기본값)."""
    text = json.dumps(ap.build_policy(["ecs"], "1", "us-east-1"))
    assert f"role/{ap.TASK_EXECUTION_ROLE}" in text


def test_passing_an_arn_instead_of_a_name_is_rejected():
    """ARN 을 통째로 넘기면 `role/arn:aws:...` 같은 쓰레기 ARN 이 만들어진다."""
    with pytest.raises(ValueError, match="ARN 이 아니라 이름"):
        ap.build_policy(task_execution_role="arn:aws:iam::1:role/LabRole")


# ── [핵심] 정책 ↔ 실제 코드 대조 ──────────────────────────────────────
#
# 정적 분석으로는 **절대** 안 보이지만 실제로 필요한 권한.
# 근거 없이 늘리지 말 것 — 여기 적는 순간 최소권한 원칙에 구멍이 난다.

PINNED_ACTIONS: dict[str, str] = {
    "ecr:BatchCheckLayerAvailability":
        "docker push — 파이썬이 아니라 docker 데몬이 호출한다",
    "ecr:InitiateLayerUpload":
        "docker push — 파이썬이 아니라 docker 데몬이 호출한다",
    "ecr:UploadLayerPart":
        "docker push — 파이썬이 아니라 docker 데몬이 호출한다",
    "ecr:CompleteLayerUpload":
        "docker push — 파이썬이 아니라 docker 데몬이 호출한다",
    "ecr:PutImage":
        "docker push — 파이썬이 아니라 docker 데몬이 호출한다",
    "iam:PassRole":
        "ecs:RegisterTaskDefinition 에 실행 역할 ARN 을 넘기면 AWS 가 내부적으로 "
        "요구한다. 코드에 pass_role 이라는 호출은 존재하지 않는다",
    "ecr:BatchGetImage":
        "배포 전 보안 스캔(trivy)·SBOM(syft)이 원격 이미지를 끌어온다. "
        "외부 실행 파일이 하는 일이라 파이썬 코드에 안 보인다",
    "ecr:GetDownloadUrlForLayer":
        "같은 이유 — trivy/syft 가 ECR 에서 레이어를 받아온다",
}

#: 아직 코드가 안 쓰지만 미리 발급하는 권한. **카드 번호를 반드시 적는다.**
#: 그 카드가 끝나면 코드에서 호출이 발견되고, 아래 자기청소 테스트가
#: "이제 여기서 빼라"고 알려준다.
PLANNED_ACTIONS: dict[str, str] = {
    "s3:CreateBucket":       "FR-05-03 S3 배포 BYO 전환 — 미착수",
    "s3:ListBucket":         "FR-05-03 S3 배포 BYO 전환 — 미착수",
    "s3:GetBucketLocation":  "FR-05-03 S3 배포 BYO 전환 — 미착수",
    "s3:PutBucketWebsite":   "FR-05-03 S3 배포 BYO 전환 — 미착수",
    "s3:PutBucketPolicy":    "FR-05-03 S3 배포 BYO 전환 — 미착수",
    "s3:PutObject":          "FR-05-03 S3 배포 BYO 전환 — 미착수",
    "s3:GetObject":          "FR-05-03 S3 배포 BYO 전환 — 미착수",
    "s3:DeleteObject":       "FR-05-03 S3 배포 BYO 전환 — 미착수",
}


@pytest.fixture(scope="module")
def scan() -> ac.ScanResult:
    return ac.scan_source(CORE_DIR)


def test_scanner_actually_finds_the_calls_we_know_are_there(scan):
    """**이 테스트가 없으면 나머지 대조가 전부 무의미하다.**

    스캐너가 조용히 빈 목록을 돌려주면 "빠진 권한 0" 이 되어 통과해 버린다.
    눈으로 확인한 호출 몇 개를 못 박아, 스캐너가 죽으면 여기서 먼저 터지게 한다.
    """
    found = {(c.service, c.operation) for c in scan.calls}
    must_find = {
        ("sts", "get_caller_identity"),     # api/routes/aws.py
        ("ecs", "update_service"),          # ecs_deploy_agent.py
        ("ecs", "register_task_definition"),
        ("ecs", "describe_services"),
        ("iam", "get_role"),                # agents/preflight_agent.py
        ("logs", "describe_log_groups"),
        ("ecr", "describe_repositories"),
        ("ecr", "create-repository"),       # AWS CLI 경로 — 정규식은 못 봤다
        ("ecr", "get-login-password"),      # AWS CLI 경로
        ("bedrock-runtime", "converse"),    # 인자로 넘어간 클라이언트
        ("bedrock", "list_foundation_models"),
    }
    missing = sorted(must_find - found)
    assert not missing, (
        "스캐너가 알려진 호출을 못 찾았다 — 스캐너가 깨졌을 가능성이 크다:\n  "
        + "\n  ".join(f"{s}.{o}" for s, o in missing)
    )


def test_no_aws_call_is_left_unmapped(scan):
    """[구멍 메움] IAM 액션으로 옮기지 못한 호출이 하나라도 있으면 실패.

    이전 판은 여기서 조용히 건너뛰었고, 그래서 새 호출이 통과했다.
    모르면 통과시키지 말고 **사람을 부른다.**
    """
    unmapped = scan.unmapped()
    assert not unmapped, (
        "IAM 액션으로 옮기지 못한 AWS 호출:\n  "
        + "\n  ".join(str(c) for c in unmapped)
        + "\n(aws_calls.OPERATION_TO_ACTION 또는 NOT_AN_API_CALL 에 근거와 함께 추가하세요)"
    )


def test_deliberately_ignored_helpers_are_not_reported_as_unmapped(tmp_path):
    """`client.close()` 는 권한이 아니다. 그렇다고 "모르는 호출"도 아니다.

    둘을 섞으면 누군가 `close()` 를 부르는 순간 테스트가 엉뚱하게 실패하고,
    그 사람은 테스트를 의심하는 대신 꺼버리게 된다.
    """
    (tmp_path / "mod.py").write_text("""
import boto3
def go():
    c = boto3.client("ecs")
    c.close()
""", encoding="utf-8")
    result = ac.scan_source(tmp_path)
    assert result.unmapped() == [], "close() 를 매핑 실패로 오해했다"
    assert [c.operation for c in result.ignored()] == ["close"]


def test_a_call_we_cannot_classify_is_reported(tmp_path):
    """반대로, 정말 모르겠는 호출은 조용히 넘기지 않는다."""
    (tmp_path / "mod.py").write_text("""
import boto3
def go():
    c = boto3.client("ecs")
    c._undocumented_thing()
""", encoding="utf-8")
    result = ac.scan_source(tmp_path)
    assert [c.operation for c in result.unmapped()] == ["_undocumented_thing"]


def test_every_aws_call_in_the_source_is_covered_by_the_policy(scan):
    """[핵심] 코드가 부르는 AWS API 중 정책에 빠진 것이 없어야 한다.

    빠지면 사용자가 배포 도중 권한 부족으로 막힌다.
    """
    granted = set(ap.used_actions(ap.build_policy()))
    missing = ac.missing_from_policy(scan.actions(), granted)
    where = {}
    for call in scan.calls:
        action = ac.iam_action(call)
        if action in missing:
            where.setdefault(action, []).append(call.where)
    assert not missing, (
        "코드가 호출하는데 권한표에 없는 액션:\n  "
        + "\n  ".join(f"{a}  ({', '.join(where.get(a, [])[:3])})" for a in missing)
        + "\n(aws_policy.py 를 갱신하세요)"
    )


def test_every_granted_action_has_evidence(scan):
    """[핵심 · 반대 방향] 정책의 모든 권한은 근거가 있어야 한다.

    근거는 셋 중 하나다 — 코드에서 발견됨 / PINNED / PLANNED.
    아무 데도 없으면 이유 없이 열어준 권한이다.
    """
    granted = set(ap.used_actions(ap.build_policy()))
    justified = scan.actions() | set(PINNED_ACTIONS) | set(PLANNED_ACTIONS)
    orphans = sorted(granted - justified)
    assert not orphans, (
        "근거 없이 열려 있는 권한:\n  " + "\n  ".join(orphans)
        + "\n(코드에서 쓰거나, PINNED/PLANNED 에 이유를 적거나, 정책에서 빼세요)"
    )


def test_pinned_and_planned_entries_are_not_stale(scan):
    """자기청소 — 목록에 적어둔 권한이 실제로 쓰이기 시작하면 알려준다.

    PLANNED 는 "아직 코드가 안 쓴다"는 뜻이다. 그 카드가 끝나 코드가 쓰기
    시작했는데도 PLANNED 에 남아 있으면, 그 권한은 이제 **자동 대조의
    보호를 못 받는다.** 그래서 옮기라고 실패시킨다.
    """
    discovered = scan.actions()
    stale_planned = sorted(discovered & set(PLANNED_ACTIONS))
    assert not stale_planned, (
        "이제 코드가 실제로 쓰는데 아직 PLANNED 에 있다 — 빼세요:\n  "
        + "\n  ".join(stale_planned)
    )
    stale_pinned = sorted(discovered & set(PINNED_ACTIONS))
    assert not stale_pinned, (
        "정적 분석이 이제 찾아내는데 아직 PINNED 에 있다 — 빼세요:\n  "
        + "\n  ".join(stale_pinned)
    )


def test_pinned_and_planned_do_not_overlap():
    both = sorted(set(PINNED_ACTIONS) & set(PLANNED_ACTIONS))
    assert not both, f"PINNED 와 PLANNED 에 동시에 있다: {both}"


def test_every_planned_action_names_a_card():
    """PLANNED 는 근거가 흐려지기 쉽다. 카드 번호를 강제한다."""
    for action, reason in PLANNED_ACTIONS.items():
        assert re.search(r"FR-\d{2}-\d{2}", reason), \
            f"{action}: PLANNED 사유에 카드 번호가 없다 — {reason!r}"


def test_scan_reports_what_it_skipped(scan):
    """건너뛴 파일을 조용히 숨기지 않는다. 숨기면 '다 봤다'고 오해한다."""
    assert scan.skipped, "건너뛴 파일이 하나도 없다 — 제외 규칙이 안 먹었다"
    for line in scan.skipped:
        assert " — " in line, f"제외 사유가 없다: {line}"


# ── 정적 분석이 못 보는 것 (따로 고정) ────────────────────────────────

def test_docker_push_actions_are_present():
    """docker CLI 가 쓰는 레이어 업로드 권한 — 파이썬 코드에는 안 보인다."""
    granted = set(ap.used_actions(ap.build_policy(["ecs"])))
    for action in [
        "ecr:GetAuthorizationToken",
        "ecr:CreateRepository",
        "ecr:BatchCheckLayerAvailability",
        "ecr:InitiateLayerUpload",
        "ecr:UploadLayerPart",
        "ecr:CompleteLayerUpload",
        "ecr:PutImage",
    ]:
        assert action in granted, f"docker push 에 필요한 {action} 이 빠졌다"


def test_passrole_present_even_though_no_python_call_shows_it():
    """PassRole 도 코드 grep 으로는 안 드러난다 — 배포 마지막 단계에서야 터진다."""
    assert "iam:PassRole" in ap.used_actions(ap.build_policy(["ecs"]))


# ── 이름 변환 규칙 ───────────────────────────────────────────────────

@pytest.mark.parametrize("service,operation,expected", [
    ("ecs", "update_service",            "ecs:UpdateService"),
    ("sts", "get_caller_identity",       "sts:GetCallerIdentity"),
    ("logs", "describe_log_groups",      "logs:DescribeLogGroups"),
    ("s3", "list_objects_v2",            "s3:ListBucket"),
    # 이름에 속으면 안 되는 것 — AWS 문서상 Converse 는 InvokeModel 로 인가된다.
    ("bedrock-runtime", "converse",      "bedrock:InvokeModel"),
    ("bedrock", "list_foundation_models", "bedrock:ListFoundationModels"),
])
def test_operation_maps_to_the_right_iam_action(service, operation, expected):
    call = ac.AwsCall(service=service, operation=operation, where="시험")
    assert ac.iam_action(call) == expected


@pytest.mark.parametrize("operation", sorted(ac.NOT_AN_API_CALL))
def test_client_helpers_are_not_counted_as_api_calls(operation):
    """`close()` `get_paginator()` 는 네트워크로 안 나간다. 권한이 아니다."""
    call = ac.AwsCall(service="ecs", operation=operation, where="시험")
    assert ac.iam_action(call) is None


def test_cli_subcommand_maps_to_an_action():
    call = ac.AwsCall(service="ecr", operation="get-login-password",
                      where="시험", via="cli")
    assert ac.iam_action(call) == "ecr:GetAuthorizationToken"


# ── 스캐너 자체의 정확성 (합성 소스로) ────────────────────────────────

def _scan_snippet(tmp_path: Path, code: str) -> set[tuple[str, str]]:
    (tmp_path / "mod.py").write_text(code, encoding="utf-8")
    return {(c.service, c.operation) for c in ac.scan_source(tmp_path).calls}


def test_scanner_follows_a_client_passed_as_an_argument(tmp_path):
    """이전 판이 `bedrock:Converse` 를 놓친 바로 그 형태."""
    found = _scan_snippet(tmp_path, """
import boto3
class P:
    def _get(self):
        if self._c is None:
            self._c = boto3.client("bedrock-runtime")
        return self._c
    def run(self):
        client = self._get()
        return self._inner(client)
    def _inner(self, client):
        return client.converse(modelId="m")
""")
    assert ("bedrock-runtime", "converse") in found


def test_scanner_ignores_non_aws_objects_named_client(tmp_path):
    """`client.get(...)` 은 AWS 호출이 아니다. 정규식은 이걸 구분 못 했다."""
    found = _scan_snippet(tmp_path, """
import httpx
def fetch():
    client = httpx.Client()
    return client.get("https://example.com")
""")
    assert found == set(), f"AWS 아닌 호출을 잡았다: {found}"


def test_scanner_handles_two_methods_with_the_same_name(tmp_path):
    """같은 이름의 메서드가 여럿이면 **앞쪽 것을 놓치면 안 된다.**

    예전 판은 이름 → 정의 dict 라 마지막 정의만 남았고, 앞 클래스 안의
    AWS 호출이 통째로 검사를 빠져나갔다. 새 호출이 **조용히** 통과하는
    형태라 제일 위험한 종류의 구멍이다.
    """
    found = _scan_snippet(tmp_path, """
import boto3, httpx
class A:
    def run(self):
        client = boto3.client("ecs")
        return self.use(client)
    def use(self, client):
        return client.update_service()
class B:
    def run(self):
        return self.use(httpx.Client())
    def use(self, client):
        return client.post("/x")
""")
    assert ("ecs", "update_service") in found, \
        f"동명 메서드 때문에 A.use 안의 호출을 놓쳤다: {found}"
    assert ("ecs", "post") not in found


def test_scanner_respects_reassignment_order(tmp_path):
    """한 함수에서 같은 이름을 다른 서비스로 다시 대입할 수 있다.

    이름 하나에 값 하나만 두면 **마지막 대입만** 남아, 앞쪽 호출이 엉뚱한
    서비스로 기록된다. 필요한 권한은 목록에서 사라지고(조용히), 있지도 않은
    액션(`s3:UpdateService`)이 대신 생긴다.
    """
    found = _scan_snippet(tmp_path, """
import boto3
def go():
    c = boto3.client("ecs")
    c.update_service()
    c = boto3.client("s3")
    c.put_object()
""")
    assert found == {("ecs", "update_service"), ("s3", "put_object")}, found


def test_scanner_separates_same_attribute_in_different_classes(tmp_path):
    """두 클래스가 둘 다 `self._client` 를 쓰면 서로 오염되면 안 된다.

    흔한 형태다. 모듈 전체 dict 로 관리하면 **마지막 클래스 것만** 남아,
    앞 클래스의 호출이 엉뚱한 서비스로 기록되고 필요한 권한이 목록에서
    사라진다. 조용히 빠지는 형태라 제일 위험하다.
    """
    found = _scan_snippet(tmp_path, """
import boto3
class EcsDeployer:
    def __init__(self):
        self._client = boto3.client("ecs")
    def go(self):
        return self._client.update_service()
class S3Uploader:
    def __init__(self):
        self._client = boto3.client("s3")
    def go(self):
        return self._client.put_object()
""")
    assert ("ecs", "update_service") in found, f"ECS 쪽이 S3 에 덮였다: {found}"
    assert ("s3", "put_object") in found, found


def test_scanner_does_not_leak_names_between_functions(tmp_path):
    """한 함수의 `client` 가 다른 함수의 `client` 를 오염시키면 안 된다."""
    found = _scan_snippet(tmp_path, """
import boto3, httpx
def a():
    client = boto3.client("ecs")
    return client.update_service()
def b():
    client = httpx.Client()
    return client.post("/x")
""")
    assert found == {("ecs", "update_service")}


def test_scanner_finds_aws_cli_subprocess_calls(tmp_path):
    found = _scan_snippet(tmp_path, """
import subprocess
def push(region):
    subprocess.run(["aws", "ecr", "get-login-password", "--region", region])
""")
    assert ("ecr", "get-login-password") in found


def test_scanner_handles_definition_after_use(tmp_path):
    """정의가 사용보다 뒤에 와도 찾아야 한다 (고정점으로 도는 이유)."""
    found = _scan_snippet(tmp_path, """
import boto3
class A:
    def run(self):
        c = self._client()
        return c.describe_clusters()
    def _client(self):
        return boto3.client("ecs")
""")
    assert ("ecs", "describe_clusters") in found


def test_scanner_ignores_dynamic_service_names(tmp_path):
    """서비스 이름이 변수면 정적으로는 알 수 없다. 지어내지 않는다."""
    found = _scan_snippet(tmp_path, """
import boto3
def make(name):
    c = boto3.client(name)
    return c.something()
""")
    assert found == set()


# ── [핵심] 리소스 범위 대조 — 액션만 봐서는 안 잡히는 것 ──────────────
#
# 3차 리뷰(Codex)에서 P1 세 건이 나왔는데 **전부 리소스 범위 문제**였다.
# 액션(`ecs:RegisterTaskDefinition`)은 정책에 있는데, 그 액션이 건드리는
# 리소스(태스크 역할 ARN)가 범위 밖이라 배포가 실패하는 형태다.
#
# 액션 대조 14건 음성 대조를 다 통과하고도 이게 남았다. 검사가 액션만 보고
# 리소스는 안 봤기 때문이다. 아래가 그 구멍을 메운다.

def test_passrole_covers_every_role_the_deploy_code_passes():
    """[구멍 메움] 배포 코드가 넘기는 역할이 전부 PassRole 대상이어야 한다.

    `ecs_agent` 는 실행 역할과 태스크 역할을 **따로** 넘긴다. 하나만 PassRole
    대상에 넣으면 `RegisterTaskDefinition` 이 거부된다 — 그것도 이미지 빌드와
    푸시가 다 끝난 **배포 마지막 단계**에서.

    학교 계정용 `LabRole` 은 학교용 정책이 덮는다. 그래서 두 조합의 합집합으로
    본다.
    """
    covered: set[str] = set()
    for policy in (
        ap.build_policy(["ecs"], "1", "us-east-1"),
        ap.build_policy(["ecs"], "1", "us-east-1", ap.ACADEMY_TASK_EXECUTION_ROLE,
                        task_role=ap.ACADEMY_TASK_EXECUTION_ROLE),
    ):
        for stmt in policy["Statement"]:
            if "iam:PassRole" not in stmt["Action"]:
                continue
            res = stmt["Resource"]
            for arn in ([res] if isinstance(res, str) else res):
                covered.add(arn.rsplit("/", 1)[-1])

    found = ac.iam_roles_in_source(CORE_DIR)
    missing = {name: where for name, where in found.items() if name not in covered}
    assert not missing, (
        "배포 코드가 넘기는데 PassRole 대상에 없는 역할:\n  "
        + "\n  ".join(f"{n}  ({', '.join(w[:3])})" for n, w in sorted(missing.items()))
        + "\n(aws_policy.py 의 PassRole Resource 를 넓히거나, 코드에서 그 역할을 빼세요)"
    )


def test_role_scanner_works(tmp_path):
    """위 대조가 헛돌지 않게 못 박는다.

    `iam_roles_in_source` 가 빈 결과를 돌려주면 "빠진 역할 0" 이 되어
    통과해 버린다. 액션 스캐너 때와 **똑같은 함정**이다.

    실제 저장소 상태가 아니라 **합성 입력**으로 검사한다. 저장소에서 하드코딩이
    사라지는 건 좋은 일인데, 저장소를 기준으로 삼으면 그때 이 가드가 깨진다.
    """
    (tmp_path / "mod.py").write_text('''
ARN = "arn:aws:iam::123456789012:role/SomeHardcodedRole"
def go(agent, region):
    return agent._check_iam_role("AnotherRole", region)
''', encoding="utf-8")
    found = ac.iam_roles_in_source(tmp_path)
    assert set(found) == {"SomeHardcodedRole", "AnotherRole"}, found


def test_policy_and_deploy_path_agree_on_role_names(monkeypatch):
    """[핵심] 권한표가 인가한 역할 = 배포 경로가 실제로 쓸 역할.

    이게 어긋나면 사용자는 시킨 대로 정책을 붙였는데도 배포 전 점검이 없는
    역할을 찾다가 실패하거나, 태스크 정의가 인가 안 된 역할을 달고 등록되다
    거부된다. **정책만 맞고 코드가 안 맞는** 형태라 액션 대조로는 안 잡힌다.
    """
    def authorized(policy):
        names = set()
        for stmt in policy["Statement"]:
            if "iam:PassRole" not in stmt["Action"]:
                continue
            res = stmt["Resource"]
            for arn in ([res] if isinstance(res, str) else res):
                names.add(arn.rsplit("/", 1)[-1])
        return names

    from api.routes import aws as route

    # ① 기본 (환경변수 없음)
    monkeypatch.delenv(ap.ENV_EXECUTION_ROLE_ARN, raising=False)
    monkeypatch.delenv(ap.ENV_TASK_ROLE_ARN, raising=False)
    exec_role, task = route._resolve_roles(False, "", "")
    assert (exec_role, task) == (ap.configured_execution_role(),
                                 ap.configured_task_role())
    assert authorized(ap.build_policy(["ecs"], "1", "us-east-1", exec_role,
                                      task_role=task)) == {exec_role, task}

    # ② 배포 경로가 환경변수로 다른 역할을 쓰도록 설정된 경우
    monkeypatch.setenv(ap.ENV_EXECUTION_ROLE_ARN, "arn:aws:iam::1:role/MyExec")
    monkeypatch.setenv(ap.ENV_TASK_ROLE_ARN, "arn:aws:iam::1:role/MyTask")
    assert ap.configured_execution_role() == "MyExec"
    assert ap.configured_task_role() == "MyTask"
    exec_role, task = route._resolve_roles(False, "", "")
    assert (exec_role, task) == ("MyExec", "MyTask"), \
        "환경변수로 지정한 역할이 권한표에 반영되지 않는다"

    # ③ 학교 계정 자동 판단보다 환경변수가 앞선다 (배포 경로가 그걸 보므로)
    assert route._resolve_roles(True, "", "") == ("MyExec", "MyTask")


def test_bad_role_env_var_fails_loudly(monkeypatch):
    """역할 환경변수가 이상하면 조용히 기본값으로 떨어지면 안 된다."""
    monkeypatch.setenv(ap.ENV_EXECUTION_ROLE_ARN, "arn:aws:iam::1:role/*")
    with pytest.raises(ValueError, match="IAM 역할 이름"):
        ap.configured_execution_role()


def test_academy_uses_labrole_for_both_roles(monkeypatch):
    """학교 계정에서 한쪽만 LabRole 로 바꾸면 반쪽짜리 정책이 나온다."""
    from api.routes import aws as route
    monkeypatch.delenv(ap.ENV_EXECUTION_ROLE_ARN, raising=False)
    monkeypatch.delenv(ap.ENV_TASK_ROLE_ARN, raising=False)
    assert route._resolve_roles(True, "", "") == (
        ap.ACADEMY_TASK_EXECUTION_ROLE, ap.ACADEMY_TASK_EXECUTION_ROLE
    )
    assert route._resolve_roles(False, "", "") == (
        ap.TASK_EXECUTION_ROLE, ap.TASK_ROLE
    )
    # 명시 지정은 자동 판단보다 우선한다.
    assert route._resolve_roles(True, "MyExec", "MyTask") == ("MyExec", "MyTask")


def test_ecr_listing_denial_is_reported_as_normal_not_as_broken_credentials():
    """최소권한 정책에서 계정 전체 ECR 목록이 막히는 건 **정상**이다.

    `/api/aws/ecr/repos` 는 리포지토리 이름을 안 주고 계정 전체를 훑는데,
    권한표는 ECR 조회를 `recoder-*` 로 좁혀 놓는다. 즉 **권한표를 정확히
    따른 사용자일수록** 여기서 막힌다.

    403 으로 끊으면 화면이 "자격증명이 잘못됐다"로 표시해, 멀쩡한 키를
    의심하게 만든다. 목록만 비우고 이유를 알려야 한다.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from api.routes import aws as route

    class _Denied(Exception):
        def __str__(self):
            return "AccessDeniedException: not authorized to perform ecr:DescribeRepositories"

    class _FakeSession:
        def client(self, *a, **k):
            class _C:
                def describe_repositories(self, **kw):
                    raise _Denied()
            return _C()

    app = FastAPI()
    app.include_router(route.router)
    original = route._build_boto3_session
    route._build_boto3_session = lambda **k: _FakeSession()
    try:
        resp = TestClient(app).get("/api/aws/ecr/repos")
    finally:
        route._build_boto3_session = original

    assert resp.status_code == 200, "권한 거부를 오류로 끊으면 안 된다"
    body = resp.json()
    assert body["listing_denied"] is True
    assert body["repositories"] == []
    assert "자격증명 문제가 아닙니다" in body["message"]


def test_execution_role_and_task_role_are_different_and_both_passable():
    """둘은 서로 다른 역할이다. 기본값이 같아지면 한쪽을 잊었다는 뜻이다."""
    assert ap.TASK_EXECUTION_ROLE != ap.TASK_ROLE
    stmt = next(s for s in ap.build_policy(["ecs"], "1", "us-east-1")["Statement"]
                if "iam:PassRole" in s["Action"])
    res = stmt["Resource"]
    arns = [res] if isinstance(res, str) else res
    names = {a.rsplit("/", 1)[-1] for a in arns}
    assert names == {ap.TASK_EXECUTION_ROLE, ap.TASK_ROLE}, \
        f"PassRole 대상이 두 역할을 다 덮지 않는다: {names}"


def test_same_role_for_both_is_not_duplicated():
    """학교 계정은 둘 다 LabRole 이다. ARN 을 두 번 넣지 않는다."""
    stmt = next(s for s in ap.build_policy(
        ["ecs"], "1", "us-east-1", "LabRole", task_role="LabRole")["Statement"]
        if "iam:PassRole" in s["Action"])
    assert stmt["Resource"] == "arn:aws:iam:::1:role/LabRole".replace(":::", "::") \
        or stmt["Resource"].endswith("role/LabRole"), stmt["Resource"]
    assert isinstance(stmt["Resource"], str), "같은 역할인데 목록으로 중복됐다"


@pytest.mark.parametrize("bad", ["*", "role/*", "arn:aws:iam::1:role/X", "a b", "a/b", "?"])
def test_wildcard_or_malformed_role_names_are_rejected(bad):
    """[보안] `task_execution_role=*` 은 계정의 **모든 역할**에 PassRole 을 준다.

    이 엔드포인트는 자격증명 없이도 응답하므로, 여기서 막지 않으면 누구나
    `role/*` 짜리 정책 문구를 뽑아낼 수 있다. 최소권한 보장이 무너진다.
    """
    with pytest.raises(ValueError, match="IAM 역할 이름"):
        ap.build_policy(["ecs"], "1", "us-east-1", bad)
    with pytest.raises(ValueError, match="IAM 역할 이름"):
        ap.build_policy(["ecs"], "1", "us-east-1", task_role=bad)


#: 이 엔드포인트로 넓은 권한을 뽑아내려는 시도들. 하나라도 통과하면
#: "최소권한 정책을 준다"는 이 기능의 전제가 무너진다.
HOSTILE_NAMES = [
    "*", "?", "role/*", "*Admin*", "Admin*", "a*", "**",
    "arn:aws:iam::1:role/Admin", "../Admin", "Admin/../*",
    "OrganizationAccountAccessRole\n*", "a b", "",
]


@pytest.mark.parametrize("hostile", HOSTILE_NAMES)
def test_no_input_can_produce_a_role_wildcard(hostile):
    """[보안 · 속성] **어떤 입력으로도** `role/*` 이 나오면 안 된다.

    거부하든 기본값으로 떨어지든 상관없다. 나오면 안 되는 것만 확실하면 된다.
    이렇게 써야 검증이 헛돌지 않는다 — 고정 입력 몇 개만 확인하면, 검증이
    느슨해져도 그 입력들은 여전히 멀쩡해서 테스트가 통과해 버린다.
    """
    for kwargs in ({"task_execution_role": hostile}, {"task_role": hostile}):
        try:
            policy = ap.build_policy(["ecs"], "123456789012", "us-east-1", **kwargs)
        except ValueError:
            continue  # 거부했으면 통과
        text = json.dumps(policy)
        assert "role/*" not in text and "role/**" not in text, (
            f"{kwargs!r} 로 와일드카드 역할 ARN 이 만들어졌다 — "
            f"이 키 하나로 계정의 모든 역할을 ECS 에 넘길 수 있다"
        )
        for arn in re.findall(r"arn:aws:iam::[^\"]*", text):
            assert "*" not in arn, f"역할 ARN 에 와일드카드: {arn}"


@pytest.mark.parametrize("hostile", ["*", "us-*", "?", "**", "us east 1",
                                     "arn:aws:ecs:us-east-1", "../*", "US-EAST-1"])
def test_no_input_can_make_the_policy_region_global(hostile):
    """[보안] 리전에 와일드카드가 들어가면 정책이 **전 리전**으로 넓어진다.

    역할·클러스터 이름은 검증하면서 리전만 빼놓았었다. 같은 성질의 구멍을
    한 군데 고치고 옆자리를 안 본 것이다 — 이번 라운드에서 반복된 실수라
    적대적 입력으로 못 박는다.
    """
    try:
        policy = ap.build_policy(["ecs", "s3", "bedrock"], "123456789012", hostile)
    except ValueError:
        return  # 거부했으면 통과
    for arn in re.findall(r"arn:aws:[^\"]*", json.dumps(policy)):
        parts = arn.split(":")
        if len(parts) > 3:
            assert parts[3] != "*", f"리전 칸이 전체로 열렸다: {arn}"


@pytest.mark.parametrize("good", ["us-east-1", "ap-northeast-2", "eu-central-1",
                                  "us-gov-west-1", "cn-north-1"])
def test_real_region_names_are_accepted(good):
    """검증이 너무 빡빡하면 멀쩡한 리전이 막힌다. 실제 형태들을 고정한다."""
    assert good in ap.policy_json(["ecs"], "123456789012", good)


@pytest.mark.parametrize("hostile", HOSTILE_NAMES)
def test_no_input_can_widen_the_ecs_resource_scope(hostile):
    """클러스터·서비스 이름으로도 범위를 넓힐 수 없어야 한다."""
    for kwargs in ({"cluster": hostile}, {"service": hostile}):
        try:
            policy = ap.build_policy(["ecs"], "1", "us-east-1", **kwargs)
        except ValueError:
            continue
        for stmt in policy["Statement"]:
            if stmt["Sid"] != "EcsDeployService":
                continue
            for arn in stmt["Resource"]:
                tail = arn.split(":", 5)[-1]
                assert tail not in ("cluster/*", "service/*", "service/*/*"), \
                    f"{kwargs!r} 로 ECS 범위가 전체로 열렸다: {arn}"


# ── 클러스터·서비스 이름이 ARN 에 반영되는가 ─────────────────────────

def test_cluster_and_service_names_flow_into_the_arns():
    """사용자가 이미 있는 클러스터(`default` 등)에 배포할 수 있다.

    이름을 `recoder-*` 로 고정하면 그런 사용자는 정책을 그대로 붙여도
    배포 전 점검에서 `DescribeClusters` 가 거부된다.
    """
    stmt = next(s for s in ap.build_policy(
        ["ecs"], "1", "us-east-1", cluster="default", service="my-api")["Statement"]
        if s["Sid"] == "EcsDeployService")
    assert "arn:aws:ecs:us-east-1:1:cluster/default" in stmt["Resource"]
    assert "arn:aws:ecs:us-east-1:1:service/default/my-api" in stmt["Resource"]


def test_service_arn_nests_the_cluster_name():
    """ECS 서비스 ARN 은 `service/<클러스터>/<서비스>` 형태다."""
    stmt = next(s for s in ap.build_policy(["ecs"], "1", "us-east-1")["Statement"]
                if s["Sid"] == "EcsDeployService")
    svc = [r for r in stmt["Resource"] if ":service/" in r][0]
    assert svc.endswith(f"service/{ap.DEFAULT_CLUSTER}/{ap.DEFAULT_SERVICE}")


@pytest.mark.parametrize("bad", ["*", "a/b", "a*b", "clus ter", "arn:aws:ecs:::cluster/x"])
def test_bad_cluster_or_service_names_are_rejected(bad):
    with pytest.raises(ValueError, match="이름이 올바르지 않습니다"):
        ap.build_policy(["ecs"], "1", "us-east-1", cluster=bad)
    with pytest.raises(ValueError, match="이름이 올바르지 않습니다"):
        ap.build_policy(["ecs"], "1", "us-east-1", service=bad)


def test_prefix_wildcard_is_still_allowed_for_defaults():
    """기본값 `recoder-*` 는 접두사 와일드카드라 허용돼야 한다."""
    ap.build_policy(["ecs"], "1", "us-east-1", cluster="recoder-*", service="recoder-*")


# ── 학교 계정 감지와 안내 분기 ────────────────────────────────────────

@pytest.mark.parametrize("arn,expected", [
    # 실제 러너랩 세션에서 확인한 ARN 모양
    ("arn:aws:sts::413113423592:assumed-role/voclabs/user5057797=a@b.com", True),
    ("arn:aws:iam::413113423592:role/LabRole", True),
    ("arn:aws:iam::111122223333:user/recoder-deploy", False),
    ("arn:aws:iam::111122223333:root", False),
    ("", False),
])
def test_academy_session_is_detected_from_the_caller_arn(arn, expected):
    from api.routes import aws as route
    assert route._looks_like_academy(arn) is expected


def test_academy_steps_never_tell_you_to_create_an_iam_user():
    """학교 계정에서 IAM 사용자를 만들라고 하면 반드시 막힌다.

    `iam:CreateUser` 가 허용되지 않는 것을 실제 계정에서 확인했다. 안내가
    막히는 길을 가리키면, 사용자는 자기가 뭘 잘못한 줄 알고 시간을 버린다.
    """
    from api.routes import aws as route
    text = " ".join(route._policy_steps(True, academy=True))
    assert "사용자 생성" not in text, "학교 계정에 IAM 사용자 생성을 안내하고 있다"
    assert "액세스 키 발급" not in text, "학교 계정에서는 키를 발급할 수 없다"
    assert ap.ACADEMY_TASK_EXECUTION_ROLE in text, "LabRole 안내가 빠졌다"


def test_academy_steps_do_not_send_the_user_to_a_screen_that_cannot_accept_them():
    """'AWS 연결' 화면에는 **세션 토큰 입력칸이 없다.**

    학교 계정 자격증명은 세션 토큰이 필수라, 그 화면으로 가라고 안내하면
    사용자는 넣을 칸을 못 찾고 헤맨다. 화면에 칸이 생기기 전까지는 파일
    경로를 안내해야 한다. (입력칸이 생기면 이 테스트를 함께 고칠 것)
    """
    from api.routes import aws as route
    text = " ".join(route._policy_steps(True, academy=True))
    assert "~/.aws/credentials" in text, "자격증명 파일 경로 안내가 빠졌다"
    assert "us-east-1" in text, "학교 계정 리전 안내가 빠졌다"
    assert "화면에 붙여넣" not in text, \
        "세션 토큰을 넣을 수 없는 화면으로 안내하고 있다"


def test_normal_steps_still_walk_through_key_creation():
    """반대로 일반 계정 안내에서는 그 단계가 빠지면 안 된다."""
    from api.routes import aws as route
    text = " ".join(route._policy_steps(True, academy=False))
    assert "사용자 생성" in text and "액세스 키 발급" in text


# ── 리전을 확실할 때만 채우는가 ───────────────────────────────────────

def _policy_client(monkeypatch, session_region):
    """리전만 다르게 준 가짜 세션으로 엔드포인트를 부른다."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from api.routes import aws as route

    class _S:
        region_name = session_region

    monkeypatch.setattr(route, "_build_boto3_session", lambda **k: _S())
    app = FastAPI()
    app.include_router(route.router)
    return TestClient(app)


def test_region_stays_a_placeholder_when_it_is_not_known(monkeypatch):
    """[핵심] 모르면 **자리표시자를 남긴다.** 기본값을 몰래 채우지 않는다.

    자격증명이 `~/.aws/credentials` 에 있고 리전은 `~/.aws/config` 에만 있는
    구성이 흔하다. 예전에는 그 경우 하드코딩 기본값(`ap-northeast-2`)이 ARN 에
    박히면서 `needs_manual_fill=False` 로 나갔다. 사용자는 다 채워진 줄 알고
    붙여넣고, 실제 배포는 다른 리전이라 거부된다. **정책이 멀쩡해 보여서**
    원인을 찾기가 특히 어렵다.

    틀린 값을 조용히 채우느니 "여기 채우세요"가 낫다.
    """
    client = _policy_client(monkeypatch, None)
    body = client.get("/api/aws/policy").json()
    assert body["region"] == "", f"모르는 리전을 채웠다: {body['region']!r}"
    assert body["needs_manual_fill"] is True
    assert ap.REGION_PLACEHOLDER in body["policy_json"]
    assert "ap-northeast-2" not in body["policy_json"], \
        "하드코딩 기본 리전이 정책에 새어 들어갔다"


def test_region_comes_from_the_session_when_known(monkeypatch):
    """프로필 config 에 리전이 있으면 그걸 쓴다."""
    client = _policy_client(monkeypatch, "us-west-2")
    body = client.get("/api/aws/policy").json()
    assert body["region"] == "us-west-2"
    assert "us-west-2" in body["policy_json"]


def test_explicit_region_wins_over_the_session(monkeypatch):
    client = _policy_client(monkeypatch, "us-west-2")
    body = client.get("/api/aws/policy", params={"region": "eu-central-1"}).json()
    assert body["region"] == "eu-central-1"


# ── 권한 부족이 보안 게이트를 무력화하지 않는가 ───────────────────────

def test_trivy_failure_is_visible_in_the_findings():
    """[보안] 스캔이 실패하면 **결과에 흔적이 남아야** 한다.

    실패를 조용히 삼키면 findings 가 빈 채로 돌아가고, 호출부는 그걸
    "취약점 0건 = 통과"로 읽는다. 즉 **이미지를 한 번도 들여다보지 않은
    배포가 보안 게이트를 통과**한다.

    이건 가상의 걱정이 아니다. 권한표에 ECR **pull** 권한이 없으면 trivy 가
    원격 이미지를 못 받아 정확히 이 경로로 떨어진다. 권한 하나가 보안 게이트
    무력화로 이어지는 셈이라, 권한표 카드에서 같이 막는다.
    """
    import asyncio
    import security_scan as ss

    scanner = ss.SecurityScanner() if hasattr(ss, "SecurityScanner") else None
    if scanner is None:  # pragma: no cover - 클래스 이름이 바뀌면 알려준다
        pytest.fail("SecurityScanner 를 찾을 수 없다 — 테스트를 갱신하세요")

    async def _boom(*a, **k):
        raise RuntimeError("AccessDeniedException: ecr:BatchGetImage")

    scanner._run_cmd = _boom
    findings = asyncio.run(scanner._run_trivy("123.dkr.ecr.us-east-1.amazonaws.com/recoder-app:v1"))

    assert findings, "스캔이 실패했는데 결과가 비어 있다 — '취약점 없음'으로 오해된다"
    titles = {f.title for f in findings}
    assert "trivy_scan_failed" in titles, titles
    detail = " ".join(f.description for f in findings)
    assert "ecr:BatchGetImage" in detail, "원인을 짚어주지 않으면 사용자가 못 고친다"


def test_ecr_pull_actions_are_granted_for_image_scanning():
    """trivy/syft 가 원격 이미지를 끌어오려면 pull 권한이 따로 필요하다.

    push 권한(`PutImage` 등)과 **다른 액션**이다. 코드 grep 으로는 안 보인다 —
    외부 실행 파일이 하는 일이라 docker push 와 같은 부류다.
    """
    granted = set(ap.used_actions(ap.build_policy(["ecs"])))
    for action in ("ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"):
        assert action in granted, f"이미지 스캔에 필요한 {action} 이 빠졌다"


# ── 실행 중 기록기 (권한 0 으로 동작해야 한다) ────────────────────────

def test_recorder_captures_calls_without_valid_credentials():
    """**학교 계정에서 쓸 수 있어야 한다** — 권한도 자격증명도 없이 기록된다.

    아무도 안 듣는 포트로 보내 실패시킨 뒤에도 기록이 남는지 본다.
    `before-call` 은 요청이 나가기 직전에 발생하므로 결과와 무관하다.
    """
    boto3 = pytest.importorskip("boto3")
    from botocore.config import Config

    rec = ac.Recorder()
    rec.install()
    try:
        client = boto3.client(
            "ecs",
            region_name="us-east-1",
            aws_access_key_id="AKIAFAKEFAKEFAKEFAKE",
            aws_secret_access_key="fake",
            endpoint_url="http://127.0.0.1:1",
            config=Config(retries={"max_attempts": 1}, connect_timeout=1,
                          read_timeout=1),
        )
        with pytest.raises(Exception):
            client.describe_clusters(clusters=["없는클러스터"])
    finally:
        rec.uninstall()

    assert "ecs:DescribeClusters" in rec.actions(), \
        f"기록되지 않았다: {[str(c) for c in rec.calls()]}"


def test_recorder_dump_is_readable(tmp_path):
    rec = ac.Recorder()
    rec._seen[("ecs", "UpdateService")] = 2
    out = tmp_path / "calls.json"
    rec.dump(out)
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["calls"] == [
        {"service": "ecs", "operation": "UpdateService", "count": 2}
    ]


def test_recorded_calls_map_to_the_same_actions_as_static_scan():
    """녹음기와 정적 분석이 **같은 액션 이름**으로 수렴해야 대조가 성립한다."""
    rec = ac.Recorder()
    rec._seen[("ecs", "RegisterTaskDefinition")] = 1
    rec._seen[("bedrock-runtime", "Converse")] = 1
    assert rec.actions() == {"ecs:RegisterTaskDefinition", "bedrock:InvokeModel"}
