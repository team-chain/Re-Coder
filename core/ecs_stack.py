"""여러 서비스로 된 프로젝트(docker-compose)를 AWS ECS Fargate 에 올린다(4.0.1).

예전에는 AWS 배포가 컨테이너 하나만 올려서, 서비스·DB 가 여럿인 프로젝트는 배포 전에 막았다(실사용 2026-11-21: 「의미가 없다」).

방식
    · compose 의 서비스마다 ECS 서비스 하나(Fargate, 태스크 1개). 앱 서비스는 서비스 폴더를 빌드해 ECR 에 올리고,
      DB·Redis 처럼 이미지를 그대로 쓰는 서비스는 그 이미지를 그대로 쓴다.
    · 서비스끼리는 **ECS Service Connect** 로 compose 와 같은 이름(`http://users:3001`, `users-db:5432`)으로 찾는다 —
      코드와 nginx 설정(`proxy_pass http://gateway:8080`)을 바꾸지 않는다.
    · 밖에서 들어오는 입구는 하나(화면 서비스, 없으면 포트를 연 첫 서비스). 나머지는 같은 보안 그룹 안에서만 닿는다.
    · 의존 순서(depends_on)대로 층을 나눠, 앞 층이 안정된 뒤 다음 층을 띄운다(ECS 에는 서비스 간 depends_on 이 없다).
    · 비밀값은 로컬 배포와 같은 .env(또는 ReCoder 가 만든 사용자 폴더의 값)를 쓴다.

알리는 한계(숨기지 않는다)
    · DB·Redis 는 컨테이너로 돈다 — 태스크를 다시 띄우면 데이터가 사라진다. 운영 데이터는 RDS·ElastiCache 로 옮겨야 한다.
    · 이미지를 받으려고 모든 태스크에 공인 IP 가 붙는다(밖에서 들어오는 길은 입구 서비스만 열린다).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None  # type: ignore

#: Fargate 최소 단위(0.25 vCPU · 0.5GB) — us-east-1 기준 시간당 요금(2026-11 공시가). 화면에는 「약」 으로만 쓴다.
_FARGATE_HOURLY = 0.25 * 0.04048 + 0.5 * 0.004445
_PUBLIC_IPV4_HOURLY = 0.005
_HOURS = 730
_DB_PORTS = (("postgres", 5432), ("mysql", 3306), ("mariadb", 3306), ("redis", 6379), ("mongo", 27017))


@dataclass
class StackService:
    name: str
    image: str = ""            # 그대로 쓰는 이미지(DB 등). 비면 context 를 빌드한다.
    context: str = ""          # 빌드 폴더(프로젝트 기준 상대 경로)
    dockerfile: str = "Dockerfile"
    port: Optional[int] = None
    environment: dict[str, str] = field(default_factory=dict)
    command: Optional[list[str]] = None
    health: Optional[dict] = None
    depends_on: list[str] = field(default_factory=list)
    public: bool = False
    data: bool = False         # 볼륨을 쓰는 서비스(DB) — AWS 에서는 데이터가 남지 않는다

    @property
    def built(self) -> bool:
        return not self.image


@dataclass
class StackPlan:
    project: str
    services: list[StackService]
    layers: list[list[str]]
    notes: list[str]
    errors: list[str]

    @property
    def entry(self) -> Optional[StackService]:
        return next((s for s in self.services if s.public), None)

    def monthly_estimate(self) -> int:
        n = len(self.services)
        return int(round(n * (_FARGATE_HOURLY + _PUBLIC_IPV4_HOURLY) * _HOURS))


_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?:(:?[-?])([^}]*))?\}|\$([A-Za-z_][A-Za-z0-9_]*)")


def interpolate(value: Any, env: dict[str, str], missing: set[str]) -> str:
    """compose 변수 치환 — ${A}, ${A:-기본}, ${A:?설명}, $A, $$ → $."""
    text = str(value)
    parts = text.split("$$")

    def one(part: str) -> str:
        def sub(m: re.Match) -> str:
            name = m.group(1) or m.group(4)
            op, arg = m.group(2) or "", m.group(3) or ""
            got = env.get(name, "")
            if got:
                return got
            if op.endswith("-"):
                return arg
            missing.add(name)
            return ""
        return _VAR.sub(sub, part)

    return "$".join(one(p) for p in parts)


def _db_port(image: str) -> Optional[int]:
    low = image.lower()
    return next((port for key, port in _DB_PORTS if key in low), None)


def _port_of(name: str, svc: dict, env: dict[str, str]) -> Optional[int]:
    if env.get("PORT", "").isdigit():
        return int(env["PORT"])
    for item in svc.get("ports") or []:
        m = re.search(r"(\d+)(?:/tcp)?\s*$", str(item))
        if m:
            return int(m.group(1))
    test = " ".join(map(str, (svc.get("healthcheck") or {}).get("test") or []))
    m = re.search(r"(?:127\.0\.0\.1|localhost):(\d+)", test)
    if m:
        return int(m.group(1))
    image = str(svc.get("image") or "")
    return _db_port(image) if image else None


def _seconds(value: Any, default: int) -> int:
    m = re.fullmatch(r"\s*(\d+)\s*(ms|s|m)?\s*", str(value or ""))
    if not m:
        return default
    n, unit = int(m.group(1)), m.group(2) or "s"
    return max(1, n // 1000) if unit == "ms" else n * 60 if unit == "m" else n


def _health(raw: Any, env: dict[str, str], missing: set[str]) -> Optional[dict]:
    if not isinstance(raw, dict) or raw.get("disable"):
        return None
    test = raw.get("test")
    if isinstance(test, str):
        test = ["CMD-SHELL", test]
    if not isinstance(test, list) or not test or test[0] == "NONE":
        return None
    return {
        "command": [interpolate(x, env, missing) for x in test],
        "interval": min(300, max(5, _seconds(raw.get("interval"), 30))),
        "timeout": min(60, max(2, _seconds(raw.get("timeout"), 5))),
        "retries": min(10, max(1, int(raw.get("retries") or 3))),
        "startPeriod": min(300, _seconds(raw.get("start_period"), 0) + 30),
    }


def _layers(services: list[StackService]) -> tuple[list[list[str]], list[str]]:
    names = {s.name for s in services}
    deps = {s.name: [d for d in s.depends_on if d in names] for s in services}
    done: set[str] = set()
    layers: list[list[str]] = []
    while len(done) < len(names):
        layer = sorted(n for n in names - done if all(d in done for d in deps[n]))
        if not layer:
            return layers, [f"서비스 의존 관계가 서로를 기다립니다: {', '.join(sorted(names - done))}"]
        layers.append(layer)
        done.update(layer)
    return layers, []


def plan(workspace: str | Path, env: dict[str, str]) -> StackPlan:
    """compose 파일 → 올릴 서비스 목록과 순서. AWS 를 부르지 않는다."""
    ws = Path(workspace)
    errors: list[str] = []
    notes: list[str] = []
    try:
        import compose_deploy
    except ImportError:  # pragma: no cover
        from core import compose_deploy  # type: ignore
    project = compose_deploy.project_name(ws)
    path = next((ws / n for n in ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml") if (ws / n).is_file()), None)
    if path is None or yaml is None:
        return StackPlan(project, [], [], notes, ["docker-compose.yml 을 찾지 못했습니다."])
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001
        return StackPlan(project, [], [], notes, [f"docker-compose.yml 을 읽지 못했습니다: {exc}"])
    missing: set[str] = set()
    services: list[StackService] = []
    for name, svc in (data.get("services") or {}).items():
        if not isinstance(svc, dict):
            continue
        raw_env = svc.get("environment") or {}
        if isinstance(raw_env, list):
            raw_env = dict(item.split("=", 1) if "=" in item else (item, "") for item in map(str, raw_env))
        environment = {str(k): interpolate(v if v is not None else "", env, missing) for k, v in raw_env.items()}
        build = svc.get("build")
        context, dockerfile = "", "Dockerfile"
        if isinstance(build, str):
            context = build
        elif isinstance(build, dict):
            context, dockerfile = str(build.get("context") or "."), str(build.get("dockerfile") or "Dockerfile")
        image = "" if build else interpolate(svc.get("image") or "", env, missing)
        if not image and not context:
            errors.append(f"{name}: 이미지도 빌드 폴더도 없습니다.")
            continue
        command = svc.get("command")
        if isinstance(command, str):
            command = ["sh", "-c", command]
        depends = svc.get("depends_on") or []
        depends = list(depends.keys()) if isinstance(depends, dict) else [str(d) for d in depends]
        services.append(StackService(
            name=str(name), image=image, context=context.strip("./") if context not in (".", "./") else ".", dockerfile=dockerfile,
            port=_port_of(str(name), svc, environment), environment=environment,
            command=[interpolate(c, env, missing) for c in command] if isinstance(command, list) else None,
            health=_health(svc.get("healthcheck"), env, missing), depends_on=depends,
            public=bool(svc.get("ports")), data=bool(svc.get("volumes")) and bool(image)))
    if missing:
        errors.append(f"비밀값이 비어 있습니다: {', '.join(sorted(missing))} — 로컬 배포를 한 번 하거나 .env 에 넣어 주세요.")
    #: 입구는 하나 — 화면(web) 서비스가 있으면 그것만, 없으면 포트를 연 첫 서비스.
    exposed = [s for s in services if s.public]
    entry = next((s for s in exposed if s.name in ("web", "frontend", "ui")), exposed[0] if exposed else None)
    for s in services:
        s.public = s is entry
    if entry is None:
        errors.append("밖에서 들어올 서비스(ports 를 연 서비스)가 없습니다.")
    elif entry.port is None:
        errors.append(f"{entry.name}: 포트를 알 수 없습니다.")
    for s in services:
        if s.port is None and s.depends_on is not None and any(s.name in t.depends_on for t in services):
            errors.append(f"{s.name}: 다른 서비스가 기다리는데 포트를 알 수 없습니다.")
    layers, cycle = _layers(services)
    errors += cycle
    if any(s.data for s in services):
        notes.append("DB·Redis 는 컨테이너로 돌아서, 태스크를 다시 띄우면 데이터가 사라집니다. 운영 데이터는 RDS·ElastiCache 로 옮기세요.")
    notes.append("이미지를 받기 위해 모든 태스크에 공인 IP 가 붙습니다. 밖에서 들어오는 길은 입구 서비스만 열립니다.")
    return StackPlan(project, services, layers, notes, errors)


def is_stack(workspace: str | Path | None) -> bool:
    """서비스를 둘 이상 직접 빌드하는 compose 프로젝트인가 — AWS 에서 이 모듈로 올린다."""
    if not workspace:
        return False
    try:
        import compose_deploy
    except ImportError:  # pragma: no cover
        from core import compose_deploy  # type: ignore
    try:
        return bool(compose_deploy.load(workspace)) or len(compose_deploy.built_services(workspace)) >= 2
    except Exception:  # noqa: BLE001
        return False


def service_name(project: str, svc: str) -> str:
    return f"{project}-{svc}"[:255]


def task_definition(svc: StackService, *, project: str, image: str, execution_role: str, task_role: str,
                    log_group: str, region: str) -> dict:
    container: dict[str, Any] = {
        "name": svc.name,
        "image": image,
        "essential": True,
        "environment": [{"name": k, "value": v} for k, v in sorted(svc.environment.items())],
        "logConfiguration": {"logDriver": "awslogs", "options": {
            "awslogs-group": log_group, "awslogs-region": region, "awslogs-stream-prefix": svc.name}},
    }
    if svc.port:
        container["portMappings"] = [{"name": f"{svc.name}-{svc.port}", "containerPort": svc.port, "protocol": "tcp",
                                      **({"appProtocol": "http"} if svc.built else {})}]
    if svc.command:
        container["command"] = svc.command
    if svc.health:
        container["healthCheck"] = svc.health
    td: dict[str, Any] = {
        "family": service_name(project, svc.name),
        "networkMode": "awsvpc",
        "requiresCompatibilities": ["FARGATE"],
        "cpu": "256",
        "memory": "512",
        "executionRoleArn": execution_role,
        "containerDefinitions": [container],
        "runtimePlatform": {"cpuArchitecture": "X86_64", "operatingSystemFamily": "LINUX"},
    }
    if task_role:
        td["taskRoleArn"] = task_role
    return td


def service_connect(svc: StackService, namespace: str) -> dict:
    """서버 역할(포트가 있는 서비스)은 compose 와 같은 이름·포트로 찾게 하고, 나머지는 찾기만 한다."""
    config: dict[str, Any] = {"enabled": True, "namespace": namespace}
    if svc.port:
        config["services"] = [{"portName": f"{svc.name}-{svc.port}", "discoveryName": svc.name,
                               "clientAliases": [{"port": svc.port, "dnsName": svc.name}]}]
    return config


def ensure_namespace(ecs: Any, cluster: str, namespace: str) -> None:
    """클러스터의 기본 Service Connect 이름공간(Cloud Map) — 없으면 ECS 가 만든다."""
    found = (ecs.describe_clusters(clusters=[cluster]).get("clusters") or [{}])[0]
    current = ((found.get("serviceConnectDefaults") or {}).get("namespace") or "")
    if current.rstrip("/").split("/")[-1] in (namespace, "") and current:
        return
    ecs.update_cluster(cluster=cluster, serviceConnectDefaults={"namespace": namespace})


INTERNAL_GROUP_DESCRIPTION = "ReCoder stack internal traffic"


def ensure_internal_group(ec2: Any, *, vpc_id: str, name: str, tags: dict) -> str:
    """같은 그룹끼리만 오가는 보안 그룹(서비스 사이 통신). 밖에서는 들어오지 못한다."""
    found = ec2.describe_security_groups(Filters=[{"Name": "group-name", "Values": [name]},
                                                  {"Name": "vpc-id", "Values": [vpc_id]}]).get("SecurityGroups") or []
    if found:
        gid = found[0]["GroupId"]
        managed = any(t.get("Key") == "recoder:managed" and t.get("Value") == "true" for t in found[0].get("Tags") or [])
        #: 태그 권한이 없던 때 만든 그룹은 태그가 없다 — ReCoder 가 쓰는 설명이면 우리 것으로 본다.
        if not managed and found[0].get("Description") != INTERNAL_GROUP_DESCRIPTION:
            raise RuntimeError(f"같은 이름의 보안 그룹 {name} 이 이미 있는데 ReCoder 가 만든 것이 아닙니다.")
    else:
        tag_list = [{"Key": k, "Value": v} for k, v in tags.items()]
        try:
            gid = ec2.create_security_group(GroupName=name, Description=INTERNAL_GROUP_DESCRIPTION, VpcId=vpc_id,
                                            TagSpecifications=[{"ResourceType": "security-group", "Tags": tag_list}])["GroupId"]
        except Exception as exc:  # noqa: BLE001
            #: 옛 권한표(ec2:CreateTags 없음)면 태그 없이 만들고, 태그는 나중에 붙여 본다 — 배포를 멈추지 않는다.
            code = str(getattr(exc, "response", {}).get("Error", {}).get("Code", ""))
            if code not in ("UnauthorizedOperation", "AccessDenied", "AccessDeniedException"):
                raise
            gid = ec2.create_security_group(GroupName=name, Description=INTERNAL_GROUP_DESCRIPTION, VpcId=vpc_id)["GroupId"]
            try:
                ec2.create_tags(Resources=[gid], Tags=tag_list)
            except Exception:  # noqa: BLE001
                pass
    try:
        ec2.authorize_security_group_ingress(GroupId=gid, IpPermissions=[{
            "IpProtocol": "tcp", "FromPort": 0, "ToPort": 65535, "UserIdGroupPairs": [{"GroupId": gid}]}])
    except Exception as exc:  # noqa: BLE001
        if "Duplicate" not in str(exc) and "InvalidPermission.Duplicate" not in getattr(exc, "response", {}).get("Error", {}).get("Code", ""):
            raise
    return gid


def upsert_service(ecs: Any, *, cluster: str, name: str, task_definition_arn: str, subnets: list[str],
                   groups: list[str], connect: dict, tags: dict) -> str:
    """서비스가 있으면 새 태스크 정의로 갱신, 없으면 만든다. 서비스 ARN 을 돌려준다."""
    found = [s for s in ecs.describe_services(cluster=cluster, services=[name]).get("services") or [] if s.get("status") == "ACTIVE"]
    network = {"awsvpcConfiguration": {"subnets": subnets, "securityGroups": groups, "assignPublicIp": "ENABLED"}}
    deployment = {"deploymentCircuitBreaker": {"enable": True, "rollback": False},
                  "minimumHealthyPercent": 0, "maximumPercent": 200}
    if found:
        resp = ecs.update_service(cluster=cluster, service=name, taskDefinition=task_definition_arn, desiredCount=1,
                                  networkConfiguration=network, serviceConnectConfiguration=connect,
                                  deploymentConfiguration=deployment, forceNewDeployment=True)
    else:
        resp = ecs.create_service(cluster=cluster, serviceName=name, taskDefinition=task_definition_arn, desiredCount=1,
                                  launchType="FARGATE", networkConfiguration=network, serviceConnectConfiguration=connect,
                                  deploymentConfiguration=deployment, enableECSManagedTags=True, propagateTags="SERVICE",
                                  tags=[{"key": k, "value": v} for k, v in tags.items()])
    return str(resp["service"]["serviceArn"])


def wait_stable(ecs: Any, *, cluster: str, names: list[str], timeout: float = 900.0, interval: float = 10.0,
                sleep: Callable[[float], None] = __import__("time").sleep,
                monotonic: Callable[[], float] = __import__("time").monotonic) -> dict[str, str]:
    """서비스들이 안정(실행 1 · 진행 중 배포 끝)될 때까지 기다린다. {서비스: 상태} — 실패면 'failed: 이유'."""
    deadline = monotonic() + timeout
    state: dict[str, str] = {n: "pending" for n in names}
    while True:
        resp = ecs.describe_services(cluster=cluster, services=names)
        for svc in resp.get("services") or []:
            n = svc.get("serviceName", "")
            primary = next((d for d in svc.get("deployments") or [] if d.get("status") == "PRIMARY"), {})
            if primary.get("rolloutState") == "FAILED":
                state[n] = "failed: " + str(primary.get("rolloutStateReason") or "배포 실패")
            elif primary.get("rolloutState") == "COMPLETED" and svc.get("runningCount", 0) >= 1:
                state[n] = "stable"
        if all(v == "stable" or v.startswith("failed") for v in state.values()):
            return state
        if monotonic() >= deadline:
            return {n: (v if v != "pending" else "failed: 시간 안에 안정되지 않음") for n, v in state.items()}
        sleep(interval)
