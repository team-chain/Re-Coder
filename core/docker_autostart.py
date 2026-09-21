"""Docker Desktop 자동 기동 — 꺼져 있으면 코어가 직접 백그라운드로 띄운다.

## 왜 있나

로컬 배포·보안 스캔은 Docker 데몬이 전제다. 지금까지는 데몬이 꺼져 있으면
"Docker Desktop 을 실행한 뒤 다시 시도하세요"라고 사용자에게 미뤘다 —
사용자는 앱을 찾아 켜고, 엔진이 뜰 때까지 기다렸다가, 하던 일을 처음부터
다시 눌러야 했다. 이 모듈은 그 대기를 코어가 대신한다: 백그라운드로
Docker Desktop 을 띄우고, 데몬이 응답할 때까지 폴링한 뒤 원래 작업을
계속한다.

## 원칙 — 주도적이되 숨기지 않는다

- 자동 시작을 **시도했다는 사실과 결과**는 항상 message 로 호출자에게
  돌아가고, 호출자는 그것을 화면에 보여준다. 몰래 켜지 않는다.
- 시간 내에 못 뜨면 **fail-closed 그대로다** — 검사를 통과한 척하지 않고,
  무엇을 시도했고 왜 안 됐는지를 담아 실패한다.
- 자동 시작을 원하지 않으면 `RECODER_DOCKER_AUTOSTART=0` 으로 끈다.

## 재시도 폭주 방지

화면 폴링이 스캔·배포 준비 상태를 반복 조회하므로, 실패한 직후에 또
Docker Desktop 을 실행하면 앱이 여러 번 뜨거나 부팅 중에 재실행된다.
모듈 전역 쿨다운(기본 120초) 안에서는 재시도하지 않고 직전 결과를
돌려준다. 같은 이유로 동시 호출은 락으로 직렬화한다.
"""
from __future__ import annotations

import logging
import os
import platform
import subprocess
import sys
import threading
import time
from dataclasses import dataclass

#: 자동 기동은 실기기에서만 재현되는 문제가 많다 — 단계마다 남긴다(코어 stderr).
#: logging 설정이 이 모듈을 걸러낼 수 있어 stderr 에도 직접 쓴다(확장 디버그 콘솔에 보임).
_logger = logging.getLogger("recoder.docker_autostart")


class _Note:
    def warning(self, msg: str, *args) -> None:
        text = msg % args if args else msg
        _logger.warning(text)
        try:
            print(f"{time.strftime('%H:%M:%S')} {text}", file=sys.stderr, flush=True)
        except Exception:  # noqa: BLE001
            pass


_log = _Note()

#: 자동 시작 스위치. "0" 일 때만 꺼진다 — 기본은 켜짐.
ENV_AUTOSTART = "RECODER_DOCKER_AUTOSTART"
#: 데몬 준비 대기 상한(초). ECS 경로가 executor 에서 블로킹으로 돌므로 과하게 길게 잡지 않는다.
#: 실기기(Apple Silicon, Docker Desktop 콜드 스타트)에서 75초를 넘긴 사례가 있어 120초.
#: 이 안에 못 뜨면 launched=True/ready=False 로 돌려주고, 확장이 뒤에서 이어서 확인한다.
ENV_WAIT_SECONDS = "RECODER_DOCKER_AUTOSTART_WAIT"
DEFAULT_WAIT_SECONDS = 120
#: 실패 후 재시도 쿨다운(초).
_RETRY_COOLDOWN_SECONDS = 120

_POLL_INTERVAL_SECONDS = 3.0
#: `open -a` 자체가 끝나기를 기다리는 상한(초). 런처라 보통 1초 안이다.
_OPEN_TIMEOUT_SECONDS = 15
#: 실행 뒤 이 시간이 지나도 앱 프로세스가 없으면 한 번 다시 띄운다(종료 중 무시된 경우).
_RELAUNCH_AFTER_SECONDS = 12
#: 앱은 없는데 백엔드(com.docker.backend)가 남아 있으면 "종료 중" 이다. 그 위에 바로
#: `open` 하면 Docker Desktop 이 시작 중 상태에서 멈춘다(실기기: 끄고 15초 뒤 재실행 →
#: 수 분 지나도 데몬 없음). 백엔드가 내려갈 때까지 최대 이만큼 기다린 뒤 띄운다.
_BACKEND_DRAIN_SECONDS = 45
_BACKEND_POLL_SECONDS = 2.0
#: 프로세스는 있는데 데몬이 죽어 있고, 그 프로세스가 이만큼 오래됐으면 "부팅 중" 이
#: 아니라 "멈춤/엔진만 꺼짐" 이다(실기기: Quit 뒤 GUI 는 사라지는데 backend·helper 가
#: 데몬 없이 무기한 남는다 — 그 위에 `open` 하면 꼬인다). 정리한 뒤 새로 띄운다.
_STALE_PROCESS_SECONDS = 90
#: 정리: SIGTERM 뒤 이만큼 기다리고, 남으면 SIGKILL.
_KILL_GRACE_SECONDS = 10

_lock = threading.Lock()
_last_attempt_at: float = 0.0
_last_result: "AutostartResult | None" = None


@dataclass
class AutostartResult:
    """자동 기동 시도의 전말 — 화면에 그대로 보여줄 수 있는 형태."""

    attempted: bool   #: 이번(또는 쿨다운 내 직전) 호출에서 실행을 시도했는가
    launched: bool    #: Docker Desktop 프로세스 실행에 성공했는가
    ready: bool       #: 데몬이 응답하는 상태로 끝났는가
    waited_seconds: int
    message: str
    #: 앱은 떠 있는데(띄웠거나 부팅 중) 데몬만 아직인 상태 — 호출자는 "실패" 로
    #: 끝내지 말고 뒤에서 이어 확인해도 된다. 실행 자체가 안 됐으면 False.
    starting: bool = False


def daemon_up(timeout: float = 5.0) -> bool:
    """`docker info` 가 성공하면 데몬이 살아 있다. CLI 부재·타임아웃은 down 취급."""
    try:
        proc = subprocess.run(
            ["docker", "info"], capture_output=True, timeout=timeout,
        )
        return proc.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False


def app_running() -> bool:
    """Docker Desktop **앱 프로세스**가 살아 있는가(데몬 준비와는 별개).

    macOS 만 확인한다 — 다른 플랫폼·확인 실패는 True(모른다 → 다시 띄우지
    않는다). 재실행은 "확실히 없을 때"만 해야 부팅 중 중복 실행이 없다.
    """
    if not _can_detect_processes():
        return True
    try:
        proc = subprocess.run(
            ["pgrep", "-f", "Docker.app/Contents/MacOS/Docker"],
            capture_output=True, timeout=5,
        )
        return proc.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return True


def _can_detect_processes() -> bool:
    """앱·백엔드 프로세스 유무를 볼 수 있는 플랫폼인가(pgrep, macOS)."""
    return platform.system() == "Darwin"


def backend_running() -> bool:
    """Docker Desktop **백엔드**(com.docker.backend) 프로세스가 살아 있는가.

    macOS 만 확인한다 — 다른 플랫폼·확인 실패는 False(모른다 → 기다리지 않는다).
    """
    if not _can_detect_processes():
        return False
    try:
        proc = subprocess.run(
            ["pgrep", "-f", "com.docker.backend"], capture_output=True, timeout=5,
        )
        return proc.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False


def _docker_pids() -> list[int]:
    """Docker.app 아래서 돌고 있는 모든 프로세스(GUI·helper·backend·build)."""
    try:
        proc = subprocess.run(
            ["pgrep", "-f", "Docker.app/Contents/"], capture_output=True, text=True, timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return []
    return [int(x) for x in proc.stdout.split() if x.strip().isdigit()]


def _parse_etime(text: str) -> int:
    """`ps -o etime=` 형식([[dd-]hh:]mm:ss)을 초로."""
    text = text.strip()
    days = 0
    if "-" in text:
        d, text = text.split("-", 1)
        days = int(d)
    parts = [int(x) for x in text.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    h, m, sec = parts
    return days * 86400 + h * 3600 + m * 60 + sec


def _oldest_process_age(pids: list[int]) -> int:
    """pids 중 가장 오래 산 프로세스의 나이(초). 확인 실패는 0(모른다 → 어리다고 본다)."""
    if not pids:
        return 0
    try:
        proc = subprocess.run(
            ["ps", "-o", "etime=", "-p", ",".join(str(p) for p in pids)],
            capture_output=True, text=True, timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return 0
    ages = []
    for line in proc.stdout.splitlines():
        try:
            ages.append(_parse_etime(line))
        except ValueError:
            continue
    return max(ages) if ages else 0


def _kill_docker_processes() -> None:
    """남은 Docker.app 프로세스를 SIGTERM → (유예 뒤) SIGKILL 로 정리한다.

    데몬이 죽어 있을 때만 부른다 — 컨테이너가 도는 상태를 죽이는 일은 없다.
    """
    _log.warning("[docker-autostart] 멈춘 프로세스 정리 시작 pids=%s", _docker_pids())
    try:
        subprocess.run(["pkill", "-TERM", "-f", "Docker.app/Contents/"], capture_output=True, timeout=5)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return
    started = time.monotonic()
    while (time.monotonic() - started) < _KILL_GRACE_SECONDS:
        time.sleep(_BACKEND_POLL_SECONDS)
        if not _docker_pids():
            _log.warning("[docker-autostart] SIGTERM 뒤 %.0f초 만에 모두 종료", time.monotonic() - started)
            return
    _log.warning("[docker-autostart] SIGTERM 유예 초과 → SIGKILL pids=%s", _docker_pids())
    try:
        subprocess.run(["pkill", "-KILL", "-f", "Docker.app/Contents/"], capture_output=True, timeout=5)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return


def _drain_shutdown() -> tuple[int, bool]:
    """Docker Desktop 프로세스가 남아 있으면 사라질 때까지 기다린다(필요하면 정리한다).

    데몬이 죽어 있는데 프로세스가 있으면 셋 중 하나다:
      - **종료 중** — 곧 사라진다 → 사라지면 띄운다.
      - **부팅 중** — 프로세스가 어리다(< _STALE_PROCESS_SECONDS) → 띄우지 말고 기다린다.
      - **멈춤 / 엔진만 꺼짐** — 프로세스가 오래됐는데 데몬이 없다 → 정리하고 띄운다.
        (실기기: Quit 뒤 backend·helper 가 데몬 없이 무기한 남고, 그 위에 `open` 하면
        소켓은 생기는데 /version 500 으로 수 분간 멈춘다.)

    반환: (기다린 초, 프로세스가 다 사라져 띄워도 되는가, 멈춘 프로세스를 정리했는가).
    프로세스를 못 보는 플랫폼은 (0, True, False) — 예전처럼 바로 띄운다.
    """
    if not _can_detect_processes():
        return 0, True, False
    started = time.monotonic()
    killed = False
    while True:
        pids = _docker_pids()
        if not pids:
            return int(time.monotonic() - started), True, killed
        age = _oldest_process_age(pids)
        _log.warning("[docker-autostart] 데몬 없음, 프로세스 %d개 (가장 오래된 %d초)", len(pids), age)
        if not killed and age >= _STALE_PROCESS_SECONDS:
            killed = True
            _kill_docker_processes()
            continue
        if (time.monotonic() - started) >= _BACKEND_DRAIN_SECONDS:
            return int(time.monotonic() - started), False, killed
        time.sleep(_BACKEND_POLL_SECONDS)


def _windows_candidates() -> list[str]:
    """Windows 의 Docker Desktop 실행 파일 후보 — 존재하는 것만 돌려준다."""
    roots = [
        os.environ.get("ProgramFiles", r"C:\Program Files"),
        os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
        os.environ.get("LOCALAPPDATA", ""),
    ]
    names = [
        os.path.join("Docker", "Docker", "Docker Desktop.exe"),
    ]
    found = []
    for root in roots:
        if not root:
            continue
        for name in names:
            path = os.path.join(root, name)
            if os.path.exists(path):
                found.append(path)
    return found


def _launch() -> tuple[bool, str]:
    """플랫폼별로 Docker Desktop 을 **백그라운드로** 실행한다.

    반환: (실행 시도 성공 여부, 사람이 읽을 설명)
    코어를 블로킹하지 않도록 어떤 경우에도 프로세스 종료를 기다리지 않는다.
    """
    system = platform.system()
    try:
        if system == "Windows":
            candidates = _windows_candidates()
            if not candidates:
                return False, (
                    "Docker Desktop 실행 파일을 찾지 못했습니다 — 설치돼 있다면 "
                    "직접 실행해 주세요."
                )
            # DETACHED_PROCESS: 코어가 죽어도 Docker 는 남는다. 콘솔도 안 띄운다.
            flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
                subprocess, "CREATE_NEW_PROCESS_GROUP", 0
            )
            subprocess.Popen(
                [candidates[0]],
                creationflags=flags,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                close_fds=True,
            )
            return True, f"Docker Desktop 실행: {candidates[0]}"
        if system == "Darwin":
            # `open -a` 는 런처라 금방 끝난다 — 종료 코드를 봐야 "실행했다"고 말할 수
            # 있다. 실기기에서 조용히 실패한 뒤 데몬만 기다리다 끝난 사례가 있었다.
            proc = subprocess.run(
                ["open", "-a", "Docker"],
                capture_output=True, text=True, timeout=_OPEN_TIMEOUT_SECONDS,
            )
            _log.warning("[docker-autostart] open -a Docker → rc=%s stdout=%r stderr=%r",
                         proc.returncode, proc.stdout.strip(), proc.stderr.strip())
            if proc.returncode != 0:
                detail = (proc.stderr or proc.stdout or "").strip()
                return False, (
                    "Docker Desktop 실행 실패(open -a Docker"
                    f"{': ' + detail if detail else ''}) — 설치돼 있는지 확인해 주세요."
                )
            return True, "Docker Desktop 실행: open -a Docker"
        # Linux 데몬은 systemd 권한이 얽혀 있어 조용한 자동 시작이 오히려
        # 실패를 숨긴다. 명시적으로 지원하지 않는다고 말한다.
        return False, (
            "Linux 에서는 Docker 데몬 자동 시작을 지원하지 않습니다 — "
            "`sudo systemctl start docker` 로 직접 시작해 주세요."
        )
    except Exception as exc:  # noqa: BLE001 — 실행 실패는 사유와 함께 fail-closed
        return False, f"Docker Desktop 실행 실패: {exc}"


def ensure_docker(wait_seconds: int | None = None) -> AutostartResult:
    """데몬이 꺼져 있으면 자동 시작을 시도하고, 준비될 때까지 기다린다.

    항상 AutostartResult 를 돌려준다 — 예외를 던지지 않으므로 호출자는
    ready 를 보고 기존 실패 경로(fail-closed)를 그대로 타면 된다.
    """
    global _last_attempt_at, _last_result

    if daemon_up():
        return AutostartResult(False, False, True, 0, "Docker 데몬 실행 중")

    if os.environ.get(ENV_AUTOSTART, "1").strip() == "0":
        return AutostartResult(
            False, False, False, 0,
            "Docker 데몬이 꺼져 있고 자동 시작이 비활성화돼 있습니다"
            f"({ENV_AUTOSTART}=0). Docker Desktop 을 직접 실행해 주세요.",
        )

    with _lock:
        # 락을 기다리는 동안 다른 호출이 이미 띄웠을 수 있다.
        if daemon_up():
            return AutostartResult(False, False, True, 0, "Docker 데몬 실행 중")

        limit = wait_seconds
        if limit is None:
            try:
                limit = int(os.environ.get(ENV_WAIT_SECONDS, str(DEFAULT_WAIT_SECONDS)))
            except ValueError:
                limit = DEFAULT_WAIT_SECONDS

        now = time.monotonic()
        in_cooldown = (
            _last_result is not None
            and (now - _last_attempt_at) < _RETRY_COOLDOWN_SECONDS
        )
        # 쿨다운의 목적은 "부팅 중인 앱을 또 띄우지 않기" 다. 직전 결과를 그대로
        # 돌려주는 건 틀리다 — 직전엔 성공했는데 사용자가 방금 껐다면 "자동 조치함"
        # 을 보여 주면서 데몬은 죽어 있는 꼴이 된다(실기기에서 실제로 났다).
        # 그래서: 앱이 살아 있고 직전에 실패했을 때만 재실행을 막고 기다린다.
        if in_cooldown and not _last_result.ready and app_running():
            _last_attempt_at = now
            _last_result = _wait_ready(limit, launched_now=False)
            return _last_result

        _last_attempt_at = now
        drained, clear, cleaned = _drain_shutdown()
        if not clear:
            # 프로세스가 계속 남아 있다 — 부팅 중이다. 그 위에 또 띄우지 않는다.
            _last_result = _wait_ready(max(limit - drained, 0), launched_now=False)
            _last_result.waited_seconds += drained
            return _last_result
        launched, how = _launch()
        if not launched:
            _last_result = AutostartResult(True, False, False, drained, how)
            return _last_result

        # 정리·종료 대기에 쓴 시간은 상한에서 뺀다(확장 요청 제한 200초 안에 끝나게).
        # 단 부팅에 최소 60초는 준다.
        budget = max(limit - drained, min(limit, 60)) if drained else limit
        _last_result = _wait_ready(budget, launched_now=True)
        if drained:
            _last_result.waited_seconds += drained
        if cleaned:
            _last_result.message = "멈춰 있던 Docker Desktop 을 정리하고 " + _last_result.message
        return _last_result


def _wait_ready(limit: int, *, launched_now: bool) -> AutostartResult:
    """데몬이 응답할 때까지 최대 limit 초 폴링한다.

    launched_now — 이번 호출에서 `_launch()` 를 했는가. False 면 부팅 중인 앱을
    기다리는 것이라 다시 띄우지 않는다.
    """
    started = time.monotonic()
    relaunched = not launched_now
    _log.warning("[docker-autostart] 데몬 대기 시작 limit=%ss launched_now=%s", limit, launched_now)
    while (time.monotonic() - started) < limit:
        if daemon_up():
            waited = int(time.monotonic() - started)
            _log.warning("[docker-autostart] 데몬 준비 (%d초)", waited)
            return AutostartResult(
                True, launched_now, True, waited,
                f"Docker Desktop 을 자동 시작했습니다 (준비까지 {waited}초)."
                if launched_now else
                f"시작 중이던 Docker Desktop 이 준비됐습니다 ({waited}초 대기).",
            )
        # 앱이 종료되는 도중에 `open` 을 부르면 무시되고 프로세스가 사라진다
        # (사용자가 방금 Docker 를 끈 직후 진단을 돌리는 흔한 순서). 한 번만
        # 다시 띄운다 — 매 폴링마다 띄우면 부팅 중 재실행이 된다.
        elapsed = time.monotonic() - started
        if not relaunched and elapsed >= _RELAUNCH_AFTER_SECONDS and not app_running():
            relaunched = True
            _log.warning("[docker-autostart] %.0f초 지나도 앱 프로세스 없음 → 다시 실행 (pids=%s)", elapsed, _docker_pids())
            launched, how = _launch()
            if not launched:
                return AutostartResult(True, False, False, int(elapsed), how)
        time.sleep(_POLL_INTERVAL_SECONDS)

    _log.warning("[docker-autostart] %s초 안에 데몬 미준비 pids=%s", int(limit), _docker_pids())
    return AutostartResult(
        True, launched_now, False, int(limit),
        f"Docker Desktop 을 실행했지만 {int(limit)}초 안에 데몬이 준비되지 "
        "않았습니다 — 아직 시작 중일 수 있습니다."
        if launched_now else
        f"Docker Desktop 이 시작 중이지만 {int(limit)}초 안에 데몬이 준비되지 "
        "않았습니다 — 아직 시작 중일 수 있습니다. 계속 안 되면 메뉴바 고래 아이콘 → "
        "Quit 한 뒤 다시 자동 조치해 주세요.",
        starting=True,
    )


def reset_for_tests() -> None:
    """테스트 전용 — 쿨다운·직전 결과 초기화."""
    global _last_attempt_at, _last_result
    with _lock:
        _last_attempt_at = 0.0
        _last_result = None
