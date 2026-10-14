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

import logging
import re
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
COST_NOTE = "CloudFront 는 나가는 데이터·요청 수만큼 요금이 나옵니다(작은 앱은 매달 몇 달러 이하, 1년 무료 사용량 있음)."


def comment_for(name: str) -> str:
    return f"recoder:{name}"


@dataclass
class CdnResult:
    distribution_id: str
    domain: str
    status: str
    created: bool
    alias: str = ""
    notes: list[str] = field(default_factory=list)

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


def _find(cloudfront: Any, comment: str) -> Optional[dict]:
    marker = ""
    while True:
        kwargs = {"Marker": marker} if marker else {}
        page = cloudfront.list_distributions(**kwargs).get("DistributionList", {})
        for item in page.get("Items", []) or []:
            if item.get("Comment") == comment:
                return item
        if not page.get("IsTruncated"):
            return None
        marker = page.get("NextMarker", "")


def _config(name: str, origin: str, *, alias: str = "", certificate_arn: str = "") -> dict:
    cfg: dict = {
        "CallerReference": f"{name}-{int(time.time())}",
        "Comment": comment_for(name),
        "Enabled": True,
        "Origins": {"Quantity": 1, "Items": [{
            "Id": "recoder-alb", "DomainName": origin,
            "CustomOriginConfig": {"HTTPPort": 80, "HTTPSPort": 443, "OriginProtocolPolicy": "http-only",
                                   "OriginSslProtocols": {"Quantity": 1, "Items": ["TLSv1.2"]},
                                   "OriginReadTimeout": 60, "OriginKeepaliveTimeout": 5},
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
    }
    if alias and certificate_arn:
        cfg["Aliases"] = {"Quantity": 1, "Items": [alias]}
        cfg["ViewerCertificate"] = {"ACMCertificateArn": certificate_arn, "SSLSupportMethod": "sni-only",
                                    "MinimumProtocolVersion": "TLSv1.2_2021"}
    return cfg


def ensure_distribution(cloudfront: Any, *, name: str, origin: str, tags: Optional[dict] = None,
                        alias: str = "", certificate_arn: str = "") -> CdnResult:
    """ALB 를 원본으로 하는 배포를 확보한다. 있으면 원본·도메인만 맞춘다."""
    existing = _find(cloudfront, comment_for(name))
    if existing is None:
        try:
            dist = cloudfront.create_distribution_with_tags(DistributionConfigWithTags={
                "DistributionConfig": _config(name, origin, alias=alias, certificate_arn=certificate_arn),
                "Tags": {"Items": [{"Key": k, "Value": v} for k, v in (tags or {}).items()]},
            })["Distribution"]
        except Exception as exc:  # noqa: BLE001
            raise InfraError("HTTPS 주소(CloudFront)를 만들지 못했습니다.", detail=error_message(exc),
                             remedy="권한표에 2.0.27 의 cloudfront 권한이 있는지 확인하세요(권한 템플릿 스택 업데이트).") from exc
        return CdnResult(str(dist["Id"]), str(dist["DomainName"]), str(dist.get("Status", "")), True,
                         alias=alias if certificate_arn else "")
    current = cloudfront.get_distribution_config(Id=existing["Id"])
    cfg = current["DistributionConfig"]
    want = _config(name, origin, alias=alias, certificate_arn=certificate_arn)
    changed = False
    if cfg["Origins"]["Items"][0].get("DomainName") != origin:
        cfg["Origins"] = want["Origins"]
        changed = True
    if certificate_arn and (cfg.get("Aliases", {}).get("Items") or []) != [alias]:
        cfg["Aliases"], cfg["ViewerCertificate"] = want["Aliases"], want["ViewerCertificate"]
        changed = True
    if not cfg.get("Enabled"):
        cfg["Enabled"] = True
        changed = True
    if changed:
        cloudfront.update_distribution(Id=existing["Id"], IfMatch=current["ETag"], DistributionConfig=cfg)
    aliases = cfg.get("Aliases", {}).get("Items") or []
    return CdnResult(str(existing["Id"]), str(existing["DomainName"]), "InProgress" if changed else str(existing.get("Status", "")),
                     False, alias=aliases[0] if aliases else "")


# ---------------------------------------------------------------------------
# 내 도메인 — ACM 인증서(us-east-1) + Route 53
# ---------------------------------------------------------------------------


def _zone_for(route53: Any, domain: str) -> Optional[dict]:
    """도메인을 담는 공개 호스티드 영역(가장 긴 일치)."""
    parts = domain.rstrip(".").split(".")
    for i in range(len(parts) - 1):
        candidate = ".".join(parts[i:]) + "."
        zones = route53.list_hosted_zones_by_name(DNSName=candidate, MaxItems="1").get("HostedZones", [])
        if zones and zones[0]["Name"] == candidate and not zones[0].get("Config", {}).get("PrivateZone"):
            return zones[0]
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
        for cert in acm.list_certificates(CertificateStatuses=status).get("CertificateSummaryList", []):
            if cert.get("DomainName") == domain:
                arn = str(cert["CertificateArn"])
                break
        if arn:
            break
    if not arn:
        arn = str(acm.request_certificate(DomainName=domain, ValidationMethod="DNS",
                                          Tags=[{"Key": k, "Value": v} for k, v in (tags or {}).items()],
                                          IdempotencyToken=re.sub(r"\W", "", domain)[:32] or "recoder")["CertificateArn"])
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
            _upsert(route53, state.zone_id, {"Name": record["Name"], "Type": record["Type"], "TTL": 300,
                                             "ResourceRecords": [{"Value": record["Value"]}]})
            state.notes.append("Route 53 에 인증서 검증 레코드를 넣었습니다 — 보통 수 분 안에 발급됩니다. 발급 뒤 다시 배포하면 도메인이 연결됩니다.")
    elif record and not state.issued:
        state.notes.append(f"도메인 DNS 에 검증 레코드를 넣으세요: {record['Name']} CNAME {record['Value']} — 발급된 뒤 다시 배포하면 도메인이 연결됩니다.")
    return state


def point_domain(route53: Any, zone_id: str, domain: str, distribution_domain: str) -> None:
    """Route 53 별칭 A·AAAA → CloudFront."""
    for kind in ("A", "AAAA"):
        _upsert(route53, zone_id, {"Name": domain, "Type": kind, "AliasTarget": {
            "HostedZoneId": CLOUDFRONT_ZONE_ID, "DNSName": distribution_domain, "EvaluateTargetHealth": False}})


# ---------------------------------------------------------------------------
# 정리 — 비활성화(수 분) 뒤에야 지울 수 있다
# ---------------------------------------------------------------------------


def disable_or_delete(cloudfront: Any, distribution_id: str) -> str:
    """켜져 있으면 끄고(다음 정리 때 지움), 꺼진 채 배포가 끝났으면 지운다. 결과 문장."""
    current = cloudfront.get_distribution_config(Id=distribution_id)
    cfg = current["DistributionConfig"]
    if cfg.get("Enabled"):
        cfg["Enabled"] = False
        cloudfront.update_distribution(Id=distribution_id, IfMatch=current["ETag"], DistributionConfig=cfg)
        return "껐습니다 — CloudFront 가 끄는 데 15분쯤 걸려, 그 뒤 정리를 다시 실행하면 지웁니다."
    dist = cloudfront.get_distribution(Id=distribution_id)
    if dist["Distribution"].get("Status") != "Deployed":
        return "끄는 중입니다 — 잠시 뒤 정리를 다시 실행하면 지웁니다."
    cloudfront.delete_distribution(Id=distribution_id, IfMatch=dist["ETag"])
    return "지웠습니다."


def is_not_found(exc: BaseException) -> bool:
    return error_code(exc) in ("NoSuchDistribution", "ResourceNotFoundException")
