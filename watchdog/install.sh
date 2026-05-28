#!/usr/bin/env bash
#
# install.sh — ReCoder EC2 Watchdog 데몬 설치 스크립트.
#
# 동작:
#   1. /opt/recoder/watchdog 에 watchdog 소스 복사
#   2. /var/log/recoder 디렉토리 생성
#   3. python3-pip + requests + psutil 설치
#   4. /etc/recoder/watchdog.env (없으면 템플릿 생성)
#   5. systemd unit 등록 및 enable + start
#
# 지원 배포판: Amazon Linux 2 / Amazon Linux 2023 / Ubuntu 20.04+
#
# 실행 (EC2 SSH 상에서):
#   sudo bash install.sh
#
# 환경변수 사전 지정 (선택):
#   RECODER_WATCHDOG_PROJECT_ID=my-prod \
#   RECODER_WATCHDOG_DISCORD_WEBHOOK_URL=https://... \
#   sudo -E bash install.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

INSTALL_PREFIX="${RECODER_WATCHDOG_INSTALL_PREFIX:-/opt/recoder/watchdog}"
LOG_DIR="${RECODER_WATCHDOG_LOG_DIR:-/var/log/recoder}"
ENV_DIR="${RECODER_WATCHDOG_ENV_DIR:-/etc/recoder}"
ENV_FILE="${ENV_DIR}/watchdog.env"
UNIT_PATH="/etc/systemd/system/recoder-watchdog.service"
SERVICE_NAME="recoder-watchdog.service"

log() { printf '[install] %s\n' "$*"; }
err() { printf '[install][ERROR] %s\n' "$*" >&2; }

require_root() {
    if [[ $EUID -ne 0 ]]; then
        err "이 스크립트는 root 권한이 필요합니다. sudo bash install.sh 로 실행하세요."
        exit 1
    fi
}

detect_pkg_manager() {
    if command -v apt-get >/dev/null 2>&1; then
        echo "apt"
    elif command -v dnf >/dev/null 2>&1; then
        echo "dnf"
    elif command -v yum >/dev/null 2>&1; then
        echo "yum"
    else
        echo "unknown"
    fi
}

install_python_deps() {
    local pm
    pm="$(detect_pkg_manager)"
    log "package manager: ${pm}"

    case "${pm}" in
        apt)
            export DEBIAN_FRONTEND=noninteractive
            apt-get update -y
            apt-get install -y python3 python3-pip
            ;;
        dnf)
            dnf install -y python3 python3-pip
            ;;
        yum)
            yum install -y python3 python3-pip
            ;;
        *)
            err "지원되지 않는 패키지 매니저입니다. python3 + pip3 를 수동 설치하세요."
            exit 1
            ;;
    esac

    log "pip 의존성 설치: requests, psutil"
    if ! python3 -m pip install --upgrade --no-cache-dir requests psutil; then
        # pip 가 너무 오래된 경우 폴백
        python3 -m pip install --no-cache-dir requests psutil
    fi
}

copy_sources() {
    log "watchdog 소스 복사 → ${INSTALL_PREFIX}"
    mkdir -p "${INSTALL_PREFIX}"
    cp -f \
        "${SCRIPT_DIR}/__init__.py" \
        "${SCRIPT_DIR}/recoder_watchdog.py" \
        "${SCRIPT_DIR}/config.py" \
        "${SCRIPT_DIR}/docker_monitor.py" \
        "${SCRIPT_DIR}/notifier.py" \
        "${SCRIPT_DIR}/masking.py" \
        "${INSTALL_PREFIX}/"

    # 패키지 import 경로 호환 — /opt/recoder/watchdog 이 패키지처럼 동작하려면
    # /opt/recoder 가 sys.path 에 있어야 한다. recoder_watchdog.py 가 자동으로 추가.

    chmod 0755 "${INSTALL_PREFIX}"
    chmod 0644 "${INSTALL_PREFIX}/"*.py
    chmod 0755 "${INSTALL_PREFIX}/recoder_watchdog.py"
}

ensure_dirs() {
    log "로그 디렉토리 생성: ${LOG_DIR}"
    mkdir -p "${LOG_DIR}"
    chmod 0750 "${LOG_DIR}"

    log "환경변수 디렉토리 생성: ${ENV_DIR}"
    mkdir -p "${ENV_DIR}"
    chmod 0750 "${ENV_DIR}"
}

write_env_template() {
    if [[ -f "${ENV_FILE}" ]]; then
        log "환경 파일이 이미 존재합니다: ${ENV_FILE} — 덮어쓰지 않음"
        return
    fi
    log "환경 파일 템플릿 생성: ${ENV_FILE}"
    cat > "${ENV_FILE}" <<EOF
# /etc/recoder/watchdog.env — ReCoder Watchdog 환경변수
# systemd EnvironmentFile= 로 로드됩니다.

# 필수
RECODER_WATCHDOG_PROJECT_ID=${RECODER_WATCHDOG_PROJECT_ID:-my-project}
RECODER_WATCHDOG_ENVIRONMENT=${RECODER_WATCHDOG_ENVIRONMENT:-production}

# 권장 — 미설정 시 hostname 사용
RECODER_WATCHDOG_HOST=${RECODER_WATCHDOG_HOST:-}

# Discord webhook (비워두면 알림 전송 안 함, jsonl 만 기록)
RECODER_WATCHDOG_DISCORD_WEBHOOK_URL=${RECODER_WATCHDOG_DISCORD_WEBHOOK_URL:-}

# 헬스체크 URL 목록 (콤마 분리, name=url 형식)
# 예: api=http://127.0.0.1:8080/health,worker=http://127.0.0.1:9090/health
RECODER_WATCHDOG_HEALTH_CHECK_URLS=${RECODER_WATCHDOG_HEALTH_CHECK_URLS:-}

# 폴링 주기 (초)
RECODER_WATCHDOG_POLL_INTERVAL=${RECODER_WATCHDOG_POLL_INTERVAL:-5}
RECODER_WATCHDOG_HEALTH_INTERVAL=${RECODER_WATCHDOG_HEALTH_INTERVAL:-30}

# 임계치
RECODER_WATCHDOG_MEMORY_THRESHOLD=${RECODER_WATCHDOG_MEMORY_THRESHOLD:-90}
RECODER_WATCHDOG_HEALTH_FAIL_THRESHOLD=${RECODER_WATCHDOG_HEALTH_FAIL_THRESHOLD:-3}
RECODER_WATCHDOG_SPAM_WINDOW_SECONDS=${RECODER_WATCHDOG_SPAM_WINDOW_SECONDS:-60}

# 파일 경로 / 로깅
RECODER_WATCHDOG_INCIDENT_PATH=${RECODER_WATCHDOG_INCIDENT_PATH:-/var/log/recoder/incidents.jsonl}
RECODER_WATCHDOG_LOG_LEVEL=${RECODER_WATCHDOG_LOG_LEVEL:-INFO}
EOF
    chmod 0640 "${ENV_FILE}"
}

install_systemd_unit() {
    log "systemd unit 설치: ${UNIT_PATH}"
    cp -f "${SCRIPT_DIR}/recoder-watchdog.service" "${UNIT_PATH}"
    chmod 0644 "${UNIT_PATH}"

    systemctl daemon-reload
    systemctl enable "${SERVICE_NAME}"
    if systemctl is-active --quiet "${SERVICE_NAME}"; then
        log "기존 데몬 재시작"
        systemctl restart "${SERVICE_NAME}"
    else
        log "데몬 시작"
        systemctl start "${SERVICE_NAME}"
    fi
}

verify() {
    log "데몬 상태 확인"
    if systemctl is-active --quiet "${SERVICE_NAME}"; then
        log "OK — ${SERVICE_NAME} active"
    else
        err "데몬이 active 상태가 아닙니다. 다음 명령으로 상태 확인:"
        err "  sudo systemctl status ${SERVICE_NAME}"
        err "  sudo journalctl -u ${SERVICE_NAME} -n 100 --no-pager"
        exit 1
    fi

    log "Python import 검증"
    if ! python3 -c "import sys; sys.path.insert(0, '/opt/recoder'); import watchdog.recoder_watchdog" 2>/dev/null; then
        err "watchdog.recoder_watchdog import 실패"
        exit 1
    fi
    log "OK — import 성공"
}

main() {
    require_root
    log "ReCoder Watchdog 설치 시작"
    install_python_deps
    ensure_dirs
    copy_sources
    write_env_template
    install_systemd_unit
    verify
    log "===================="
    log "설치 완료. 다음 단계:"
    log "  1. ${ENV_FILE} 편집하여 PROJECT_ID, DISCORD_WEBHOOK_URL, HEALTH_CHECK_URLS 설정"
    log "  2. sudo systemctl restart ${SERVICE_NAME}"
    log "  3. sudo journalctl -u ${SERVICE_NAME} -f 로 로그 모니터링"
    log "  4. tail -f ${LOG_DIR}/incidents.jsonl 로 인시던트 확인"
}

main "$@"
