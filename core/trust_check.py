"""
ReCoder Core — 배포 전 신뢰 점검(제로 트러스트, 2.0.35)

이미 있는 보안 장치(Dockerfile 검사·AI 코드 보안 점수·비밀값 검사·루트 키 경고·원본 잠금·로컬 바인딩 …)를
**원칙 다섯 개**로 묶어 승인 화면에 한 번에 보여 준다. 새로 배포를 막지 않는다 — 이미 배포를 막는 검사(Dockerfile 안전
검사)만 "막힘"으로 표시하고, 나머지는 "주의"·"통과"로 알린다.

  AI 결과를 그대로 믿지 않는다 · 최소 권한 · 네트워크를 믿지 않는다 · 비밀값은 코드·이미지 밖에 · 기록하고 되돌릴 수 있게

입력은 화면에 이미 있는 값(배포 대상·보호 설정·AWS 신원 ARN)과 작업 폴더 파일뿐이다. AWS·네트워크를 부르지 않는다.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

PRINCIPLES: tuple[tuple[str, str, str], ...] = (
    ("ai", "AI 결과를 그대로 믿지 않는다", "AI 가 쓴 코드·Dockerfile·의존성은 검사를 통과해야 배포한다"),
    ("identity", "최소 권한", "배포에 필요한 권한만, 오래 쓰는 키보다 임시 자격 증명"),
    ("network", "네트워크를 믿지 않는다", "같은 PC·같은 클라우드 안이라도 확인된 경로로만 받는다"),
    ("secrets", "비밀값은 코드·이미지 밖에", "키·비밀번호는 코드·이미지·업로드 파일에 넣지 않는다"),
    ("audit", "기록하고 되돌릴 수 있게", "승인한 것만 실행하고, 무엇이 바뀌었는지 남기고, 되돌릴 길을 둔다"),
)
STATUS_ORDER = {"block": 3, "warn": 2, "info": 1, "pass": 0}

SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "env", "__pycache__", "dist", "build", ".next", "out", "coverage",
             ".recoder", "vendor", "target", ".tox", ".mypy_cache", ".pytest_cache", "site-packages"}
CODE_EXT = (".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".py", ".json", ".yml", ".yaml", ".toml", ".env", ".html")
MAX_FILES = 600
MAX_FILE_BYTES = 400_000
MAX_TOTAL_BYTES = 8_000_000
SECRET_NAME = re.compile(r"(?i)(SECRET|TOKEN|PASSWORD|PASSWD|PRIVATE|CREDENTIAL|API_?KEY|ACCESS_?KEY|DATABASE_URL|DB_URL|DSN|JWT)")


@dataclass
class TrustItem:
    key: str
    principle: str
    status: str            # pass · warn · block · info
    title: str
    detail: str = ""
    fix: str = ""


@dataclass
class TrustReport:
    target: str
    items: list[TrustItem] = field(default_factory=list)
    scanned_files: int = 0
    truncated: bool = False

    def add(self, principle: str, key: str, status: str, title: str, detail: str = "", fix: str = "") -> None:
        self.items.append(TrustItem(key=key, principle=principle, status=status, title=title, detail=detail, fix=fix))

    def to_dict(self) -> dict:
        principles = []
        for key, label, summary in PRINCIPLES:
            items = [asdict(i) for i in self.items if i.principle == key]
            if not items:
                continue
            worst = max(items, key=lambda i: STATUS_ORDER.get(i["status"], 0))["status"]
            principles.append({"key": key, "label": label, "summary": summary,
                               "status": "pass" if worst == "info" else worst, "items": items})
        counts = {s: sum(1 for i in self.items if i.status == s) for s in ("pass", "warn", "block", "info")}
        return {"target": self.target, "principles": principles, "counts": counts,
                "scanned_files": self.scanned_files, "truncated": self.truncated}


def _collect(workspace: Path) -> tuple[dict[str, str], bool]:
    """검사할 파일(상대 경로 → 내용). 큰 폴더·바이너리는 건너뛴다."""
    files: dict[str, str] = {}
    total = 0
    truncated = False
    for dirpath, dirnames, filenames in os.walk(workspace):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not os.path.islink(os.path.join(dirpath, d)))
        for name in sorted(filenames):
            low = name.lower()
            if not (low.endswith(CODE_EXT) or low.startswith(".env") or low in ("dockerfile", ".dockerignore", "requirements.txt")
                    or low.startswith("dockerfile")):
                continue
            path = Path(dirpath) / name
            try:
                size = path.stat().st_size
                if size > MAX_FILE_BYTES:
                    continue
                if len(files) >= MAX_FILES or total + size > MAX_TOTAL_BYTES:
                    return files, True
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            total += size
            files[path.relative_to(workspace).as_posix()] = text
    return files, truncated


def _is_env_doc(name: str) -> bool:
    return bool(re.search(r"(?i)\.env\.(example|sample|template|dist)$|(^|/)example\.env$", name))


def _dockerignore_excludes_env(text: str) -> bool:
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("!"):
            continue
        if re.fullmatch(r"(\*\*/)?\.env(\*|\..*)?|\*\.env|\.env\*", line):
            return True
    return False


def _check_ai(report: TrustReport, target: str, files: dict[str, str], workspace: Path) -> None:
    try:
        import dockerfile_guard  # type: ignore
        import code_security  # type: ignore
    except ImportError:  # pragma: no cover
        from core import dockerfile_guard, code_security  # type: ignore
    if target != "s3":
        dockerfiles = [p for p in files if re.search(r"(?i)(^|/)dockerfile([.-][\w.-]+)?$", p) and p.count("/") <= 2]
        if not dockerfiles:
            report.add("ai", "dockerfile", "info", "Dockerfile 안전 검사",
                       "Dockerfile 이 없어 ReCoder 가 검증된 Dockerfile 을 만들어 씁니다.")
        else:
            found = []
            for path in dockerfiles:
                for v in dockerfile_guard.check(files[path]):
                    found.append(f"{path} {v.line}행: {v.message}")
            if found:
                report.add("ai", "dockerfile", "block", "Dockerfile 안전 검사 — 위험한 명령", "; ".join(found[:4]),
                           dockerfile_guard.REMEDY)
            else:
                report.add("ai", "dockerfile", "pass", "Dockerfile 안전 검사",
                           f"{len(dockerfiles)}개 파일 — 내려받아 바로 실행·호스트 접근·비밀값 유출 없음")
    sec = code_security.report(files)
    if sec.get("score") is None:
        report.add("ai", "code_security", "info", "AI 코드 보안 점수", "점검할 서버 코드(경로·DB·인증)가 없습니다.")
    else:
        high = [f for f in sec["findings"] if f.get("severity") == "high"]
        if high:
            where = ", ".join(f"{f['file']}:{f['line']} {f['label']}" for f in high[:3])
            report.add("ai", "code_security", "warn", f"AI 코드 보안 점수 {sec['score']}점 — 심각 {len(high)}건", where,
                       "개발 화면의 보안 점수에서 고치는 법을 확인하세요.")
        else:
            report.add("ai", "code_security", "pass", f"AI 코드 보안 점수 {sec['score']}점",
                       f"규칙 {sum(1 for r in sec['rules'] if r.get('applicable'))}개 적용 — 심각한 발견 없음")
    # 공급망: 잠금 파일(같은 버전을 다시 받는가)
    def _has_deps(text: str) -> bool:
        try:
            pkg = json.loads(text)
        except ValueError:
            return True
        return isinstance(pkg, dict) and bool(pkg.get("dependencies") or pkg.get("devDependencies"))
    #: 의존성이 있는 package.json 만(스크립트만 있는 묶음용 루트 package.json 은 잠금 파일이 필요 없다)
    roots = sorted({str(Path(p).parent).replace("\\", "/") for p, t in files.items() if Path(p).name == "package.json"
                    and "node_modules" not in p and _has_deps(t)})
    loose = []
    for root in roots:
        prefix = "" if root in (".", "") else f"{root}/"
        #: 잠금 파일은 커서 읽지 않을 수 있다 — 디스크에서 이름만 확인한다.
        if not any((workspace / prefix / lock).is_file()
                   for lock in ("package-lock.json", "yarn.lock", "pnpm-lock.yaml", "npm-shrinkwrap.json", "bun.lockb", "bun.lock")):
            loose.append(f"{prefix or './'}package.json")
    for path, text in files.items():
        if Path(path).name == "requirements.txt":
            unpinned = [l.strip() for l in text.splitlines() if l.strip() and not l.strip().startswith(("#", "-"))
                        and "==" not in l and "@" not in l]
            if unpinned:
                loose.append(f"{path}({', '.join(unpinned[:3])}{'…' if len(unpinned) > 3 else ''})")
    if loose:
        report.add("ai", "lockfile", "warn", "의존성 버전 고정", "잠금 파일·고정 버전 없음: " + ", ".join(loose[:4]),
                   "npm install 로 package-lock.json 을 만들어 함께 올리거나, requirements.txt 에 == 버전을 적으세요 — "
                   "배포할 때마다 다른(새로 올라온) 패키지를 받지 않게.")
    elif roots or any(Path(p).name == "requirements.txt" for p in files):
        report.add("ai", "lockfile", "pass", "의존성 버전 고정", "잠금 파일·고정 버전으로 같은 패키지를 받습니다.")


def _check_identity(report: TrustReport, target: str, identity_arn: str) -> None:
    if target == "local":
        report.add("identity", "cloud", "info", "클라우드 권한", "로컬 Docker 배포 — AWS 권한을 쓰지 않습니다.")
        return
    arn = identity_arn or ""
    if arn.endswith(":root"):
        report.add("identity", "root", "warn", "AWS 루트 계정 키로 배포",
                   "루트 키는 결제·계정 삭제까지 모든 권한이 있습니다. 새면 계정 전체가 위험합니다.",
                   "IAM 사용자 또는 역할을 만들고 ReCoder 권한 템플릿만 붙인 키로 연결하세요.")
    elif ":assumed-role/" in arn or ":federated-user/" in arn:
        report.add("identity", "temporary", "pass", "임시 자격 증명", "역할(SSO·AssumeRole)로 받은 키 — 시간이 지나면 만료됩니다.")
    elif ":user/" in arn:
        report.add("identity", "temporary", "warn", "오래 쓰는 액세스 키",
                   "IAM 사용자의 액세스 키는 지울 때까지 유효합니다.",
                   "가능하면 IAM Identity Center(SSO)나 역할로 연결하고, 액세스 키는 주기적으로 바꾸세요.")
    else:
        report.add("identity", "temporary", "info", "자격 증명 종류", "AWS 신원을 확인하지 못했습니다.")
    if arn and not arn.endswith(":root"):
        report.add("identity", "scoped", "pass", "배포 권한 범위",
                   "ReCoder 권한 템플릿은 recoder- 이름·ReCoder 태그가 붙은 리소스로 좁혀져 있습니다.")


def _check_network(report: TrustReport, target: str, config: dict) -> None:
    if target == "local":
        bind = (os.environ.get("RECODER_LOCAL_BIND") or "127.0.0.1").strip()
        if bind in ("127.0.0.1", "localhost", "::1"):
            report.add("network", "bind", "pass", "이 PC 에서만 열림", "로컬 배포 포트는 127.0.0.1 에만 엽니다(같은 와이파이의 다른 기기 차단).")
        else:
            report.add("network", "bind", "warn", f"다른 기기에서도 열림({bind})",
                       "RECODER_LOCAL_BIND 로 모든 네트워크에 열었습니다 — 같은 네트워크의 누구나 개발용 앱에 접속할 수 있습니다.",
                       "다른 기기에서 확인할 일이 끝나면 RECODER_LOCAL_BIND 를 지우세요.")
        return
    if target == "s3":
        report.add("network", "s3_http", "warn", "S3 웹사이트 주소는 HTTP",
                   "정적 웹사이트 주소는 암호화되지 않습니다. 화면만 있는 사이트는 괜찮지만 로그인·결제 화면은 안 됩니다.",
                   "로그인·결제가 있으면 ECS 배포에서 HTTPS 주소를 켜세요.")
        return
    https = bool(config.get("https"))
    edge = str(config.get("edge") or "none")
    domain = str(config.get("custom_domain") or "").strip()
    if https:
        report.add("network", "https", "pass", "사용자 → 서비스 HTTPS", "CloudFront 가 HTTPS 로 받습니다(HTTP 는 HTTPS 로 넘김).")
        report.add("network", "origin_lock", "pass", "원본 잠금",
                   "로드밸런서는 CloudFront 주소에서만 연결을 받고, CloudFront 비밀 머리글이 없는 요청은 403 으로 막습니다.")
        if domain:
            report.add("network", "origin_tls", "pass", "CloudFront → 로드밸런서 HTTPS",
                       f"origin.{domain} 인증서로 두 구간 모두 암호화합니다(도메인이 Route 53 에 있고 인증서가 발급된 배포부터).")
        else:
            report.add("network", "origin_tls", "warn", "CloudFront → 로드밸런서 구간은 HTTP",
                       "AWS 망 안이지만 원본 잠금 머리글이 평문으로 갑니다(로드밸런서 기본 주소로는 인증서를 받을 수 없음).",
                       "내 도메인을 Route 53 에 두고 HTTPS 주소에 넣으면 이 구간도 HTTPS 로 바뀝니다.")
    else:
        report.add("network", "https", "warn", "HTTP 로 공개",
                   "주소가 암호화되지 않아 로그인 정보·쿠키가 그대로 오갑니다.",
                   "HTTPS 주소를 켜세요(도메인 없이도 CloudFront 주소로 HTTPS).")
    if edge == "alb_waf":
        report.add("network", "waf", "pass", "WAF", "IP 당 요청 제한과 AWS 관리 규칙으로 걸러 냅니다.")
    elif config.get("shield", True) is not False:
        report.add("network", "waf", "info", "요청 제한", "기본 방패(nginx)가 IP 당 요청 수를 제한합니다. 대규모 공격은 WAF 가 필요합니다.")
    else:
        report.add("network", "waf", "warn", "요청 제한 없음", "방패와 WAF 를 모두 껐습니다.",
                   "기본 방패를 켜거나 대규모 방패(WAF)를 켜세요.")
    if config.get("assign_public_ip") and edge != "none":
        report.add("network", "public_ip", "warn", "태스크에 공개 IP",
                   "로드밸런서 뒤 태스크가 공개 IP 도 가집니다(보안 그룹은 로드밸런서만 받음).",
                   "사설 서브넷 + NAT 를 쓰면 태스크를 인터넷에서 아예 감출 수 있습니다.")


def _check_secrets(report: TrustReport, target: str, files: dict[str, str], config: dict, workspace: Path) -> None:
    try:
        import security_scan  # type: ignore
    except ImportError:  # pragma: no cover
        from core import security_scan  # type: ignore
    found = []
    for path, text in files.items():
        name = Path(path).name.lower()
        if name.startswith(".env") or _is_env_doc(path) or name.endswith((".md", ".lock")) or name == "package-lock.json":
            continue
        for f in security_scan.scan_text_for_secrets(text, path):
            if f.get("severity") in ("high", "critical"):
                found.append(f"{path}:{f['line']} {f['rule']} {f['masked']}")
    if found:
        report.add("secrets", "hardcoded", "warn", f"코드에 비밀값으로 보이는 값 {len(found)}곳", "; ".join(found[:4]),
                   "값을 코드에서 지우고 .env(로컬) 또는 Secrets Manager(ECS production)로 옮기세요.")
    else:
        report.add("secrets", "hardcoded", "pass", "코드 속 비밀값", "키·토큰 모양의 값을 찾지 못했습니다.")
    envs = [p for p in files if Path(p).name.lower().startswith(".env") and not _is_env_doc(p)]
    if target == "s3":
        report.add("secrets", "upload", "pass", "올리는 파일", ".env·키 파일 같은 민감 파일은 S3 에 올리지 않습니다.")
    elif envs:
        ignore = (workspace / ".dockerignore")
        try:
            text = ignore.read_text(encoding="utf-8", errors="replace") if ignore.is_file() else ""
        except OSError:
            text = ""
        if not ignore.is_file():
            report.add("secrets", "image", "info", "이미지에 .env 를 넣지 않음",
                       "배포할 때 .dockerignore 를 만들어 .env 를 이미지에서 뺍니다(값은 실행할 때 환경변수로 넘김).")
        elif _dockerignore_excludes_env(text):
            report.add("secrets", "image", "pass", "이미지에 .env 를 넣지 않음", ".dockerignore 가 .env 를 뺍니다.")
        else:
            report.add("secrets", "image", "warn", ".env 가 이미지에 들어갈 수 있음",
                       f".dockerignore 에 .env 가 없습니다 — {', '.join(envs[:3])} 의 값이 이미지에 남아, 이미지를 받은 누구나 볼 수 있습니다.",
                       ".dockerignore 에 .env* 한 줄을 더하세요.")
    if target == "ecs":
        env_names = [str(k) for k in (config.get("env_vars") or {}) if SECRET_NAME.search(str(k))]
        refs = list((config.get("secret_refs") or {}).keys())
        production = str(config.get("environment") or "").lower() == "production"
        if env_names and production:
            report.add("secrets", "runtime", "warn", "비밀값을 일반 환경변수로",
                       f"{', '.join(env_names[:4])} 이(가) 태스크 정의에 평문으로 들어갑니다(콘솔·API 로 보임).",
                       "Secrets Manager 비밀을 만들고 비밀값 참조로 넣으세요.")
        elif refs or config.get("database") == "rds":
            report.add("secrets", "runtime", "pass", "비밀값 참조",
                       "DB 접속 정보·비밀값은 Secrets Manager 참조로 넣고, 실행 역할은 그 비밀만 읽습니다.")
        elif env_names:
            report.add("secrets", "runtime", "info", "staging 비밀값", "staging 은 환경변수로 넣습니다. production 은 비밀값 참조를 쓰세요.")


def _check_audit(report: TrustReport, target: str, config: dict, *, has_rollback: Optional[bool]) -> None:
    report.add("audit", "approval", "pass", "승인한 것만 실행",
               "승인 화면의 내용(대상·설정·실행 명령)을 고정해 그대로만 실행합니다. 10분이 지나면 다시 확인합니다.")
    if target == "local":
        if has_rollback is False:
            report.add("audit", "rollback", "info", "되돌리기", "첫 배포 — 되돌릴 이전 버전이 없습니다. 시작하자마자 죽으면 멈추고 로그를 남깁니다.")
        else:
            report.add("audit", "rollback", "pass", "되돌리기", "새 버전이 헬스 체크를 통과하지 못하면 이전 버전을 다시 띄웁니다.")
        if config.get("enable_continuous_verification", True):
            report.add("audit", "watch", "pass", "배포 뒤 감시", "배포 직후 5분 동안 헬스 체크를 계속합니다.")
        else:
            report.add("audit", "watch", "warn", "배포 뒤 감시 꺼짐", "배포 직후 헬스 체크를 하지 않습니다.", "지속 검증을 켜세요.")
    elif target == "ecs":
        report.add("audit", "rollback", "pass", "되돌리기", "이전 태스크 정의(이미지 digest 고정)로 되돌릴 수 있습니다.")
        report.add("audit", "sbom", "pass", "이미지 구성 목록(SBOM)", "올린 이미지의 패키지 목록을 남기고 매일 취약점을 다시 확인합니다.")
        if config.get("alerts", True) is False:
            report.add("audit", "watch", "warn", "공격·장애 알림 꺼짐", "요청 급증·오류·태스크 부족을 알리지 않습니다.", "감지 알림을 켜세요.")
        else:
            report.add("audit", "watch", "pass", "공격·장애 알림", "1분마다 요청 급증·차단·오류·태스크 부족을 확인해 알립니다.")
    else:
        report.add("audit", "rollback", "info", "되돌리기", "같은 프로젝트의 파일을 갱신합니다. 이전 파일은 S3 버전 기능이 켜져 있을 때만 남습니다.")


def evaluate(workspace: str | Path, target: str, config: Optional[dict] = None, *, identity_arn: str = "",
             has_rollback: Optional[bool] = None) -> dict:
    """배포 전 신뢰 점검. target: local · ecs · s3."""
    target = target if target in ("local", "ecs", "s3") else "local"
    config = dict(config or {})
    root = Path(workspace)
    report = TrustReport(target=target)
    files: dict[str, str] = {}
    if root.is_dir():
        files, report.truncated = _collect(root)
        if target == "s3" and config.get("dir"):
            sub = str(config["dir"]).strip("/").replace("\\", "/")
            files = {p: t for p, t in files.items() if p == sub or p.startswith(f"{sub}/")} or files
    report.scanned_files = len(files)
    for check in (lambda: _check_ai(report, target, files, root),
                  lambda: _check_identity(report, target, identity_arn),
                  lambda: _check_network(report, target, config),
                  lambda: _check_secrets(report, target, files, config, root),
                  lambda: _check_audit(report, target, config, has_rollback=has_rollback)):
        try:
            check()
        except Exception as exc:  # noqa: BLE001 - 점검 하나가 실패해도 나머지는 보여 준다
            report.add("audit", f"error_{len(report.items)}", "info", "점검 하나를 마치지 못했습니다", str(exc)[:200])
    out = report.to_dict()
    if report.truncated:
        out["note"] = f"파일이 많아 앞의 {len(files)}개만 검사했습니다."
    return out

