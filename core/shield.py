"""기본 방패 — 앱 앞에 nginx 보조 컨테이너를 두어 IP 당 요청 수를 제한한다 (2.0.25).

왜 필요한가
    ECS 배포는 앱이 공인 IP 의 앱 포트로 바로 노출된다. 스크립트로 초당 수백~수천 건을 보내면 앱(과 DB)이 그대로 받는다.
    같은 태스크 안에 nginx 를 두고 공개 포트를 nginx 가 맡게 하면, 한 IP 가 너무 많이 보내는 요청은 앱에 닿기 전에 429 로 돌려보낸다.

어떻게
    - 공개 포트(앱 포트 P)는 방패가 듣고, 앱은 P+1 에서 듣는다(PORT 환경변수). 같은 태스크라 127.0.0.1 로 넘긴다.
      보안 그룹은 P 만 열려 있어 P+1 은 밖에서 닿지 않는다.
    - 설정 파일은 볼륨 없이 환경변수(base64)로 넣고 시작 명령이 /etc/nginx/conf.d 에 쓴다 — ECS 와 Docker 가 같은 방식.
    - 접속 기록은 JSON 한 줄씩(src=recoder-shield) 표준 출력 → CloudWatch Logs. 공격·장애 알림(ops_alerts)이 이것을 센다.
    - 헬스 경로는 제한하지 않는다(ECS·ALB 헬스 체크가 막히면 태스크가 교체된다).

한계 (안내에 쓴다)
    - 초당 수만 건 규모는 막지 못한다(회선이 먼저 막힌다) — 그 규모는 ALB + AWS WAF(대규모 방패) 몫.
    - 학교·회사처럼 한 공인 IP 뒤에 사람이 많으면 정상 사용자도 429 를 받을 수 있다 — 기본값은 너무 엄격하지 않게.
"""
from __future__ import annotations

import base64
import ipaddress
import json
import re
from dataclasses import dataclass
from typing import Optional

#: ECR Public 의 Docker 공식 nginx 이미지 — Fargate 가 Docker Hub 내려받기 제한 없이 받는다.
IMAGE = "public.ecr.aws/docker/library/nginx:stable-alpine"
CONTAINER = "recoder-shield"
LEVELS = {
    #: 초당 요청(평균) · 순간 몰림 허용 · IP 당 동시 연결
    "loose": {"rate": 50, "burst": 100, "conn": 100, "label": "느슨"},
    "normal": {"rate": 20, "burst": 40, "conn": 50, "label": "보통"},
    "strict": {"rate": 5, "burst": 10, "conn": 20, "label": "엄격"},
}
DEFAULT_LEVEL = "normal"
#: 방패 컨테이너가 쓰는 메모리 여유(MiB) — nginx 는 작다.
MEMORY_MIB = 64


def deniable(value: str) -> bool:
    """막아도 되는 주소 — 공인 IP(또는 대역)만. 사설·루프백을 막으면 헬스 체크·ALB 가 막혀 태스크가 교체된다."""
    try:
        net = ipaddress.ip_network(str(value).strip(), strict=False)
    except ValueError:
        return False
    return net.is_global and net.prefixlen >= (16 if net.version == 4 else 32)

#: 시작 명령: 환경변수의 설정을 쓰고, 문법을 확인한 뒤 nginx 를 앞쪽 프로세스로 띄운다.
BOOT = ('echo "$RECODER_SHIELD_CONF_B64" | base64 -d > /etc/nginx/conf.d/default.conf '
        '&& nginx -t -q && exec nginx -g "daemon off;"')


@dataclass
class ShieldDecision:
    on: bool
    level: str = DEFAULT_LEVEL
    reason: str = ""
    app_port: int = 0
    public_port: int = 0


def decide(*, enabled: bool, level: str, public_port: int, port_from_env: bool, static_site: bool = False) -> ShieldDecision:
    """방패를 켤 수 있는지. 못 켜면 이유를 돌려준다(배포는 방패 없이 계속)."""
    lv = level if level in LEVELS else DEFAULT_LEVEL
    if not enabled:
        return ShieldDecision(False, lv, "배포 설정에서 방패를 껐습니다.")
    if static_site:
        return ShieldDecision(False, lv, "정적 화면 이미지(nginx)는 이미 웹 서버라 방패를 붙이지 않습니다.")
    if not port_from_env:
        return ShieldDecision(False, lv, "앱이 포트를 PORT 환경변수로 받지 않고 코드에 고정해 두어, 앱을 방패 뒤 포트로 옮길 수 "
                                         "없습니다. 서버 시작 코드를 `process.env.PORT` 를 쓰게 고치면 다음 배포부터 방패가 켜집니다.")
    if not 1 <= public_port <= 65534:
        return ShieldDecision(False, lv, f"포트 {public_port} 다음 번호를 앱 포트로 쓸 수 없습니다.")
    return ShieldDecision(True, lv, "", app_port=public_port + 1, public_port=public_port)


def _health_paths(path: str) -> list[str]:
    path = path if path.startswith("/") else "/" + path
    return [path] if path != "/" else []


def nginx_conf(*, public_port: int, app_port: int, level: str = DEFAULT_LEVEL, health_path: str = "/health",
               deny: Optional[list[str]] = None, trust_vpc_proxy: bool = False, trust_cdn: bool = False) -> str:
    """nginx 설정 본문. deny: 막을 IP(대응 제안 '이 IP 차단'). trust_vpc_proxy: ALB 뒤면 X-Forwarded-For 로 실제 IP.
    trust_cdn(2.0.27): CloudFront → ALB 뒤면 CloudFront-Viewer-Address 의 사용자 IP 로 센다 — ALB 는 CloudFront 주소에서만
    받게 막혀 있어(보안 그룹) 이 머리글을 사용자가 위조해 보낼 수 없다."""
    lv = LEVELS.get(level, LEVELS[DEFAULT_LEVEL])
    denies = [d.strip() for d in (deny or []) if deniable(d)]
    client = "$recoder_client" if trust_cdn else "$remote_addr"
    key = "$recoder_client" if trust_cdn else "$binary_remote_addr"
    if trust_cdn:
        proto = "map $http_cloudfront_forwarded_proto $recoder_proto { default $http_cloudfront_forwarded_proto; '' $scheme; }"
    elif trust_vpc_proxy:
        proto = "map $http_x_forwarded_proto $recoder_proto { default $http_x_forwarded_proto; '' $scheme; }"
    else:
        proto = "map $scheme $recoder_proto { default $scheme; }"
    lines = [
        "# ReCoder 기본 방패 — 자동 생성(손으로 고치지 마세요: 다음 배포 때 다시 만듭니다)",
    ]
    if trust_cdn:
        lines += [
            "map $http_cloudfront_viewer_address $recoder_client {",
            '  "~^(?<v4>[0-9.]+):[0-9]+$" $v4;',
            '  "~^(?<v6>[0-9A-Fa-f:]+):[0-9]+$" $v6;',
            "  default $remote_addr;",
            "}",
        ]
    lines += [
        "log_format recoder_json escape=json '{\"src\":\"recoder-shield\",\"time\":\"$time_iso8601\","
        f"\"remote_addr\":\"{client}\",\"method\":\"$request_method\",\"uri\":\"$uri\",\"status\":$status,"
        "\"bytes\":$body_bytes_sent,\"rt\":$request_time,\"ua\":\"$http_user_agent\"}';",
        f"limit_req_zone {key} zone=perip:10m rate={lv['rate']}r/s;",
        f"limit_conn_zone {key} zone=conn:10m;",
        "map $http_upgrade $connection_upgrade { default upgrade; '' close; }",
        # ALB·CloudFront 뒤면 사용자가 들어온 실제 프로토콜(https)을 앱에 넘긴다(보안 쿠키·리다이렉트).
        proto,
    ]
    if trust_cdn and denies:
        #: deny 지시어는 연결 주소(CloudFront)만 본다 — 사용자 IP 로 막으려면 geo 로 판정한다.
        lines += ["geo $recoder_client $recoder_denied {", "  default 0;", *[f"  {d} 1;" for d in denies], "}"]
    lines += [
        "server {",
        f"  listen {public_port};",
        "  server_tokens off;",
        "  access_log /dev/stdout recoder_json;",
        "  client_max_body_size 10m;",
    ]
    if trust_vpc_proxy:
        lines += ["  set_real_ip_from 10.0.0.0/8;", "  set_real_ip_from 172.16.0.0/12;", "  set_real_ip_from 192.168.0.0/16;",
                  "  real_ip_header X-Forwarded-For;", "  real_ip_recursive on;"]
    if trust_cdn:
        if denies:
            lines += ["  if ($recoder_denied) { return 403; }"]
    else:
        lines += [f"  deny {d.strip()};" for d in denies]
    upstream = f"http://127.0.0.1:{app_port}"
    proxy = ["proxy_http_version 1.1;", "proxy_set_header Host $host;", f"proxy_set_header X-Real-IP {client};",
             "proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;", "proxy_set_header X-Forwarded-Proto $recoder_proto;",
             "proxy_set_header Upgrade $http_upgrade;", "proxy_set_header Connection $connection_upgrade;",
             "proxy_read_timeout 60s;"]
    for path in _health_paths(health_path):
        lines += [f"  location = {path} {{", "    access_log off;", f"    proxy_pass {upstream};",
                  *[f"    {p}" for p in proxy], "  }"]
    lines += [
        "  location / {",
        f"    limit_req zone=perip burst={lv['burst']} nodelay;",
        "    limit_req_status 429;",
        f"    limit_conn conn {lv['conn']};",
        "    limit_conn_status 429;",
        f"    proxy_pass {upstream};",
        *[f"    {p}" for p in proxy],
        "  }",
        "}",
    ]
    return "\n".join(lines) + "\n"


def proxy_mode(trust_vpc_proxy: bool = False, trust_cdn: bool = False) -> str:
    return "cdn" if trust_cdn else "vpc" if trust_vpc_proxy else ""


def container(*, public_port: int, app_port: int, level: str, health_path: str, deny: Optional[list[str]] = None,
              trust_vpc_proxy: bool = False, log: Optional[dict] = None, trust_cdn: bool = False) -> dict:
    conf = nginx_conf(public_port=public_port, app_port=app_port, level=level, health_path=health_path,
                      deny=deny, trust_vpc_proxy=trust_vpc_proxy or trust_cdn, trust_cdn=trust_cdn)
    check = health_path if health_path.startswith("/") else "/" + health_path
    item: dict = {
        "name": CONTAINER, "image": IMAGE, "essential": True,
        "portMappings": [{"containerPort": int(public_port), "protocol": "tcp"}],
        "entryPoint": ["sh", "-c"], "command": [BOOT],
        "environment": [{"name": "RECODER_SHIELD_CONF_B64", "value": base64.b64encode(conf.encode("utf-8")).decode("ascii")},
                        {"name": "RECODER_SHIELD_LEVEL", "value": level},
                        {"name": "RECODER_SHIELD_DENY", "value": ",".join(deny or [])},
                        #: 대응(차단·강도) 때 다시 만들어도 앞단(ALB·CloudFront)을 잊지 않게 기록한다.
                        {"name": "RECODER_SHIELD_PROXY", "value": proxy_mode(trust_vpc_proxy, trust_cdn)}],
        "healthCheck": {"command": ["CMD-SHELL", f"wget -q -T 4 -O /dev/null http://127.0.0.1:{int(public_port)}{check} || exit 1"],
                        "interval": 15, "timeout": 5, "retries": 3, "startPeriod": 20},
    }
    if log:
        options = dict(log.get("options") or {})
        options["awslogs-stream-prefix"] = CONTAINER
        item["logConfiguration"] = {**log, "options": options}
    return item


def _rewrite_port(command: list, old: int, new: int) -> list:
    return [re.sub(rf"(127\.0\.0\.1|localhost):{old}(?=[/\"'\s]|$)", rf"\g<1>:{new}", c) if isinstance(c, str) else c
            for c in command]


def apply(task_def: dict, app_container: str, decision: ShieldDecision, *, health_path: str = "/health",
          deny: Optional[list[str]] = None, trust_vpc_proxy: bool = False, trust_cdn: bool = False) -> dict:
    """태스크 정의에 방패를 넣는다 — 앱은 P+1 로 옮기고, 공개 포트·외부 헬스는 방패가 맡는다."""
    if not decision.on:
        return task_def
    containers = task_def.setdefault("containerDefinitions", [])
    app = next((c for c in containers if c.get("name") == app_container), None)
    if app is None or any(c.get("name") == CONTAINER for c in containers):
        return task_def
    P, A = decision.public_port, decision.app_port
    env = [e for e in app.get("environment", []) if e.get("name") != "PORT"]
    app["environment"] = env + [{"name": "PORT", "value": str(A)}]
    app["portMappings"] = [{"containerPort": A, "protocol": "tcp"}]
    if isinstance(app.get("healthCheck"), dict) and app["healthCheck"].get("command"):
        app["healthCheck"]["command"] = _rewrite_port(list(app["healthCheck"]["command"]), P, A)
    #: 같은 태스크의 보조 컨테이너(모의 결제의 웹훅 주소 등)가 앱을 P 로 부르던 것도 A 로.
    for c in containers:
        if c is app or c.get("name") == CONTAINER:
            continue
        for e in c.get("environment", []) or []:
            if isinstance(e.get("value"), str) and f"127.0.0.1:{P}" in e["value"]:
                e["value"] = e["value"].replace(f"127.0.0.1:{P}", f"127.0.0.1:{A}")
    log = app.get("logConfiguration") if isinstance(app.get("logConfiguration"), dict) else None
    shield = container(public_port=P, app_port=A, level=decision.level, health_path=health_path, deny=deny,
                       trust_vpc_proxy=trust_vpc_proxy, log=log, trust_cdn=trust_cdn)
    #: 앱이 헬스 체크를 가지면 HEALTHY 뒤에, 아니면 시작한 뒤에 방패가 뜬다.
    shield["dependsOn"] = [{"containerName": app_container, "condition": "HEALTHY" if app.get("healthCheck") else "START"}]
    containers.append(shield)
    try:
        memory = int(str(task_def.get("memory") or "0"))
    except ValueError:
        memory = 0
    if memory and memory < 512:
        task_def["memory"] = "512"
    return task_def


def summary(decision: ShieldDecision) -> dict:
    lv = LEVELS.get(decision.level, LEVELS[DEFAULT_LEVEL])
    return {"on": decision.on, "level": decision.level, "label": lv["label"], "rate": lv["rate"], "burst": lv["burst"],
            "public_port": decision.public_port, "app_port": decision.app_port, "reason": decision.reason}


def _proxy_from_conf(conf_b64: str) -> str:
    """2.0.26 이전 태스크 정의에는 RECODER_SHIELD_PROXY 가 없다 — 넣어 둔 nginx 설정에서 앞단을 알아낸다."""
    try:
        conf = base64.b64decode(conf_b64.encode("ascii"), validate=False).decode("utf-8", "replace")
    except (ValueError, UnicodeError):
        return ""
    if "cloudfront_viewer_address" in conf:
        return "cdn"
    if "real_ip_header" in conf:
        return "vpc"
    return ""


def current_settings(task_def: dict) -> dict:
    """등록된 태스크 정의에서 방패의 강도·차단 IP 를 읽는다(대응 제안이 이것을 바꿔 새 리비전을 만든다)."""
    for c in task_def.get("containerDefinitions", []) or []:
        if c.get("name") == CONTAINER:
            env = {e.get("name"): e.get("value", "") for e in c.get("environment", []) or []}
            return {"on": True, "level": env.get("RECODER_SHIELD_LEVEL") or DEFAULT_LEVEL,
                    "deny": [d for d in (env.get("RECODER_SHIELD_DENY") or "").split(",") if d],
                    "proxy": env.get("RECODER_SHIELD_PROXY") or _proxy_from_conf(env.get("RECODER_SHIELD_CONF_B64") or "")}
    return {"on": False, "level": DEFAULT_LEVEL, "deny": [], "proxy": ""}


def reconfigure(task_def: dict, *, level: Optional[str] = None, deny_add: Optional[list[str]] = None,
                health_path: str = "/health", trust_vpc_proxy: Optional[bool] = None) -> dict:
    """방패 설정만 바꾼 태스크 정의(등록용 사본). 방패가 없으면 그대로."""
    td = json.loads(json.dumps(task_def))
    shield = next((c for c in td.get("containerDefinitions", []) if c.get("name") == CONTAINER), None)
    if shield is None:
        return td
    cur = current_settings(td)
    lv = level if level in LEVELS else cur["level"]
    deny = list(dict.fromkeys(cur["deny"] + [d.strip() for d in (deny_add or []) if deniable(d)]))
    public = int((shield.get("portMappings") or [{}])[0].get("containerPort") or 0)
    #: 앞단(ALB·CloudFront)은 태스크 정의에 기록된 대로 — 잊으면 모든 사용자가 ALB 주소 하나로 세어져 함께 막힌다.
    proxy = cur.get("proxy", "")
    vpc = proxy in ("vpc", "cdn") if trust_vpc_proxy is None else trust_vpc_proxy
    fresh = container(public_port=public, app_port=public + 1, level=lv, health_path=health_path, deny=deny,
                      trust_vpc_proxy=vpc, log=shield.get("logConfiguration"), trust_cdn=proxy == "cdn")
    if shield.get("logConfiguration"):
        fresh["logConfiguration"] = shield["logConfiguration"]
    fresh["dependsOn"] = shield.get("dependsOn", [])
    #: 사람이 콘솔에서 더한 컨테이너 설정(memory·cpu 등)은 그대로 둔다 — 방패가 관리하는 키만 바꾼다.
    keep = {k: v for k, v in shield.items() if k not in fresh}
    shield.clear()
    shield.update({**keep, **fresh})
    return td
