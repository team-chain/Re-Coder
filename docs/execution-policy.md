# 실행 직전 정책 통제 — 2.0.41

문서 근거 수정과 MCP 분석 도구가 실제 작업을 시작하기 직전에 정책을 확인한다.
기본 `local` 모드는 서버 없이 로컬 경로·복사본·테스트 보호·승인 조건을 적용한다.
`opa` 모드는 같은 로컬 제한에 OPA의 명시적 허용을 추가로 요구한다.

## 적용 지점

| 작업 | 검사 직후 실행되는 것 |
| --- | --- |
| `repair.inspect` | 복사본의 배포 준비 점검(온라인/오프라인) |
| `repair.generate` | 수정 모델 호출 |
| `repair.rule_fix` | 확인된 규칙으로 복사본 수정 |
| `repair.edit` | 모델 수정안의 후보 파일 쓰기 |
| `repair.verify` | 복사본 재빌드 검증, 기본 Docker 실행 |
| `repair.apply` | 명시적으로 승인한 검증 결과를 원본 파일에 적용 |
| `mcp.call` | 등록된 `recoder_analyze` handler 호출 |

명령문·승인 여부·정책 모드를 모델 응답에서 가져오지 않는다. Core가 실제 실행 경로의
정보를 구성한다. 파일 경로 이탈·비밀 파일·테스트 수정은 로컬에서 거부하므로 OPA가
허용해도 우회할 수 없다. 캐시된 수정안도 후보 파일 쓰기·재빌드·승인 정책을 다시 확인한다.
정책 모드/접속 대상이 바뀌면 수정 캐시도 구분한다.

이 범위는 문서 근거 수정 파이프라인과 MCP 분석 호출이다. 기존 코드 생성·배포·확장 전체의
모든 파일/명령 실행을 통제하는 전역 샌드박스는 아니다. Docker 네트워크 격리나 컨테이너
보안 설정을 대신하지 않는다. 기존 배포 게이트는 별도로 유지된다.

## OPA 활성화

검증 환경은 OPA 1.20.2다. 정책 서버를 실행하고 Core 프로세스 환경을 설정한다.

```sh
opa run --server --addr 127.0.0.1:8181 --skip-version-check policies/
# 별도 터미널에서 Core 실행 전에 설정
export RECODER_EXECUTION_POLICY=opa
export OPA_URL=http://127.0.0.1:8181
# 인증을 구성한 서버라면 기존 OPA_TOKEN도 사용한다.
```

`OPA_URL`만 설정하면 실행 통제 모드가 바뀌지 않는다. `RECODER_EXECUTION_POLICY=local`로
로컬 모드를 선택할 수 있다. 알 수 없는 값은 설정 오류로 거부한다. 실행 중 환경/모델
설정을 바꿨다면 Core를 재시작한다. OPA의 정책/데이터 변경은 다음 검사부터 반영된다.

요청은 `POST /v1/data/recoder/execution/allow`로 보낸다. `allow`와 비어 있지 않은
정책 버전이 있어야 실행한다. 서버 중단·타임아웃·잘못된 응답·정의되지 않은 정책·
`allow_with_approval`은 차단한다. 기존 OPA 클라이언트의 낮은 승인 등급에 대한 오프라인
허용을 이 경로에서는 사용하지 않는다. OPA 모드에서 서버가 내려가도 로컬 모드로 돌아가지 않는다.

## 운영자 제한 예시

다음 JSON을 정책 서버 데이터로 로드하면 원본 적용을 중단할 수 있다.
예를 들어 `work/opa-data.json`에 저장한 뒤 서버 인자에 그 경로를 추가한다.

```json
{
  "recoder_execution": {
    "blocked_actions": ["repair.apply"],
    "workspace_ids": []
  }
}
```

`blocked_actions`는 위 표의 작업 이름 목록이다. `workspace_ids`가 빈 배열이면 작업 폴더를
추가 제한하지 않으며, 값이 있으면 일치하는 폴더만 허용한다. ID는 Core가
`SHA256(os.path.normcase(str(Path(workspace).expanduser().resolve())))`로 계산한다.
두 설정은 배열이어야 한다. 파일 내용·로그·비밀값·원본 폴더 절대 경로는 정책 입력으로
보내지 않고, 작업 이름·폴더 ID·상대 파일 이름·검증/승인 사실만 보낸다.

준비 때 허용됐어도 승인 시 정책이 차단하면 파일을 쓰지 않는다. 차단된 승인 기록은 남기되
검증 유효 시간을 연장하지 않는다. 제한을 해제하면 유효한 수정안에 대해 다시 승인할 수 있다.
정책 검사 기록은 수정 결과의 HMAC에 포함하며 승인 기록은 최근 32개까지 보존한다.
이 변경으로 **이전 버전에서 저장한 수정안/캐시는 다시 검증해야 한다.**

## 검증

```sh
python -m pytest core/tests/test_execution_policy.py -q
opa test policies/ -v
python scripts/smoke_execution_policy.py
```

마지막 명령은 임시 로컬 OPA 서버와 실제 Docker를 사용한다. 정상 허용, 준비 후 적용 철회,
Docker 실행 전 차단, 새 허용 후 정확한 내용 적용, OPA 중단 시 차단을 검사한다. 모델만 고정
fixture이므로 유료 LLM 호출이 없다. CI에서도 정책 테스트와 실제 서버 통합을 필수로 실행한다.
화면은 차단 사유를 표시하고, MCP 감사 기록에는 정책 결정과 버전을 추가한다.

참고: [OPA REST API](https://www.openpolicyagent.org/docs/rest-api),
[OPA CI 설정](https://github.com/open-policy-agent/setup-opa).
