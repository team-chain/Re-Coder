"""
ReCoder Core — 관리형 DB: Amazon RDS for PostgreSQL (2.0.27, 선택)

staging 의 태스크 안 임시 Postgres 는 태스크가 바뀌면 데이터가 사라진다. 관리형 DB 를 켜면 RDS 를 만들어 데이터가 남는다.

  · 작은 인스턴스(db.t4g.micro, 20GB gp3, 암호화, 자동 백업 1일, 삭제 보호), 인터넷에서 접속 불가(PubliclyAccessible=False),
    보안 그룹은 이 서비스의 태스크 보안 그룹에서 오는 5432 만 허용한다.
  · 비밀번호는 ReCoder 가 만들고 접속 정보 전체(DATABASE_URL·PGHOST 등)를 Secrets Manager 비밀 하나에 JSON 으로 둔다.
    태스크에는 staging 이면 그 값을 환경변수로, production 이면 비밀 ARN(valueFrom)으로 넣는다.
    (RDS 가 관리하는 비밀번호(ManageMasterUserPassword)는 사용자명·비밀번호만 담아 생성 앱이 읽는 DATABASE_URL 을 만들 수 없다.)
  · 생성 앱의 DB 드라이버는 저마다 TLS 설정이 달라(node-postgres 는 sslmode=require 를 인증서 검증으로 다룬다) 연결이 깨지기
    쉽다. 그래서 TLS 강제(rds.force_ssl)는 끈 파라미터 그룹을 쓴다 — 접속은 VPC 안·태스크 보안 그룹에서만 열린다.
  · 만드는 데 5~10분 걸린다. 생성은 인프라 단계에서 시작하고, 기다림은 태스크 정의 등록 직전에 한다(그 사이 이미지 빌드).
"""
from __future__ import annotations

import json
import logging
import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional
from urllib.parse import quote

try:
    from core.aws_infra import InfraError, error_code, error_message, ensure_security_group  # type: ignore
except ImportError:  # pragma: no cover
    from aws_infra import InfraError, error_code, error_message, ensure_security_group  # type: ignore

logger = logging.getLogger(__name__)

INSTANCE_CLASS = "db.t4g.micro"
STORAGE_GB = 20
USER = "recoder"
DB_NAME = "app"
PORT = 5432
FALLBACK_VERSION = ("16", "postgres16")
COST_NOTE = ("관리형 DB(RDS)는 켜 두는 동안 계속 요금이 나옵니다(db.t4g.micro + 20GB, us-east-1 기준 대략 월 15달러 이상 · "
             "새 계정 1년 무료 사용량 있음). 그만 쓰면 「AWS 리소스 정리」로 지우세요(최종 스냅숏을 남깁니다).")
WAIT_S = 1500


def db_identifier(service: str) -> str:
    """RDS 식별자(63자, 문자로 시작, 하이픈 연속·끝 불가)."""
    try:
        from core.aws_edge import edge_name  # type: ignore
    except ImportError:  # pragma: no cover
        from aws_edge import edge_name  # type: ignore
    return f"{edge_name(service)}-db"


def secret_name(service: str) -> str:
    return f"recoder/{re.sub(r'[^A-Za-z0-9/_+=.@-]', '-', service)}/database"


@dataclass
class DbHandle:
    identifier: str
    secret_arn: str
    created: bool
    security_group: str
    status: str = ""
    notes: list[str] = field(default_factory=list)
    #: 메모리에만 — 기록·로그에 남기지 않는다.
    password: str = field(default="", repr=False)


def engine(rds: Any) -> tuple[str, str]:
    """기본 PostgreSQL 버전과 파라미터 그룹 계열. 조회가 안 되면 16."""
    try:
        versions = rds.describe_db_engine_versions(Engine="postgres", DefaultOnly=True).get("DBEngineVersions", [])
        if versions:
            return str(versions[0]["EngineVersion"]), str(versions[0]["DBParameterGroupFamily"])
    except Exception as exc:  # noqa: BLE001
        logger.info("RDS 기본 버전 조회 실패 — %s 로 만듭니다: %s", FALLBACK_VERSION[0], error_message(exc))
    return FALLBACK_VERSION


def _describe(rds: Any, identifier: str) -> Optional[dict]:
    try:
        return rds.describe_db_instances(DBInstanceIdentifier=identifier)["DBInstances"][0]
    except Exception as exc:  # noqa: BLE001
        if error_code(exc) == "DBInstanceNotFound" or error_code(exc) == "DBInstanceNotFoundFault":
            return None
        raise


def _tag_list(tags: Optional[dict]) -> list[dict]:
    return [{"Key": k, "Value": v} for k, v in (tags or {}).items()]


def _subnet_group(rds: Any, ec2: Any, name: str, vpc_id: str, tags: Optional[dict]) -> str:
    try:
        if rds.describe_db_subnet_groups(DBSubnetGroupName=name).get("DBSubnetGroups"):
            return name
    except Exception as exc:  # noqa: BLE001
        if "NotFound" not in error_code(exc):
            raise
    subnets = ec2.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])["Subnets"]
    by_zone: dict[str, str] = {}
    for s in sorted(subnets, key=lambda s: (str(s.get("AvailabilityZone")), str(s["SubnetId"]))):
        by_zone.setdefault(str(s.get("AvailabilityZone")), str(s["SubnetId"]))
    if len(by_zone) < 2:
        raise InfraError("관리형 DB 는 가용 영역 2곳 이상의 서브넷이 필요한데 1곳만 찾았습니다.",
                         remedy="VPC 에 다른 가용 영역의 서브넷을 하나 더 만들거나 관리형 DB 를 끄세요.")
    rds.create_db_subnet_group(DBSubnetGroupName=name, DBSubnetGroupDescription="ReCoder managed database",
                               SubnetIds=list(by_zone.values()), Tags=_tag_list(tags))
    return name


def _parameter_group(rds: Any, name: str, family: str, tags: Optional[dict]) -> str:
    try:
        exists = bool(rds.describe_db_parameter_groups(DBParameterGroupName=name).get("DBParameterGroups"))
    except Exception as exc:  # noqa: BLE001
        if "NotFound" not in error_code(exc):
            raise
        exists = False
    if not exists:
        rds.create_db_parameter_group(DBParameterGroupName=name, DBParameterGroupFamily=family,
                                      Description="ReCoder - TLS optional for generated app drivers", Tags=_tag_list(tags))
    rds.modify_db_parameter_group(DBParameterGroupName=name, Parameters=[
        {"ParameterName": "rds.force_ssl", "ParameterValue": "0", "ApplyMethod": "pending-reboot"}])
    return name


def start(rds: Any, ec2: Any, secretsmanager: Any, *, service: str, vpc_id: str, task_security_groups: list[str],
          tags: Optional[dict] = None) -> DbHandle:
    """없으면 만들기를 시작하고(기다리지 않음), 있으면 그대로 쓴다. 비밀번호는 먼저 비밀에 둔다(중간에 멈춰도 잃지 않게)."""
    ident = db_identifier(service)
    db_sg = ""
    for i, group in enumerate(task_security_groups):
        db_sg = ensure_security_group(ec2, vpc_id=vpc_id, name=f"{service}-db-sg", port=PORT,
                                      description="ReCoder managed database (tasks only)", tags=tags if i == 0 else None,
                                      source_group=group)
    existing = _describe(rds, ident)
    sec_name = secret_name(service)
    secret_arn, password = _secret(secretsmanager, sec_name, tags, create=existing is None)
    if existing is not None:
        return DbHandle(ident, secret_arn, False, db_sg, str(existing.get("DBInstanceStatus", "")), password=password)
    version, family = engine(rds)
    subnet_group = _subnet_group(rds, ec2, f"{ident}-subnets", vpc_id, tags)
    params = _parameter_group(rds, f"{ident}-params", family, tags)
    try:
        rds.create_db_instance(
            DBInstanceIdentifier=ident, Engine="postgres", EngineVersion=version, DBInstanceClass=INSTANCE_CLASS,
            AllocatedStorage=STORAGE_GB, StorageType="gp3", StorageEncrypted=True, MasterUsername=USER,
            MasterUserPassword=password, DBName=DB_NAME, Port=PORT, VpcSecurityGroupIds=[db_sg],
            DBSubnetGroupName=subnet_group, DBParameterGroupName=params, PubliclyAccessible=False, MultiAZ=False,
            BackupRetentionPeriod=1, DeletionProtection=True, CopyTagsToSnapshot=True, AutoMinorVersionUpgrade=True,
            Tags=_tag_list(tags),
        )
    except Exception as exc:  # noqa: BLE001
        raise InfraError("관리형 DB(RDS)를 만들지 못했습니다.", detail=error_message(exc),
                         remedy="권한표에 2.0.27 의 rds 권한이 있는지(권한 템플릿 스택 업데이트), 계정의 RDS 한도를 확인하세요.") from exc
    logger.info("RDS 생성 시작: %s", ident)
    return DbHandle(ident, secret_arn, True, db_sg, "creating", password=password)


def _secret(sm: Any, name: str, tags: Optional[dict], *, create: bool) -> tuple[str, str]:
    """(ARN, 비밀번호). 비밀이 없으면 새 비밀번호로 만든다. 있으면 저장된 비밀번호를 읽는다."""
    try:
        value = sm.get_secret_value(SecretId=name)
        data = json.loads(value.get("SecretString") or "{}")
        password = str(data.get("PGPASSWORD") or data.get("password") or "")
        if password:
            return str(value["ARN"]), password
    except Exception as exc:  # noqa: BLE001
        if error_code(exc) != "ResourceNotFoundException":
            raise InfraError("관리형 DB 접속 정보를 Secrets Manager 에서 읽지 못했습니다.", detail=error_message(exc),
                             remedy="권한표에 2.0.27 의 secretsmanager 권한이 있는지 확인하세요.") from exc
        if not create:
            raise InfraError("관리형 DB 는 있는데 ReCoder 가 만든 접속 정보(비밀)가 없습니다.",
                             remedy=f"Secrets Manager 의 {name} 을 지웠다면 RDS 를 정리하고 다시 만들거나, DATABASE_URL 을 비밀값 참조로 직접 넣으세요.")
        password = secrets.token_hex(24)
        arn = sm.create_secret(Name=name, Description="ReCoder managed database connection",
                               SecretString=json.dumps({"password": password}), Tags=_tag_list(tags))["ARN"]
        return str(arn), password
    raise InfraError(f"Secrets Manager 의 {name} 에 비밀번호가 없습니다.", remedy="그 비밀을 지운 뒤 다시 배포하세요.")


def wait(rds: Any, identifier: str, *, timeout: float = WAIT_S, sleep: Callable[[float], None] = time.sleep,
         on_wait: Optional[Callable[[str, float], None]] = None) -> dict:
    """available 이 될 때까지. on_wait(상태, 지난 초) 로 진행을 알린다."""
    started = time.monotonic()
    while True:
        db = _describe(rds, identifier)
        if db is None:
            raise InfraError(f"관리형 DB({identifier})가 사라졌습니다.", remedy="다시 배포하면 새로 만듭니다.")
        status = str(db.get("DBInstanceStatus", ""))
        if status == "available" and db.get("Endpoint"):
            return db
        if status in ("failed", "incompatible-parameters", "incompatible-restore", "storage-full", "deleting"):
            raise InfraError(f"관리형 DB 상태가 '{status}' 라 쓸 수 없습니다.", remedy="RDS 콘솔에서 상태를 확인하세요.")
        waited = time.monotonic() - started
        if waited > timeout:
            raise InfraError(f"관리형 DB 가 {int(timeout // 60)}분 안에 준비되지 않았습니다(상태: {status}).",
                             remedy="조금 뒤 다시 배포하면 만들어진 DB 를 그대로 씁니다.")
        if on_wait:
            on_wait(status, waited)
        sleep(20)


def connection(endpoint: dict, password: str, env_names: list[str]) -> dict[str, str]:
    """앱에 넘길 접속 정보 — 태스크 안 임시 DB 와 같은 이름 규칙(local_services.app_env)으로."""
    try:
        from core import local_services  # type: ignore
    except ImportError:  # pragma: no cover
        import local_services  # type: ignore
    host = str(endpoint["Address"])
    env = local_services.app_env("ecs", "postgres", password, env_names, host=host)
    url = f"postgresql://{USER}:{quote(password, safe='')}@{host}:{int(endpoint.get('Port', PORT))}/{DB_NAME}"
    return {k: (url if v.startswith("postgresql://") else v) for k, v in env.items()}


def store_connection(sm: Any, secret_arn: str, values: dict[str, str]) -> None:
    sm.put_secret_value(SecretId=secret_arn, SecretString=json.dumps({**values, "password": values.get("PGPASSWORD", "")}))


def secret_refs(secret_arn: str, names: list[str]) -> dict[str, str]:
    """production: 값 대신 JSON 키 참조(arn:…:secret:이름-xxxx:KEY::)."""
    return {name: f"{secret_arn}:{name}::" for name in names}


#: 처음 한 번 — DB 가 비어 있으면(공개 스키마에 테이블 0개) 프로젝트의 초기화 SQL 을 한 트랜잭션으로 넣는다.
INIT_IMAGE = "public.ecr.aws/docker/library/postgres:16-alpine"
INIT_BOOT = (
    'n=$(psql "$DATABASE_URL" -tAc "select count(*) from information_schema.tables where table_schema=\'public\'") '
    '|| { echo "[recoder] DB 에 접속하지 못했습니다" >&2; exit 0; }; '
    'if [ "$n" = "0" ] && [ -n "$RECODER_INIT_SQL_B64" ]; then '
    'echo "$RECODER_INIT_SQL_B64" | base64 -d > /tmp/recoder-init.sql && '
    'psql "$DATABASE_URL" -v ON_ERROR_STOP=1 --single-transaction -f /tmp/recoder-init.sql '
    '&& echo "[recoder] 초기화 SQL 을 넣었습니다" || echo "[recoder] 초기화 SQL 실패 - 되돌렸습니다" >&2; fi; exit 0'
)


def init_container(init_sql_b64: str, *, plain_url: str = "", secret_url_ref: str = "") -> dict:
    item: dict = {"name": "recoder-db-init", "image": INIT_IMAGE, "essential": False,
                  "entryPoint": ["sh", "-c"], "command": [INIT_BOOT],
                  "environment": [{"name": "RECODER_INIT_SQL_B64", "value": init_sql_b64}],
                  "logConfiguration": {"logDriver": "awslogs", "options": {"awslogs-stream-prefix": "recoder-db-init"}}}
    if secret_url_ref:
        item["secrets"] = [{"name": "DATABASE_URL", "valueFrom": secret_url_ref}]
    else:
        item["environment"].append({"name": "DATABASE_URL", "value": plain_url})
    return item


# ---------------------------------------------------------------------------
# 정리
# ---------------------------------------------------------------------------


def delete_instance(rds: Any, identifier: str) -> str:
    """삭제 보호를 풀고 최종 스냅숏을 남기며 지운다(지우는 데 수 분)."""
    db = _describe(rds, identifier)
    if db is None:
        return "이미 없습니다."
    if db.get("DBInstanceStatus") == "deleting":
        return "지우는 중입니다."
    if db.get("DeletionProtection"):
        rds.modify_db_instance(DBInstanceIdentifier=identifier, DeletionProtection=False, ApplyImmediately=True)
    snap = f"{identifier}-final-{time.strftime('%Y%m%d%H%M', time.gmtime())}"
    rds.delete_db_instance(DBInstanceIdentifier=identifier, FinalDBSnapshotIdentifier=snap, DeleteAutomatedBackups=True)
    return (f"지우기 시작했습니다(수 분) — 최종 스냅숏 {snap} 을 남겼습니다. 스냅숏도 보관 요금이 나오니 필요 없으면 RDS 콘솔에서 지우세요.")
