"""ECS 에서 앱이 실제로 시작하도록 실행 설정을 준비한다 (2.0.12).

왜 필요한가
    실기기(2.0.11 쇼핑몰 0ce6): ECS 배포가 태스크 정의까지 가더라도 앱은 DATABASE_URL·JWT_SECRET·결제 키가 없으면
    시작하자마자 `process.exit(1)` 한다. 로컬 Docker 배포는 DB 컨테이너·서명 키·모의 결제 서버를 함께 준비하지만
    ECS 는 아무것도 준비하지 않아 "배포는 됐는데 태스크가 계속 죽는" 상태가 됐다.

무엇을 하나 (값은 기록·로그에 남기지 않는다 — 이름만 남긴다)
    · staging: 로컬 배포와 같은 것을 **같은 태스크 안** 보조 컨테이너로 준비한다.
        - DB(PostgreSQL·Redis·MongoDB): ECR Public 공식 이미지, 127.0.0.1 에서만 듣는다(밖에서 접속 불가).
          프로젝트의 초기화 SQL 로 테이블을 만든다. 데이터는 태스크가 바뀌면 처음부터 다시 시작한다(임시 DB).
        - 앱 서명 키(JWT_SECRET 등): 서비스마다 한 번 만들어 PC 에 보관하고(0600) 다시 배포해도 같은 값을 쓴다.
        - 결제 키가 없고 앱이 모의 결제를 지원하면: 앱 이미지의 node 로 모의 결제 서버를 띄운다(로컬 데모와 같음).
        - 앱이 둔 샘플 데이터 스크립트가 있으면 태스크가 뜰 때 한 번 돌린다(임시 DB 라 매번 비어 있다).
    · production: 임시 DB·평문 서명 키·모의 결제를 쓰지 않는다. 비어 있는 값은 Secrets Manager ARN 으로 넣으라고
      **배포 전에** 막는다(AWS 자원을 하나도 만들기 전).
    · 그 밖에 앱이 요구하는데 아무도 줄 수 없는 값(외부 서비스 키 등)도 배포 전에 막는다.
"""
from __future__ import annotations

import base64
import json
import os
import re
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

try:  # 코어 안에서 실행(bare) / 패키지로 실행(core.*) 둘 다
    import deploy_settings
    import local_services
except ImportError:  # pragma: no cover
    from core import deploy_settings, local_services  # type: ignore

#: 앱 이미지로 띄우는 보조 컨테이너는 렌더링 때 실제 이미지 주소로 바뀐다.
APP_IMAGE = "{{app_image}}"

#: ECR Public 의 Docker 공식 이미지 — Fargate 가 Docker Hub 내려받기 제한 없이 받는다.
_IMAGES = {
    "postgres": "public.ecr.aws/docker/library/postgres:16-alpine",
    "redis": "public.ecr.aws/docker/library/redis:7-alpine",
    "mongodb": "public.ecr.aws/docker/library/mongo:7",
}
#: 보조 컨테이너를 함께 띄울 때 태스크 메모리 최소값(MiB).
_MIN_MEMORY = {"postgres": 1024, "redis": 1024, "mongodb": 2048, "payment": 1024}
#: 태스크 정의 전체는 64KiB 가 한도다 — 초기화 SQL 을 환경변수로 넣을 수 있는 크기.
_INIT_SQL_LIMIT = 24_000
_HEALTH = {
    "postgres": ["CMD-SHELL", "pg_isready -h 127.0.0.1 -U recoder -d app || exit 1"],
    "redis": ["CMD-SHELL", "redis-cli -h 127.0.0.1 ping | grep -q PONG || exit 1"],
    "mongodb": ["CMD-SHELL", "mongosh --quiet --host 127.0.0.1 --eval 'db.runCommand({ping:1}).ok' | grep -q 1 || exit 1"],
}
_LABELS = {"postgres": "PostgreSQL", "redis": "Redis", "mongodb": "MongoDB"}
#: 이 이름은 ECS 배포가 직접 넣는다.
_ALWAYS_PROVIDED = {"PORT", "ENVIRONMENT"}
_STORE_ENV = "RECODER_ECS_RUNTIME_DIR"
_KEY_RE = re.compile(r"[^A-Za-z0-9_.-]+")

#: 태스크 안에서 postgres 공식 이미지의 초기화 단계에 우리 SQL 을 한 트랜잭션으로 넣는다.
#: 실패해도 DB 는 뜨고(되돌림) 앱 로그가 원인을 알린다 — 초기화 실패로 DB 까지 죽으면 원인을 보기 더 어렵다.
_PG_BOOT = (
    'if [ -n "$RECODER_INIT_SQL_B64" ]; then '
    'echo "$RECODER_INIT_SQL_B64" | base64 -d > /tmp/recoder-init.sql && '
    "printf '%s\\n' "
    "'psql -v ON_ERROR_STOP=1 --single-transaction --username \"$POSTGRES_USER\" --dbname \"$POSTGRES_DB\" "
    "-f /tmp/recoder-init.sql || echo \"[recoder] 초기화 SQL 실패 - 되돌렸습니다\" >&2' "
    "> /docker-entrypoint-initdb.d/recoder-init.sh; fi; "
    "exec docker-entrypoint.sh postgres -c listen_addresses=127.0.0.1"
)


@dataclass
class RuntimePlan:
    """앱 컨테이너에 더할 환경변수와 함께 띄울 보조 컨테이너. 값(env)은 기록에 남기지 않는다."""

    env: dict[str, str] = field(default_factory=dict)
    sidecars: list[dict] = field(default_factory=list)
    depends_on: list[dict] = field(default_factory=list)
    min_memory: int = 0
    notes: list[str] = field(default_factory=list)
    missing: list[dict] = field(default_factory=list)
    services: list[str] = field(default_factory=list)
    demo: bool = False
    generated: list[str] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return bool(self.missing)

    def block_message(self) -> tuple[str, str]:
        names = ", ".join(m["name"] for m in self.missing)
        reasons = " ".join(dict.fromkeys(m["reason"] for m in self.missing))
        return (f"앱이 시작할 때 요구하는 설정이 없어 ECS 배포를 멈췄습니다: {names}",
                reasons + " 고급 설정 › 앱 실행 설정의 '비밀값 참조'에 "
                '{"이름":"arn:aws:secretsmanager:리전:계정:secret:이름"} 처럼 넣고 다시 배포하세요. '
                "AWS 자원은 아직 아무것도 만들지 않았습니다.")

    def summary(self) -> dict:
        """화면·기록용 — 이름과 안내만."""
        return {"services": list(self.services), "demo": self.demo, "generated": list(self.generated),
                "sidecars": [c["name"] for c in self.sidecars], "min_memory": self.min_memory,
                "notes": list(self.notes), "missing": list(self.missing)}


def _store_dir() -> Path:
    override = os.environ.get(_STORE_ENV)
    return Path(override) if override else Path.home() / ".recoder" / "ecs_runtime"


def _store_file(key: str) -> Path:
    return _store_dir() / f"{_KEY_RE.sub('_', key)[:150] or 'default'}.json"


def _load(key: str) -> dict:
    try:
        data = json.loads(_store_file(key).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(key: str, data: dict) -> None:
    path = _store_file(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _readiness(workspace: str):
    try:
        from build_readiness import analyze
    except ImportError:  # pragma: no cover
        from core.build_readiness import analyze  # type: ignore
    try:
        return analyze(workspace, online=False)
    except Exception:  # noqa: BLE001 - 점검 실패는 "보조 서비스 없음" 으로 본다
        return None


def _sidecar_log(name: str) -> dict:
    """앱 컨테이너의 로그 설정을 렌더링 때 그대로 복사한다(접두어만 다르게)."""
    return {"logDriver": "awslogs", "options": {"awslogs-stream-prefix": name}}


def _db_container(kind: str, password: str, init_sql: Optional[str]) -> dict:
    name = f"recoder-{kind}"
    container: dict = {
        "name": name, "image": _IMAGES[kind], "essential": True,
        "healthCheck": {"command": _HEALTH[kind], "interval": 5, "timeout": 3, "retries": 10, "startPeriod": 30},
        "logConfiguration": _sidecar_log(name),
    }
    if kind == "postgres":
        env = [{"name": "POSTGRES_USER", "value": "recoder"}, {"name": "POSTGRES_PASSWORD", "value": password},
               {"name": "POSTGRES_DB", "value": "app"}]
        if init_sql:
            env.append({"name": "RECODER_INIT_SQL_B64",
                        "value": base64.b64encode(init_sql.encode("utf-8")).decode("ascii")})
        container.update(entryPoint=["sh", "-c"], command=[_PG_BOOT], environment=env)
    elif kind == "redis":
        container["command"] = ["redis-server", "--bind", "127.0.0.1", "--protected-mode", "yes"]
    else:
        container["command"] = ["mongod", "--bind_ip", "127.0.0.1"]
    return container


def _mock_container(app_port: int, secret: str) -> dict:
    name = "recoder-payment-mock"
    #: 모의 결제 서버도 태스크 밖에서는 닿지 않게 127.0.0.1 에서만 듣는다.
    script = deploy_settings.MOCK_PAYMENT_JS.replace(".listen(PORT,'0.0.0.0')", ".listen(PORT,'127.0.0.1')")
    return {
        "name": name, "image": APP_IMAGE, "essential": True,
        "entryPoint": ["node", "-e", script], "command": [],
        "environment": [{"name": "MOCK_PORT", "value": str(deploy_settings._MOCK_PORT)},
                        {"name": "MOCK_WEBHOOK_URL", "value": f"http://127.0.0.1:{int(app_port)}/api/webhooks/stripe"},
                        {"name": "MOCK_WEBHOOK_SECRET", "value": secret}],
        "logConfiguration": _sidecar_log(name),
    }


def _seed_container(workdir: Optional[str], script: str, depends: list[dict]) -> dict:
    name = "recoder-seed"
    container = {
        "name": name, "image": APP_IMAGE, "essential": False,
        "entryPoint": ["node"], "command": [script],
        "dependsOn": [d for d in depends if d["condition"] == "HEALTHY"],
        "logConfiguration": _sidecar_log(name),
    }
    if workdir:
        container["workingDirectory"] = workdir
    return container


def _covered(names: Iterable[str], provided: set[str]) -> bool:
    return any(n in provided for n in names)


def plan(workspace: Optional[str], *, key: str, environment: str, provided: Iterable[str],
         container_port: int, create: bool = True, readiness=None) -> RuntimePlan:
    """이 앱을 ECS 에서 띄우는 데 필요한 것. create=False 면 키를 만들거나 저장하지 않는다(미리보기)."""
    result = RuntimePlan()
    if not workspace or not Path(workspace).is_dir():
        return result
    production = (environment or "").strip().lower() == "production"
    given = set(provided) | _ALWAYS_PROVIDED
    required = deploy_settings.required_env(workspace)
    readiness = readiness if readiness is not None else _readiness(workspace)
    env_names = sorted(set(deploy_settings.all_env_names(workspace)) | set(getattr(readiness, "env_names", []) or []))
    kinds = [k for k in (getattr(readiness, "services", []) or []) if k in _IMAGES]
    data = _load(key)
    stored = data.get("generated") if isinstance(data.get("generated"), dict) else {}
    changed = False

    def secret_for(name: str, length: int) -> str:
        nonlocal changed
        value = stored.get(name)
        if not (isinstance(value, str) and len(value) >= length):
            value = secrets.token_hex(max(32, (length + 1) // 2)) if create else "<generated>"
            if create:
                stored[name] = value
                changed = True
        return value

    # 1) 앱이 쓰는 DB — 접속 정보를 사용자가 이미 넣었으면(RDS 등) 그대로 쓴다.
    for kind in kinds:
        conn = local_services.app_env("ecs", kind, "x", env_names, host="127.0.0.1")
        if _covered(conn, given):
            continue
        reads = [n for n in conn if n in env_names or n in required] or [next(iter(conn))]
        if production:
            for name in reads:
                if name in required or name == reads[0]:
                    result.missing.append({"name": name, "reason": (
                        f"운영(production) 배포는 태스크 안 임시 {_LABELS[kind]} 를 쓰지 않습니다 — "
                        "RDS 같은 관리형 DB 주소를 Secrets Manager 에 넣고 그 ARN 을 연결하세요.")})
            continue
        password = secret_for(f"__{kind}_password", 24)
        for name, value in local_services.app_env("ecs", kind, password, env_names, host="127.0.0.1").items():
            result.env.setdefault(name, value)
        init_sql, init_name = None, ""
        if kind == "postgres":
            path = local_services.find_init_sql(workspace)
            if path is not None:
                try:
                    text = path.read_text(encoding="utf-8-sig", errors="replace")
                except OSError:
                    text = ""
                if len(text.encode("utf-8")) <= _INIT_SQL_LIMIT:
                    init_sql, init_name = (text or None), path.name
                else:
                    result.notes.append(f"초기화 SQL({path.name})이 커서 태스크 정의에 넣지 못했습니다 — "
                                        "앱이 스스로 테이블을 만들지 않으면 조회가 실패합니다.")
        prisma = "@prisma/client" in " ".join((Path(workspace) / d / "package.json").read_text(encoding="utf-8", errors="replace")
                                             for d in ("", "backend", "server", "api") if (Path(workspace) / d / "package.json").is_file())
        if kind == "postgres" and not init_sql and not prisma and not any("초기화 SQL" in n for n in result.notes):
            result.notes.append("테이블을 만드는 SQL(schema.sql 등)을 찾지 못해 빈 DB 로 시작합니다 — 앱이 시작할 때 테이블을 "
                                "스스로 만들지 않으면(Prisma 는 migrate·db push 가 필요) 조회가 실패합니다.")
        container = _db_container(kind, password, init_sql)
        result.sidecars.append(container)
        result.depends_on.append({"containerName": container["name"], "condition": "HEALTHY"})
        result.services.append(kind)
        result.min_memory = max(result.min_memory, _MIN_MEMORY[kind])
        result.notes.append(
            f"{_LABELS[kind]} 를 같은 태스크 안에 함께 띄웁니다({_IMAGES[kind]}, 127.0.0.1 에서만 접속)"
            + (f" — {init_name} 로 테이블을 만듭니다" if init_sql else "")
            + ". staging 용 임시 DB 라 태스크가 바뀌면 데이터가 처음부터 다시 시작합니다.")

    # 2) 결제 키 — 앱이 모의 결제를 지원하고 키가 없으면 데모로(staging 만).
    names_set = set(env_names)
    payment_missing = [n for n in required if "STRIPE" in n and n not in given]
    choice = deploy_settings.payment_choice_for(workspace) if payment_missing else None
    #: 모의 결제는 결제 계약상 NODE_ENV=test 에서만 동작한다 — 사용자가 NODE_ENV 를 직접 정했으면 데모로 바꾸지 않는다.
    if payment_missing and not production and "NODE_ENV" not in given \
            and deploy_settings.demo_supported(workspace, names_set) \
            and (not choice or choice.get("payment") != "keys"):
        webhook = data.get("demo_webhook_secret")
        if not (isinstance(webhook, str) and webhook):
            webhook = "whsec_ecs_demo_" + secrets.token_hex(16) if create else "<generated>"
            if create:
                data["demo_webhook_secret"] = webhook
                changed = True
        demo_env = {"NODE_ENV": "test", "PAYMENT_MODE": "mock", "STRIPE_SECRET_KEY": "sk_test_ecs_demo_only",
                    "STRIPE_WEBHOOK_SECRET": webhook, "STRIPE_MOCK_HOST": "127.0.0.1",
                    "STRIPE_MOCK_PORT": str(deploy_settings._MOCK_PORT), "SEED_DEMO": "true"}
        for name, value in demo_env.items():
            if name not in given:
                result.env[name] = value
        mock = _mock_container(container_port, webhook)
        result.sidecars.append(mock)
        result.depends_on.append({"containerName": mock["name"], "condition": "START"})
        result.demo = True
        result.min_memory = max(result.min_memory, _MIN_MEMORY["payment"])
        result.notes.append("결제 키가 없어 결제를 끈 모의 결제 서버를 같은 태스크에 띄웁니다(실제 결제 없음, NODE_ENV=test).")

    # 3) 그 밖에 앱이 시작할 때 요구하는 값.
    for name in sorted(required):
        if name in given or name in result.env or any(m["name"] == name for m in result.missing):
            continue
        if deploy_settings.kind_of(name) == "generate":
            if production:
                result.missing.append({"name": name, "reason": (
                    "운영(production) 배포는 앱 서명 키를 태스크 정의에 평문으로 넣지 않습니다 — "
                    "Secrets Manager 에 만들고 ARN 을 연결하세요.")})
                continue
            result.env[name] = secret_for(name, required[name])
            result.generated.append(name)
            continue
        hint = deploy_settings._HINTS.get(name, (name, ""))[1]
        result.missing.append({"name": name, "reason": (
            f"{name} 은(는) 앱이 시작할 때 꼭 필요한 외부 값이라 자동으로 만들 수 없습니다"
            + (f"({hint})." if hint else ".")
            + (" 이 앱은 모의 결제를 지원하지 않습니다." if "STRIPE" in name and not production else ""))})
    if result.generated:
        result.notes.append(f"앱 서명 키({', '.join(result.generated)})를 이 서비스용으로 만들어 PC 에 보관하고 "
                            "태스크 환경변수로 넣습니다(staging). 운영은 Secrets Manager ARN 을 쓰세요.")

    # 4) 임시 DB 는 태스크가 뜰 때마다 비어 있다 — 앱이 둔 샘플 데이터 스크립트를 그때 한 번 돌린다.
    if "postgres" in result.services and not result.missing:
        seed = local_services.seed_command(workspace)
        script, workdir = (seed["script"], seed["workdir"]) if seed else (None, None)
        if not script and result.demo:
            script = deploy_settings.demo_init_script(workspace)
            workdir = "/app" if script else None
        if script:
            result.sidecars.append(_seed_container(workdir, script, result.depends_on))
            result.notes.append(f"태스크가 뜰 때 앱의 샘플 데이터 스크립트({script})를 한 번 돌립니다.")

    if changed:
        data["generated"] = stored
        _save(key, data)
    return result


def store_key(region: str, cluster: str, service: str) -> str:
    return f"{region}-{cluster}-{service}"


def apply(task_def: dict, app_container: str, runtime: Optional[RuntimePlan], image: str) -> dict:
    """렌더링한 태스크 정의에 실행 설정을 더한다. 사용자가 직접 준 값은 덮어쓰지 않는다."""
    if runtime is None or not (runtime.env or runtime.sidecars):
        return task_def
    containers = task_def.setdefault("containerDefinitions", [])
    app = next((c for c in containers if c.get("name") == app_container), None)
    if app is None:
        return task_def
    have = {e.get("name") for e in app.get("environment", [])} | {s.get("name") for s in app.get("secrets", [])}
    app["environment"] = list(app.get("environment", [])) + [
        {"name": k, "value": v} for k, v in runtime.env.items() if k not in have]
    if runtime.depends_on:
        app["dependsOn"] = list(runtime.depends_on)
    log = app.get("logConfiguration") if isinstance(app.get("logConfiguration"), dict) else None
    for sidecar in runtime.sidecars:
        item = json.loads(json.dumps(sidecar))
        if item.get("image") == APP_IMAGE:
            item["image"] = image
        if item.get("name") == "recoder-seed":
            #: 샘플 데이터 스크립트는 앱과 같은 DB 주소·설정(SEED_DEMO 등)으로 돈다.
            item["environment"] = json.loads(json.dumps(app.get("environment", [])))
            if app.get("secrets"):
                item["secrets"] = json.loads(json.dumps(app["secrets"]))
        if log:
            options = dict(log.get("options") or {})
            options["awslogs-stream-prefix"] = item["name"]
            item["logConfiguration"] = {**log, "options": options}
        containers.append(item)
    if runtime.min_memory:
        try:
            current = int(str(task_def.get("memory") or "0"))
        except ValueError:
            current = 0
        if current < runtime.min_memory:
            task_def["memory"] = str(runtime.min_memory)
    return task_def
