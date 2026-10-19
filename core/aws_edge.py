"""
ReCoder Core — 대규모 방패: 로드밸런서(ALB) + AWS WAF (2.0.26, 선택)

기본 방패(shield.py, 태스크 안 nginx)는 태스크 하나가 감당할 수 있는 만큼만 막는다. 초당 수천~수만 건이
몰리면 태스크까지 오기 전에 막아야 한다 — 그 자리가 ALB 앞의 WAF 다.

  인터넷 → ALB(80) ─ WAF 웹 ACL(IP 당 속도 제한 + AWS 관리 규칙 2개) → 태스크(ALB 보안 그룹에서만 받음)

원칙
  · aws_infra 와 같다: 멱등, boto3 클라이언트를 인자로 받음(moto 로 시험), 실패는 사람이 읽는 문장.
  · ALB·WAF 는 켜 두기만 해도 매달 요금이 나온다 — 배포 요청에 비용 확인(edge_cost_approved)이 없으면
    아무것도 만들지 않는다(ecs_agent 가 막는다).
  · 이 호출에서 **새로 만든 것만** 실패 때 되돌린다. 이미 있던 ALB·대상 그룹은 건드리지 않는다.
  · 새로 만드는 ALB·대상 그룹·리스너·웹 ACL 에는 생성 요청에 recoder:* 태그를 함께 넣는다(정리 기능이 찾는 표시).
    이 권한은 2.0.26 권한표에만 있으므로 옛 권한표처럼 "태그 권한만 없는" 경우가 없다.
"""
from __future__ import annotations

import hashlib
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

#: ecs_agent 가 `core.aws_infra.InfraError` 로 잡는다 — 같은 클래스여야 하므로 core 쪽을 먼저(둘 다 sys.path 에 있으면
#: `aws_infra` 와 `core.aws_infra` 는 서로 다른 모듈이 된다).
try:
    from core.aws_infra import (InfraError, error_code, error_message, ensure_security_group,  # type: ignore
                                internet_routable_subnets)
except ImportError:  # pragma: no cover
    from aws_infra import (InfraError, error_code, error_message, ensure_security_group,  # type: ignore
                           internet_routable_subnets)

logger = logging.getLogger(__name__)

#: WAF 속도 제한 창(초) — AWS 가 허용하는 값만.
WINDOWS = (60, 120, 300, 600)
DEFAULT_RATE_LIMIT = 2000
DEFAULT_WINDOW = 300
MIN_RATE_LIMIT = 10
MAX_RATE_LIMIT = 2_000_000_000
LISTENER_PORT = 80
#: 2.0.35 원본 구간 HTTPS — 내 도메인을 쓰는 HTTPS 주소에서 CloudFront 가 이 포트로 붙는다.
HTTPS_LISTENER_PORT = 443
#: TLS 1.2 이상(1.3 우선) — https://docs.aws.amazon.com/elasticloadbalancing/latest/application/describe-ssl-policies.html
SSL_POLICY = "ELBSecurityPolicy-TLS13-1-2-2021-06"
#: 웹 ACL 설명 — AWS 가 허용하는 문자(영문·숫자·_+=:#@/-,. 공백)만. 한글·긴 줄표(—)를 넣으면 생성이 거절된다(2.0.32).
WAF_DESCRIPTION = "ReCoder edge shield - per-IP rate limit + AWS managed rules"
#: WAF 범위 — ALB 는 지역(REGIONAL), CloudFront 는 전역(CLOUDFRONT, 반드시 us-east-1 클라이언트).
REGIONAL = "REGIONAL"
CLOUDFRONT = "CLOUDFRONT"
#: 2.0.31 HTTPS 주소 — CloudFront 가 붙이는 비밀 머리글. 이 값이 맞는 요청만 ALB 가 앱으로 넘긴다
#: (보안 그룹의 CloudFront 주소 목록은 **남의 CloudFront 배포**도 통과시키므로 한 겹 더).
ORIGIN_HEADER = "X-Recoder-Origin"
ORIGIN_RULE_PRIORITY = 10
DENY_ACTION = {"Type": "fixed-response",
               "FixedResponseConfig": {"StatusCode": "403", "ContentType": "text/plain", "MessageBody": "Forbidden"}}

#: AWS 관리 규칙 — 일반 공격 패턴, 알려진 악성 입력.
MANAGED_RULES = ("AWSManagedRulesCommonRuleSet", "AWSManagedRulesKnownBadInputsRuleSet")
#: CommonRuleSet 의 본문 8KB 제한은 사진 업로드(multer 등)를 막는다 — 차단 대신 기록만.
COUNT_ONLY = {"AWSManagedRulesCommonRuleSet": ("SizeRestrictions_BODY",)}

COST_NOTE = ("ALB 는 켜 두는 시간만큼 + 처리량만큼, WAF 는 웹 ACL·규칙마다 매달 + 요청 수만큼 요금이 나옵니다 "
             "(트래픽이 거의 없어도 us-east-1 기준 대략 월 25달러 이상). 쓰지 않으면 「AWS 리소스 정리」로 지우세요.")
PRICING_LINKS = ("https://aws.amazon.com/elasticloadbalancing/pricing/", "https://aws.amazon.com/waf/pricing/")


def edge_name(service: str) -> str:
    """ALB·대상 그룹 이름(32자, 영문·숫자·하이픈). 권한표가 recoder-* 로 좁히므로 항상 recoder- 로 시작한다."""
    slug = re.sub(r"[^a-z0-9-]+", "-", str(service or "").lower()).strip("-")
    if slug.startswith("recoder-"):
        slug = slug[len("recoder-"):]
    slug = re.sub(r"-+", "-", slug)[:18].strip("-") or "app"
    digest = hashlib.sha1(str(service).encode("utf-8")).hexdigest()[:4]
    return f"recoder-{slug}-{digest}"


def global_acl_name(name: str, region: str) -> str:
    """CloudFront(전역) 웹 ACL 이름 — 같은 서비스 이름을 여러 리전에 배포해도 겹치지 않게 리전을 붙인다(2.0.32)."""
    return f"{name}-{region}" if region else name


def _tag_list(tags: Optional[dict]) -> list[dict]:
    return [{"Key": k, "Value": v} for k, v in (tags or {}).items()]


def _visibility(metric: str) -> dict:
    return {"SampledRequestsEnabled": True, "CloudWatchMetricsEnabled": True,
            "MetricName": re.sub(r"[^A-Za-z0-9_-]", "-", metric)[:128]}


def web_acl_rules(name: str, rate_limit: int = DEFAULT_RATE_LIMIT, window: int = DEFAULT_WINDOW) -> list[dict]:
    """웹 ACL 규칙: ① IP 당 속도 제한(차단) ② AWS 관리 규칙 2개."""
    if window not in WINDOWS:
        raise InfraError(f"WAF 속도 제한 창은 {', '.join(map(str, WINDOWS))}초 중 하나여야 합니다: {window}")
    if not MIN_RATE_LIMIT <= int(rate_limit) <= MAX_RATE_LIMIT:
        raise InfraError(f"WAF 속도 제한은 창당 {MIN_RATE_LIMIT}회 이상이어야 합니다: {rate_limit}")
    rules: list[dict] = [{
        "Name": "recoder-rate-per-ip",
        "Priority": 0,
        "Statement": {"RateBasedStatement": {"Limit": int(rate_limit), "EvaluationWindowSec": int(window),
                                             "AggregateKeyType": "IP"}},
        "Action": {"Block": {}},
        "VisibilityConfig": _visibility(f"{name}-rate"),
    }]
    for i, group in enumerate(MANAGED_RULES, start=1):
        statement: dict = {"VendorName": "AWS", "Name": group}
        if group in COUNT_ONLY:
            statement["RuleActionOverrides"] = [{"Name": r, "ActionToUse": {"Count": {}}} for r in COUNT_ONLY[group]]
        rules.append({
            "Name": f"aws-{group}",
            "Priority": i,
            "Statement": {"ManagedRuleGroupStatement": statement},
            "OverrideAction": {"None": {}},
            "VisibilityConfig": _visibility(f"{name}-{group}"),
        })
    return rules


def rate_of(rules: list[dict]) -> tuple[int, int] | None:
    """웹 ACL 규칙에서 (창당 한도, 창) — 운영 화면·비교용."""
    for rule in rules or []:
        rb = (rule.get("Statement") or {}).get("RateBasedStatement")
        if rb:
            return int(rb.get("Limit", 0)), int(rb.get("EvaluationWindowSec", DEFAULT_WINDOW))
    return None


@dataclass
class EdgeResult:
    name: str
    alb_arn: str
    dns_name: str
    target_group_arn: str
    listener_arn: str
    alb_security_group: str
    task_security_group: str
    web_acl_arn: str = ""
    created: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    #: ALB 별칭 레코드에 쓰는 ALB 의 호스티드 영역 ID(2.0.35 원본 구간 HTTPS)
    zone_id: str = ""

    @property
    def url(self) -> str:
        return f"http://{self.dns_name}"


# ---------------------------------------------------------------------------
# 서브넷 — ALB 는 가용 영역 2곳 이상이 필요하고, 태스크가 뜰 영역을 모두 포함해야 한다
# ---------------------------------------------------------------------------


def alb_subnets(ec2: Any, vpc_id: str, task_subnets: list[str]) -> list[str]:
    """ALB 서브넷 — 태스크가 뜨는 영역은 모두 포함하고(빠지면 그 영역 태스크로 요청이 가지 않는다), 영역마다
    **인터넷 게이트웨이로 나가는(공개) 서브넷**을 고른다. 태스크가 사설 서브넷(NAT)에 있어도 ALB 는 공개 서브넷에 있어야
    인터넷에서 닿는다."""
    try:
        mine = ec2.describe_subnets(SubnetIds=list(task_subnets))["Subnets"] if task_subnets else []
        others = ec2.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])["Subnets"]
    except Exception as exc:  # noqa: BLE001
        raise InfraError("로드밸런서를 둘 서브넷을 확인하지 못했습니다.", detail=error_message(exc),
                         remedy="권한표의 ec2:DescribeSubnets 를 확인하세요.") from exc
    every = {str(s["SubnetId"]): s for s in [*others, *mine]}
    routable, checked = internet_routable_subnets(ec2, vpc_id, list(every))
    # 라우팅을 못 봤거나 igw 경로가 하나도 안 보이면(권한·특이한 라우팅) "공인 IP 자동 할당" 서브넷으로 대신 가린다.
    public = set(routable) if checked and routable else {sid for sid, s in every.items() if s.get("MapPublicIpOnLaunch")}
    task_ids = {str(s["SubnetId"]) for s in mine}

    def rank(sid: str) -> tuple:
        return (sid not in public, sid not in task_ids, sid)

    zones: dict[str, list[str]] = {}
    for sid, s in every.items():
        zones.setdefault(str(s.get("AvailabilityZone")), []).append(sid)
    by_zone: dict[str, str] = {}
    for zone in sorted({str(s.get("AvailabilityZone")) for s in mine}):
        by_zone[zone] = min(zones[zone], key=rank)
    # 다른 영역은 공개 서브넷이 있는 곳부터(가용 영역 2곳 이상 필요, 3곳까지).
    for zone in sorted(zones, key=lambda z: (rank(min(zones[z], key=rank))[0], z)):
        if len(by_zone) >= 3:
            break
        by_zone.setdefault(zone, min(zones[zone], key=rank))
    if len(by_zone) < 2:
        raise InfraError(
            "로드밸런서는 가용 영역 2곳 이상의 서브넷이 필요한데 1곳만 찾았습니다.",
            remedy="VPC 에 다른 가용 영역의 공개 서브넷을 하나 더 만들거나, 대규모 방패를 끄고 배포하세요.")
    private = [sid for sid in by_zone.values() if sid not in public]
    if private:
        logger.warning("ALB 서브넷 중 공개가 아닌 것: %s — 그 영역에는 공개 서브넷이 없습니다.", ",".join(private))
    return list(by_zone.values())


# ---------------------------------------------------------------------------
# ALB · 대상 그룹 · 리스너
# ---------------------------------------------------------------------------


def _find_load_balancer(elbv2: Any, name: str) -> dict | None:
    try:
        lbs = elbv2.describe_load_balancers(Names=[name]).get("LoadBalancers", [])
    except Exception as exc:  # noqa: BLE001
        if error_code(exc) in ("LoadBalancerNotFound", "LoadBalancerNotFoundException"):
            return None
        raise InfraError("로드밸런서를 조회하지 못했습니다.", detail=error_message(exc),
                         remedy="권한표의 elasticloadbalancing:DescribeLoadBalancers 를 확인하세요.") from exc
    return lbs[0] if lbs else None


def _find_target_group(elbv2: Any, name: str) -> dict | None:
    try:
        groups = elbv2.describe_target_groups(Names=[name]).get("TargetGroups", [])
    except Exception as exc:  # noqa: BLE001
        if error_code(exc) in ("TargetGroupNotFound", "TargetGroupNotFoundException"):
            return None
        raise InfraError("대상 그룹을 조회하지 못했습니다.", detail=error_message(exc)) from exc
    return groups[0] if groups else None


def _health_check(health_path: str) -> dict:
    return {"HealthCheckProtocol": "HTTP", "HealthCheckPath": health_path or "/",
            "HealthCheckIntervalSeconds": 15, "HealthCheckTimeoutSeconds": 5,
            "HealthyThresholdCount": 2, "UnhealthyThresholdCount": 3, "Matcher": {"HttpCode": "200-499"}}


# ---------------------------------------------------------------------------
# WAF
# ---------------------------------------------------------------------------


def find_web_acl(wafv2: Any, name: str, scope: str = REGIONAL) -> dict | None:
    """이름으로 웹 ACL 요약을 찾는다(scope=CLOUDFRONT 는 us-east-1 클라이언트로)."""
    marker = None
    while True:
        kwargs: dict = {"Scope": scope, "Limit": 100}
        if marker:
            kwargs["NextMarker"] = marker
        resp = wafv2.list_web_acls(**kwargs)
        for acl in resp.get("WebACLs", []):
            if acl.get("Name") == name:
                return acl
        marker = resp.get("NextMarker")
        if not marker or not resp.get("WebACLs"):
            return None


def ensure_web_acl(wafv2: Any, name: str, *, rate_limit: int, window: int, tags: Optional[dict],
                   scope: str = REGIONAL) -> tuple[str, bool]:
    """웹 ACL 을 확보하고 (ARN, 새로 만들었는가). 있으면 속도 제한을 요청 값으로 맞춘다."""
    rules = web_acl_rules(name, rate_limit, window)
    existing = find_web_acl(wafv2, name, scope)
    if existing is None:
        summary = wafv2.create_web_acl(
            Name=name, Scope=scope, DefaultAction={"Allow": {}}, Rules=rules,
            VisibilityConfig=_visibility(name),
            Description=WAF_DESCRIPTION,
            **({"Tags": _tag_list(tags)} if tags else {}),
        )["Summary"]
        logger.info("WAF 웹 ACL 생성: %s", name)
        return str(summary["ARN"]), True
    current = wafv2.get_web_acl(Name=name, Scope=scope, Id=existing["Id"])
    if rate_of(current["WebACL"].get("Rules", [])) != (int(rate_limit), int(window)) or \
            len(current["WebACL"].get("Rules", [])) != len(rules):
        wafv2.update_web_acl(Name=name, Scope=scope, Id=existing["Id"], DefaultAction={"Allow": {}},
                             Rules=rules, VisibilityConfig=_visibility(name), LockToken=current["LockToken"],
                             Description=WAF_DESCRIPTION)
        logger.info("WAF 웹 ACL 갱신: %s (%s회/%s초)", name, rate_limit, window)
    return str(existing["ARN"]), False


def associate(wafv2: Any, acl_arn: str, alb_arn: str, *, sleep: Callable[[float], None] = time.sleep,
              attempts: int = 36) -> None:
    """방금 만든 ALB 는 잠시 WAF 가 못 본다(WAFUnavailableEntityException) — 5초씩 최대 3분 기다려 다시."""
    for i in range(attempts):
        try:
            wafv2.associate_web_acl(WebACLArn=acl_arn, ResourceArn=alb_arn)
            return
        except Exception as exc:  # noqa: BLE001
            if error_code(exc) != "WAFUnavailableEntityException" or i == attempts - 1:
                raise
            sleep(5)


def detach_recoder_acl(wafv2: Any, alb_arn: str, name: str, *, sleep: Callable[[float], None] = time.sleep,
                       reason: str = "WAF 를 껐습니다") -> str:
    """우리가 ALB 에 붙인 지역 웹 ACL 을 떼고 지운다(켜 둔 채 매달 요금이 나가지 않게).

    떼어 낸 직후에는 잠시 '아직 연결됨'(WAFAssociatedItemException)으로 지우기가 거절된다 — 기다려 다시. 끝내 못 지우면
    배포를 실패시키지 않고 안내만 남긴다(떼어 냈으므로 동작에는 영향 없음, 요금만 남음)."""
    attached = (wafv2.get_web_acl_for_resource(ResourceArn=alb_arn) or {}).get("WebACL") or {}
    if attached.get("Name") != name:
        return ""
    wafv2.disassociate_web_acl(ResourceArn=alb_arn)
    try:
        delete_web_acl(wafv2, name, sleep=sleep)
    except Exception as exc:  # noqa: BLE001
        logger.warning("지역 웹 ACL 삭제 실패(떼어 냄): %s", error_message(exc))
        return (f"{reason} — 웹 ACL {name} 을 로드밸런서에서 뗐지만 지우지 못했습니다({error_message(exc)}). "
                "「AWS 리소스 정리」에서 지우세요(남겨 두면 매달 요금).")
    return f"{reason} — 웹 ACL {name} 을 떼고 지웠습니다."


def delete_web_acl(wafv2: Any, name: str, scope: str = REGIONAL, *, sleep: Callable[[float], None] = time.sleep,
                   attempts: int = 6) -> bool:
    acl = find_web_acl(wafv2, name, scope)
    if acl is None:
        return False
    for i in range(attempts):
        lock = wafv2.get_web_acl(Name=name, Scope=scope, Id=acl["Id"])["LockToken"]
        try:
            wafv2.delete_web_acl(Name=name, Scope=scope, Id=acl["Id"], LockToken=lock)
            return True
        except Exception as exc:  # noqa: BLE001
            if error_code(exc) not in ("WAFAssociatedItemException", "WAFOptimisticLockException") or i == attempts - 1:
                raise
            sleep(10)
    return False


# ---------------------------------------------------------------------------
# 2.0.31 원본 잠금 — CloudFront 가 붙인 비밀 머리글이 있는 요청만 앱으로
# ---------------------------------------------------------------------------


def _rules(elbv2: Any, listener_arn: str) -> list[dict]:
    return elbv2.describe_rules(ListenerArn=listener_arn).get("Rules", [])


def _header_condition(rule: dict) -> dict | None:
    for cond in rule.get("Conditions", []) or []:
        if cond.get("Field") == "http-header" and \
                str((cond.get("HttpHeaderConfig") or {}).get("HttpHeaderName", "")).lower() == ORIGIN_HEADER.lower():
            return cond
    return None


def origin_rule(elbv2: Any, listener_arn: str) -> dict | None:
    return next((r for r in _rules(elbv2, listener_arn) if _header_condition(r)), None)


def _free_priority(rules: list[dict]) -> int:
    used = {int(r["Priority"]) for r in rules if str(r.get("Priority", "")).isdigit()}
    return next(p for p in range(ORIGIN_RULE_PRIORITY, 50001) if p not in used)


def is_locked(elbv2: Any, listener_arn: str) -> bool:
    listener = (elbv2.describe_listeners(ListenerArns=[listener_arn]).get("Listeners") or [{}])[0]
    return (listener.get("DefaultActions") or [{}])[0].get("Type") == "fixed-response"


def _forward(tg_arn: str) -> list[dict]:
    return [{"Type": "forward", "TargetGroupArn": tg_arn}]


def _forwards_to(actions: list[dict], tg_arn: str) -> bool:
    return [a.get("TargetGroupArn") for a in actions or [] if a.get("Type") == "forward"] == [tg_arn] and len(actions) == 1


def lock_origin(elbv2: Any, listener_arn: str, tg_arn: str, secret: str, *, also: Optional[list[str]] = None) -> None:
    """머리글 규칙을 맞추고, 기본 동작을 403 으로. 이미 잠겨 있으면 값·대상만 맞춘다.

    also: 함께 받아 줄 값(머리글을 새 값으로 바꾸는 동안 옛 값도 통과 — 값 목록은 OR)."""
    if not secret:
        raise InfraError("원본 잠금 머리글 값이 비었습니다(내부 오류).")
    values = list(dict.fromkeys([secret, *(also or [])]))[:5]
    conditions = [{"Field": "http-header", "HttpHeaderConfig": {"HttpHeaderName": ORIGIN_HEADER, "Values": values}}]
    rules = _rules(elbv2, listener_arn)
    rule = next((r for r in rules if _header_condition(r)), None)
    if rule is None:
        #: 사용자가 만든 규칙과 우선순위가 겹치지 않게 비어 있는 번호를 쓴다.
        elbv2.create_rule(ListenerArn=listener_arn, Priority=_free_priority(rules), Conditions=conditions,
                          Actions=_forward(tg_arn))
    else:
        current = ((_header_condition(rule) or {}).get("HttpHeaderConfig") or {}).get("Values") or []
        if current != values or not _forwards_to(rule.get("Actions", []), tg_arn):
            elbv2.modify_rule(RuleArn=rule["RuleArn"], Conditions=conditions, Actions=_forward(tg_arn))
    elbv2.modify_listener(ListenerArn=listener_arn, DefaultActions=[DENY_ACTION])


def widen_origin_lock(elbv2: Any, listener_arn: str, tg_arn: str, secret: str) -> bool:
    """이미 잠긴 리스너에 새 머리글 값을 **더한다**(옛 값 유지). CloudFront 가 새 값을 퍼뜨리는 동안 403 이 나지 않게.
    잠겨 있지 않으면 아무것도 하지 않고 False."""
    if not is_locked(elbv2, listener_arn):
        return False
    rule = origin_rule(elbv2, listener_arn)
    old = ((_header_condition(rule) or {}).get("HttpHeaderConfig") or {}).get("Values") or [] if rule else []
    if secret in old:
        return True
    lock_origin(elbv2, listener_arn, tg_arn, secret, also=list(old))
    return True


def unlock_origin(elbv2: Any, listener_arn: str, tg_arn: str) -> bool:
    """HTTPS 주소를 끈 배포 — 기본 동작을 다시 앱으로, 머리글 규칙은 지운다. 바꿨으면 True."""
    changed = False
    rule = origin_rule(elbv2, listener_arn)
    listener = (elbv2.describe_listeners(ListenerArns=[listener_arn]).get("Listeners") or [{}])[0]
    if not _forwards_to(listener.get("DefaultActions", []), tg_arn):
        elbv2.modify_listener(ListenerArn=listener_arn, DefaultActions=_forward(tg_arn))
        changed = True
    if rule is not None:
        elbv2.delete_rule(RuleArn=rule["RuleArn"])
        changed = True
    return changed


# ---------------------------------------------------------------------------
# 2.0.35 원본 구간 HTTPS — 443 리스너(80 과 같은 잠금)
# ---------------------------------------------------------------------------


def allow_https_from_cloudfront(ec2: Any, group_id: str, prefix_list: str) -> None:
    """ALB 보안 그룹에 CloudFront 원본 전용 목록 → 443 을 더한다(이미 있으면 그대로)."""
    try:
        ec2.authorize_security_group_ingress(GroupId=group_id, IpPermissions=[{
            "IpProtocol": "tcp", "FromPort": HTTPS_LISTENER_PORT, "ToPort": HTTPS_LISTENER_PORT,
            "PrefixListIds": [{"PrefixListId": prefix_list, "Description": "CloudFront origin-facing only"}]}])
    except Exception as exc:  # noqa: BLE001
        if error_code(exc) != "InvalidPermission.Duplicate":
            raise


def https_listener(elbv2: Any, alb_arn: str) -> dict | None:
    listeners = elbv2.describe_listeners(LoadBalancerArn=alb_arn).get("Listeners", [])
    return next((l for l in listeners if int(l.get("Port", 0)) == HTTPS_LISTENER_PORT), None)


def ensure_https_listener(elbv2: Any, *, alb_arn: str, tg_arn: str, certificate_arn: str, http_listener_arn: str,
                          tags: Optional[dict] = None) -> str:
    """443 리스너를 80 리스너와 **같은 동작**으로 맞춘다 — 80 이 잠겨 있으면 443 도 기본 403 + 같은 머리글 규칙.
    CloudFront 가 원본을 443 으로 바꾸는 순간에도 잠금이 비지 않게, 규칙을 먼저 만든 뒤에 돌려준다."""
    http = (elbv2.describe_listeners(ListenerArns=[http_listener_arn]).get("Listeners") or [{}])[0]
    locked = (http.get("DefaultActions") or [{}])[0].get("Type") == "fixed-response"
    default = [DENY_ACTION] if locked else _forward(tg_arn)
    certs = [{"CertificateArn": certificate_arn}]
    current = https_listener(elbv2, alb_arn)
    if current is None:
        tag_list = _tag_list(tags)
        current = elbv2.create_listener(LoadBalancerArn=alb_arn, Port=HTTPS_LISTENER_PORT, Protocol="HTTPS",
                                        SslPolicy=SSL_POLICY, Certificates=certs, DefaultActions=default,
                                        **({"Tags": tag_list} if tag_list else {}))["Listeners"][0]
    else:
        have = [c.get("CertificateArn") for c in current.get("Certificates") or []]
        if have[:1] != [certificate_arn] or current.get("SslPolicy") != SSL_POLICY:
            elbv2.modify_listener(ListenerArn=current["ListenerArn"], Protocol="HTTPS", Port=HTTPS_LISTENER_PORT,
                                  SslPolicy=SSL_POLICY, Certificates=certs)
    arn = str(current["ListenerArn"])
    if locked:
        rule = origin_rule(elbv2, http_listener_arn)
        values = ((_header_condition(rule) or {}).get("HttpHeaderConfig") or {}).get("Values") or [] if rule else []
        if values:
            lock_origin(elbv2, arn, tg_arn, values[0], also=list(values[1:]))
    elif not _forwards_to(current.get("DefaultActions", []), tg_arn) and \
            (current.get("DefaultActions") or [{}])[0].get("Type") != "fixed-response":
        elbv2.modify_listener(ListenerArn=arn, DefaultActions=default)
    return arn


# ---------------------------------------------------------------------------
# 전체
# ---------------------------------------------------------------------------


def ensure_edge(
    ec2: Any, elbv2: Any, wafv2: Any, *,
    service: str, vpc_id: str, subnet_ids: list[str], app_port: int, health_path: str = "/health",
    waf: bool = True, rate_limit: int = DEFAULT_RATE_LIMIT, window: int = DEFAULT_WINDOW,
    tags: Optional[dict] = None, sleep: Callable[[float], None] = time.sleep, cdn: bool = False,
) -> EdgeResult:
    """ALB + 대상 그룹 + 리스너(80) (+ WAF) 를 확보한다. 실패하면 이 호출이 새로 만든 것만 되돌린다."""
    if not vpc_id:
        raise InfraError("로드밸런서를 만들 VPC 를 확인하지 못했습니다.")
    name = edge_name(service)
    created: list[tuple[str, str]] = []
    notes: list[str] = []
    try:
        subnets = alb_subnets(ec2, vpc_id, list(subnet_ids))
        if cdn:
            #: 2.0.27 HTTPS 주소 — ALB 는 CloudFront 에서 오는 연결만 받는다.
            try:
                from core import aws_cdn  # type: ignore
            except ImportError:  # pragma: no cover
                import aws_cdn  # type: ignore
            alb_sg = ensure_security_group(ec2, vpc_id=vpc_id, name=f"{service}-alb-cf-sg", port=LISTENER_PORT,
                                           description="ReCoder load balancer (CloudFront only)", tags=tags,
                                           prefix_list=aws_cdn.cloudfront_prefix_list(ec2))
        else:
            alb_sg = ensure_security_group(ec2, vpc_id=vpc_id, name=f"{service}-alb-sg", port=LISTENER_PORT,
                                           description="ReCoder load balancer (public HTTP)", tags=tags)
        task_sg = ensure_security_group(ec2, vpc_id=vpc_id, name=f"{service}-task-sg", port=app_port,
                                        description="ReCoder ECS tasks behind the load balancer",
                                        tags=tags, source_group=alb_sg)

        lb = _find_load_balancer(elbv2, name)
        if lb is None:
            try:
                lb = elbv2.create_load_balancer(
                    Name=name, Subnets=subnets, SecurityGroups=[alb_sg], Scheme="internet-facing",
                    Type="application", IpAddressType="ipv4", Tags=_tag_list(tags),
                )["LoadBalancers"][0]
            except Exception as exc:  # noqa: BLE001
                raise InfraError("로드밸런서를 만들지 못했습니다.", detail=error_message(exc),
                                 remedy="권한표에 2.0.26 의 elasticloadbalancing 권한이 있는지 확인하세요"
                                        "(권한 템플릿을 다시 적용).") from exc
            created.append(("alb", str(lb["LoadBalancerArn"])))
            logger.info("ALB 생성: %s", name)
        elif lb.get("VpcId") and lb.get("VpcId") != vpc_id:
            raise InfraError(f"같은 이름의 로드밸런서({name})가 다른 VPC 에 있습니다.",
                             remedy="그 로드밸런서를 지우거나 서비스 이름을 바꿔 배포하세요.")
        elif list(lb.get("SecurityGroups") or []) != [alb_sg]:
            #: HTTPS 주소를 켜거나 끄면 ALB 가 받는 곳(인터넷 전체 ↔ CloudFront 만)을 바꾼다.
            elbv2.set_security_groups(LoadBalancerArn=lb["LoadBalancerArn"], SecurityGroups=[alb_sg])
            notes.append("로드밸런서가 " + ("CloudFront 에서 오는 연결만" if cdn else "인터넷 전체에서 오는 연결을") + " 받게 바꿨습니다.")
        alb_arn = str(lb["LoadBalancerArn"])

        tg = _find_target_group(elbv2, name)
        if tg is None:
            tg = elbv2.create_target_group(
                Name=name, Protocol="HTTP", Port=int(app_port), VpcId=vpc_id, TargetType="ip",
                Tags=_tag_list(tags), **_health_check(health_path),
            )["TargetGroups"][0]
            created.append(("tg", str(tg["TargetGroupArn"])))
            # 새 버전으로 바꿀 때 옛 태스크를 5분(기본) 붙잡지 않는다.
            elbv2.modify_target_group_attributes(
                TargetGroupArn=tg["TargetGroupArn"],
                Attributes=[{"Key": "deregistration_delay.timeout_seconds", "Value": "30"}])
        else:
            if tg.get("VpcId") != vpc_id or tg.get("TargetType") != "ip":
                raise InfraError(f"같은 이름의 대상 그룹({name})이 다른 VPC 에 있거나 IP 대상이 아닙니다.",
                                 remedy="그 대상 그룹을 지운 뒤 다시 배포하세요.")
            if tg.get("HealthCheckPath") != (health_path or "/"):
                elbv2.modify_target_group(TargetGroupArn=tg["TargetGroupArn"], **_health_check(health_path))
        tg_arn = str(tg["TargetGroupArn"])

        listeners = elbv2.describe_listeners(LoadBalancerArn=alb_arn).get("Listeners", [])
        http = next((l for l in listeners if int(l.get("Port", 0)) == LISTENER_PORT), None)
        forward = _forward(tg_arn)
        if http is None:
            http = elbv2.create_listener(LoadBalancerArn=alb_arn, Port=LISTENER_PORT, Protocol="HTTP",
                                         DefaultActions=forward, Tags=_tag_list(tags))["Listeners"][0]
            created.append(("listener", str(http["ListenerArn"])))
        elif cdn and (http.get("DefaultActions") or [{}])[0].get("Type") == "fixed-response":
            #: 원본 잠금이 걸린 리스너 — 잠금은 그대로 두고 머리글 규칙의 대상만 맞춘다(_provision_cdn 이 값을 맞춘다).
            rule = origin_rule(elbv2, str(http["ListenerArn"]))
            if rule is None:
                elbv2.modify_listener(ListenerArn=http["ListenerArn"], DefaultActions=forward)
            elif not _forwards_to(rule.get("Actions", []), tg_arn):
                elbv2.modify_rule(RuleArn=rule["RuleArn"], Actions=forward)
        elif not cdn and (http.get("DefaultActions") or [{}])[0].get("Type") == "fixed-response":
            # 잠긴 리스너만 규칙을 조회한다(옛 권한표로 하는 평소 재배포는 DescribeRules 없이 그대로 돈다).
            if unlock_origin(elbv2, str(http["ListenerArn"]), tg_arn):
                notes.append("HTTPS 주소를 꺼 로드밸런서가 다시 모든 요청을 앱으로 넘깁니다.")
        elif not _forwards_to(http.get("DefaultActions", []), tg_arn):
            elbv2.modify_listener(ListenerArn=http["ListenerArn"], DefaultActions=forward)

        acl_arn = ""
        if cdn:
            #: 2.0.31 — CloudFront 뒤의 ALB 에서는 모든 요청이 CloudFront 엣지 주소로 보여 IP 당 제한이 엣지 단위로 묶인다.
            #: WAF 는 CloudFront 에(전역 범위) 붙인다 — 예전에 ALB 에 붙여 둔 지역 웹 ACL 은 CloudFront 쪽을 붙인 **뒤에**
            #: ecs_agent._provision_cdn 이 뗀다(그 사이 보호가 비지 않게). WAF 를 끈 배포면 여기서 바로 뗀다.
            if not waf and not any(k == "alb" for k, _ in created):
                note = detach_recoder_acl(wafv2, alb_arn, name, sleep=sleep)
                if note:
                    notes.append(note)
        elif waf:
            acl_arn, made = ensure_web_acl(wafv2, name, rate_limit=rate_limit, window=window, tags=tags)
            if made:
                created.append(("waf", name))
            already = ""
            if not any(k == "alb" for k, _ in created):
                already = str(((wafv2.get_web_acl_for_resource(ResourceArn=alb_arn) or {}).get("WebACL") or {})
                              .get("ARN") or "")
            if already != acl_arn:
                associate(wafv2, acl_arn, alb_arn, sleep=sleep)
        elif not any(k == "alb" for k, _ in created):
            note = detach_recoder_acl(wafv2, alb_arn, name, sleep=sleep)
            if note:
                notes.append(note)

        return EdgeResult(name=name, alb_arn=alb_arn, dns_name=str(lb.get("DNSName") or ""), zone_id=str(lb.get("CanonicalHostedZoneId") or ""),
                          target_group_arn=tg_arn, listener_arn=str(http["ListenerArn"]),
                          alb_security_group=alb_sg, task_security_group=task_sg, web_acl_arn=acl_arn,
                          created=[k for k, _ in created], notes=notes)
    except Exception as exc:  # noqa: BLE001
        leftovers = rollback(elbv2, wafv2, created, sleep=sleep)
        if isinstance(exc, InfraError):
            if leftovers:
                exc.detail = "\n".join(filter(None, [exc.detail, *leftovers]))
            raise
        raise InfraError("대규모 방패(로드밸런서·WAF)를 준비하지 못했습니다.",
                         detail="\n".join([error_message(exc), *leftovers]),
                         remedy="권한표에 2.0.26 의 elasticloadbalancing·wafv2 권한이 있는지 확인하세요"
                                "(권한 템플릿을 다시 적용). 대규모 방패를 끄면 지금 권한으로 배포됩니다.") from exc


def rollback(elbv2: Any, wafv2: Any, created: list[tuple[str, str]], *,
             sleep: Callable[[float], None] = time.sleep) -> list[str]:
    """이 호출이 만든 것만 역순으로 지운다. 지우지 못한 것은 문장으로 돌려준다(정리 기능 안내)."""
    left: list[str] = []
    for kind, ref in reversed(created):
        try:
            if kind == "waf":
                delete_web_acl(wafv2, ref, sleep=sleep)
            elif kind == "listener":
                elbv2.delete_listener(ListenerArn=ref)
            elif kind == "alb":
                elbv2.delete_load_balancer(LoadBalancerArn=ref)
            elif kind == "tg":
                # ALB 가 지워지는 동안은 대상 그룹이 "사용 중" — 잠깐 기다려 다시.
                for i in range(6):
                    try:
                        elbv2.delete_target_group(TargetGroupArn=ref)
                        break
                    except Exception as exc:  # noqa: BLE001
                        if error_code(exc) != "ResourceInUse" or i == 5:
                            raise
                        sleep(5)
        except Exception as exc:  # noqa: BLE001
            left.append(f"되돌리지 못함({kind} {ref}): {error_message(exc)} — 「AWS 리소스 정리」에서 지우세요.")
    return left
