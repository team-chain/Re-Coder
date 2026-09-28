"""Read-only deployment canvas projection. Existing deployment routes own mutations."""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone

from fastapi import APIRouter

from core import s3_byo

router = APIRouter(prefix="/api/deploy/canvas", tags=["deployment-canvas"])


def same_workspace(record, workspace: str) -> bool:
    source = record.request.workspace_path if record.request else None
    return bool(source and workspace and os.path.normcase(os.path.realpath(source)) == os.path.normcase(os.path.realpath(workspace)))


def scan_projection(record) -> dict:
    """Only allowlisted metadata crosses the webview boundary; no secret values."""
    result = record.scan_result
    steps = {s.key: s.status for s in record.progress_steps}
    gaps = list(record.scan_gaps) + (list(result.tool_errors) if result else [])
    findings = []
    if result:
        for f in result.findings:
            findings.append({
                "tool": f.tool.value, "severity": f.severity.value,
                "rule": f.rule_id or "", "location": f.location or "",
                "title": "시크릿 탐지 (값 숨김)" if f.tool.value == "gitleaks" else f.title,
                "fix": "비밀값을 제거하고 키를 교체하세요." if f.tool.value == "gitleaks" else (f.fix_suggestion or ""),
            })
    tools = []
    for tool, step, present in (
        ("hadolint", "source_scan", bool(result and result.dockerfile_path)),
        ("gitleaks", "source_scan", bool(result and result.repo_path)),
        ("trivy", "image_scan", bool(result and result.image)),
    ):
        status = steps.get(step, "pending")
        tool_gaps = [g for g in gaps if tool in g.lower()]
        if tool_gaps:
            state = "unverified"
        elif status == "running":
            state = "running"
        elif present:
            state = "findings" if any(f["tool"] == tool for f in findings) else "passed"
        else:
            state = "not_run" if status in ("done", "skipped", "failed") else "pending"
        tools.append({"tool": tool, "state": state, "detail": "; ".join(tool_gaps)})
    # A persisted rejected request is not proof that OPA accepted it.
    policy_denied = (record.error_message or "").startswith("정책 통과 실패")
    policy_unknown = record.provisioned.get("policy_warning") or (record.error_message or "").startswith("정책 평가")
    policy_started = any(state in ("running", "done") for state in steps.values()) or bool(result)
    policy_state = "findings" if policy_denied else "unverified" if policy_unknown else "accepted" if policy_started else "pending"
    tools.append({"tool": "OPA", "state": policy_state,
                  "detail": record.provisioned.get("policy_warning", "서버 정책 게이트 차단" if policy_denied else "배포 요청 승인 여부")})
    return {"blocked": policy_denied or bool(result and result.blocked), "tools": tools, "findings": findings, "gaps": gaps}


def read_topology(session, record) -> dict:
    """AWS names/counts/task IDs are observed, never reconstructed from desired count."""
    from botocore.config import Config
    client = session.client("ecs", region_name=record.region, config=Config(connect_timeout=3, read_timeout=5, retries={"max_attempts": 0}))
    response = client.describe_services(cluster=record.cluster, services=[record.service])
    services = response.get("services", [])
    if not services:
        raise ValueError("ECS 서비스를 찾지 못했습니다. 삭제 여부와 조회 권한을 확인하세요.")
    service = services[0]
    arns = client.list_tasks(cluster=record.cluster, serviceName=record.service, maxResults=100).get("taskArns", [])
    tasks = client.describe_tasks(cluster=record.cluster, tasks=arns).get("tasks", []) if arns else []
    exposure = []
    exposure_warning = ""
    if service.get("loadBalancers"):
        try:
            elb = session.client("elbv2", region_name=record.region, config=Config(connect_timeout=3, read_timeout=5, retries={"max_attempts": 0}))
            targets = [b["targetGroupArn"] for b in service["loadBalancers"] if b.get("targetGroupArn")]
            groups = elb.describe_target_groups(TargetGroupArns=targets).get("TargetGroups", []) if targets else []
            arns = list({arn for g in groups for arn in g.get("LoadBalancerArns", [])})
            balancers = elb.describe_load_balancers(LoadBalancerArns=arns).get("LoadBalancers", []) if arns else []
            exposure = [{"name": lb.get("LoadBalancerName", ""), "dns": lb.get("DNSName", ""), "scheme": lb.get("Scheme", ""), "type": lb.get("Type", "")} for lb in balancers]
        except Exception:
            exposure_warning = "로드 밸런서 연결은 있지만 공개 범위를 조회하지 못했습니다."
    return {
        "cluster": record.cluster, "service": service.get("serviceName", record.service), "region": record.region,
        "desired": service.get("desiredCount"), "running": service.get("runningCount"),
        "task_definition": service.get("taskDefinition", ""),
        "tasks": [{"id": t.get("taskArn", "").rsplit("/", 1)[-1], "status": t.get("lastStatus", "UNKNOWN"),
                   "health": t.get("healthStatus", "UNKNOWN"), "launch_type": t.get("launchType", ""),
                   "images": [{"image": c.get("image", ""), "digest": c.get("imageDigest", "")} for c in t.get("containers", [])]} for t in tasks],
        "observed_at": datetime.now(timezone.utc).isoformat(), "truncated": len(tasks) == 100,
        "exposure": exposure, "exposure_warning": exposure_warning,
    }


def read_target(session, region: str, cluster: str, service: str) -> dict:
    """Approval must refer to the chosen service's CURRENT revision, not an older rollback."""
    from botocore.config import Config
    client = session.client("ecs", region_name=region, config=Config(connect_timeout=3, read_timeout=5, retries={"max_attempts": 0}))
    response = client.describe_services(cluster=cluster, services=[service])
    services = response.get("services", [])
    if not services:
        if any(f.get("reason") not in ("MISSING",) for f in response.get("failures", [])):
            raise ValueError("서비스 조회 실패")
        return {"exists": False, "task_definition": "", "images": []}
    current = services[0]
    definition = current.get("taskDefinition", "")
    images = []
    # Task runtime imageDigest is immutable even when containerDefinitions use a mutable tag.
    # Read runtime tasks first: existing deployment roles already allow this, and
    # a running service should not need a new permission merely to show its image.
    arns = client.list_tasks(cluster=cluster, serviceName=service, maxResults=100).get("taskArns", [])
    if arns:
        tasks = client.describe_tasks(cluster=cluster, tasks=arns).get("tasks", [])
        actual = [{"image": c.get("image", ""), "digest": c.get("imageDigest", "")} for t in tasks if t.get("taskDefinitionArn") == definition for c in t.get("containers", [])]
        if actual:
            images = list({(x["image"], x["digest"]): x for x in actual}.values())
    if not images and definition:
        task = client.describe_task_definition(taskDefinition=definition).get("taskDefinition", {})
        images = [{"image": c.get("image", ""), "digest": ""} for c in task.get("containerDefinitions", [])]
    return {"exists": True, "task_definition": definition, "images": images}


@router.get("/target")
async def canvas_target(region: str, cluster: str, service: str, include_budget: bool = True) -> dict:
    from botocore.config import Config
    from api.routes import aws
    status = await aws.get_aws_status()
    result = {"exists": None, "task_definition": "", "images": [], "budget": None, "warnings": []}
    if not status.ready:
        result["warnings"].append("AWS 미연결")
        return result
    session = aws._build_boto3_session(region=region)
    try:
        result.update(await asyncio.to_thread(read_target, session, region, cluster, service))
    except Exception:
        result["warnings"].append("현재 서비스와 롤백 이미지 조회 실패 — 이름·권한을 확인하세요.")
    if include_budget and status.identity:
        try:
            def budget():
                client = session.client("budgets", config=Config(connect_timeout=3, read_timeout=5, retries={"max_attempts": 0}))
                response = client.describe_budgets(AccountId=status.identity.account, MaxResults=100)
                budgets = [b for b in response.get("Budgets", []) if b.get("BudgetType") == "COST"]
                return [{"name": b.get("BudgetName", ""), "unit": b.get("BudgetLimit", {}).get("Unit", ""),
                         "limit": b.get("BudgetLimit", {}).get("Amount"), "spent": b.get("CalculatedSpend", {}).get("ActualSpend", {}).get("Amount"),
                         "period": b.get("TimeUnit", "")} for b in budgets]
            result["budget"] = await asyncio.to_thread(budget)
        except Exception:
            result["warnings"].append("AWS 예산 조회 권한이 없거나 예산이 설정되지 않았습니다.")
    return result


@router.get("")
async def canvas_snapshot(workspace_path: str = "", project: str = "") -> dict:
    from api.routes import aws, ecs
    from api.routes.deploy_ecs import to_status_response
    from deployment_inputs import dockerfile_runtime_port
    from pathlib import Path
    status = await aws.get_aws_status()
    records = sorted((r for r in ecs._deploy_records.values() if same_workspace(r, workspace_path)), key=lambda r: r.started_at, reverse=True)
    record = records[0] if records else None
    snapshot = {
        "aws": {"ready": status.ready, "region": status.region if status.ready else "",
                "account": status.identity.account if status.ready and status.identity else ""},
        "deployment": to_status_response(record).model_dump(mode="json"),
        "resource": None, "topology": None, "scan": None, "s3": None, "warnings": [],
        "container_port": dockerfile_runtime_port(Path(workspace_path) / "Dockerfile") if workspace_path else None,
    }
    if record:
        snapshot["resource"] = {"cluster": record.cluster, "service": record.service, "region": record.region,
            "image": record.image_uri or record.image or "", "image_digest": record.image_digest or "",
            "previous_task_definition": record.previous_task_definition_arn or ""}
        snapshot["scan"] = scan_projection(record)
    if status.ready:
        session = aws._build_boto3_session(region=status.region)
        if record and record.cluster and record.service:
            try:
                snapshot["topology"] = await asyncio.to_thread(read_topology, session, record)
            except Exception:
                snapshot["warnings"].append("ECS 실시간 조회 실패 — 권한·네트워크를 확인하세요. 저장된 배포 기록은 유지됩니다.")
        if project and status.identity:
            bucket = s3_byo.bucket_name(project, status.identity.account)
            try:
                def inspect_bucket():
                    from botocore.config import Config
                    client = session.client("s3", config=Config(connect_timeout=3, read_timeout=5, retries={"max_attempts": 0}))
                    client.head_bucket(Bucket=bucket)
                    region = client.get_bucket_location(Bucket=bucket).get("LocationConstraint") or "us-east-1"
                    return {"bucket": bucket, "region": region}
                snapshot["s3"] = await asyncio.to_thread(inspect_bucket)
            except Exception:
                snapshot["warnings"].append("이 프로젝트의 S3 버킷이 아직 없거나 조회 권한이 없습니다.")
    return snapshot
