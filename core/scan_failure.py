"""보안 스캔이 못 돌았을 때 — raw 에러를 **원인 + 다음 행동**으로 바꾼다.

보드 카드 「스캔 실패 표시가 raw 에러 — 원인·다음 행동을 담은 미검증 문구로 교체」.

무엇이 문제였나 (2026-09-20 실기기)
    로컬 Docker 배포 화면에서 스캔이 실패하면 "Error: trivy 스캔 실패" 또는
    trivy 의 stderr 가 그대로 떴다. 실제 원인은 둘 중 하나였는데 화면으로는
    구분이 안 됐다 — Docker Desktop 미실행(npipe 연결 실패) / 이미지 미빌드
    (No such image). fail-closed 원칙대로 "확인하지 못했습니다" 는 유지하되,
    **왜** 못 했고 **다음에 뭘 하면** 되는지를 같이 내려준다.

설계
    - 분류는 코어 한 곳(여기)에서 한다. 화면은 reason_code 로 분기만 한다.
    - 분류 기준은 스캐너·docker 의 stderr 문구다. 문구는 버전에 따라 조금씩
      달라지므로 여러 패턴을 OR 로 묶고, 못 맞추면 unknown 으로 두고 raw 첫
      줄을 원인에 남긴다 — 정보를 숨기진 않는다.
    - 결과 dict 는 ScanResult 에 그대로 합쳐진다: reason_code / cause /
      next_action / summary.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

#: 화면이 분기하는 코드. 새 코드를 추가하면 확장(ShipMode·Hubs)의 안내문도 같이.
DOCKER_NOT_RUNNING = "docker_not_running"
DOCKER_MISSING = "docker_missing"
IMAGE_NOT_FOUND = "image_not_found"
DOCKERFILE_MISSING = "dockerfile_missing"
SCANNER_MISSING = "scanner_missing"
SCANNER_PULL_FAILED = "scanner_pull_failed"
TIMEOUT = "timeout"
DEPENDENCIES_MISSING = "dependencies_missing"
REQUEST_FAILED = "request_failed"
UNKNOWN = "unknown"


@dataclass(frozen=True)
class ScanFailure:
    reason_code: str
    cause: str          # 무엇이 안 됐나 — 한 문장
    next_action: str    # 사용자가 다음에 할 일 — 한 문장
    raw: str = ""       # 분류 근거(원문 첫 줄). 화면은 접어서 보여 준다.

    def as_fields(self) -> dict:
        return {
            "reason_code": self.reason_code,
            "cause": self.cause,
            "next_action": self.next_action,
            "summary": f"확인하지 못했습니다 — {self.cause}",
            "message": self.raw or self.cause,
        }


_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # Docker 데몬이 없다 — Windows(npipe) / mac·linux(sock) / 공통 문구
    (DOCKER_NOT_RUNNING, re.compile(
        r"cannot connect to the docker daemon|is the docker daemon running|"
        r"docker daemon is not running|error during connect|"
        r"npipe|pipe/docker_engine|docker_engine|"
        r"/var/run/docker\.sock.*(no such file|connection refused|permission denied)|"
        r"connect: connection refused|dial unix|docker desktop.*(not running|starting)|"
        r"데몬이 없어|docker 데몬",
        re.IGNORECASE)),
    # 이미지가 아직 없다
    (IMAGE_NOT_FOUND, re.compile(
        r"no such image|image not found|unable to find image|"
        r"manifest unknown|manifest_unknown|repository does not exist|"
        r"pull access denied|not found: manifest|"
        r"image scan error.*not found|failed to find image|"
        r"아직 빌드되지 않아|이미지가 지정되지 않았",
        re.IGNORECASE)),
    # 스캐너 이미지(aquasec/trivy)를 못 받아옴 — 네트워크
    (SCANNER_PULL_FAILED, re.compile(
        r"unable to find image 'aquasec/trivy|pull.*aquasec/trivy|"
        r"failed to download|dial tcp.*i/o timeout|TLS handshake timeout|"
        r"temporary failure in name resolution|no route to host|"
        r"db download error|failed to update (the )?vulnerability database",
        re.IGNORECASE)),
    # 스캐너 바이너리가 없다 (hadolint / gitleaks 를 직접 실행하는 경우)
    (SCANNER_MISSING, re.compile(
        r"(hadolint|gitleaks|trivy)[^\n]*(not found|no such file|command not found|is not recognized)|"
        r"\[errno 2\][^\n]*(hadolint|gitleaks|trivy)",
        re.IGNORECASE)),
    (DOCKERFILE_MISSING, re.compile(r"dockerfile not found|dockerfile 을 찾지 못", re.IGNORECASE)),
]

_TEXT = {
    DOCKER_NOT_RUNNING: (
        "Docker Desktop 이 실행 중이 아니어서 스캐너를 띄우지 못했습니다.",
        "Docker Desktop 을 시작한 뒤 다시 검사하세요. 코어가 자동 시작을 시도했지만 준비되지 않았습니다.",
    ),
    DOCKER_MISSING: (
        "이 컴퓨터에 docker 명령이 없습니다.",
        "Docker Desktop 을 설치하세요. 설치 후에는 코어가 필요할 때 자동으로 띄웁니다.",
    ),
    IMAGE_NOT_FOUND: (
        "이미지가 아직 빌드되지 않아 검사할 대상이 없습니다.",
        "먼저 빌드하세요 — 배포 실행 직전에 빌드된 이미지를 다시 검사합니다.",
    ),
    DOCKERFILE_MISSING: (
        "검사할 Dockerfile 이 없습니다.",
        "인프라 파일 생성으로 Dockerfile 을 먼저 만드세요.",
    ),
    SCANNER_MISSING: (
        "스캐너가 설치돼 있지 않습니다.",
        "스캐너를 설치하거나 Docker 를 켜 두세요 — Docker 가 있으면 스캐너를 컨테이너로 대신 실행합니다.",
    ),
    SCANNER_PULL_FAILED: (
        "스캐너 이미지 또는 취약점 DB 를 내려받지 못했습니다.",
        "네트워크 연결을 확인하고 다시 검사하세요. 사내망이면 프록시 설정이 필요할 수 있습니다.",
    ),
    TIMEOUT: (
        "스캔이 제한 시간(300초) 안에 끝나지 않았습니다.",
        "첫 실행은 취약점 DB 다운로드로 오래 걸립니다. 잠시 뒤 다시 검사하세요.",
    ),
    DEPENDENCIES_MISSING: (
        "코어에 스캔 실행 구성요소(InfraAgent)가 없습니다.",
        "코어 의존성을 설치하고 재시작하세요 (pip install -r core/requirements.txt).",
    ),
    REQUEST_FAILED: (
        "코어와의 요청이 끝나기 전에 끊겼습니다.",
        "코어 상태를 확인하고 다시 검사하세요. 반복되면 코어를 재시작하세요.",
    ),
    UNKNOWN: (
        "스캐너가 오류로 끝났습니다.",
        "아래 오류 원문을 확인하고 다시 검사하세요. 반복되면 이슈로 남겨 주세요.",
    ),
}


def _first_line(text: str) -> str:
    for line in (text or "").splitlines():
        line = line.strip()
        if line:
            return line[:300]
    return ""


def classify(text: str, *, scan_type: str = "", target: str = "",
             code: Optional[str] = None) -> ScanFailure:
    """stderr/메시지에서 실패 사유를 분류한다. code 를 주면 분류를 건너뛴다.

    target(이미지 이름)이 있으면 IMAGE_NOT_FOUND 원인 문장에 넣는다 —
    "app:latest 가 아직 빌드되지 않아" 가 "이미지가" 보다 다음 행동을 만든다.
    """
    raw = _first_line(text)
    reason = code or UNKNOWN
    if code is None:
        for candidate, pattern in _PATTERNS:
            if pattern.search(text or ""):
                reason = candidate
                break
    cause, next_action = _TEXT.get(reason, _TEXT[UNKNOWN])
    if reason == IMAGE_NOT_FOUND and target:
        cause = f"이미지 '{target}' 가 아직 빌드되지 않아 검사할 대상이 없습니다."
    if reason == UNKNOWN and raw:
        cause = f"스캐너가 오류로 끝났습니다: {raw}"
    if reason == SCANNER_MISSING and scan_type:
        cause = f"{scan_type} 스캐너가 설치돼 있지 않습니다."
    return ScanFailure(reason_code=reason, cause=cause, next_action=next_action, raw=raw)


def failure_fields(text: str, *, scan_type: str = "", target: str = "",
                   code: Optional[str] = None) -> dict:
    """ScanResult 에 합칠 필드만 바로 얻는다."""
    return classify(text, scan_type=scan_type, target=target, code=code).as_fields()
