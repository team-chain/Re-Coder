"""Verify managed container builds before the existing ECS pipeline changes any resources.

No caller-provided scan results, S3 locations, repository names, or image URLs are trusted.
Source snapshots use the same exclusions and bounds as extension/managedBundle.ts.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from core.schemas import ManagedBuildReference, SecurityScanResult, SecurityFinding, SecurityScanTool, SecurityScanSeverity as Severity, SBOMRecord

REPOSITORY = "recoder-managed-images-v1"
_HEX = re.compile(r"[a-f0-9]{64}\Z")
_OMIT_DIR = {".git", ".aws", ".ssh", ".recoder", ".vscode", "node_modules", ".venv", "venv", "dist", "build", "coverage", "__pycache__"}
_OMIT_FILE = re.compile(r"(?:^\.env(?:\.|$)|\.(?:pem|key|p12|pfx|keystore|sqlite|db|log|vsix|bundle)$|^(?:credentials|id_rsa|id_ed25519|\.npmrc|\.pypirc)$)", re.I)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _safe_path(value) -> bool:
    if not isinstance(value, str) or not value or len(value) > 1024:
        return False
    return all(p not in ("", ".", "..") and not re.search(r'[\\:\x00-\x1f<>"|?*]', p)
               and not p.endswith((".", " ")) and not re.match(r"^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)", p, re.I)
               for p in value.split("/"))


def source_manifest(workspace: str) -> list[dict]:
    _require(bool(workspace), "관리형 배포의 프로젝트 폴더가 없습니다.")
    root = Path(workspace).resolve(strict=True)
    _require(root.is_dir(), "프로젝트 폴더가 없습니다.")
    files, names, total = [], set(), 0

    def walk(folder: Path):
        nonlocal total
        for p in folder.iterdir():
            name = p.name
            if name in _OMIT_DIR or _OMIT_FILE.search(name) or (name.startswith(".") and name not in (".gitignore", ".dockerignore")):
                continue
            _require(not p.is_symlink() and p.resolve().is_relative_to(root), "프로젝트 밖 경로나 심볼릭 링크는 배포할 수 없습니다.")
            if p.is_dir():
                walk(p)
                continue
            _require(p.is_file(), "일반 파일이 아닌 빌드 소스입니다.")
            relative = p.relative_to(root).as_posix()
            _require(_safe_path(relative) and relative.lower() not in names, "빌드 소스 경로가 올바르지 않거나 중복됩니다.")
            before = p.stat()
            _require(before.st_size <= 5 * 1024 * 1024, "빌드 소스 파일 크기 제한을 초과했습니다.")
            with p.open("rb") as stream:
                opened = os.fstat(stream.fileno())
                data = stream.read(5 * 1024 * 1024 + 1)
                after = os.fstat(stream.fileno())
            _require((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) ==
                     (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns) ==
                     (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
                     and len(data) == before.st_size and not p.is_symlink() and p.resolve().is_relative_to(root),
                     "검증 중 소스가 변경되었습니다. 다시 준비하세요.")
            total += len(data)
            _require(total <= 32 * 1024 * 1024 and len(files) < 4000, "빌드 소스 크기 제한을 초과했습니다.")
            names.add(relative.lower())
            files.append({"path": relative, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()})

    walk(root)
    return sorted(files, key=lambda f: f["path"])


def _read_object(client, bucket: str, key: str) -> bytes:
    response = client.get_object(Bucket=bucket, Key=key)
    body = response["Body"]
    try:
        data = body.read(4 * 1024 * 1024 + 1)
    finally:
        body.close()
    _require(len(data) <= 4 * 1024 * 1024, "관리형 빌드 결과 크기를 초과했습니다.")
    return data


def validate_report(report: dict, ref: ManagedBuildReference, account: str, region: str) -> None:
    _require(isinstance(report, dict) and type(report.get("schema")) is int and report["schema"] == 1 and report.get("status") == "completed"
             and report.get("runId") == ref.run_id and report.get("buildId") == ref.build_id
             and report.get("sourceSha256") == ref.source_sha256, "관리형 빌드 결과가 요청과 다르거나 완료되지 않았습니다.")
    digest = report.get("imageDigest", "")
    _require(isinstance(digest, str) and re.fullmatch(r"sha256:[a-f0-9]{64}", digest) is not None
             and report.get("image") == f"{account}.dkr.ecr.{region}.amazonaws.com/{REPOSITORY}@{digest}", "검사한 이미지 주소가 올바르지 않습니다.")
    sbom_hash = report.get("sbomSha256")
    _require(isinstance(sbom_hash, str) and bool(_HEX.fullmatch(sbom_hash)), "SBOM 해시가 없습니다.")
    files = report.get("sourceFiles")
    _require(isinstance(files, list) and 0 < len(files) <= 4000, "빌드 소스 목록이 없습니다.")
    names, size = set(), 0
    for f in files:
        _require(isinstance(f, dict) and _safe_path(f.get("path")) and type(f.get("size")) is int
                 and 0 <= f["size"] <= 5 * 1024 * 1024 and isinstance(f.get("sha256"), str)
                 and bool(_HEX.fullmatch(f["sha256"])) and f["path"].lower() not in names, "빌드 소스 목록이 올바르지 않습니다.")
        names.add(f["path"].lower())
        size += f["size"]
    _require(size <= 32 * 1024 * 1024 and any(f["path"] == "Dockerfile" for f in files), "Dockerfile이 없거나 빌드 사본이 너무 큽니다.")
    policy, checks = report.get("policy"), report.get("checks")
    _require(isinstance(policy, dict) and policy.get("decision") == "allow" and isinstance(checks, list) and len(checks) == 4, "배포 정책 검사를 통과하지 못했습니다.")
    for tool in ("gitleaks", "hadolint", "dependencies", "image"):
        found = [c for c in checks if isinstance(c, dict) and c.get("tool") == tool]
        _require(len(found) == 1, "검사 결과가 중복되거나 없습니다.")
        c = found[0]
        if tool == "dependencies" and c.get("status") == "not_applicable" and c.get("findings") == []:
            continue
        findings = c.get("findings")
        _require(c.get("status") == "completed" and isinstance(findings, list) and len(findings) <= (5000 if tool == "image" else 200)
                 and all(type(c.get(k)) is int and c[k] >= 0 for k in ("count", "criticalCount", "highCount", "blockingCount")), "보안 검사가 완료되지 않았습니다.")
        _require(c["count"] >= len(findings) and c["blockingCount"] == c["criticalCount"] + c["highCount"] <= c["count"], "검사 요약이 올바르지 않습니다.")
        if tool == "image":
            _require(c["count"] == len(findings), "이미지 검사 상세가 누락되어 배포를 중단했습니다.")
        for f in findings:
            _require(isinstance(f, dict) and isinstance(f.get("title"), str) and f.get("severity") in ("critical", "high", "medium", "low", "unknown", "negligible"), "검사 상세 결과가 올바르지 않습니다.")
        for key, severity in (("criticalCount", "critical"), ("highCount", "high")):
            _require(c[key] >= sum(f["severity"] == severity for f in findings), "검사 요약과 상세 결과가 다릅니다.")
        _require(not (tool == "gitleaks" and c["count"] or tool == "hadolint" and c["blockingCount"] or tool == "image" and c["criticalCount"]), "배포를 차단하는 보안 문제가 있습니다.")


@dataclass(frozen=True)
class VerifiedBuild:
    report: dict
    sbom: bytes

    def assert_sources(self, workspace: str) -> None:
        _require(source_manifest(workspace) == sorted(self.report["sourceFiles"], key=lambda f: f["path"]),
                 "이미지 준비 후 소스가 변경되었습니다. 이미지를 다시 준비하고 배포하세요.")

    def scan_result(self) -> SecurityScanResult:
        findings = []
        for check in self.report["checks"]:
            if check["tool"] == "dependencies":
                continue
            tool = {"image": SecurityScanTool.TRIVY, "gitleaks": SecurityScanTool.GITLEAKS, "hadolint": SecurityScanTool.HADOLINT}[check["tool"]]
            for f in check["findings"]:
                severity = f["severity"]
                if severity not in {s.value for s in Severity}:
                    severity = "low"
                findings.append(SecurityFinding(tool=tool, severity=Severity(severity), title=f["title"],
                                                location=f.get("file"), fix_suggestion=f.get("fix")))
        result = SecurityScanResult(findings=findings, image=self.report["image"])
        result.compute_pass()
        return result

    def save_sbom(self) -> SBOMRecord:
        from core.sbom import _SBOM_DIR
        _SBOM_DIR.mkdir(parents=True, exist_ok=True)
        target = _SBOM_DIR / (self.report["sbomSha256"] + ".json")
        # Content-addressed cache shared with existing vulnerability rechecks.
        import tempfile
        with tempfile.NamedTemporaryFile(dir=_SBOM_DIR, prefix="managed-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(self.sbom)
        try:
            _require(not target.is_symlink(), "SBOM 저장 경로가 올바르지 않습니다.")
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        return SBOMRecord(image=self.report["image"], sbom_path=str(target), sbom_hash=self.report["sbomSha256"],
                          package_count=len(json.loads(self.sbom).get("components", [])), generated_at=datetime.now(timezone.utc).isoformat())


def verify_build(ref: ManagedBuildReference, workspace: str, region: str, clients: dict) -> VerifiedBuild:
    account = clients["sts"].get_caller_identity()["Account"]
    _require(bool(re.fullmatch(r"\d{12}", account)), "AWS 계정을 확인하지 못했습니다.")
    bucket = f"recoder-container-{account}-{region}"
    builds = clients["codebuild"].batch_get_builds(ids=[ref.build_id]).get("builds", [])
    _require(len(builds) == 1, "AWS 빌드 기록이 없습니다.")
    build = builds[0]
    _require(build.get("id") == ref.build_id and build.get("projectName") == "recoder-container-v1" and build.get("buildStatus") == "SUCCEEDED"
             and build.get("arn") == f"arn:aws:codebuild:{region}:{account}:build/{ref.build_id}"
             and build.get("source", {}).get("type") == "S3" and build["source"].get("location") == f"{bucket}/sources/{ref.run_id}.zip", "현재 AWS 계정·리전에서 성공한 이미지 빌드가 아닙니다.")
    ended = build.get("endTime")
    _require(isinstance(ended, datetime) and ended.tzinfo is not None
             and -300 <= (datetime.now(timezone.utc) - ended).total_seconds() <= 24 * 60 * 60,
             "이미지 검사 결과가 24시간을 넘었거나 완료 시간을 확인할 수 없습니다. 다시 준비하세요.")
    env = build.get("environment", {}).get("environmentVariables", [])
    for key, value in (("RECODER_RUN_ID", ref.run_id), ("RECODER_SOURCE_SHA256", ref.source_sha256), ("RECODER_REPORT_BUCKET", bucket)):
        _require([v.get("value") for v in env if isinstance(v, dict) and v.get("name") == key] == [value], "AWS 빌드 식별자가 다릅니다.")
    report = json.loads(_read_object(clients["s3"], bucket, f"reports/{ref.run_id}/report.json"))
    validate_report(report, ref, account, region)
    sbom = _read_object(clients["s3"], bucket, f"reports/{ref.run_id}/sbom.json")
    _require(hashlib.sha256(sbom).hexdigest() == report["sbomSha256"], "SBOM SHA256이 검사 결과와 다릅니다.")
    data = json.loads(sbom)
    _require(isinstance(data, dict) and data.get("bomFormat") == "CycloneDX" and isinstance(data.get("specVersion"), str)
             and isinstance(data.get("components"), list), "유효한 CycloneDX SBOM이 아닙니다.")
    images = clients["ecr"].describe_images(repositoryName=REPOSITORY, imageIds=[{"imageTag": ref.run_id}]).get("imageDetails", [])
    _require(len(images) == 1 and images[0].get("imageDigest") == report["imageDigest"], "ECR 이미지가 검사한 이미지와 다릅니다.")
    verified = VerifiedBuild(report, sbom)
    verified.assert_sources(workspace)
    return verified
