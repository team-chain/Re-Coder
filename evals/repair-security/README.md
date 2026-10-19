# ReCoder 수정안 보안 회귀 평가 (2.0.40)

Promptfoo 0.124.1이 고정 공격 사례 19개를 실제 `RepairPipeline`·`RepairStore`에
재생한다. 정상 수정 준비/승인 2개를 대조군으로 포함하므로 모든 요청을 차단해도 통과하지 못한다.
제품을 사용하는 사람은 Promptfoo를 설치할 필요가 없다. 이 폴더는 개발·CI 전용이다.

## 확인하는 것

- 모델 응답의 경로 이탈·절대 경로·비밀 파일·테스트 덮어쓰기를 거부하는가.
- 가짜/누락 인용, 비밀값 삽입, 호스트 명령 필드, 모델의 `approved` 주장을 거부하는가.
- 재빌드 실패·클라우드 미검증·변경된 작업 폴더·변조된 DB·다른 작업 폴더에 대한 승인을 막는가.
- 승인 전 원본/테스트/폴더 밖 파일을 보존하고, 합성 비밀값을 프롬프트와 결과에서 가리는가.

모델과 빌드 검증기는 **고정 fixture**다. 수정안 검증·실제 readiness 규칙·문서 선택·
저장 무결성·승인 적용은 제품 코드를 호출한다. 별도 `scripts/smoke_grounded_repair.py`가
실제 Docker 재빌드 경로를 검사한다. 이 평가를 **실제 LLM의 프롬프트 인젝션/탈옥 저항성,
일반적인 공격 성공률, 모든 위협의 탐지율**로 해석하면 안 된다. 로그 주입 사례도 악성
응답을 재생하는 것으로, 모델이 실제로 그 지시를 따랐다는 뜻은 아니다.

## 실행

Node 22.22 이상(권장 24)과 Core 의존성이 설치된 Python 3.11 이상을 사용한다.

```sh
python -m pip install -r core/requirements.txt
npm ci --ignore-scripts --omit=optional --prefix evals/repair-security
npm run setup:native --prefix evals/repair-security
# 필요하면 PROMPTFOO_PYTHON에 Core 의존성이 있는 Python 실행 파일을 지정한다.
npm test --prefix evals/repair-security
```

`run.cjs`가 텔레메트리·업데이트 확인·호스팅된 공격 생성을 끄고, 로컬 설정 디렉터리를
분리한다. 임시 프로젝트만 수정하며 유료 LLM·실제 AWS·Docker 호출이 없다.
입력/기대값은 `cases.yaml`, 관측 결과는 `results/security.json`에 있다. 실패하면 종료 코드가
0이 아니므로 CI가 막힌다. JSON은 성공/실패와 관계없이 CI artifact로 보관한다.
사용자의 코드나 실제 비밀값을 평가 fixture에 넣지 않는다.

의존성은 평가에 필요한 부분만 설치한다. `--omit=optional`은 사용하지 않는 클라우드
SDK·JKS 키 저장소·Nunjucks의 선택형 watcher를 제외한다. 2026-10-20 기준 이 경로의
`node-forge`·`braces`에는 수정되지 않은 advisory가 있으므로 전체 선택 의존성을 추가하면
다시 검토해야 한다. `.npmrc`에도 같은 설정을 둔다. `basic-ftp`는 수정된 6.2.3으로
고정 override한다. SQLite 플랫폼 바인딩은 CLI 초기화에 필요하므로 `setup:native`가
현재 OS/CPU용 `@libsql` 하나만 잠금 파일의 정확한 버전으로 설치한다(설치 script는 실행하지 않음).
이 평가는 FTP/네트워크 provider를 사용하지 않는다.
CI는 실제 설치 대상에 `npm audit --omit=optional --audit-level=moderate`도 실행한다.

2026-10-20 로컬 검증: 19/19 통과, 설치 구성 audit 0건. 잘못된 기대값을 넣은 별도
대조군은 failure 1/error 0/종료 코드 100으로 실패했다. 이는 평가 판정 실패가 CI를 막는지
확인한 것으로 공격 방어 성능 수치에는 포함하지 않는다.

관련 advisory: [braces](https://github.com/advisories/GHSA-vfj7-8cjw-p6xm),
[node-forge](https://github.com/advisories/GHSA-86w9-cpqp-85rv),
[basic-ftp](https://github.com/advisories/GHSA-c475-qrg2-pj4r).

확장은 새로운 공격 사례와 독립적인 기대값을 `cases.yaml`에 추가하는 방식으로 한다.
실제 모델 평가를 추가하려면 별도 provider·예산·수동 실행 경로를 마련하고 결과를 분리한다.

참고: [Python provider](https://www.promptfoo.dev/docs/providers/python/),
[JavaScript assertions](https://www.promptfoo.dev/docs/configuration/expected-outputs/javascript/),
[텔레메트리 설정](https://www.promptfoo.dev/docs/configuration/telemetry/).
