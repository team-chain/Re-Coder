# ReCoder EC2 Watchdog

설계서 §3.2.4 / §4.1.3 에 정의된 EC2 인스턴스용 컨테이너 모니터링 데몬.

ReCoder core 와 **독립적으로** 동작하며, Python 표준 라이브러리 + `requests` + `psutil` 만 사용한다. Docker SDK 의존성을 추가하지 않고 `subprocess` 로 docker CLI 를 호출한다.

---

## 무엇을 감지하는가

| 이벤트 | 트리거 | severity |
| --- | --- | --- |
| `container_crash` | 직전 polling 에서 running 이었으나 현재 not-running | critical / warning |
| `health_check_failed` | 등록된 헬스체크 URL 3회 연속 실패 (기본) | critical |
| `health_check_recovered` | 실패 알림 후 복구 | info |
| `container_oom_killed` | `docker events --filter event=oom` 수신 | critical |
| `container_exit_nonzero` | `docker events --filter event=die` 의 exitCode != 0 | warning |
| `container_memory_high` | docker stats 의 MemPerc >= 90% (조정 가능) | warning |
| `docker_daemon_unavailable` | docker CLI/daemon 응답 불가 | warning |
| `ecs_tasks_unhealthy` | ECS running < desired 가 3회 연속 (조정 가능) | critical / warning |
| `ecs_task_restart_loop` | 관측 창 안에서 태스크가 2회 이상 중단 | critical |
| `http_5xx_spike` | ALB 5xx 비율 > 5% (최소 요청 수 충족 시) | critical |
| `latency_p95_high` | ALB TargetResponseTime p95 > 3초 | warning |
| `ecs_monitoring_unavailable` | 3분 넘게 ECS/CloudWatch 를 못 읽음 | warning |

아래 5종(ECS/CloudWatch)은 **`RECODER_WATCHDOG_ECS_CLUSTER` 와
`ECS_SERVICE` 를 채웠을 때만** 동작한다. 비워 두면 그 경로가 통째로 꺼지고,
로컬 도커 감시만 그대로 돈다. 켜졌는지는 `--check` 의 `ecs=` 항목으로 본다.

감지 시 두 가지 작업을 동시 수행한다:

1. `incident.jsonl` 에 append (설계 A.9 스키마)
2. Discord webhook 으로 POST 알림 (실패해도 jsonl 저장은 보장됨)

60초 이내 동일 fingerprint(`SHA256(error_type + container_name + masked_message_prefix)`) 는 spam suppression 되어 중복 알림 차단.

모든 로그/메시지는 16종 패턴 마스킹(`masking.py`)을 통과한 후에만 저장/전송된다.

---

## 디렉토리 구조

```
watchdog/
  __init__.py
  recoder_watchdog.py     # 메인 데몬
  config.py               # 환경변수 로더
  docker_monitor.py       # docker CLI 래퍼
  notifier.py             # Discord webhook 전송
  masking.py              # 16종 민감정보 마스킹
  install.sh              # EC2 설치 스크립트
  recoder-watchdog.service # systemd unit
  README.md               # 이 파일
```

---

## 설치 (EC2)

### 빠른 설치

```bash
# 1) 코드 업로드 (예시 — rsync)
rsync -av watchdog/ ec2-user@<EC2_IP>:/tmp/watchdog/

# 2) EC2 SSH 접속 후 설치
ssh ec2-user@<EC2_IP>
cd /tmp/watchdog
sudo bash install.sh

# 3) 환경변수 편집
sudo nano /etc/recoder/watchdog.env

# 4) 재시작
sudo systemctl restart recoder-watchdog
```

### 환경변수 주입 후 설치

```bash
sudo \
  RECODER_WATCHDOG_PROJECT_ID=my-prod \
  RECODER_WATCHDOG_DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/... \
  RECODER_WATCHDOG_HEALTH_CHECK_URLS=api=http://127.0.0.1:8080/health,worker=http://127.0.0.1:9090/health \
  bash install.sh
```

`install.sh` 동작:

1. `python3` + `pip3` 설치 (apt / dnf / yum 자동 감지)
2. `pip3 install requests psutil`
3. 소스를 `/opt/recoder/watchdog/` 로 복사
4. `/var/log/recoder/` 로그 디렉토리 생성
5. `/etc/recoder/watchdog.env` 템플릿 생성 (이미 있으면 보존)
6. `/etc/systemd/system/recoder-watchdog.service` 등록 후 enable + start

---

## 환경변수 레퍼런스

| 변수 | 기본값 | 설명 |
| --- | --- | --- |
| `RECODER_WATCHDOG_PROJECT_ID` | `unknown-project` | 프로젝트 식별자 (incident 에 기록) |
| `RECODER_WATCHDOG_HOST` | `socket.gethostname()` | 호스트 식별자 |
| `RECODER_WATCHDOG_ENVIRONMENT` | `production` | `production` / `staging` 등 |
| `RECODER_WATCHDOG_DISCORD_WEBHOOK_URL` | (빈값) | 비우면 webhook 비활성, jsonl 만 기록 |
| `RECODER_WATCHDOG_INCIDENT_PATH` | `/var/log/recoder/incidents.jsonl` | append 대상 |
| `RECODER_WATCHDOG_HEALTH_CHECK_URLS` | (빈값) | `name=url,name2=url2` |
| `RECODER_WATCHDOG_POLL_INTERVAL` | `5` | 컨테이너 polling 주기 (초) |
| `RECODER_WATCHDOG_HEALTH_INTERVAL` | `30` | 헬스체크 주기 (초) |
| `RECODER_WATCHDOG_HEALTH_FAIL_THRESHOLD` | `3` | 연속 실패 임계치 |
| `RECODER_WATCHDOG_MEMORY_THRESHOLD` | `90` | 메모리 사용률 알림 임계 (%) |
| `RECODER_WATCHDOG_SPAM_WINDOW_SECONDS` | `60` | 동일 fingerprint 차단 윈도 |
| `RECODER_WATCHDOG_HEALTH_TIMEOUT` | `5` | 헬스체크 HTTP timeout (초) |
| `RECODER_WATCHDOG_DEPLOYMENT_ID` | (빈값) | 최근 배포 ID (incident 에 기록) |
| `RECODER_WATCHDOG_LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `RECODER_WATCHDOG_DOCKER_BIN` | `docker` | docker 바이너리 경로 |

### ECS / CloudWatch 감시 (FR-06-01/02)

| 변수 | 기본값 | 설명 |
| --- | --- | --- |
| `RECODER_WATCHDOG_ECS_CLUSTER` | (빈값) | 비우면 ECS 감시 **전체가 꺼짐** |
| `RECODER_WATCHDOG_ECS_SERVICE` | (빈값) | 클러스터와 함께 있어야 켜진다 |
| `RECODER_WATCHDOG_AWS_REGION` | `AWS_REGION` → `AWS_DEFAULT_REGION` | 위 둘을 채웠으면 **필수** |
| `RECODER_WATCHDOG_ALB_NAME` | (빈값) | 비우면 헬스만 보고 트래픽 지표는 건너뜀 |
| `RECODER_WATCHDOG_TARGET_GROUP` | (빈값) | 없으면 다른 대상 그룹 트래픽이 섞인다 |
| `RECODER_WATCHDOG_ECS_INTERVAL` | `60` | ECS polling 주기 (초, 최소 15) |
| `RECODER_WATCHDOG_ECS_WINDOW_SECONDS` | `300` | 지표 관측 창 (초, 최소 60) |
| `RECODER_WATCHDOG_ERROR_RATE_THRESHOLD` | `0.05` | 5xx 비율 임계 (0~1) |
| `RECODER_WATCHDOG_MIN_REQUESTS` | `20` | 이 미만이면 에러율로 판정하지 않음 |
| `RECODER_WATCHDOG_P95_THRESHOLD_SECONDS` | `3.0` | p95 응답 시간 임계 (초) |
| `RECODER_WATCHDOG_UNHEALTHY_POLLS` | `3` | 연속 desired 미달 임계 |

`ALB_NAME` / `TARGET_GROUP` 은 ARN 전체가 아니라 **CloudWatch 차원 값**이다 —
ARN 의 `loadbalancer/` 뒷부분만 쓴다.

```
ARN     arn:aws:elasticloadbalancing:us-east-1:123456789012:loadbalancer/app/recoder-alb/1a2b3c4d5e6f7g8h
ALB_NAME                                                                 app/recoder-alb/1a2b3c4d5e6f7g8h

ARN          arn:aws:elasticloadbalancing:us-east-1:123456789012:targetgroup/recoder-tg/1a2b3c4d5e6f7g8h
TARGET_GROUP                                                     targetgroup/recoder-tg/1a2b3c4d5e6f7g8h
```

필요한 IAM 권한: `ecs:DescribeServices`, `ecs:ListTasks`, `ecs:DescribeTasks`,
`cloudwatch:GetMetricStatistics`.

**`MIN_REQUESTS` 를 낮추지 말 것.** 요청 1건 중 1건이 5xx 면 에러율은 100% 다.
배포 직후에는 트래픽이 거의 없어 거의 항상 그렇게 되고, 그 알림이 몇 번
반복되면 사람들이 경고를 무시하기 시작한다 — 그 시점에 감시 기능은 꺼진 것과
같다.

---

## incident.jsonl 스키마 (설계 A.9)

```json
{
  "alert_id": "8a36c0ee-...",
  "source": "watchdog",
  "project_id": "my-prod",
  "environment": "production",
  "host": "ip-10-0-1-23",
  "container_name": "api",
  "alert_type": "container_crash",
  "severity": "critical",
  "detected_at": "2026-05-27T03:21:44.123456+00:00",
  "message": "container api transitioned running → exited (exit_code=137)",
  "logs_excerpt": ["...", "..."],
  "health_check_result": {},
  "metric_snapshot": {"exit_code": 137, "image": "myrepo/api:1.2.3"},
  "recent_deployment_id": "dep-abc",
  "fingerprint": "sha256...",
  "mask_version": "watchdog-mask-v1"
}
```

`core/incident_timeline.py` 빌더가 이 파일을 직접 읽어 IncidentTimeline 으로 통합한다.

---

## 운영

### 상태 확인

```bash
sudo systemctl status recoder-watchdog
sudo journalctl -u recoder-watchdog -f
tail -f /var/log/recoder/incidents.jsonl
```

### 설정 검증

```bash
sudo -u root /usr/bin/python3 /opt/recoder/watchdog/recoder_watchdog.py --check
```

`--check` 는 환경변수를 출력하고 docker daemon 가용 여부만 확인 후 종료한다.

### 재시작 / 중지

```bash
sudo systemctl restart recoder-watchdog
sudo systemctl stop recoder-watchdog
sudo systemctl disable recoder-watchdog
```

---

## 제약 및 주의사항

- **docker daemon 권한**: 데몬은 root 로 실행되므로 `docker` socket 접근에 별도 group 설정 필요 없음. non-root 로 운영하려면 `User=ec2-user` + `usermod -aG docker ec2-user`.
- **메모리**: systemd unit 의 `MemoryMax=512M` 로 상한 설정. 정상 동작 시 50MB 미만 예상.
- **24/7 안정성**: container_states / fingerprint_cache 모두 maxlen / LRU 적용으로 무한 증가 방지.
- **graceful shutdown**: SIGTERM 수신 시 docker events stream 정리 후 최대 30초 내 종료.
- **Discord 실패**: 재시도 3회 (1s, 2s, 4s 지수 백오프) + 429 Retry-After 존중. 모두 실패해도 incident.jsonl 은 보존됨.

---

## 검증

```bash
# 구문 / import 검증 (개발 머신)
python3 -m py_compile watchdog/*.py
python3 -c "import sys; sys.path.insert(0, '.'); import watchdog.recoder_watchdog"

# 설정 검증 (EC2)
sudo /usr/bin/python3 /opt/recoder/watchdog/recoder_watchdog.py --check
```

실제 EC2 인스턴스에서의 동작 검증 (docker daemon, 헬스체크 URL, Discord webhook 송수신) 은 배포 후 운영자가 수행해야 한다.
