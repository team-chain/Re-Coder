#!/usr/bin/env python3
"""
watchdog/tools/verify_against_aws.py — **진짜 AWS 에 붙여서** 감시를 검증한다.

왜 필요한가
    moto 와 스텁은 "내가 상상한 AWS" 를 검사한다. 상상이 틀리면 테스트는
    통과하고 실제 배포에서만 조용히 안 돈다. 이 카드의 DoD 는
    「일부러 죽인 앱에서 '이상' 이벤트가 발생함」 이라, 진짜로 죽는 앱을
    띄워 보기 전에는 충족했다고 말할 수 없다.

무엇을 하는가
    1. 정상 서비스(sleep)를 띄우고 → **알림이 안 나가는지** 확인 (음성 대조)
    2. 크래시 서비스(즉시 exit 1)를 띄우고 → **알림이 나가는지** 확인
    3. 만든 것을 전부 지운다

음성 대조가 없으면 2번은 아무것도 증명하지 못한다. 항상 알림을 내는
감시기도 2번을 통과하기 때문이다.

쓰는 법
    export AWS_ACCESS_KEY_ID=... AWS_SECRET_ACCESS_KEY=... AWS_SESSION_TOKEN=...
    python3 watchdog/tools/verify_against_aws.py --region us-east-1

    --keep     검사 후 자원을 지우지 않는다(디버깅용)
    --skip-ok  음성 대조(정상 서비스)를 건너뛴다 — 권장하지 않는다

주의
    실제 자원을 만든다(ECS 클러스터·서비스·태스크). Fargate 최소 사양으로
    몇 분만 돌리고 지우지만, 중간에 죽으면 --cleanup-only 로 정리한다.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cloudwatch_monitor import CloudWatchUnavailableError, EcsTarget, collect_service_health  # noqa: E402
from cloudwatch_thresholds import MetricWindow, Thresholds, judge, next_unhealthy_streak  # noqa: E402

CLUSTER = "recoder-watchdog-verify"
FAMILY_OK = "recoder-verify-healthy"
FAMILY_BAD = "recoder-verify-crash"
SERVICE_OK = "recoder-verify-healthy-svc"
SERVICE_BAD = "recoder-verify-crash-svc"
#: public ECR 미러 — Docker Hub 는 인증 없이 당기면 rate limit 에 걸린다.
IMAGE = "public.ecr.aws/docker/library/busybox:latest"


def log(msg: str) -> None:
    print(f"[verify] {msg}", flush=True)


# ---------------------------------------------------------------------------
# AWS 준비
# ---------------------------------------------------------------------------


def whoami(session: Any, region: str) -> tuple[str, str]:
    ident = session.client("sts", region_name=region).get_caller_identity()
    return ident["Account"], ident["Arn"]


def pick_network(session: Any, region: str) -> tuple[list, str]:
    """기본 VPC 의 서브넷과 기본 보안 그룹.

    Learner Lab 은 기본 VPC 가 있다. 없으면 여기서 멈추는 게 맞다 —
    VPC 까지 만들기 시작하면 검사가 아니라 배포 도구가 된다.
    """
    ec2 = session.client("ec2", region_name=region)
    vpcs = ec2.describe_vpcs(Filters=[{"Name": "isDefault", "Values": ["true"]}])["Vpcs"]
    if not vpcs:
        raise SystemExit("기본 VPC 가 없습니다. --vpc 로 지정하거나 수동으로 만드세요.")
    vpc_id = vpcs[0]["VpcId"]

    subnets = ec2.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])["Subnets"]
    #: 퍼블릭 IP 를 줄 수 있는 서브넷이어야 public ECR 에서 이미지를 당긴다.
    usable = [s["SubnetId"] for s in subnets if s.get("MapPublicIpOnLaunch")] or [
        s["SubnetId"] for s in subnets
    ]
    if not usable:
        raise SystemExit(f"{vpc_id} 에 서브넷이 없습니다.")

    groups = ec2.describe_security_groups(
        Filters=[{"Name": "vpc-id", "Values": [vpc_id]},
                 {"Name": "group-name", "Values": ["default"]}],
    )["SecurityGroups"]
    if not groups:
        raise SystemExit(f"{vpc_id} 에 default 보안 그룹이 없습니다.")

    log(f"네트워크: vpc={vpc_id} subnets={usable[:2]} sg={groups[0]['GroupId']}")
    return usable[:2], groups[0]["GroupId"]


def execution_role(session: Any, account: str) -> Optional[str]:
    """Fargate 가 이미지를 당길 때 쓰는 실행 역할.

    Learner Lab 은 LabRole 하나로 통일돼 있고 IAM 생성 권한이 없다.
    못 찾으면 None 을 돌려주고, 역할 없이 시도한다.
    """
    iam = session.client("iam")
    for name in ("LabRole", "ecsTaskExecutionRole"):
        try:
            return iam.get_role(RoleName=name)["Role"]["Arn"]
        except Exception:  # noqa: BLE001 — 없으면 다음 후보
            continue
    log("실행 역할을 못 찾았습니다 — 역할 없이 시도합니다(이미지 pull 이 실패할 수 있음).")
    return None


def register_task(ecs: Any, family: str, command: list, role: Optional[str]) -> str:
    kwargs: dict = dict(
        family=family,
        requiresCompatibilities=["FARGATE"],
        networkMode="awsvpc",
        cpu="256",
        memory="512",
        containerDefinitions=[{
            "name": "app",
            "image": IMAGE,
            "command": command,
            "essential": True,
        }],
    )
    if role:
        kwargs["executionRoleArn"] = role
    arn = ecs.register_task_definition(**kwargs)["taskDefinition"]["taskDefinitionArn"]
    log(f"태스크 정의 등록: {family}")
    return arn


def create_service(ecs: Any, name: str, task_arn: str, subnets: list, sg: str) -> None:
    ecs.create_service(
        cluster=CLUSTER,
        serviceName=name,
        taskDefinition=task_arn,
        desiredCount=1,
        launchType="FARGATE",
        networkConfiguration={"awsvpcConfiguration": {
            "subnets": subnets, "securityGroups": [sg], "assignPublicIp": "ENABLED",
        }},
    )
    log(f"서비스 생성: {name}")


# ---------------------------------------------------------------------------
# 관측 — **실제 감시 코드를 그대로 부른다**
# ---------------------------------------------------------------------------


def observe(target: EcsTarget, *, minutes: float, interval: float, label: str) -> dict:
    """watchdog 의 판정 경로를 그대로 태우고, 나온 알림과 **시간**을 모은다.

    시간을 같이 재는 이유
        임계치가 `unhealthy_polls` — 즉 **횟수**로 되어 있다. 같은 3회라도
        주기가 60초면 3분, 15초면 45초다. 정상 Fargate 배포가 태스크를
        띄우는 데 걸리는 시간보다 짧으면, **모든 정상 배포마다 경보가 난다.**
        그 시간은 추측이 아니라 실측해야 알 수 있어서 여기서 잰다.
    """
    thresholds = Thresholds()
    streak = 0
    seen: dict = {}
    started = time.monotonic()
    ready_at: Optional[float] = None
    alerts_before_ready: set = set()
    deadline = started + minutes * 60

    while time.monotonic() < deadline:
        try:
            health = collect_service_health(target, window_seconds=300)
        except CloudWatchUnavailableError as exc:
            log(f"  [{label}] 수집 실패: {exc}")
            time.sleep(interval)
            continue

        elapsed = time.monotonic() - started
        if ready_at is None and health.desired > 0 and health.running >= health.desired:
            ready_at = elapsed
            log(f"  [{label}] ★ running==desired 도달: {ready_at:.0f}초")

        streak = next_unhealthy_streak(health, streak)
        anomalies = judge(health, MetricWindow(window_seconds=300), thresholds, streak)
        for a in anomalies:
            seen.setdefault(a.alert_type, a.message)
            if ready_at is None:
                #: 아직 한 번도 정상에 도달하지 못했는데 나온 알림.
                #: 정상 배포 중이라면 이건 **오탐**이다.
                alerts_before_ready.add(a.alert_type)

        log(f"  [{label}] {elapsed:5.0f}s running={health.running}/{health.desired} "
            f"pending={health.pending} stopped={health.stopped_recently} "
            f"streak={streak} → {sorted(seen) or '이상 없음'}")
        time.sleep(interval)

    return {
        "alerts": seen,
        "ready_seconds": ready_at,
        "alerts_before_ready": alerts_before_ready,
        "interval": interval,
        "polls_to_alert_seconds": Thresholds().unhealthy_polls * interval,
    }


# ---------------------------------------------------------------------------
# 정리
# ---------------------------------------------------------------------------


def cleanup(session: Any, region: str) -> None:
    ecs = session.client("ecs", region_name=region)
    for name in (SERVICE_OK, SERVICE_BAD):
        try:
            ecs.update_service(cluster=CLUSTER, service=name, desiredCount=0)
            ecs.delete_service(cluster=CLUSTER, service=name, force=True)
            log(f"서비스 삭제: {name}")
        except Exception as exc:  # noqa: BLE001
            log(f"서비스 삭제 생략({name}): {str(exc)[:80]}")
    for family in (FAMILY_OK, FAMILY_BAD):
        try:
            for arn in ecs.list_task_definitions(familyPrefix=family).get(
                    "taskDefinitionArns", []):
                ecs.deregister_task_definition(taskDefinition=arn)
            log(f"태스크 정의 해제: {family}")
        except Exception as exc:  # noqa: BLE001
            log(f"태스크 정의 해제 생략({family}): {str(exc)[:80]}")
    try:
        ecs.delete_cluster(cluster=CLUSTER)
        log(f"클러스터 삭제: {CLUSTER}")
    except Exception as exc:  # noqa: BLE001
        log(f"클러스터 삭제 생략: {str(exc)[:80]}")


# ---------------------------------------------------------------------------


def main() -> int:
    p = argparse.ArgumentParser(description="Watchdog ECS 감시를 실제 AWS 로 검증")
    p.add_argument("--region", default="us-east-1")
    p.add_argument("--minutes", type=float, default=4.0, help="크래시 서비스 관측 시간(분)")
    p.add_argument("--ok-minutes", type=float, default=3.0, help="정상 서비스 관측 시간(분)")
    p.add_argument("--interval", type=float, default=20.0, help="폴링 주기(초)")
    p.add_argument("--keep", action="store_true", help="검사 후 자원을 남긴다")
    p.add_argument("--skip-ok", action="store_true", help="음성 대조를 건너뛴다")
    p.add_argument("--cleanup-only", action="store_true", help="정리만 하고 끝낸다")
    args = p.parse_args()

    import boto3

    session = boto3.session.Session(region_name=args.region)

    if args.cleanup_only:
        cleanup(session, args.region)
        return 0

    account, arn = whoami(session, args.region)
    log(f"계정 {account} / {arn}")
    log(f"리전 {args.region}")

    ecs = session.client("ecs", region_name=args.region)
    subnets, sg = pick_network(session, args.region)
    role = execution_role(session, account)

    ecs.create_cluster(clusterName=CLUSTER)
    log(f"클러스터 준비: {CLUSTER}")

    results: dict = {}
    try:
        # ── 음성 대조: 멀쩡한 서비스 ─────────────────────────────────
        if not args.skip_ok:
            ok_arn = register_task(ecs, FAMILY_OK, ["sh", "-c", "sleep 3600"], role)
            create_service(ecs, SERVICE_OK, ok_arn, subnets, sg)
            log("정상 서비스가 뜰 때까지 관측한다 — 여기서 알림이 나오면 오탐이다.")
            results["healthy"] = observe(
                EcsTarget(cluster=CLUSTER, service=SERVICE_OK, region=args.region),
                minutes=args.ok_minutes, interval=args.interval, label="정상",
            )

        # ── 본 검사: 즉시 죽는 서비스 ────────────────────────────────
        bad_arn = register_task(
            ecs, FAMILY_BAD,
            ["sh", "-c", "echo 부팅; sleep 5; echo 크래시; exit 1"], role,
        )
        create_service(ecs, SERVICE_BAD, bad_arn, subnets, sg)
        log("일부러 죽는 서비스를 관측한다 — 여기서 알림이 없으면 감시가 안 되는 것이다.")
        results["crash"] = observe(
            EcsTarget(cluster=CLUSTER, service=SERVICE_BAD, region=args.region),
            minutes=args.minutes, interval=args.interval, label="크래시",
        )
    finally:
        if args.keep:
            log("--keep: 자원을 남깁니다. 끝나면 --cleanup-only 로 지우세요.")
        else:
            cleanup(session, args.region)

    # ── 판정 ────────────────────────────────────────────────────────
    print("\n" + "=" * 66)
    healthy = results.get("healthy")
    crash = results.get("crash") or {}
    crash_alerts = crash.get("alerts", {})

    ok_control = True
    if healthy is not None:
        false_alarms = healthy["alerts_before_ready"]
        ok_control = not false_alarms
        ready = healthy["ready_seconds"]
        print("음성 대조(정상 서비스)")
        print(f"  running==desired 도달   : "
              f"{f'{ready:.0f}초' if ready is not None else '관측 시간 안에 도달 못함'}")
        print(f"  현재 임계치가 경보까지  : {healthy['polls_to_alert_seconds']:.0f}초 "
              f"(unhealthy_polls={Thresholds().unhealthy_polls} × 주기 {healthy['interval']:.0f}초)")
        if false_alarms:
            print(f"  → **오탐** {sorted(false_alarms)} — 정상 배포 중에 경보가 났다.")
            print("     임계치가 배포 소요 시간보다 짧다는 뜻이다.")
        else:
            print("  → 통과 — 정상 배포 중 경보 없음")
    else:
        print("음성 대조(정상 서비스) : 건너뜀 (--skip-ok) — 오탐을 배제하지 못한다")

    expected = {"ecs_tasks_unhealthy", "ecs_task_restart_loop"}
    hit = expected & set(crash_alerts)
    print("\n크래시 서비스 (이 카드의 DoD)")
    print(f"  → {'통과 — ' + str(sorted(hit)) if hit else '실패 — 알림이 하나도 안 나왔다'}")
    for name, message in crash_alerts.items():
        print(f"  · {name}: {message}")
    print("=" * 66)

    return 0 if (hit and ok_control) else 1


if __name__ == "__main__":
    raise SystemExit(main())
