"""로컬 Docker 배포에 앱이 쓰는 DB(PostgreSQL·MongoDB·Redis)를 함께 띄운다.

왜 필요한가 (2026-09-27 실기기)
    AI 가 만든 쇼핑몰은 PostgreSQL 을 쓰고, 시작할 때 DB 에 접속하지 못하면 종료한다.
    로컬 Docker 배포는 앱 컨테이너 하나만 띄웠으므로 컨테이너가 계속 죽고 "배포 실패"로
    끝났다. 앱이 쓰는 DB 를 같은 Docker 네트워크의 컨테이너로 띄우고 접속 정보를
    환경변수로 넘기면 PC 에 DB 를 설치하지 않아도 그대로 동작한다.

원칙
    - 데이터는 Docker 볼륨(<앱>-<종류>-data)에 남는다. 다시 배포해도 지워지지 않는다.
    - 비밀번호는 컨테이너마다 한 번 만들어 ~/.recoder/local_services/ 에 사용자 전용으로
      보관한다(같은 볼륨을 다시 쓰려면 같은 비밀번호가 필요하다).
    - 앱이 이미 설정한 환경변수는 덮어쓰지 않는다.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

_HOME = Path.home() / ".recoder" / "local_services"
_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.\-]{0,100}$")


@dataclass(frozen=True)
class Service:
    kind: str
    label: str
    image: str
    port: int
    data_path: str
    ready: tuple[str, ...]


SERVICES: dict[str, Service] = {
    "postgres": Service("postgres", "PostgreSQL", "postgres:16-alpine", 5432, "/var/lib/postgresql/data",
                        ("pg_isready", "-h", "127.0.0.1", "-U", "recoder", "-d", "app")),
    "mongodb": Service("mongodb", "MongoDB", "mongo:7", 27017, "/data/db",
                       ("mongosh", "--quiet", "--eval", "db.runCommand({ping:1}).ok")),
    "redis": Service("redis", "Redis", "redis:7-alpine", 6379, "/data", ("redis-cli", "ping")),
}


def network_name(container: str) -> str:
    return f"recoder-{container}"


def service_container(container: str, kind: str) -> str:
    return f"{container}-{kind}"


def _secret_file(container: str) -> Path:
    return _HOME / f"{container}.json"


def _load(container: str) -> dict:
    try:
        data = json.loads(_secret_file(container).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(container: str, data: dict) -> None:
    _HOME.mkdir(parents=True, exist_ok=True)
    path = _secret_file(container)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def configured(container: str) -> list[str]:
    """이 앱 컨테이너에 함께 띄우기로 한 서비스 종류."""
    kinds = _load(container).get("services")
    return [k for k in kinds if k in SERVICES] if isinstance(kinds, list) else []


def app_env(container: str, kind: str, password: str, env_names: list[str]) -> dict[str, str]:
    """앱 컨테이너에 넘길 접속 정보. env_names 는 코드가 읽는 환경변수 이름들."""
    host = service_container(container, kind)
    env: dict[str, str] = {}
    if kind == "postgres":
        url = f"postgresql://recoder:{password}@{host}:5432/app"
        env.update({"DATABASE_URL": url, "PGHOST": host, "PGPORT": "5432", "PGUSER": "recoder",
                    "PGPASSWORD": password, "PGDATABASE": "app"})
        hints = ("DATABASE", "POSTGRES", "PG_", "DB_")
        foreign = ("MONGO", "REDIS", "MYSQL", "SQLITE")
    elif kind == "mongodb":
        url = f"mongodb://{host}:27017/app"
        env.update({"MONGODB_URI": url, "MONGO_URI": url, "MONGO_URL": url})
        hints = ("MONGO", "DATABASE", "DB_")
        foreign = ("POSTGRES", "PG_", "REDIS", "MYSQL", "SQLITE")
    else:
        url = f"redis://{host}:6379"
        env.update({"REDIS_URL": url})
        hints = ("REDIS",)          # Redis 는 자기 이름이 든 변수만 — DB_HOST 같은 일반 이름은 주 DB 의 것이다
        foreign = ()
    parts = {"postgres": {"HOST": host, "PORT": "5432", "USER": "recoder", "PASSWORD": password, "PASS": password,
                          "NAME": "app", "DATABASE": "app", "DB": "app"},
             "mongodb": {"HOST": host, "PORT": "27017", "NAME": "app", "DATABASE": "app", "DB": "app"},
             "redis": {"HOST": host, "PORT": "6379"}}[kind]
    for name in env_names:
        upper = name.upper()
        if not any(h in upper for h in hints) or any(f in upper for f in foreign):
            continue
        if re.search(r"URL|URI|CONNECTION", upper):
            env.setdefault(name, url)
            continue
        for suffix, value in parts.items():
            if re.search(rf"(?:^|_){suffix}$", upper):
                env.setdefault(name, value)
                break
    return env


def plan(container: str, kinds: list[str], env_names: list[str]) -> tuple[dict[str, str], list[str]]:
    """(앱에 넘길 환경변수, 승인 화면 안내). 비밀번호는 처음 한 번 만들어 보관한다."""
    if not _NAME_RE.match(container or ""):
        return {}, []
    data = _load(container)
    passwords = data.get("passwords") if isinstance(data.get("passwords"), dict) else {}
    env: dict[str, str] = {}
    notes: list[str] = []
    for kind in kinds:
        if kind not in SERVICES:
            continue
        password = passwords.get(kind) or secrets.token_urlsafe(18)
        passwords[kind] = password
        for key, value in app_env(container, kind, password, env_names).items():
            env.setdefault(key, value)  # 먼저 온 주 DB(postgres → mongodb → redis)가 같은 이름을 가진다
        svc = SERVICES[kind]
        notes.append(
            f"{svc.label} 컨테이너({service_container(container, kind)}, {svc.image})를 함께 띄우고 앱에 접속 정보를 "
            f"환경변수로 넘깁니다. 데이터는 Docker 볼륨 {service_container(container, kind)}-data 에 남습니다.")
    _save(container, {"services": [k for k in kinds if k in SERVICES], "passwords": passwords})
    return env, notes


Runner = Callable[[list[str], int], subprocess.CompletedProcess]


def _run(args: list[str], timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(args, shell=False, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


#: 프로젝트가 함께 둔 DB 초기화 SQL — docker-compose 라면 /docker-entrypoint-initdb.d 로 넣는 파일이다.
_INIT_SQL_CANDIDATES = (
    "init.sql", "schema.sql", "db/init.sql", "db/schema.sql", "database/init.sql", "database/schema.sql",
    "sql/init.sql", "sql/schema.sql", "server/init.sql", "server/schema.sql", "server/db/init.sql", "server/db/schema.sql",
    "backend/init.sql", "backend/schema.sql", "backend/db/init.sql", "backend/db/schema.sql",
    "backend/src/schema.sql", "backend/src/init.sql", "backend/src/db/schema.sql", "src/schema.sql", "src/init.sql",
    "src/db/schema.sql", "server/src/schema.sql",
)
_INIT_SQL_MAX_BYTES = 2_000_000


def find_init_sql(workspace: str) -> Optional[Path]:
    """앱이 기대하는 테이블을 만드는 SQL 파일(있을 때만). CREATE TABLE 이 없으면 초기화 파일로 보지 않는다."""
    if not workspace:
        return None
    root = Path(workspace)
    for rel in _INIT_SQL_CANDIDATES:
        path = root / rel
        try:
            if path.is_file() and path.stat().st_size <= _INIT_SQL_MAX_BYTES:
                text = path.read_text(encoding="utf-8-sig", errors="replace")
                if re.search(r"\bcreate\s+table\b", text, re.I):
                    return path
        except OSError:
            continue
    return None


def _exec_sql(name: str, sql: str, timeout: int = 120) -> subprocess.CompletedProcess:
    """서비스 컨테이너 안 psql 로 SQL 을 한 트랜잭션으로 실행한다(표준 입력으로 전달)."""
    return subprocess.run(["docker", "exec", "-i", name, "psql", "-U", "recoder", "-d", "app", "-v", "ON_ERROR_STOP=1",
                           "--single-transaction", "-q"],
                          shell=False, input=sql, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _initialize_postgres(name: str, init_sql: Path, progress, run: Runner) -> None:
    """DB 에 테이블이 하나도 없을 때만(처음 만든 볼륨) 프로젝트의 초기화 SQL 을 실행한다.

    docker-compose 의 initdb 처럼 한 번만 — 이미 테이블이 있으면(재배포) 손대지 않아 데이터가 그대로다.
    실패해도 배포는 계속하고(트랜잭션이라 반쯤 적용되지 않는다), 앱 로그·화면 확인이 원인을 알린다.
    """
    count = run(["docker", "exec", name, "psql", "-U", "recoder", "-d", "app", "-tAc",
                 "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'"], 30)
    if count.returncode != 0 or (count.stdout or "").strip() != "0":
        return
    try:
        sql = init_sql.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return
    if progress:
        progress(f"빈 DB 에 {init_sql.name} 로 테이블을 만듭니다")
    try:
        done = _exec_sql(name, sql)
    except (OSError, subprocess.SubprocessError) as exc:
        done = subprocess.CompletedProcess([], 1, "", str(exc))
    if done.returncode != 0 and progress:
        progress(f"{init_sql.name} 실행이 실패해 되돌렸습니다: {(done.stderr or '').strip()[:200]}")


def ensure(container: str, progress: Optional[Callable[[str], None]] = None, run: Runner = _run,
           wait_seconds: int = 90, init_sql: Optional[Path] = None) -> list[str]:
    """네트워크와 서비스 컨테이너를 준비하고 응답할 때까지 기다린다. 앱 docker run 에 붙일 인자를 돌려준다.

    실패하면 RuntimeError(사람이 읽을 원인).
    """
    kinds = configured(container)
    if not kinds:
        return []
    passwords = _load(container).get("passwords") or {}
    net = network_name(container)
    if run(["docker", "network", "inspect", net], 30).returncode != 0:
        created = run(["docker", "network", "create", net], 30)
        if created.returncode != 0 and "already exists" not in (created.stderr or ""):
            raise RuntimeError(f"Docker 네트워크 {net} 를 만들지 못했습니다: {(created.stderr or '').strip()[:200]}")
    for kind in kinds:
        svc = SERVICES[kind]
        name = service_container(container, kind)
        state = run(["docker", "inspect", "-f", "{{.State.Running}}", name], 30)
        if state.returncode != 0:
            if progress:
                progress(f"{svc.label} 컨테이너를 준비합니다(처음에는 이미지를 내려받습니다)")
            args = ["docker", "run", "-d", "--name", name, "--network", net, "--network-alias", name,
                    "--restart", "unless-stopped", "-v", f"{name}-data:{svc.data_path}"]
            if kind == "postgres":
                args += ["-e", "POSTGRES_USER=recoder", "-e", f"POSTGRES_PASSWORD={passwords.get(kind, '')}",
                         "-e", "POSTGRES_DB=app"]
            args.append(svc.image)
            started = run(args, 600)
            if started.returncode != 0:
                raise RuntimeError(f"{svc.label} 컨테이너를 시작하지 못했습니다: {(started.stderr or '').strip()[:300]}")
        elif (state.stdout or "").strip() != "true":
            run(["docker", "start", name], 60)
        # 예전 배포에서 만든 컨테이너가 네트워크에서 빠졌을 수 있다(이미 연결돼 있으면 무시된다).
        run(["docker", "network", "connect", "--alias", name, net, name], 30)
        deadline = time.monotonic() + wait_seconds
        while True:
            probe = run(["docker", "exec", name, *svc.ready], 30)
            if probe.returncode == 0:
                break
            if time.monotonic() >= deadline:
                raise RuntimeError(f"{svc.label} 이 {wait_seconds}초 안에 준비되지 않았습니다. "
                                   f"`docker logs {name}` 로 원인을 확인하세요.")
            time.sleep(2)
        if kind == "postgres" and passwords.get(kind):
            #: 데이터 볼륨은 남았는데 보관한 비밀번호가 바뀌었을 수 있다(~/.recoder 초기화·재설치·다른 PC 에서 복사).
            #: 그러면 앱이 "password authentication failed" 로 죽는다(실제 재현). 컨테이너 안 로컬 소켓(신뢰 인증)으로
            #: 비밀번호를 지금 값으로 맞춘다. 실패해도 배포는 계속하고, 앱 로그 진단이 원인을 알린다.
            password = str(passwords[kind])
            if re.fullmatch(r"[A-Za-z0-9_\-]{8,128}", password):
                synced = run(["docker", "exec", name, "psql", "-U", "recoder", "-d", "app", "-v", "ON_ERROR_STOP=1",
                              "-c", f"ALTER USER recoder WITH PASSWORD '{password}'"], 30)
                if synced.returncode != 0 and progress:
                    progress(f"{svc.label} 비밀번호를 맞추지 못했습니다 — 앱이 접속하지 못하면 볼륨 {name}-data 를 확인하세요")
        if kind == "postgres" and init_sql is not None:
            _initialize_postgres(name, init_sql, progress, run)
    return ["--network", net]


def network_args(container: str, run: Runner = _run) -> list[str]:
    """복구·롤백처럼 서비스가 이미 떠 있는 경로에서 앱을 같은 네트워크에 붙인다."""
    demo = False
    try:
        import deploy_settings
        demo = bool(deploy_settings.load(container).get("demo"))
    except Exception:  # noqa: BLE001
        demo = False
    if not configured(container) and not demo:
        return []
    net = network_name(container)
    return ["--network", net] if run(["docker", "network", "inspect", net], 30).returncode == 0 else []
