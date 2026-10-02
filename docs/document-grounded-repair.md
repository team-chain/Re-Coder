# 문서 근거 기반 배포 오류 수정 — 1차 구현

기준: `develop`의 `4b2a51d`, 작업 브랜치 `feat/document-grounded-repair`.

이번 변경은 코어 API와 검증 파이프라인을 구현한다. VS Code 전용 버튼, 실제 모델 비교 실험,
ECS/IAM 환경 재현까지 완료한 제품 릴리스는 아니다. 실험 결과나 비용 절감률을 임의로 채우지 않는다.

## 처리 흐름

1. 기존 `build_readiness.analyze()`로 검사한다. 오류가 있으면 기존 규칙을 사용하고 LLM·문서 검색을 호출하지 않는다.
   자동 수정 가능한 규칙은 복사본에 적용한 뒤 빌드한다. 수동 수정·새 파일 생성이 필요한 규칙은 기존 readiness API로 안내한다.
2. 원본 로그 최대 100만 자에서 비밀을 가리고, 오류 코드·npm 버전·ECS 중지 사유 등을 최대 4,000자/16줄로 추린다.
3. BM25 키워드 검색과 선택적 로컬 임베딩 검색을 reciprocal rank fusion으로 합쳐 문서 4개를 선택한다.
4. 로그 핵심, 관련 파일 최대 24,000자/12개, 문서 몇 단락으로 JSON 수정안을 생성한다.
   D 조건은 소형 모델 우선이며 실패하면 대형 모델을 한 번 호출한다. 전체 생성 예산은 두 번이다.
5. 각 시도는 원본에서 새 복사본을 만든다. 문서 ID·파일 경로·스키마·정적 검사와 실제 Docker 빌드를 검증한다.
6. 빌드 오류는 원래 실패가 재현되고 수정 후 빌드가 성공해야 `ready_for_approval`이 된다.
   승인 시 검증에 사용한 파일 목록 전체의 SHA-256을 다시 비교한다. 변경됐거나 한 시간이 지나면 재검증이 필요하다.
7. 승인된 내용만 원본에 쓴다. 배포를 실행하지 않는다. 실패·미검증 수정안은 승인 API가 거부한다.

ECS·IAM·S3·런타임 오류는 로컬 빌드만으로 검증할 수 없다. 해당 단계는 `environment_verification_required`
또는 `manual_action_required`를 반환하며 승인 가능한 상태가 되지 않는다.

## API 사용

기존 코어 서버의 세션 토큰 인증을 사용한다. 별도 공개 서버를 추가하지 않는다.

| API | 용도 |
|---|---|
| `POST /api/repair/prepare` | 오류 분석, 수정안 작성, 복사본에서 검증 |
| `GET /api/repair/{id}` | 기록·문서 출처·diff·비용·검증 결과 확인 |
| `POST /api/repair/{id}/approve` | 검증된 수정안만 원본에 적용 |
| 기존 `POST /api/deploy/readiness/fix` | 기존 규칙 수정 흐름 |

`prepare` 요청 예시:

```json
{
  "workspace_path": "/absolute/path/to/project",
  "log": "Missing parameter name ... Express 5",
  "stage": "build",
  "strategy": "D",
  "use_cache": true
}
```

배포 실패 진단 응답의 `repair` 필드에 API 주소, 규칙/문서 분기, 오류 단계를 붙였다.
자동으로 유료 모델을 호출하지 않는다. 클라이언트가 `prepare`를 호출해야 시작한다.

## 문서와 의미 검색

`core/grounded_repair/sources.json`에는 Docker, npm, Node, Vite, Express, AWS ECS/ECR/IAM/S3를 다루는
공식 문서 링크와 직접 작성한 요약 10개가 들어 있다. **공식 원문을 복제한 색인으로 표시하지 않는다.**
각 출처의 원문 수집 상태는 아직 `pending_review`다. 문서 라이선스 후보 링크는 검토 완료를 뜻하지 않는다.

기본 설치는 `keyword_only`로 동작한다. 의미 검색은 다음과 같이 로컬 모델을 명시적으로 설정한다.

```sh
python -m pip install -r core/requirements-repair.txt
export RECODER_REPAIR_EMBEDDING_MODEL=/absolute/path/to/local/sentence-transformers-model
```

모델은 별도로 내려받아 준비한다. 요청 처리 중 다운로드하거나 유료 임베딩 API를 호출하지 않는다.
한국어 증상도 검색하려면 다국어 모델을 선택해야 한다. 설정한 모델을 읽지 못하면 오류를 반환하며
의미 검색이 된 것처럼 숨기지 않는다. 반환값의 `retrieval_mode`로 실제 검색 방식을 확인한다.
현재 테스트는 주입한 벡터를 이용해 의미가 같지만 단어가 다른 검색을 검증한다. 실제 임베딩 모델 품질 실험은 남아 있다.

원문 수집 시에는 별도 manifest에 다음 항목을 기록해야 한다.

- `fulltext_status: "approved"`
- `license_status: "reviewed"`, `license_url`, `attribution`, `reviewed_at`
- 허용된 공식 HTTPS `url`, `id`, `title`

```sh
PYTHONPATH=core python -m grounded_repair.refresh reviewed-sources.json work/corpus.json
export RECODER_REPAIR_CORPUS=/absolute/path/to/work/corpus.json
```

원문은 문단으로 나누고 출처·라이선스·내용 해시·수집 시각을 남긴다. 리다이렉트, 허용 도메인 밖 URL,
2 MB 초과 문서는 거부한다. 소스 manifest와 생성 색인은 서로 다른 파일이어야 한다.
검색 결과에 인용한 문서 ID가 존재하는지는 자동 검사하지만, 문서가 수정 논리를 실제로 뒷받침하는지는 별도 검토한다.
`citation_review`의 기본값은 `not_reviewed`다.

## 비용 측정과 캐시

기존 코드 조사 결과, 일반 라우터의 일부 경로는 토큰을 글자 수로 추정하고 Gemini/API 키 공급자 비용을
정확히 반영하지 못한다. 새 `LLMProviderRouter.call_repair()`는 공급자의 `call()`이 반환하는 사용량과
`token_source`를 보존하고 통합 `cost_ledger`에도 기록한다. API 키 공급자는 실제 사용량을 반환할 수 있지만,
현재 Bedrock 동기 공급자는 추정 토큰을 반환한다. SDK 내부 전송 재시도 횟수는 측정하지 않는다.

실험에 사용할 **실제 모델 ID별** 단가(USD/백만 토큰)를 환경변수에 JSON으로 입력한다.
아래의 모델 이름과 숫자는 형식 예시이며 실제 가격표가 아니다.

```sh
export RECODER_REPAIR_PRICES='{"your-small-model-id":{"input":1.0,"output":5.0},"your-large-model-id":{"input":3.0,"output":15.0}}'
```

단가가 없거나 실패한 호출의 사용량을 확인하지 못하면 비용은 `null`이다. 무료로 계산하지 않는다.
모델 호출 실패 시 Gemini 등 다른 공급자로 몰래 바꾸지 않는다. 다음 모델 선택은 파이프라인이 담당한다.

성공한 답은 SQLite에 저장하고 동일 워크스페이스·파일 해시·오류·문서 버전·모델/단가·전략·검증기 조건에서만
24시간 이내 재사용한다. 캐시 결과도 다시 빌드한다. API에서 A/B/C 실험의 캐시는 강제로 끄며,
실험 실행기는 D도 끈다. 저장 위치는 `RECODER_REPAIR_DB`로 지정할 수 있다.

## A/B/C/D 비교

| 조건 | 모델 | 문서 | 최대 생성 횟수 |
|---|---|---|---|
| A | 대형 → 대형 | 없음 | 2 |
| B | 대형 → 대형 | 있음 | 2 |
| C | 소형 → 소형 | 있음 | 2 |
| D | 소형 → 대형 | 있음 | 2 |

모든 조건은 같은 로그 요약·관련 파일·정적 검사·검증기를 사용한다. 순서는 시드로 무작위화한다.
모델 효과를 비교하는 본 실험과 원본 로그 대비 입력 축소 실험은 구분한다.

`benchmarks/repair/cases.json`에는 36개 **과제 초안**이 있다. npm 6, Node/프레임워크 6, Docker 6,
ECS/ECR 6, IAM/S3 6, 코드 로직 대조군 6이다. 아직 실패 프로젝트 36개를 구현한 것은 아니다.
실행할 프로젝트를 만들고 실제 실패를 확인한 후에만 `status: "ready"`, `workspace`, `log_file`을 지정한다.
코드 로직 과제는 오류를 검출하는 테스트를 이미지 빌드 단계에 포함해야 한다.

```sh
python scripts/benchmark_repair.py --cases benchmarks/repair/cases.json
# 준비된 과제와 단가를 검토한 다음 유료 호출을 실행할 때만:
python scripts/benchmark_repair.py --cases prepared-cases.json --output work/experiment --live
```

실행기는 원본에 수정안을 적용하지 않는다. `runs.json`과 `report.json`을 기록한다. 해결률, 첫 시도 해결 수,
생성 호출 수, 해결까지 호출 수, 전체 실패 비용을 포함한 해결당 비용, 중앙 소요 시간, 인용 검토 정확도를 집계한다.
규칙으로 해결한 과제는 별도 집계하며 모델의 성과로 더하지 않는다. 재현 실패·검증 환경 실패는 무효 과제로 분리한다.
인용의 실제 정확도는 사람이 `citation_review`를 `correct`/`incorrect`로 평가해야 계산된다.

## 고객 AWS 계정에서 문서 갱신

`infra/repair-documents.yaml`은 SAM 템플릿이다. 고객 계정에 private/versioned S3 버킷과 갱신 Lambda를 만들 수 있다.
일일 스케줄은 기본 비활성이다. `sam build -t infra/repair-documents.yaml`로 패키징한 후 배포할 수 있으며,
라이선스를 검토한 manifest를 `sources/manifest.json`에 올린 뒤 스케줄을 활성화한다.
실제 AWS 배포는 이번 작업에서 수행하지 않았다. SAM CLI가 없는 환경이므로 배포 검증도 남아 있다.

Lambda는 manifest 읽기와 `index/corpus.json` 쓰기만 허용된다. 수집 중 오류가 나면 기존 색인을 유지한다.
새 색인을 고객 측에서 내려받고 `RECODER_REPAIR_CORPUS`로 연결한다. 클라이언트의 자동 S3 동기화는 후속 작업이다.

## 검증 및 남은 작업

```sh
python -m pytest core/tests/test_grounded_repair.py core/tests/test_repair_metrics.py core/tests/test_repair_api_refresh.py -q
python scripts/smoke_grounded_repair.py
```

신규 테스트 38개 통과. 실제 Docker 스모크에서 COPY 경로 오타로 빌드 실패 → 수정 복사본 빌드 성공 →
승인 전 원본 유지 → 승인 후 빌드 성공을 확인했다. 스모크의 모델 응답은 고정 픽스처이며 유료 AI 성능을 측정한 것은 아니다.

전체 코어 테스트 첫 실행: 2,209개 통과, 1개 스킵, 7개 실패(당시 신규 테스트 29개 포함).
실패 7개는 수정 전 `develop`에서도 재현했다. 프로젝트 프로필을 홈 디렉터리에 쓰려는 테스트 5개,
Dockerfile 포트 기대값 1개, 브라우저 검사 환경 1개다. 이후 추가된 비용/API 테스트를 포함한 관련 테스트 65개도 통과했다.

후속 작업은 실제 다국어 임베딩 모델 선정·검증, 공식 원문 이용 조건 검토, 실패 프로젝트 36개 구현,
ECS/IAM 환경 검증기, VS Code 실패 카드와 승인 UI 연결, 유료 모델 비교 실험이다.
현재 수정기는 기존 텍스트 파일 편집만 지원하며, 복사본은 32 MB/4,000파일로 제한한다.
빌드에 필요한 비밀·사용자 환경 파일은 복사하지 않으므로 해당 프로젝트에는 별도의 검증 환경이 필요하다.
