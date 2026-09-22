"""배포 요청의 **브랜치를 정하는 단 하나의 자리.**

## 왜 별도 모듈인가

이 판단이 두 곳에 흩어져 있었다. 확장용 변환기(`api/routes/deploy_ecs.py`)
는 작업 폴더에서 브랜치를 알아냈지만, 코어 라우트(`api/routes/ecs.py`)는
요청 본문의 `branch` 를 그대로 정책에 넘겼다. 그래서 `/api/deploy/ecs` 로는
막히는 배포가 `/api/ecs/deploy` 로는 그냥 나갔다 — **한쪽만 고친 보안 수정은
고치지 않은 것과 같다.**

두 라우트가 같은 함수를 부르게 하려면 둘 다 임포트할 수 있는 자리에 있어야
한다. 라우트 모듈끼리 서로 임포트하면 순환이 생기므로 여기 둔다.

## 판단 규칙

    작업 폴더에서 **관측된** 브랜치 → 없으면 호출자가 신고한 값

호출자의 신고를 먼저 믿으면 안 된다. "프로덕션은 main 에서만" 규칙을
`{"branch": "main"}` 한 줄로 통과할 수 있게 되기 때문이다. 승인 레벨을 요청
본문에서 못 내리게 막아 놓고 브랜치는 자기 신고를 믿으면, 같은 구멍이 이름만
바꿔 남는다.

반대로 관측할 방법이 아예 없을 때(작업 폴더가 없는 CI 호출 등)까지 막으면
아무도 못 쓰므로, 그때는 신고를 받는다. 대신 프로덕션 배포는 브랜치가 비면
정책이 거부한다(`core/opa_gate.PRODUCTION_BRANCHES`).

## 왜 "이 폴더가 그 저장소 것인지"를 더 따지지 않나

`git rev-parse` 는 상위 폴더로 거슬러 올라가 가장 가까운 저장소를 찾는다.
그래서 홈 디렉터리가 git 저장소인 사람이 그 안의 폴더를 배포하면 홈 저장소의
브랜치가 잡힌다. 이걸 걸러 보려고 `git ls-files` 로 "추적되는 폴더인가"를
확인하는 갈래를 넣었다가 **더 큰 구멍을 만들었다.**

  - 작업 폴더가 아직 커밋 전이거나 `.gitignore` 에 걸려 있으면 추적되지
    않는다. 그러면 "관측 실패"로 떨어지고, 규칙이 **호출자의 신고를 믿는
    쪽으로** 넘어간다. 즉 `.gitignore` 에 한 줄 넣는 것만으로 브랜치 검사를
    통째로 우회할 수 있게 된다 — 막으려던 바로 그 우회다.
  - `git ls-files` 는 인덱스를 갱신하면서 저장소의 `core.fsmonitor` 프로그램을
    실행한다. 남이 준 `.git` 이 섞인 폴더를 배포하는 순간 임의 실행이 된다.
    `rev-parse` 는 그러지 않는다.

**모호하면 관측값 쪽으로 기운다.** 상위 저장소의 브랜치라도 그건 여전히
"이 코드가 놓여 있는 저장소의 상태"이고, 무엇보다 호출자가 고를 수 없는
값이다. 신고 쪽으로 떨어지는 것보다 항상 안전하다.
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

#: git 하위 명령 하나에 허용하는 시간(초). 자격증명 프롬프트나 느린 네트워크
#: 파일시스템에 붙잡히면 배포 요청 전체가 멈춘다.
_GIT_TIMEOUT = 5

#: 저장소가 들고 있는 설정으로 **바깥 프로그램이 실행되지 않게** 못 박는다.
#: 배포 대상 폴더는 남이 준 것일 수 있고, 이 코드는 HTTP 핸들러 안에서 돈다.
_GIT_SAFE_FLAGS = (
    "-c", "core.fsmonitor=",
    "-c", "core.hooksPath=/dev/null",
    "-c", "core.pager=cat",
)


def _git_env() -> dict:
    """git 이 **작업 폴더가 아닌 다른 저장소**를 보지 않게 정리한 환경.

    코어가 git 훅이나 래퍼에서 실행되면 `GIT_DIR` / `GIT_WORK_TREE` /
    `GIT_INDEX_FILE` 이 상속되고, 그러면 `cwd` 가 조용히 무시된다.
    `GIT_TERMINAL_PROMPT=0` 은 자격증명 프롬프트로 멈추는 것을 막는다.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_OPTIONAL_LOCKS"] = "0"
    return env


def _git(workspace_path: str, *args: str) -> Optional[str]:
    """작업 폴더에서 git 을 돌리고 stdout 을 돌려준다. 실패하면 None."""
    try:
        proc = subprocess.run(
            ["git", *_GIT_SAFE_FLAGS, *args],
            cwd=workspace_path, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=_GIT_TIMEOUT,
            env=_git_env(),
        )
    except Exception as exc:  # noqa: BLE001
        logger.info("git %s 실패: %s", " ".join(args), exc)
        return None
    if proc.returncode != 0:
        return None
    return (proc.stdout or "").strip()


def current_branch(workspace_path: Optional[str]) -> str:
    """작업 폴더의 현재 git 브랜치. **관측 못 하면 빈 문자열.**

    빈 문자열은 "제한 없음"이 아니라 "모름"이다. 프로덕션 배포는 브랜치를
    모르면 거부된다 — `core/opa_gate._local_deploy_gate` 규칙 5.
    """
    if not workspace_path:
        return ""
    try:
        if not Path(workspace_path).is_dir():
            return ""
    except (OSError, ValueError):
        # 윈도우는 경로에 못 쓰는 문자가 있으면 `is_dir()` 이 던진다.
        return ""

    name = _git(workspace_path, "rev-parse", "--abbrev-ref", "HEAD")
    # 분리된 HEAD 는 브랜치가 아니다. 이름인 척하면 규칙이 "현재: HEAD" 라는
    # 엉뚱한 사유를 낸다.
    if not name or name == "HEAD":
        return ""
    return name


def resolve_branch(workspace_path: Optional[str], claimed: str = "") -> str:
    """정책 평가가 볼 브랜치. **관측 우선, 없을 때만 신고.**"""
    observed = current_branch(workspace_path)
    claimed = (claimed or "").strip()
    if observed and claimed and observed != claimed:
        logger.warning(
            "요청이 보낸 브랜치(%r)와 작업 폴더의 실제 브랜치(%r)가 다릅니다 — "
            "정책 판단에는 작업 폴더 쪽을 씁니다.", claimed, observed,
        )
    return observed or claimed


__all__ = ["current_branch", "resolve_branch"]
