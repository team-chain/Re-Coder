"""
ReCoder Core — AWS 리소스 정리 (2.0.26)

배포를 그만 쓸 때 ReCoder 가 만든 AWS 리소스를 찾아 **중지**하거나 **완전 삭제**한다.

안전장치
  · 지우는 것은 `recoder:managed=true` 태그가 있는 리소스뿐이다(2.0.23 부터 새로 만들 때만 붙인다).
    태그 없는 옛 리소스·사용자가 만든 리소스는 "수동 확인 필요"로 보여 주기만 한다.
  · plan() 은 아무것도 바꾸지 않는다. delete() 는 승인(approved=True)과 항목 id 를 받아, 그 자리에서
    **목록을 다시 만들고** 태그를 다시 확인한 항목만 지운다(화면이 오래됐거나 id 가 조작돼도 남의 것은 안 지움).
  · 중지(stop)는 서비스 태스크 수만 0 으로 — 리소스는 남고 다시 배포하면 살아난다. 되돌릴 수 있으므로
    태그 없는 서비스도 중지는 허용한다.
  · 삭제 순서는 의존 관계를 따른다: 서비스 → 태스크 정의 → WAF → 로드밸런서 → 대상 그룹 → 경보·알림 주제
    → 이미지 저장소 → 로그 → 보안 그룹(네트워크가 풀릴 때까지 재시도) → 빈 클러스터 → S3 버킷.

boto3 클라이언트 묶음(dict)을 인자로 받는다 — moto 로 그대로 시험한다.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Iterable, Optional

try:
    from core.aws_infra import MANAGED_TAG, SERVICE_TAG, PROJECT_TAG, error_code, error_message  # type: ignore
except ImportError:  # pragma: no cover
    from aws_infra import MANAGED_TAG, SERVICE_TAG, PROJECT_TAG, error_code, error_message  # type: ignore

logger = logging.getLogger(__name__)

PREFIX = "recoder"
CLIENTS = ("ecs", "ecr", "logs", "ec2", "elbv2", "wafv2", "cloudwatch", "sns", "s3",
           # 2.0.27 HTTPS 주소·관리형 DB — acm_global 은 us-east-1 클라이언트(CloudFront 인증서)
           "cloudfront", "acm_global", "rds", "secretsmanager",
           # 2.0.31 CloudFront 에 붙인 WAF(전역 범위) — us-east-1 클라이언트
           "wafv2_global",
           # 2.0.35 원본 구간 HTTPS — 배포 리전 인증서(acm) · origin.<도메인> 레코드(route53)
           "acm", "route53",
           # 2.0.48 24시간 감시자(Lambda · 깨우는 규칙 · 기록 표)
           "lambda", "events", "dynamodb")
#: 리전을 us-east-1 로 고정해 만드는 클라이언트(이름 → 실제 서비스).
GLOBAL_CLIENTS = {"acm_global": "acm", "wafv2_global": "wafv2"}

#: 삭제 순서(작을수록 먼저).
ORDER = {
    #: 감시자를 먼저 지운다 — 서비스가 사라지는 동안 '장애'로 알리지 않게
    "watcher": -1,
    "ecs_service": 0, "task_definitions": 1, "cloudfront": 2, "waf_cdn": 3, "acm_certificate": 4, "waf": 5, "alb": 6,
    #: 로드밸런서(443 리스너)가 쓰는 인증서 — 로드밸런서를 지운 뒤에
    "acm_origin_certificate": 6.5,
    "target_group": 7, "rds_instance": 8, "rds_subnet_group": 9, "rds_parameter_group": 10, "secret": 11,
    "alarm": 12, "sns_topic": 13, "ecr": 14, "log_group": 15, "security_group": 16, "ecs_cluster": 17, "s3_bucket": 18,
}
#: DB 가 남아 있으면 지우지 않는 것(DB 가 쓰는 중이거나, 지우면 다음 배포가 DB 비밀번호를 잃는다).
DB_DEPENDENTS = ("rds_subnet_group", "rds_parameter_group", "secret")
LABELS = {
    "ecs_service": "ECS 서비스", "task_definitions": "태스크 정의", "waf": "WAF 웹 ACL", "alb": "로드밸런서",
    "target_group": "대상 그룹", "alarm": "CloudWatch 경보", "sns_topic": "알림 주제(메일)", "ecr": "이미지 저장소(ECR)",
    "log_group": "로그 그룹", "security_group": "보안 그룹", "ecs_cluster": "ECS 클러스터", "s3_bucket": "S3 버킷",
    "cloudfront": "HTTPS 주소(CloudFront)", "acm_certificate": "도메인 인증서(ACM)", "rds_instance": "관리형 DB(RDS)",
    "rds_subnet_group": "DB 서브넷 그룹", "rds_parameter_group": "DB 파라미터 그룹", "secret": "DB 접속 정보(Secrets Manager)",
    "waf_cdn": "WAF 웹 ACL(HTTPS 주소)",
    "acm_origin_certificate": "원본 구간 인증서(ACM · 로드밸런서)",
    "watcher": "24시간 감시자(Lambda · 규칙 · 기록 표)",
}
COST = {
    "ecs_service": "실행 중인 태스크만큼 시간당 요금", "task_definitions": "요금 없음", "waf": "매달 고정 요금 + 요청 수",
    "alb": "켜 두는 시간만큼 + 처리량", "target_group": "요금 없음", "alarm": "경보당 매달 소액",
    "sns_topic": "거의 없음", "ecr": "보관 용량(GB당 매달)", "log_group": "보관 용량(GB당 매달)",
    "security_group": "요금 없음", "ecs_cluster": "요금 없음(빈 클러스터)", "s3_bucket": "보관 용량 + 요청 수",
    "cloudfront": "나가는 데이터·요청 수", "acm_certificate": "요금 없음", "rds_instance": "켜 두는 동안 시간당 + 저장 용량",
    "rds_subnet_group": "요금 없음", "rds_parameter_group": "요금 없음", "secret": "비밀당 매달 소액",
    "waf_cdn": "매달 고정 요금 + 요청 수",
    "acm_origin_certificate": "요금 없음",
    "watcher": "거의 없음(월 $0.1 미만)",
}
def make_clients(session: Any) -> dict:
    """boto3 세션 하나로 정리에 쓰는 클라이언트 묶음(인증서는 us-east-1)."""
    out = {name: session.client(name) for name in CLIENTS if name not in GLOBAL_CLIENTS}
    for name, service in GLOBAL_CLIENTS.items():
        out[name] = session.client(service, region_name="us-east-1")
    return out


UNTAGGED_NOTE = ("ReCoder 태그가 없어 지우지 않습니다(2.0.23 이전에 만들었거나 직접 만든 것일 수 있음) — "
                 "AWS 콘솔에서 확인한 뒤 직접 지우세요.")


@dataclass
class Item:
    id: str
    kind: str
    name: str
    arn: str = ""
    service: str = ""
    project: str = ""
    tagged: bool = False
    detail: str = ""
    cluster: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def label(self) -> str:
        return LABELS.get(self.kind, self.kind)

    def view(self) -> dict:
        out = asdict(self)
        out.pop("extra", None)
        out.update(label=self.label, cost=COST.get(self.kind, ""), deletable=self.tagged,
                   note="" if self.tagged else UNTAGGED_NOTE)
        return out


def _tags_dict(raw: Iterable[dict] | dict | None) -> dict[str, str]:
    if isinstance(raw, dict):
        return {str(k): str(v) for k, v in raw.items()}
    out: dict[str, str] = {}
    for t in raw or []:
        key = t.get("Key", t.get("key"))
        if key is not None:
            out[str(key)] = str(t.get("Value", t.get("value", "")))
    return out


def _managed(tags: dict[str, str]) -> bool:
    return tags.get(MANAGED_TAG) == "true"


def _recoderish(name: str) -> bool:
    return PREFIX in str(name or "").lower()


def _pages(client: Any, operation: str, key: str, **kwargs) -> list:
    out: list = []
    for page in client.get_paginator(operation).paginate(**kwargs):
        out.extend(page.get(key, []))
    return out


# ---------------------------------------------------------------------------
# 찾기
# ---------------------------------------------------------------------------


def _find_ecs(ecs: Any, items: list[Item], records: Iterable[dict]) -> None:
    wanted_from_records = {(r.get("cluster"), r.get("service")) for r in records if r.get("cluster") and r.get("service")}
    record_clusters = {c for c, _ in wanted_from_records}
    #: 권한표는 recoder-* 클러스터만 열어 준다 — 그 밖의 클러스터는 조회하지 않는다(기록에 있는 것은 예외).
    clusters = [a for a in _pages(ecs, "list_clusters", "clusterArns")
                if _recoderish(a.rsplit("/", 1)[-1]) or a.rsplit("/", 1)[-1] in record_clusters]
    families: dict[str, Item] = {}
    described: list[dict] = []
    for i in range(0, len(clusters), 100):
        described += ecs.describe_clusters(clusters=clusters[i:i + 100], include=["TAGS"]).get("clusters", [])
    for cluster in described:
        if cluster.get("status") != "ACTIVE":  # 지운 클러스터는 한동안 INACTIVE 로 목록에 남는다
            continue
        arn = str(cluster["clusterArn"])
        name = str(cluster.get("clusterName") or arn.rsplit("/", 1)[-1])
        ctags = _tags_dict(cluster.get("tags"))
        service_arns = _pages(ecs, "list_services", "serviceArns", cluster=arn)
        services: list[dict] = []
        for i in range(0, len(service_arns), 10):
            services += ecs.describe_services(cluster=arn, services=service_arns[i:i + 10], include=["TAGS"]).get("services", [])
        services = [s for s in services if s.get("status") == "ACTIVE"]
        mine = False
        for svc in services:
            stags = _tags_dict(svc.get("tags"))
            sname = str(svc["serviceName"])
            interesting = _managed(stags) or (name, sname) in wanted_from_records or _recoderish(sname)
            if not interesting:
                continue
            mine = True
            items.append(Item(
                id=f"ecs_service:{name}/{sname}", kind="ecs_service", name=sname, arn=str(svc["serviceArn"]),
                service=stags.get(SERVICE_TAG, sname), project=stags.get(PROJECT_TAG, ""), tagged=_managed(stags),
                cluster=name,
                detail=f"실행 중 태스크 {svc.get('runningCount', 0)}개 · 원하는 수 {svc.get('desiredCount', 0)}",
                extra={"running": int(svc.get("runningCount", 0)), "desired": int(svc.get("desiredCount", 0))}))
            family = str(svc.get("taskDefinition", "")).rsplit("/", 1)[-1].split(":")[0]
            if family and family not in families:
                families[family] = Item(id=f"task_definitions:{family}", kind="task_definitions", name=family,
                                        service=stags.get(SERVICE_TAG, sname), project=stags.get(PROJECT_TAG, ""))
        if _managed(ctags) or mine or _recoderish(name):
            items.append(Item(id=f"ecs_cluster:{name}", kind="ecs_cluster", name=name, arn=arn, tagged=_managed(ctags),
                              project=ctags.get(PROJECT_TAG, ""),
                              detail=f"활성 서비스 {len(services)}개"))
    for family, item in families.items():
        arns = _pages(ecs, "list_task_definitions", "taskDefinitionArns", familyPrefix=family, status="ACTIVE")
        arns = [a for a in arns if a.rsplit("/", 1)[-1].split(":")[0] == family]
        tagged = [a for a in arns if _managed(_tags_dict(ecs.list_tags_for_resource(resourceArn=a).get("tags")))]
        item.tagged = bool(tagged)
        item.detail = f"등록된 버전 {len(arns)}개" + (f" (ReCoder 태그 {len(tagged)}개만 해제)" if tagged and len(tagged) != len(arns) else "")
        item.extra = {"arns": tagged}
        items.append(item)


def _find_ecr(ecr: Any, items: list[Item]) -> None:
    for repo in _pages(ecr, "describe_repositories", "repositories"):
        if not _recoderish(repo["repositoryName"]):
            continue
        tags = _tags_dict(ecr.list_tags_for_resource(resourceArn=repo["repositoryArn"]).get("tags"))
        if not (_managed(tags) or _recoderish(repo["repositoryName"])):
            continue
        images = _pages(ecr, "describe_images", "imageDetails", repositoryName=repo["repositoryName"])
        size = sum(int(i.get("imageSizeInBytes", 0)) for i in images)
        items.append(Item(id=f"ecr:{repo['repositoryName']}", kind="ecr", name=repo["repositoryName"],
                          arn=repo["repositoryArn"], tagged=_managed(tags), service=tags.get(SERVICE_TAG, ""),
                          project=tags.get(PROJECT_TAG, ""),
                          detail=f"이미지 {len(images)}개 · {size / 1024 / 1024:,.0f} MB", extra={"bytes": size}))


def _find_logs(logs: Any, items: list[Item]) -> None:
    # /ecs/<태스크 패밀리> — 패밀리 이름은 자유라 이름으로 거르지 않고 태그를 본다(권한표가 /ecs/* 를 연다).
    for group in _pages(logs, "describe_log_groups", "logGroups", logGroupNamePrefix="/ecs/"):
        arn = str(group.get("arn") or "")
        arn = arn[:-2] if arn.endswith(":*") else arn
        tags = _tags_dict(logs.list_tags_for_resource(resourceArn=arn).get("tags")) if arn else {}
        if not (_managed(tags) or _recoderish(group["logGroupName"])):
            continue
        size = int(group.get("storedBytes", 0) or 0)
        items.append(Item(id=f"log_group:{group['logGroupName']}", kind="log_group", name=group["logGroupName"],
                          arn=arn, tagged=_managed(tags), service=tags.get(SERVICE_TAG, ""),
                          project=tags.get(PROJECT_TAG, ""), detail=f"보관 {size / 1024 / 1024:,.1f} MB"))


def _find_security_groups(ec2: Any, items: list[Item]) -> None:
    for sg in _pages(ec2, "describe_security_groups", "SecurityGroups"):
        tags = _tags_dict(sg.get("Tags"))
        name = str(sg.get("GroupName", ""))
        if name == "default":
            continue
        if not (_managed(tags) or "ReCoder" in str(sg.get("Description", ""))):
            continue
        refs = sorted({p.get("GroupId") for perm in sg.get("IpPermissions", [])
                       for p in perm.get("UserIdGroupPairs", []) if p.get("GroupId")})
        items.append(Item(id=f"security_group:{sg['GroupId']}", kind="security_group", name=f"{name} ({sg['GroupId']})",
                          arn=sg["GroupId"], tagged=_managed(tags), service=tags.get(SERVICE_TAG, ""),
                          project=tags.get(PROJECT_TAG, ""), detail=f"VPC {sg.get('VpcId', '')}",
                          extra={"refs": refs}))


def _find_elb(elbv2: Any, wafv2: Any, items: list[Item]) -> None:
    lbs = [lb for lb in _pages(elbv2, "describe_load_balancers", "LoadBalancers") if _recoderish(lb["LoadBalancerName"])]
    tgs = [tg for tg in _pages(elbv2, "describe_target_groups", "TargetGroups") if _recoderish(tg["TargetGroupName"])]
    arns = [lb["LoadBalancerArn"] for lb in lbs] + [tg["TargetGroupArn"] for tg in tgs]
    tags: dict[str, dict] = {}
    for i in range(0, len(arns), 20):
        for desc in elbv2.describe_tags(ResourceArns=arns[i:i + 20]).get("TagDescriptions", []):
            tags[desc["ResourceArn"]] = _tags_dict(desc.get("Tags"))
    for lb in lbs:
        t = tags.get(lb["LoadBalancerArn"], {})
        if not (_managed(t) or _recoderish(lb["LoadBalancerName"])):
            continue
        items.append(Item(id=f"alb:{lb['LoadBalancerName']}", kind="alb", name=lb["LoadBalancerName"],
                          arn=lb["LoadBalancerArn"], tagged=_managed(t), service=t.get(SERVICE_TAG, ""),
                          project=t.get(PROJECT_TAG, ""), detail=f"http://{lb.get('DNSName', '')}"))
    for tg in tgs:
        t = tags.get(tg["TargetGroupArn"], {})
        if not (_managed(t) or _recoderish(tg["TargetGroupName"])):
            continue
        items.append(Item(id=f"target_group:{tg['TargetGroupName']}", kind="target_group", name=tg["TargetGroupName"],
                          arn=tg["TargetGroupArn"], tagged=_managed(t), service=t.get(SERVICE_TAG, ""),
                          project=t.get(PROJECT_TAG, ""), detail=f"포트 {tg.get('Port', '')}"))
    _find_waf(wafv2, items, scope="REGIONAL", kind="waf")


_REGION_SUFFIX = re.compile(r"-([a-z]{2}(?:-gov)?-[a-z]+-\d)$")


def _find_waf(wafv2: Any, items: list[Item], *, scope: str, kind: str, region: str = "",
              foreign: Optional[dict] = None) -> None:
    """scope=CLOUDFRONT 는 전역이라 모든 리전의 것이 보인다 — 이 리전 것(이름 끝 -리전)과, 리전 표시가 없는 옛 이름(2.0.31)은
    **다른 리전**의 CloudFront 배포가 쓰지 않는 것을 고른다(떼어진 옛 ACL 이 어느 리전에서도 안 보여 요금만 남지 않게, 2.0.32)."""
    foreign_acls = (foreign or {}).get("acls", set())
    marker = None
    while True:
        kwargs: dict = {"Scope": scope, "Limit": 100}
        if marker:
            kwargs["NextMarker"] = marker
        resp = wafv2.list_web_acls(**kwargs)
        for acl in resp.get("WebACLs", []):
            if not _recoderish(acl["Name"]):
                continue
            if scope == "CLOUDFRONT" and region:
                suffix = _REGION_SUFFIX.search(acl["Name"])
                if (suffix and suffix.group(1) != region) or (not suffix and acl["ARN"] in foreign_acls):
                    continue
            t = _tags_dict((wafv2.list_tags_for_resource(ResourceARN=acl["ARN"]).get("TagInfoForResource") or {})
                           .get("TagList"))
            items.append(Item(id=f"{kind}:{acl['Name']}", kind=kind, name=acl["Name"], arn=acl["ARN"], tagged=_managed(t),
                              service=t.get(SERVICE_TAG, ""), project=t.get(PROJECT_TAG, ""),
                              extra={"id": acl["Id"], "scope": scope}))
        marker = resp.get("NextMarker")
        if not marker or not resp.get("WebACLs"):
            break


def _find_alerts(cloudwatch: Any, sns: Any, items: list[Item]) -> None:
    for alarm in _pages(cloudwatch, "describe_alarms", "MetricAlarms", AlarmNamePrefix=f"{PREFIX}-"):
        t = _tags_dict(cloudwatch.list_tags_for_resource(ResourceARN=alarm["AlarmArn"]).get("Tags"))
        items.append(Item(id=f"alarm:{alarm['AlarmName']}", kind="alarm", name=alarm["AlarmName"], arn=alarm["AlarmArn"],
                          tagged=_managed(t), service=t.get(SERVICE_TAG, ""), project=t.get(PROJECT_TAG, ""),
                          detail=str(alarm.get("AlarmDescription", ""))))
    for topic in _pages(sns, "list_topics", "Topics"):
        arn = topic["TopicArn"]
        name = arn.rsplit(":", 1)[-1]
        if not name.startswith(f"{PREFIX}-"):
            continue
        t = _tags_dict(sns.list_tags_for_resource(ResourceArn=arn).get("Tags"))
        items.append(Item(id=f"sns_topic:{name}", kind="sns_topic", name=name, arn=arn, tagged=_managed(t),
                          service=t.get(SERVICE_TAG, ""), project=t.get(PROJECT_TAG, "")))


def _find_s3(s3: Any, region: str, items: list[Item]) -> None:
    for bucket in s3.list_buckets().get("Buckets", []):
        name = bucket["Name"]
        if not name.startswith(f"{PREFIX}-"):
            continue
        try:
            where = s3.get_bucket_location(Bucket=name).get("LocationConstraint") or "us-east-1"
        except Exception:  # noqa: BLE001
            continue
        if where != region:
            continue
        try:
            t = _tags_dict(s3.get_bucket_tagging(Bucket=name).get("TagSet"))
        except Exception as exc:  # noqa: BLE001
            if error_code(exc) not in ("NoSuchTagSet", "NoSuchTagSetError"):
                raise
            t = {}
        listed = s3.list_objects_v2(Bucket=name, MaxKeys=1000)
        count = int(listed.get("KeyCount", 0))
        items.append(Item(id=f"s3_bucket:{name}", kind="s3_bucket", name=name, arn=f"arn:aws:s3:::{name}",
                          tagged=_managed(t), service=t.get(SERVICE_TAG, ""), project=t.get(PROJECT_TAG, ""),
                          detail=f"파일 {count}{'+' if listed.get('IsTruncated') else ''}개"))


def _find_cdn(cloudfront: Any, acm: Any, items: list[Item], region: str = "", foreign: Optional[dict] = None) -> None:
    """CloudFront·인증서는 전역 — 이 리전의 ALB 를 원본으로 하는 배포와, 이 리전 배포를 위해 만든 인증서만(2.0.32).
    foreign 에 다른 리전 배포가 쓰는 웹 ACL·도메인을 모은다(옛 이름 ACL·인증서를 고를 때 쓴다)."""
    foreign = foreign if foreign is not None else {}
    foreign.setdefault("acls", set())
    foreign.setdefault("domains", set())
    try:
        from core import aws_cdn  # type: ignore
    except ImportError:  # pragma: no cover
        import aws_cdn  # type: ignore
    marker = ""
    while True:
        page = cloudfront.list_distributions(**({"Marker": marker} if marker else {})).get("DistributionList", {})
        for dist in page.get("Items", []) or []:
            if not str(dist.get("Comment", "")).startswith(f"{PREFIX}:"):
                continue
            if region and aws_cdn.item_region(dist) not in ("", region):
                if dist.get("WebACLId"):
                    foreign["acls"].add(str(dist["WebACLId"]))
                foreign["domains"] |= set((dist.get("Aliases") or {}).get("Items") or [])
                continue
            t = _tags_dict(cloudfront.list_tags_for_resource(Resource=dist["ARN"]).get("Tags", {}).get("Items"))
            aliases = (dist.get("Aliases") or {}).get("Items") or []
            items.append(Item(id=f"cloudfront:{dist['Id']}", kind="cloudfront",
                              name=str(dist["Comment"])[len(PREFIX) + 1:].split("@", 1)[0],
                              arn=dist["ARN"], tagged=_managed(t), service=t.get(SERVICE_TAG, ""),
                              project=t.get(PROJECT_TAG, ""),
                              detail=f"https://{(aliases or [dist['DomainName']])[0]} · {'켜짐' if dist.get('Enabled') else '꺼짐'} · {dist.get('Status', '')}",
                              extra={"id": dist["Id"], "web_acl": str(dist.get("WebACLId") or "")}))
        if not page.get("IsTruncated"):
            break
        marker = page.get("NextMarker", "")
    for cert in acm.list_certificates().get("CertificateSummaryList", []):
        t = _tags_dict(acm.list_tags_for_certificate(CertificateArn=cert["CertificateArn"]).get("Tags"))
        if not _managed(t):
            continue  # 사용자가 직접 만든 인증서는 목록에도 넣지 않는다(이름으로 구분할 수 없음)
        if t.get("recoder:origin") == "true":
            continue  # 원본 구간 인증서 — _find_origin_certs 가 배포 리전에서 따로 찾는다(us-east-1 배포면 같은 목록)
        cert_region = t.get("recoder:region", "")
        if region and (cert_region != region if cert_region else cert.get("DomainName") in foreign["domains"]):
            continue  # 다른 리전 배포의 인증서
        items.append(Item(id=f"acm_certificate:{cert['CertificateArn'].rsplit('/', 1)[-1]}", kind="acm_certificate",
                          name=cert.get("DomainName", ""), arn=cert["CertificateArn"], tagged=True,
                          service=t.get(SERVICE_TAG, ""), project=t.get(PROJECT_TAG, ""),
                          detail=str(cert.get("Status", ""))))


def _find_origin_certs(acm: Any, items: list[Item]) -> None:
    """2.0.35 원본 구간 HTTPS 인증서(origin.<도메인>, 배포 리전) — recoder:origin 태그가 있는 것만."""
    token = ""
    while True:
        page = acm.list_certificates(**({"NextToken": token} if token else {}))
        for cert in page.get("CertificateSummaryList", []):
            t = _tags_dict(acm.list_tags_for_certificate(CertificateArn=cert["CertificateArn"]).get("Tags"))
            if not _managed(t) or t.get("recoder:origin") != "true":
                continue
            items.append(Item(id=f"acm_origin_certificate:{cert['CertificateArn'].rsplit('/', 1)[-1]}",
                              kind="acm_origin_certificate", name=cert.get("DomainName", ""), arn=cert["CertificateArn"],
                              tagged=True, service=t.get(SERVICE_TAG, ""), project=t.get(PROJECT_TAG, ""),
                              detail=str(cert.get("Status", ""))))
        token = page.get("NextToken") or ""
        if not token:
            return


def _find_rds(rds: Any, sm: Any, items: list[Item]) -> None:
    for db in _pages(rds, "describe_db_instances", "DBInstances"):
        ident = str(db["DBInstanceIdentifier"])
        if not _recoderish(ident):
            continue
        t = _tags_dict(db.get("TagList"))
        items.append(Item(id=f"rds_instance:{ident}", kind="rds_instance", name=ident, arn=db.get("DBInstanceArn", ""),
                          tagged=_managed(t), service=t.get(SERVICE_TAG, ""), project=t.get(PROJECT_TAG, ""),
                          detail=f"{db.get('DBInstanceClass', '')} · {db.get('AllocatedStorage', '')}GB · {db.get('DBInstanceStatus', '')}"))
    for group in _pages(rds, "describe_db_subnet_groups", "DBSubnetGroups"):
        name = str(group["DBSubnetGroupName"])
        if not _recoderish(name):
            continue
        t = _tags_dict(rds.list_tags_for_resource(ResourceName=group["DBSubnetGroupArn"]).get("TagList"))
        items.append(Item(id=f"rds_subnet_group:{name}", kind="rds_subnet_group", name=name, arn=group["DBSubnetGroupArn"],
                          tagged=_managed(t), service=t.get(SERVICE_TAG, ""), project=t.get(PROJECT_TAG, "")))
    for group in _pages(rds, "describe_db_parameter_groups", "DBParameterGroups"):
        name = str(group["DBParameterGroupName"])
        if not _recoderish(name):
            continue
        t = _tags_dict(rds.list_tags_for_resource(ResourceName=group["DBParameterGroupArn"]).get("TagList"))
        items.append(Item(id=f"rds_parameter_group:{name}", kind="rds_parameter_group", name=name,
                          arn=group["DBParameterGroupArn"], tagged=_managed(t), service=t.get(SERVICE_TAG, ""),
                          project=t.get(PROJECT_TAG, "")))
    for secret in _pages(sm, "list_secrets", "SecretList", Filters=[{"Key": "name", "Values": [f"{PREFIX}/"]}]):
        if not str(secret.get("Name", "")).startswith(f"{PREFIX}/"):
            continue
        t = _tags_dict(secret.get("Tags"))
        items.append(Item(id=f"secret:{secret['Name']}", kind="secret", name=secret["Name"], arn=secret["ARN"],
                          tagged=_managed(t), service=t.get(SERVICE_TAG, ""), project=t.get(PROJECT_TAG, "")))


def _watcher_mod():
    try:
        import watcher  # type: ignore
    except ImportError:  # pragma: no cover
        from core import watcher  # type: ignore
    return watcher


def _find_watchers(clients: dict, region: str, items: list[Item], records: Iterable[dict] = ()) -> None:
    """감시자 함수가 있는지(이름이 서비스에서 정해진다 — 목록 권한 없이 GetFunction 으로).

    지금 있는 ECS 서비스뿐 아니라 감시 목록 · 배포 기록의 서비스도 본다 — 서비스만 먼저 지워져 감시자가 혼자 남은 경우도 찾게.
    """
    watcher = _watcher_mod()
    session = watcher.from_clients(clients)
    candidates: dict[tuple[str, str], str] = {(i.cluster, i.name): i.project for i in items if i.kind == "ecs_service"}
    try:
        try:
            import ops_alerts  # type: ignore
        except ImportError:  # pragma: no cover
            from core import ops_alerts  # type: ignore
        for m in ops_alerts.monitors():
            if m.get("region") == region:
                candidates.setdefault((str(m.get("cluster")), str(m.get("service"))), "")
    except Exception:  # noqa: BLE001
        pass
    for r in records:
        if (r.get("region") or region) == region and r.get("cluster") and r.get("service"):
            candidates.setdefault((str(r["cluster"]), str(r["service"])), "")
    for (cluster, service), project in candidates.items():
        found = watcher.installed(session, region=region, cluster=cluster, service=service)
        if found:
            items.append(Item(id=f"watcher:{cluster}/{service}", kind="watcher", name=found["name"], arn=found["arn"],
                              service=service, project=project, tagged=found["tagged"], cluster=cluster))


def find(clients: dict, *, region: str, records: Iterable[dict] = ()) -> tuple[list[Item], list[str]]:
    """리전의 ReCoder 리소스(태그 있는 것 + 이름·기록으로 보이는 태그 없는 것). 못 본 종류는 문장으로."""
    items: list[Item] = []
    gaps: list[str] = []
    foreign: dict = {"acls": set(), "domains": set()}
    steps: list[tuple[str, Callable[[], None]]] = [
        ("ECS", lambda: _find_ecs(clients["ecs"], items, list(records))),
        ("ECR", lambda: _find_ecr(clients["ecr"], items)),
        ("로그", lambda: _find_logs(clients["logs"], items)),
        ("보안 그룹", lambda: _find_security_groups(clients["ec2"], items)),
        ("로드밸런서·WAF", lambda: _find_elb(clients["elbv2"], clients["wafv2"], items)),
        ("경보·알림", lambda: _find_alerts(clients["cloudwatch"], clients["sns"], items)),
        ("S3", lambda: _find_s3(clients["s3"], region, items)),
        ("HTTPS 주소", lambda: _find_cdn(clients["cloudfront"], clients["acm_global"], items, region, foreign)),
        ("HTTPS 주소 WAF", lambda: _find_waf(clients["wafv2_global"], items, scope="CLOUDFRONT", kind="waf_cdn",
                                           region=region, foreign=foreign)),
        ("관리형 DB", lambda: _find_rds(clients["rds"], clients["secretsmanager"], items)),
        ("원본 구간 인증서", lambda: _find_origin_certs(clients["acm"], items)),
        ("24시간 감시자", lambda: _find_watchers(clients, region, items, list(records))),
    ]
    for label, step in steps:
        try:
            needs = {"HTTPS 주소": ("cloudfront", "acm_global"), "관리형 DB": ("rds", "secretsmanager"),
                     "HTTPS 주소 WAF": ("wafv2_global",), "원본 구간 인증서": ("acm",),
                     "24시간 감시자": ("lambda", "events", "dynamodb")}.get(label, ())
            if not all(k in clients for k in needs):
                continue
            step()
        except Exception as exc:  # noqa: BLE001
            if label == "HTTPS 주소 WAF" and not any(i.kind == "cloudfront" for i in items):
                # HTTPS 주소를 쓴 적이 없으면(옛 권한표는 us-east-1 WAF 목록을 못 봄) 경고로 시끄럽게 하지 않는다.
                logger.info("CloudFront WAF 목록 생략: %s", error_message(exc))
                continue
            gaps.append(f"{label}: 확인하지 못했습니다 — {error_message(exc)}")
    items.sort(key=lambda i: (ORDER.get(i.kind, 99), i.name))
    return items, gaps


def plan(clients: dict, *, region: str, records: Iterable[dict] = (), service: str = "") -> dict:
    """정리 계획 — 아무것도 바꾸지 않는다. 서비스별로 묶고, 남아 있는 것과 요금 성격을 보여 준다."""
    items, gaps = find(clients, region=region, records=records)
    if service:
        items = [i for i in items if i.service == service or i.name == service]
    groups: dict[str, list[dict]] = {}
    for item in items:
        if item.tagged:
            groups.setdefault(item.service or "공용", []).append(item.view())
    untagged = [i.view() for i in items if not i.tagged]
    running = sum(i.extra.get("running", 0) for i in items if i.kind == "ecs_service")
    summary = {
        "running_tasks": running,
        "load_balancers": sum(1 for i in items if i.kind == "alb"),
        "web_acls": sum(1 for i in items if i.kind in ("waf", "waf_cdn")),
        "image_mb": round(sum(i.extra.get("bytes", 0) for i in items if i.kind == "ecr") / 1024 / 1024, 1),
        "tagged": sum(1 for i in items if i.tagged),
        "untagged": len(untagged),
    }
    return {"region": region, "groups": [{"service": k, "items": v} for k, v in groups.items()],
            "untagged": untagged, "summary": summary, "gaps": gaps}


# ---------------------------------------------------------------------------
# 중지 · 삭제
# ---------------------------------------------------------------------------


def stop(clients: dict, *, region: str, cluster: str, service: str) -> dict:
    """태스크 수를 0 으로(되살릴 수 있음). 감시도 멈춘다 — 0 개를 '장애'로 알리지 않게."""
    clients["ecs"].update_service(cluster=cluster, service=service, desiredCount=0)
    _unwatch(region, cluster, service)
    return {"ok": True, "message": f"{service} 의 태스크 수를 0 으로 내렸습니다 — 실행 요금이 멈춥니다. 다시 배포하면 살아납니다."}


def _unwatch(region: str, cluster: str, service: str) -> None:
    try:
        try:
            import ops_alerts  # type: ignore
        except ImportError:  # pragma: no cover
            from core import ops_alerts  # type: ignore
        ops_alerts.unregister(f"{region}/{cluster}/{service}")
    except Exception:  # noqa: BLE001
        pass


class _Kept(Exception):
    """지우지 않고 둔 이유(실패가 아니라 안전한 선택)."""


def _retry(fn: Callable[[], Any], codes: tuple[str, ...], *, sleep: Callable[[float], None], attempts: int,
           delay: float) -> Any:
    for i in range(attempts):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            if error_code(exc) not in codes or i == attempts - 1:
                raise
            sleep(delay)
    return None


def _delete_one(clients: dict, item: Item, *, region: str, all_items: list[Item],
                sleep: Callable[[float], None]) -> str:
    k = item.kind
    if k == "watcher":
        watcher = _watcher_mod()
        result = watcher.uninstall(watcher.from_clients(clients), region=region, cluster=item.cluster,
                                   service=item.service, approved=True)
        if not result["ok"]:
            left = [f"{r['label']}({r.get('note', '')})" for r in result["results"] if not r["ok"]]
            raise RuntimeError("일부를 지우지 못했습니다: " + ", ".join(left))
        return "감시자 함수 · 규칙 · 기록 표 · 로그를 지웠습니다."
    if k == "ecs_service":
        ecs = clients["ecs"]
        ecs.update_service(cluster=item.cluster, service=item.name, desiredCount=0)
        ecs.delete_service(cluster=item.cluster, service=item.name, force=True)
        _unwatch(region, item.cluster, item.name)
        return "태스크를 내리고 서비스를 지웠습니다."
    if k == "task_definitions":
        arns = item.extra.get("arns", [])
        for arn in arns:
            clients["ecs"].deregister_task_definition(taskDefinition=arn)
        return f"버전 {len(arns)}개를 등록 해제했습니다."
    if k == "waf":
        wafv2 = clients["wafv2"]
        for lb in (i for i in all_items if i.kind == "alb"):
            attached = ((wafv2.get_web_acl_for_resource(ResourceArn=lb.arn) or {}).get("WebACL") or {}).get("ARN")
            if attached == item.arn:
                wafv2.disassociate_web_acl(ResourceArn=lb.arn)
        lock = wafv2.get_web_acl(Name=item.name, Scope="REGIONAL", Id=item.extra["id"])["LockToken"]
        _retry(lambda: wafv2.delete_web_acl(Name=item.name, Scope="REGIONAL", Id=item.extra["id"], LockToken=lock),
               ("WAFAssociatedItemException",), sleep=sleep, attempts=6, delay=10)
        return "로드밸런서에서 떼고 지웠습니다."
    if k == "alb":
        elbv2 = clients["elbv2"]
        elbv2.delete_load_balancer(LoadBalancerArn=item.arn)
        for _ in range(12):  # 지워질 때까지(대상 그룹·보안 그룹이 풀리도록)
            try:
                if not elbv2.describe_load_balancers(LoadBalancerArns=[item.arn]).get("LoadBalancers"):
                    break
            except Exception:  # noqa: BLE001 - LoadBalancerNotFound = 지워짐
                break
            sleep(5)
        return "지웠습니다(리스너 포함)."
    if k == "target_group":
        _retry(lambda: clients["elbv2"].delete_target_group(TargetGroupArn=item.arn), ("ResourceInUse",),
               sleep=sleep, attempts=8, delay=10)
        return "지웠습니다."
    if k == "cloudfront":
        try:
            from core import aws_cdn  # type: ignore
        except ImportError:  # pragma: no cover
            import aws_cdn  # type: ignore
        message = aws_cdn.disable_or_delete(clients["cloudfront"], item.extra["id"])
        if message != "지웠습니다.":
            raise _Kept(message)
        return message
    if k == "waf_cdn":
        try:
            from core import aws_edge  # type: ignore
        except ImportError:  # pragma: no cover
            import aws_edge  # type: ignore
        try:
            aws_edge.delete_web_acl(clients["wafv2_global"], item.name, "CLOUDFRONT", sleep=sleep, attempts=3)
        except Exception as exc:  # noqa: BLE001
            if error_code(exc) != "WAFAssociatedItemException":
                raise
            raise _Kept("CloudFront 에서 떼어 내는 중입니다 — CloudFront 를 끈 뒤(15분쯤) 정리를 다시 실행하면 지웁니다.") from exc
        return "지웠습니다."
    if k == "acm_certificate":
        try:
            _retry(lambda: clients["acm_global"].delete_certificate(CertificateArn=item.arn), ("ResourceInUseException",),
                   sleep=sleep, attempts=2, delay=10)
        except Exception as exc:  # noqa: BLE001
            if error_code(exc) != "ResourceInUseException":
                raise
            raise _Kept("CloudFront 가 아직 이 인증서를 씁니다 — CloudFront 가 지워진 뒤 정리를 다시 실행하면 지웁니다.") from exc
        return "지웠습니다."
    if k == "acm_origin_certificate":
        try:
            _retry(lambda: clients["acm"].delete_certificate(CertificateArn=item.arn), ("ResourceInUseException",),
                   sleep=sleep, attempts=3, delay=10)
        except Exception as exc:  # noqa: BLE001
            if error_code(exc) != "ResourceInUseException":
                raise
            raise _Kept("로드밸런서가 아직 이 인증서를 씁니다 — 로드밸런서가 지워진 뒤 정리를 다시 실행하면 지웁니다.") from exc
        note = ""
        if "route53" in clients:
            try:
                from core import aws_cdn  # type: ignore
            except ImportError:  # pragma: no cover
                import aws_cdn  # type: ignore
            try:
                note = aws_cdn.remove_origin_record(clients["route53"], item.name)
            except Exception as exc:  # noqa: BLE001 - 인증서는 지웠다. 레코드는 사람이 지워도 된다
                note = f"{item.name} 레코드는 지우지 못했습니다({error_message(exc)}) — Route 53 에서 지우세요."
        return "지웠습니다." + (f" {note}" if note else "")
    if k == "rds_instance":
        try:
            from core import aws_rds  # type: ignore
        except ImportError:  # pragma: no cover
            import aws_rds  # type: ignore
        try:
            message = aws_rds.delete_instance(clients["rds"], item.name)
        except aws_rds.DbBusy as busy:
            raise _Kept(str(busy)) from busy
        except Exception as exc:  # noqa: BLE001
            if error_code(exc) in ("InvalidDBInstanceState", "InvalidDBInstanceStateFault"):
                raise _Kept("DB 가 다른 작업 중이라 지금은 지울 수 없습니다 — 몇 분 뒤 정리를 다시 실행하세요.") from exc
            raise
        # 지워져야 서브넷 그룹·파라미터 그룹·DB 보안 그룹을 지울 수 있다 — 최대 15분 기다린다.
        for _ in range(45):
            try:
                clients["rds"].describe_db_instances(DBInstanceIdentifier=item.name)
            except Exception as exc:  # noqa: BLE001
                if "NotFound" in error_code(exc):
                    return message
                raise
            sleep(20)
        raise _Kept(message + " 아직 지워지는 중이라 서브넷 그룹·파라미터 그룹·접속 정보는 다음 정리 때 지웁니다.")
    if k == "rds_subnet_group":
        _retry(lambda: clients["rds"].delete_db_subnet_group(DBSubnetGroupName=item.name),
               ("InvalidDBSubnetGroupStateFault",), sleep=sleep, attempts=4, delay=20)
        return "지웠습니다."
    if k == "rds_parameter_group":
        _retry(lambda: clients["rds"].delete_db_parameter_group(DBParameterGroupName=item.name),
               ("InvalidDBParameterGroupState", "InvalidDBParameterGroupStateFault"), sleep=sleep, attempts=4, delay=20)
        return "지웠습니다."
    if k == "secret":
        clients["secretsmanager"].delete_secret(SecretId=item.arn, RecoveryWindowInDays=7)
        return "지웠습니다(7일 안에는 Secrets Manager 에서 되살릴 수 있음)."
    if k == "alarm":
        clients["cloudwatch"].delete_alarms(AlarmNames=[item.name])
        return "지웠습니다."
    if k == "sns_topic":
        clients["sns"].delete_topic(TopicArn=item.arn)
        return "지웠습니다(메일 구독 포함)."
    if k == "ecr":
        clients["ecr"].delete_repository(repositoryName=item.name, force=True)
        return "이미지와 함께 지웠습니다."
    if k == "log_group":
        clients["logs"].delete_log_group(logGroupName=item.name)
        return "지웠습니다."
    if k == "security_group":
        # 태스크·로드밸런서의 네트워크 인터페이스가 풀리는 데 몇 분 걸린다.
        _retry(lambda: clients["ec2"].delete_security_group(GroupId=item.arn), ("DependencyViolation",),
               sleep=sleep, attempts=12, delay=15)
        return "지웠습니다."
    if k == "ecs_cluster":
        ecs = clients["ecs"]
        left = _pages(ecs, "list_services", "serviceArns", cluster=item.name)
        active = [s["serviceName"] for i in range(0, len(left), 10)
                  for s in ecs.describe_services(cluster=item.name, services=left[i:i + 10]).get("services", [])
                  if s.get("status") == "ACTIVE"]
        if active:
            raise _Kept(f"다른 서비스({', '.join(active[:3])})가 남아 있어 클러스터는 두었습니다(빈 클러스터는 요금 없음).")
        _retry(lambda: clients["ecs"].delete_cluster(cluster=item.name),
               ("ClusterContainsServicesException", "ClusterContainsTasksException"),
               sleep=sleep, attempts=8, delay=15)
        return "지웠습니다."
    if k == "s3_bucket":
        s3 = clients["s3"]
        while True:
            listed = s3.list_objects_v2(Bucket=item.name, MaxKeys=1000)
            keys = [{"Key": o["Key"]} for o in listed.get("Contents", [])]
            if keys:
                errors = s3.delete_objects(Bucket=item.name, Delete={"Objects": keys, "Quiet": True}).get("Errors") or []
                if errors:
                    first = errors[0]
                    raise RuntimeError(f"파일 {len(errors)}개를 지우지 못했습니다(예: {first.get('Key')} — "
                                       f"{first.get('Code')}: {first.get('Message')}).")
            if not listed.get("IsTruncated"):
                break
        s3.delete_bucket(Bucket=item.name)
        return "파일을 비우고 지웠습니다."
    raise ValueError(f"알 수 없는 종류: {k}")


def _sg_order(groups: list[Item]) -> list[Item]:
    """다른 선택 그룹을 참조하는 그룹(태스크 → ALB 허용)을 먼저 지운다 — 참조된 쪽은 그 뒤에야 지워진다."""
    chosen = {g.arn for g in groups}
    return sorted(groups, key=lambda g: (0 if chosen & set(g.extra.get("refs", [])) else 1, g.name))


def delete(clients: dict, ids: Iterable[str], *, region: str, approved: bool, records: Iterable[dict] = (),
           sleep: Callable[[float], None] = time.sleep,
           on_progress: Optional[Callable[[dict], None]] = None) -> dict:
    """승인한 항목만 정해진 순서로 지운다. 각 항목의 결과를 돌려준다(하나가 실패해도 나머지는 계속)."""
    if not approved:
        raise PermissionError("승인 없이 지우지 않습니다.")
    wanted = list(dict.fromkeys(ids))
    items, gaps = find(clients, region=region, records=records)
    by_id = {i.id: i for i in items}
    results: list[dict] = []
    chosen: list[Item] = []
    for item_id in wanted:
        item = by_id.get(item_id)
        if item is None:
            results.append({"id": item_id, "ok": False, "skipped": True, "message": "지금은 목록에 없습니다(이미 지워졌거나 다른 리전)."})
        elif not item.tagged:
            results.append({"id": item_id, "ok": False, "skipped": True, "message": UNTAGGED_NOTE})
        else:
            chosen.append(item)
    chosen.sort(key=lambda i: (ORDER.get(i.kind, 99), i.name))
    sgs = _sg_order([i for i in chosen if i.kind == "security_group"])
    ordered = [i for i in chosen if i.kind != "security_group"]
    pos = next((n for n, i in enumerate(ordered) if ORDER[i.kind] > ORDER["security_group"]), len(ordered))
    ordered[pos:pos] = sgs
    db_left: set[str] = set()  # 지우지 못한 DB 의 서비스

    def tell(item: Item, n: int) -> None:
        if on_progress is not None:
            try:
                on_progress({"done": n, "total": len(ordered), "current": f"{item.label} {item.name}"})
            except Exception:  # noqa: BLE001 - 진행 표시는 삭제를 막지 않는다
                pass

    for n, item in enumerate(ordered):
        tell(item, n)
        if item.kind in DB_DEPENDENTS and (item.service in db_left or "" in db_left):
            results.append({"id": item.id, "label": item.label, "name": item.name, "ok": False, "skipped": True,
                            "message": "DB 가 아직 남아 있어 두었습니다 — DB 가 지워진 뒤 정리를 다시 실행하세요."})
            continue
        try:
            message = _delete_one(clients, item, region=region, all_items=items, sleep=sleep)
            results.append({"id": item.id, "label": item.label, "name": item.name, "ok": True, "message": message})
        except _Kept as kept:
            if item.kind == "rds_instance":
                db_left.add(item.service)
            results.append({"id": item.id, "label": item.label, "name": item.name, "ok": False, "skipped": True,
                            "message": str(kept)})
        except Exception as exc:  # noqa: BLE001
            hint = ""
            if error_code(exc) in ("DependencyViolation", "ResourceInUse", "ClusterContainsServicesException",
                                   "ClusterContainsTasksException"):
                hint = " — 앞 단계 리소스가 아직 정리 중입니다. 몇 분 뒤 정리를 다시 실행하세요."
            elif error_code(exc) in ("AccessDenied", "AccessDeniedException", "UnauthorizedOperation",
                                     "WAFInvalidPermissionPolicyException"):
                hint = " — 권한표에 2.0.26 정리 권한이 없습니다(권한 템플릿을 다시 적용)."
            if item.kind == "rds_instance":
                db_left.add(item.service)
            logger.warning("정리 실패 %s: %s", item.id, error_message(exc))
            results.append({"id": item.id, "label": item.label, "name": item.name, "ok": False,
                            "message": f"지우지 못했습니다: {error_message(exc)}{hint}"})
    return {"region": region, "results": results, "gaps": gaps,
            "deleted": sum(1 for r in results if r.get("ok")),
            "failed": sum(1 for r in results if not r.get("ok") and not r.get("skipped"))}
