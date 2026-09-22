# ECS 배포 진행 계약 (A3)

2026-09-23 기준. `core/schemas.py`의 추가 계약:

- `ECSDeployStep`: `key`, `label`, `status` (`pending`, `running`, `done`, `failed`, `cancelled`, `skipped`, `warning`), `started_at`, `finished_at`.
- `ECSDeployRecord.progress_steps`: 기본 빈 목록. 과거 기록을 불러올 때 존재하지 않는 단계 이력을 추정하지 않는다.
- `/api/deploy/ecs/status`: 기존 `running`, `stage` 토큰 계약을 유지한다. `stage_text`에 현재 실행 단계를 표시하고 `steps`, `error_detail`, `warnings`, `observed_at`을 추가한다. 기존 `service_url`도 확장 화면까지 전달한다.
- `deployment_id` 쿼리는 선택이다. 지정하면 해당 배포만 반환하고, 없으면 기존의 최신 배포 조회를 유지한다. 없는 ID는 404다.
- 단계 변경은 기존 ECS 기록 저장소에 저장한다. Core 재시작으로 중단된 배포는 실행 중이던 단계도 실패로 종료한다.
- 빌드 워커는 단계 이벤트를 Core 이벤트 루프에 전달한다. ECR 로그인·push 시작부터 업로드 단계로 표시한다. 진행 기록 저장 오류는 기존 배포 작업을 중단시키지 않는다.
- 웹뷰의 상태 조회 결과는 별도 ECS 진행 카드에 전달한다. 일반 배포 안내 메시지를 덮어쓰지 않으며, 요청 실패·조회 실패·배포 실패를 구분한다. 오래된 조회 응답은 완료 상태를 진행 중으로 되돌리지 않는다.

## 검증 범위

AWS와 Docker 동작을 대체한 파이프라인 테스트에서 단계 순서, 실패 위치, 취소, 생략, URL, 기록 복구를 확인한다. React 실제 렌더와 상태 전이, 호스트 전달 경로도 테스트한다. 실제 AWS 배포 검증은 별도로 수행해야 하며, 테스트를 위해 ECS 서비스를 자동으로 시작하지 않는다.

# 배포 이력 계약 (A2)

- `GET /api/deploy/history`: ECS와 로컬 배포 기록을 최신순으로 읽는다. `source=all|local|ecs`, `limit=1..500`. 환경변수와 원본 요청은 응답에 포함하지 않는다. 현재 AWS나 Docker에 접속하지 않는다.
- `POST /api/deploy/history/{source}/{deployment_id}/rollback`: `approved: true`만 허용. 로컬은 최신 컨테이너 기록과 실제 이미지 ID를 잠금 안에서 재검사하고, ECS는 기존 승인 대기 제안과 서비스 변경 검사를 재사용한다. 임의 과거 시점 복원은 제공하지 않는다.
- 로컬 실제 배포 라우트의 기록을 `~/.recoder/local_deployments.json`에 원자적으로 저장한다(0600). 이전 `projects/*_deployments.jsonl`도 읽되 새 스냅샷이 우선한다.
- `DeploymentRecord`에 `rollback_status`, `rollback_completed_at`, `rollback_error` 추가. 재시작 중단은 완료로 추정하지 않고 `unknown`으로 표시한다.
- 기존 로컬 기록에는 환경변수와 포트 등 복원 정보가 포함된다. 이력 API는 표시용 필드만 반환한다.

# 로컬 롤백 결과·감시 (B1)

- 로컬 롤백 응답에 `health_ok`(미검사 시 null), `health_check_url`, `restored_deployment_id`를 추가한다. 기존 `verification_resumed`와 함께 사용한다.
- Ship 화면은 롤백 응답의 배포 ID를 대조하고 복구된 배포의 감시를 조회한다. 늦게 도착한 실패 배포의 감시 응답은 무시한다. 헬스 미확인이나 감시 부재를 복구·감시 중으로 단정하지 않는다.

# 구조 지도 (B3)

- 파일 계층 안에서 화면 폭에 맞춰 노드를 줄바꿈한다. 많은 노드는 세로 스크롤로 확인하며 파일 이름 전체는 툴팁으로 제공한다.
- JavaScript의 인라인 화살표 함수·익명 함수도 정적 분석에 포함한다. 일반적인 Express 라우트는 HTTP 메서드·경로로 표시한다. 동적 호출과 복잡한 문법은 정적 분석의 한계가 있다.
- 새로고침 중에는 버튼을 잠그고, 식별 결과가 없을 때 실제 함수가 없다고 단정하지 않는다.

# 진단·AWS 연결 표시 (B2)

- AI 진단은 설정된 primary 모델/Provider 기본값을 먼저 ping하고 Provider 후보로만 폴백한다. 카탈로그의 임의 모델은 사용하지 않는다. 화면에서는 진단 시 성공한 모델임을 명시하며 작업별 실제 모델로 단정하지 않는다.
- 사용하지 않는 ListFoundationModels 권한을 정책 생성기와 온보딩 템플릿에서 제거했다. 실제 계정의 기존 IAM 정책은 변경하지 않는다.
- 역할 모드는 AWS 상태 응답과 화면 모두 임시 키 끝자리를 숨긴다. credentials 파일의 기본 프로필은 자동 연결로 표시하고 저장 위치 설명을 구분한다.
- 허브 홈과 로컬 Docker 카드에서 자동 조치를 직접 실행한다. 조치 상태는 항목별로 관리하고 다른 항목·이전 진단 응답이 Docker 대기 버튼을 풀지 않는다.
