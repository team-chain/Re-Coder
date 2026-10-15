"""
ReCoder Core — HTTPS 주소: 로드밸런서 앞 CloudFront (+ 선택: 내 도메인 · ACM 인증서) (2.0.27)

도메인이 없어도 `https://xxxx.cloudfront.net` 으로 바로 HTTPS 가 된다.

  사용자 ─HTTPS→ CloudFront ─HTTP→ ALB(CloudFront 주소에서만 받음) → 방패(nginx) → 앱

  · 캐시는 끈다(CachingDisabled) — 생성 앱은 로그인·장바구니처럼 사용자마다 다른 응답이 많다.
  · 사용자 머리글·쿠키·쿼리를 모두 넘기고(AllViewerAndCloudFrontHeaders), CloudFront 가 사용자 IP(CloudFront-Viewer-Address)와
    원래 프로토콜(CloudFront-Forwarded-Proto)을 붙인다 — 방패가 이것으로 IP 별 제한을 건다.
  · ALB 보안 그룹은 AWS 관리 목록(com.amazonaws.global.cloudfront.origin-facing)에서 오는 연결만 받는다 — ALB 를 직접 부르며
    CloudFront 머리글을 위조하는 길을 막는다.
  · 도메인을 주면 us-east-1 에 ACM 인증서를 DNS 검증으로 요청한다. Route 53 에 그 도메인 영역이 있으면 검증 레코드와 별칭을
    자동으로 넣고, 없으면 넣을 레코드를 알려 준다. 발급 전에는 CloudFront 기본 주소로 서비스하고, 발급된 뒤 다시 배포하면 도메인을 붙인다.
"""
from __future__ import annotations

import hashlib
import logging
import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

try:
    from core.aws_infra import InfraError, error_code, error_message  # type: ignore
except ImportError:  # pragma: no cover
    from aws_infra import InfraError, error_code, error_message  # type: ignore

logger = logging.getLogger(__name__)

#: AWS 관리 정책(고정 ID) — https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/using-managed-cache-policies.html
CACHING_DISABLED = "4135ea2d-6df8-44a3-9df3-4b5a84be39ad"
ALL_VIEWER_AND_CLOUDFRONT_HEADERS = "33f36d7e-f396-46d9-90e0-52428a34d9dc"
#: CloudFront 별칭 레코드의 고정 호스티드 영역 ID.
CLOUDFRONT_ZONE_ID = "Z2FDTNDATAQYW2"
ORIGIN_FACING_LIST = "com.amazonaws.global.cloudfront.origin-facing"
#: 한국·일본·아시아 엣지를 포함하는 가격 등급(PriceClass_100 은 북미·유럽만).
PRICE_CLASS = "PriceClass_200"
#: 2.0.31 원본 잠금 머리글(aws_edge.ORIGIN_HEADER 와 같아야 한다).
ORIGIN_HEADER = "X-Recoder-Origin"
COST_NOTE = "CloudFront 는 나가는 데이터·요청 수만큼 요금이 나옵니다(작은 앱은 매달 몇 달러 이하, 1년 무료 사용량 있음)."


def comment_for(name: str, region: str = "") -> str:
    """배포를 찾는 표시. 2.0.32 부터 리전을 붙인다 — 같은 서비스 이름을 두 리전에 배포하면 한 배포를 서로 덮어썼다."""
    return f"recoder:{name}@{region}" if region else f"recoder:{name}"


def origin_region(domain: str) -> str:
    """ALB 주소(xxx.<리전>.elb.amazonaws.com)의 리전."""
    m = re.search(r"\.([a-z]{2}(?:-gov)?-[a-z]+-\d)\.elb\.amazonaws\.com\.?$", str(domain or ""))
    return m.group(1) if m else ""


def item_region(item: dict) -> str:
    """목록 항목(list_distributions)의 리전 — 표시(@리전) 또는 원본 ALB 주소에서."""
    comment = str(item.get("Comment", ""))
    if "@" in comment:
        return comment.rsplit("@", 1)[-1]
    origins = (item.get("Origins") or {}).get("Items") or []
    return origin_region(origins[0].get("DomainName", "")) if origins else ""


@dataclass
class CdnResult:
    distribution_id: str
    domain: str
    status: str
    created: bool
    alias: str = ""
    notes: list[str] = field(default_factory=list)
    #: 원본 잠금 머리글 값과, 그 값이 이미 전 세계 CloudFront 에 퍼져 있는가(새로 만든 배포 또는 전에 넣어 둔 값).
    origin_secret: str = field(default="", repr=False)
    origin_secret_live: bool = False
    web_acl_id: str = ""
    #: 요청한 웹 ACL 이 이미 전 세계에 적용돼 있는가(새 배포 또는 이번 배포 전부터 같은 값 + Deployed).
    web_acl_live: bool = False

    @property
    def url(self) -> str:
        return f"https://{self.alias or self.domain}"


def cloudfront_prefix_list(ec2: Any) -> str:
    lists = ec2.describe_managed_prefix_lists(
        Filters=[{"Name": "prefix-list-name", "Values": [ORIGIN_FACING_LIST]}]).get("PrefixLists", [])
    if not lists:
        raise InfraError("CloudFront 주소 목록(AWS 관리 접두사 목록)을 찾지 못했습니다.",
                         remedy="이 리전에서 CloudFront 원본 보호를 쓸 수 없습니다. HTTPS 주소를 끄고 배포하세요.")
    return str(lists[0]["PrefixListId"])


def _find(cloudfront: Any, comment: str, legacy: str = "", region: str = "") -> Optional[dict]:
    """표시(comment)가 같은 배포. 없으면 옛 표시(legacy, 리전 없음) 중 원본 ALB 가 같은 리전인 것."""
    marker = ""
    fallback: Optional[dict] = None
    while True:
        kwargs = {"Marker": marker} if marker else {}
        page = cloudfront.list_distributions(**kwargs).get("DistributionList", {})
        for item in page.get("Items", []) or []:
            if item.get("Comment") == comment:
                return item
            c = str(item.get("Comment", ""))
            if legacy and fallback is None and (c == legacy or (not region and c.startswith(legacy + "@"))) and \
                    (not region or item_region(item) in ("", region)):
                fallback = item
        if not page.get("IsTruncated"):
            return fallback
        marker = page.get("NextMarker", "")


def _origin_header(cfg: dict) -> str:
    """배포 설정의 원본 잠금 머리글 값(없으면 "")."""
    for origin in (cfg.get("Origins") or {}).get("Items") or []:
        for h in (origin.get("CustomHeaders") or {}).get("Items") or []:
            if str(h.get("HeaderName", "")).lower() == ORIGIN_HEADER.lower():
                return str(h.get("HeaderValue", ""))
    return ""


def new_origin_secret() -> str:
    return secrets.token_urlsafe(32)


def _config(name: str, origin: str, *, alias: str = "", certificate_arn: str = "", origin_secret: str = "",
            web_acl_id: str = "", region: str = "") -> dict:
    cfg: dict = {
        "CallerReference": f"{name}-{int(time.time())}-{secrets.token_hex(4)}",
        "Comment": comment_for(name, region),
        "Enabled": True,
        "Origins": {"Quantity": 1, "Items": [{
            "Id": "recoder-alb", "DomainName": origin,
            "CustomOriginConfig": {"HTTPPort": 80, "HTTPSPort": 443, "OriginProtocolPolicy": "http-only",
                                   "OriginSslProtocols": {"Quantity": 1, "Items": ["TLSv1.2"]},
                                   "OriginReadTimeout": 60, "OriginKeepaliveTimeout": 5},
            "CustomHeaders": ({"Quantity": 1, "Items": [{"HeaderName": ORIGIN_HEADER, "HeaderValue": origin_secret}]}
                              if origin_secret else {"Quantity": 0}),
        }]},
        "DefaultCacheBehavior": {
            "TargetOriginId": "recoder-alb",
            "ViewerProtocolPolicy": "redirect-to-https",
            "AllowedMethods": {"Quantity": 7, "Items": ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"],
                               "CachedMethods": {"Quantity": 2, "Items": ["GET", "HEAD"]}},
            "CachePolicyId": CACHING_DISABLED,
            "OriginRequestPolicyId": ALL_VIEWER_AND_CLOUDFRONT_HEADERS,
            "Compress": True,
        },
        "PriceClass": PRICE_CLASS,
        "HttpVersion": "http2and3",
        "ViewerCertificate": {"CloudFrontDefaultCertificate": True},
        "WebACLId": web_acl_id or "",
    }
    if alias and certificate_arn:
        cfg["Aliases"] = {"Quantity": 1, "Items": [alias]}
        cfg["ViewerCertificate"] = {"ACMCertificateArn": certificate_arn, "SSLSupportMethod": "sni-only",
                                    "MinimumProtocolVersion": "TLSv1.2_2021"}
    return cfg


def ensure_distribution(cloudfront: Any, *, name: str, origin: str, tags: Optional[dict] = None,
                        alias: str = "", certificate_arn: str = "", web_acl_id: Optional[str] = None,
                        region: str = "") -> CdnResult:
    """ALB 를 원본으로 하는 배포를 확보한다. 있으면 원본·도메인·원본 잠금 머리글·WAF 만 맞춘다.

    web_acl_id: CloudFront 범위 웹 ACL ARN, "" 은 떼기, None 은 그대로 두기."""
    region = region or origin_region(origin)
    existing = _find(cloudfront, comment_for(name, region), legacy=comment_for(name), region=region)
    if existing is None:
        secret = new_origin_secret()
        try:
            dist = cloudfront.create_distribution_with_tags(DistributionConfigWithTags={
                "DistributionConfig": _config(name, origin, alias=alias, certificate_arn=certificate_arn,
                                              origin_secret=secret, web_acl_id=web_acl_id or "", region=region),
                "Tags": {"Items": [{"Key": k, "Value": v} for k, v in (tags or {}).items()]},
            })["Distribution"]
        except Exception as exc:  # noqa: BLE001
            raise InfraError("HTTPS 주소(CloudFront)를 만들지 못했습니다.", detail=error_message(exc),
                             remedy="권한표에 2.0.27 의 cloudfront 권한이 있는지 확인하세요(권한 템플릿 스택 업데이트).") from exc
        return CdnResult(str(dist["Id"]), str(dist["DomainName"]), str(dist.get("Status", "")), True,
                         alias=alias if certificate_arn else "", origin_secret=secret, origin_secret_live=True,
                         web_acl_id=web_acl_id or "", web_acl_live=True)
    current = cloudfront.get_distribution_config(Id=existing["Id"])
    cfg = current["DistributionConfig"]
    secret = _origin_header(cfg)
    #: 머리글이 있어도 배포가 아직 퍼지는 중(InProgress)이면 일부 엣지는 머리글 없이 보낸다 — 퍼진 뒤에만 '살아 있음'.
    deployed = str(existing.get("Status", "")) == "Deployed"
    live = bool(secret) and deployed
    acl_before = str(cfg.get("WebACLId") or "")
    secret = secret or new_origin_secret()
    want = _config(name, origin, alias=alias, certificate_arn=certificate_arn, origin_secret=secret,
                   web_acl_id=web_acl_id or "", region=region)
    changed = False
    if region and cfg.get("Comment") != want["Comment"]:
        cfg["Comment"] = want["Comment"]  # 옛 표시(리전 없음)를 이번에 리전 표시로 바꾼다
        changed = True
    first = cfg["Origins"]["Items"][0]
    if first.get("DomainName") != origin or _origin_header(cfg) != secret:
        # 원본을 통째로 맞춘다(사람이 더한 다른 머리글은 남긴다).
        others = [h for h in (first.get("CustomHeaders") or {}).get("Items") or []
                  if str(h.get("HeaderName", "")).lower() != ORIGIN_HEADER.lower()]
        fresh = want["Origins"]["Items"][0]
        items = others + fresh["CustomHeaders"]["Items"]
        fresh["CustomHeaders"] = {"Quantity": len(items), "Items": items}
        cfg["Origins"] = {"Quantity": 1, "Items": [fresh]}
        changed = True
    if certificate_arn and (cfg.get("Aliases", {}).get("Items") or []) != [alias]:
        cfg["Aliases"], cfg["ViewerCertificate"] = want["Aliases"], want["ViewerCertificate"]
        changed = True
    if web_acl_id == "" and acl_before and "/webacl/recoder-" not in acl_before:
        web_acl_id = None  # 사용자가 직접 붙인 웹 ACL 은 떼지 않는다
    if web_acl_id is not None and acl_before != web_acl_id:
        cfg["WebACLId"] = web_acl_id
        changed = True
    if not cfg.get("Enabled"):
        cfg["Enabled"] = True
        changed = True
    if changed:
        cloudfront.update_distribution(Id=existing["Id"], IfMatch=current["ETag"], DistributionConfig=cfg)
    aliases = cfg.get("Aliases", {}).get("Items") or []
    return CdnResult(str(existing["Id"]), str(existing["DomainName"]), "InProgress" if changed else str(existing.get("Status", "")),
                     False, alias=aliases[0] if aliases else "", origin_secret=secret, origin_secret_live=live,
                     web_acl_id=str(cfg.get("WebACLId") or ""),
                     web_acl_live=deployed and web_acl_id is not None and acl_before == web_acl_id)


# ---------------------------------------------------------------------------
# 내 도메인 — ACM 인증서(us-east-1) + Route 53
# ---------------------------------------------------------------------------


def _zone_for(route53: Any, domain: str) -> Optional[dict]:
    """도메인을 담는 공개 호스티드 영역(가장 긴 일치)."""
    parts = domain.rstrip(".").split(".")
    for i in range(len(parts) - 1):
        candidate = ".".join(parts[i:]) + "."
        # 같은 이름의 사설 영역이 공개 영역보다 먼저 나올 수 있다 — 몇 개를 받아 공개 영역을 고른다.
        zones = route53.list_hosted_zones_by_name(DNSName=candidate, MaxItems="10").get("HostedZones", [])
        public = [z for z in zones if z["Name"] == candidate and not z.get("Config", {}).get("PrivateZone")]
        if public:
            return public[0]
    return None


def _upsert(route53: Any, zone_id: str, record: dict) -> None:
    route53.change_resource_record_sets(HostedZoneId=zone_id, ChangeBatch={
        "Comment": "ReCoder", "Changes": [{"Action": "UPSERT", "ResourceRecordSet": record}]})


@dataclass
class CertificateState:
    arn: str
    status: str
    validation: Optional[dict] = None
    zone_id: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def issued(self) -> bool:
        return self.status == "ISSUED"


def ensure_certificate(acm: Any, route53: Any, domain: str, *, tags: Optional[dict] = None,
                       sleep: Callable[[float], None] = time.sleep) -> CertificateState:
    """`acm` 은 반드시 us-east-1 클라이언트(CloudFront 인증서 규칙). 발급을 기다리지 않는다."""
    arn = ""
    for status in (["ISSUED"], ["PENDING_VALIDATION"]):
        token = ""
        while not arn:  # 인증서가 많으면 여러 쪽 — 첫 쪽만 보면 같은 도메인 인증서를 또 요청한다
            page = acm.list_certificates(CertificateStatuses=status, **({"NextToken": token} if token else {}))
            arn = next((str(c["CertificateArn"]) for c in page.get("CertificateSummaryList", [])
                        if c.get("DomainName") == domain), "")
            token = page.get("NextToken") or ""
            if not token:
                break
        if arn:
            break
    if not arn:
        tag_list = [{"Key": k, "Value": v} for k, v in (tags or {}).items()]
        arn = str(acm.request_certificate(DomainName=domain, ValidationMethod="DNS",
                                          **({"Tags": tag_list} if tag_list else {}),
                                          # 도메인마다 다른 값(영문·숫자 32자) — 기호만 지우면 a-b.com 과 ab.com 이 겹친다.
                                          IdempotencyToken=hashlib.sha1(domain.lower().encode("utf-8")).hexdigest()[:32])["CertificateArn"])
    record = None
    cert: dict = {}
    for _ in range(6):  # 검증 레코드는 요청 직후 몇 초 뒤에 나타난다
        cert = acm.describe_certificate(CertificateArn=arn)["Certificate"]
        options = cert.get("DomainValidationOptions") or []
        record = options[0].get("ResourceRecord") if options else None
        if record or cert.get("Status") == "ISSUED":
            break
        sleep(5)
    state = CertificateState(arn=arn, status=str(cert.get("Status", "")), validation=record)
    try:
        zone = _zone_for(route53, domain)
    except Exception as exc:  # noqa: BLE001 - Route 53 권한·사용 없음 → 사람이 넣는다
        zone = None
        state.notes.append(f"Route 53 을 확인하지 못했습니다({error_message(exc)}) — DNS 레코드는 직접 넣으세요.")
    if zone:
        state.zone_id = str(zone["Id"]).rsplit("/", 1)[-1]
        if record and not state.issued:
            try:
                _upsert(route53, state.zone_id, {"Name": record["Name"], "Type": record["Type"], "TTL": 300,
                                                 "ResourceRecords": [{"Value": record["Value"]}]})
                state.notes.append("Route 53 에 인증서 검증 레코드를 넣었습니다 — 보통 수 분 안에 발급됩니다. 발급 뒤 다시 배포하면 도메인이 연결됩니다.")
            except Exception as exc:  # noqa: BLE001 - 배포는 CloudFront 기본 주소로 계속
                state.notes.append(f"Route 53 에 검증 레코드를 넣지 못했습니다({error_message(exc)}) — 도메인 DNS 에 직접 넣으세요: "
                                   f"{record['Name']} CNAME {record['Value']}")
    elif record and not state.issued:
        state.notes.append(f"도메인 DNS 에 검증 레코드를 넣으세요: {record['Name']} CNAME {record['Value']} — 발급된 뒤 다시 배포하면 도메인이 연결됩니다.")
    return state


def _existing_record(route53: Any, zone_id: str, domain: str, kind: str) -> Optional[dict]:
    name = domain.rstrip(".").lower() + "."
    sets = route53.list_resource_record_sets(HostedZoneId=zone_id, StartRecordName=name, StartRecordType=kind,
                                             MaxItems="1").get("ResourceRecordSets", [])
    if sets and str(sets[0].get("Name", "")).lower() == name and sets[0].get("Type") == kind:
        return sets[0]
    return None


def point_domain(route53: Any, zone_id: str, domain: str, distribution_domain: str) -> list[str]:
    """Route 53 별칭 A·AAAA → CloudFront. 다른 곳을 가리키는 레코드(사용자가 만든 서버 주소 등)는 덮어쓰지 않는다.
    돌려주는 것은 사람이 읽을 안내(비면 모두 연결함)."""
    notes: list[str] = []
    for kind in ("A", "AAAA"):
        try:
            current = _existing_record(route53, zone_id, domain, kind)
        except Exception as exc:  # noqa: BLE001
            if error_code(exc) not in ("AccessDenied", "AccessDeniedException"):
                raise
            # 옛 권한표(ListResourceRecordSets 없음) — 2.0.30 처럼 그대로 넣는다.
            logger.info("Route 53 레코드 확인 권한 없음 — 확인 없이 별칭을 넣습니다.")
            current = None
        target = str(((current or {}).get("AliasTarget") or {}).get("DNSName", "")).rstrip(".").lower()
        if current is not None and not target.endswith(".cloudfront.net"):
            what = target or ", ".join(r.get("Value", "") for r in current.get("ResourceRecords", []) or [])
            notes.append(f"{domain} 의 {kind} 레코드가 이미 다른 곳({what})을 가리켜 덮어쓰지 않았습니다 — "
                         f"CloudFront({distribution_domain})로 옮기려면 Route 53 에서 그 레코드를 별칭으로 바꾸세요.")
            continue
        _upsert(route53, zone_id, {"Name": domain, "Type": kind, "AliasTarget": {
            "HostedZoneId": CLOUDFRONT_ZONE_ID, "DNSName": distribution_domain, "EvaluateTargetHealth": False}})
    return notes


# ---------------------------------------------------------------------------
# 정리 — 비활성화(수 분) 뒤에야 지울 수 있다
# ---------------------------------------------------------------------------


def disable_or_delete(cloudfront: Any, distribution_id: str) -> str:
    """켜져 있으면 끄고(다음 정리 때 지움), 꺼진 채 배포가 끝났으면 지운다. 결과 문장."""
    current = cloudfront.get_distribution_config(Id=distribution_id)
    cfg = current["DistributionConfig"]
    if cfg.get("Enabled"):
        cfg["Enabled"] = False
        # 붙여 둔 CloudFront 범위 웹 ACL 도 떼어 같은 정리에서 지울 수 있게 한다.
        cfg["WebACLId"] = ""
        cloudfront.update_distribution(Id=distribution_id, IfMatch=current["ETag"], DistributionConfig=cfg)
        return "껐습니다 — CloudFront 가 끄는 데 15분쯤 걸려, 그 뒤 정리를 다시 실행하면 지웁니다."
    dist = cloudfront.get_distribution(Id=distribution_id)
    if dist["Distribution"].get("Status") != "Deployed":
        return "끄는 중입니다 — 잠시 뒤 정리를 다시 실행하면 지웁니다."
    cloudfront.delete_distribution(Id=distribution_id, IfMatch=dist["ETag"])
    return "지웠습니다."


def is_not_found(exc: BaseException) -> bool:
    return error_code(exc) in ("NoSuchDistribution", "ResourceNotFoundException")
