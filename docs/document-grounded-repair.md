# 문서 근거 기반 배포 오류 수정

작업 브랜치: `feat/document-grounded-repair`, 시작 commit `66887b2`.
확장과 Core의 표시 버전은 **1.1.30**이다. `develop`의 `6699ccf`를 통합했다.
새 배포 점검·보안 수정·외부 프로젝트 선택 기능을 유지하고, 프로젝트 변경 시 이전 수정안과 진행 중 응답을 무효화한다.

## 사용 흐름

빌드 실패 카드와 ECS 배포 실패 상세에 **문서 근거로 오류 수정** 패널이 있다.
버튼을 누르면 현재 `build_readiness`의 배포 준비 점검을 먼저 실행한다. 최초 31종에 한정하지 않고
1.1.30의 npm 자체 업그레이드·비어 있는 의존성 폴더 점검 등 추가 규칙도 그대로 사용한다.
규칙 오류는 LLM 없이 기존 수정 흐름을 사용한다.
그 밖의 오류는 로그 핵심, 프로젝트 파일 일부, 공식 문서 근거로 수정안을 만들고 복사본에서 검증한다.
화면에서 설명·파일 diff·근거 단락·출처 날짜·호출 수·추정 비용을 확인할 수 있다.

빌드 실패가 재현되고 수정 후 빌드가 통과한 경우에만 **검증된 수정 승인·적용** 버튼이 나온다.
승인 시 프로젝트 경로와 전체 파일 해시를 다시 검사한다. 파일이 달라졌거나 1시간이 지나면 다시 검증해야 한다.
UI 요청 ID와 활성 프로젝트 확인으로 늦게 도착한 다른 프로젝트의 결과가 적용되지 않게 한다.
승인은 파일 수정이며 자동 재배포를 실행하지 않는다.

ECS/IAM/S3/런타임의 제품 API는 빌드 통과만으로 승인하지 않는다. 해당 환경의 검증이 없으면
`environment_verification_required` 또는 수동 확인 상태를 표시한다. 연구용 로컬 계약 검사와
IAM 시뮬레이션은 이 승인 조건을 우회하지 않는다.

## 실제 의미 검색 설정

Python 개발 Core에 다음을 설치한다. 모델 다운로드는 이 명시적 설치 단계에서만 일어난다.

```sh
python -m pip install -r core/requirements.txt -r core/requirements-repair.txt
python scripts/setup_repair.py --assets work/repair-assets --config core/grounded_repair/runtime.json
python scripts/evaluate_repair_retrieval.py --output work/retrieval-check.json
```

모델은 MIT `intfloat/multilingual-e5-small`의 고정 revision을 사용한다. CPU 로컬 임베딩이며 유료 임베딩
API를 호출하지 않는다. E5의 `query: `와 `passage: ` 접두사를 구분한다. 모델/색인은 메모리에 재사용한다.
다른 위치에 설정을 만들었다면 VS Code의 `recoder.repair.configPath`에 JSON 절대 경로를 넣고 Core를 재시작한다.
PyInstaller 기본 바이너리는 PyTorch를 포함하지 않으므로 이 선택 기능은 위 의존성을 설치한 Python Core에서 실행한다.

`RECODER_REPAIR_CONFIG` 또는 환경변수 `RECODER_REPAIR_CORPUS`, `RECODER_REPAIR_EMBEDDING_MODEL`로도 지정할 수 있다.
기본 모델 경로가 없으면 `keyword_only`, 설정이 정상일 때 `hybrid`를 표시한다. 설정한 모델을 못 읽으면 오류를 반환한다.

BM25와 cosine 검색을 RRF로 합치며 정확한 오류 코드를 먼저 보장한다. 코드 예시가 긴 원문이 짧은 증상 설명을
밀어내지 않도록 가장 관련 있는 직접 작성 요약 1개를 의미 검색의 기준으로 함께 선택한다. 원문 발췌와 요약 표시는
구분된다. 입력은 로그 최대 4,000자/16줄, 파일 최대 24,000자/12개, 근거 단락 최대 4개다.

최종 색인은 **158개 단락**(직접 작성 요약 10개 + 라이선스 검토 원문 148개)이다.
한국어 증상 4개 개발 점검에서 관련 문서 Top-1 4/4를 확인했다. 이는 작은 개발 점검이며 독립적인 검색 성능 수치가 아니다.
원문 이용 조건과 오래된 AWS snapshot의 제한은 [라이선스 기록](../benchmarks/repair/LICENSES.md)을 참고한다.

## 모델·비용·예산

문서 수정 전용 Bedrock 기본 등급은 서로 다르다.

- fast: `global.anthropic.claude-haiku-4-5-20251001-v1:0`
- primary: `global.anthropic.claude-sonnet-4-5-20250929-v1:0`

기존의 일반 AI 라우팅 기본값은 바꾸지 않는다. `RECODER_REPAIR_FAST_MODEL`, `RECODER_REPAIR_PRIMARY_MODEL`로
수정 전용 모델을 설정할 수 있다. 명시적으로 선택한 API 키/게이트웨이 인증 경로는 유지한다.
이 실험은 Bedrock만 사용했다. SDK 재시도를 1회 요청으로 제한하고 Converse 응답의 실제 입출력 토큰을 기록한다.
모델·사용량 출처·재시도 수·가격 출처를 각 시도에 남긴다. 가격이 없는 호출은 비용 미확인으로 표시한다.
단가와 검토일은 `benchmarks/repair/prices.json`에 있다. 계산값은 세전 공개 단가 추정이며 청구서가 아니다.

실험은 호출 전에 UTF-8 입력 바이트, 메시지 여유분, 최대 출력 토큰으로 최악 비용을 예약한다.
SQLite 트랜잭션으로 동시 호출을 합산하고, 타임아웃/중단으로 사용량을 모르더라도 예약을 유지한다.
사용자가 승인한 총 **$30**을 모든 예비·최종 실험이 하나의 장부로 공유한다.
제품의 개별 UI 호출까지 이 연구 예산으로 제한하는 것은 아니며, 실험 실행기에 적용된다.

## 36개 실행 과제와 비교

`benchmarks/repair/fixtures/`의 36개 프로젝트는 npm 6, Node/프레임워크 6, Docker 6,
ECS/ECR 6, IAM/S3 6, 로직 대조군 6이다. 각 프로젝트의 고장 상태 실패와 별도 정답 수정 통과를 확인했다.
정답 파일과 검증 oracle은 모델이 보는 프로젝트 밖의 `references/`, `oracles/`에 둔다.
수정된 Dockerfile에서 검사를 지워도 외부 oracle을 통과해야 한다.

24개는 실제 Docker 빌드와 격리된 Node/C/Express/Vite 실행을 검증한다. AWS 관련 12개 중 8개는
실제 AWS `SimulateCustomPolicy`, 나머지 4개는 Docker HTTP/시작 시간/이미지 태그 계약을 검사한다.
SCP, endpoint 정책, KMS grant, 실제 cross-account 승인 전체나 Fargate 배포를 검증한 것은 아니다.
각 과제와 결과의 `verification_kind`, `note`에 이 범위를 기록한다.

| 조건 | 모델 | 문서 | 최대 생성 |
|---|---|---|---|
| A | 대형 → 대형 | 없음 | 2 |
| B | 대형 → 대형 | 있음 | 2 |
| C | 소형 → 소형 | 있음 | 2 |
| D | 소형 → 대형 | 있음 | 2 |

모든 조건에서 같은 요약·파일·규칙·검증기를 쓰고 캐시를 끈다. 조건 순서는 seed 42로 섞는다.
실행 중 원본 프로젝트를 바꾸지 않으며, 각 시도는 새로운 복사본으로 시작한다.
규칙 처리 과제는 모델 해결률 분모에서 제외한다. 클라우드 시뮬레이션 결과는 실제 배포 성공과 분리한다.

```sh
python scripts/validate_repair_cases.py --jobs 3
python scripts/benchmark_repair.py  # 준비 상태만 확인, 과금 없음
# 실제 유료 실험:
python scripts/benchmark_repair.py --live --output work/experiment/final \
  --budget-ledger work/experiment/budget.db --budget 30 --jobs 3
```

Docker, AWS 자격증명과 `iam:SimulateCustomPolicy`, 위 두 Bedrock 모델 사용 권한이 필요하다.
AWS 자원을 만들거나 바꾸지 않는다. macOS에서 Buildx 설정을 별도 폴더로 두려면
`BUILDX_CONFIG="$PWD/work/buildx"`를 설정한다. 작업 완료마다 결과·예산을 저장하며 같은 조건으로 재개할 수 있다.

실험 수치는 [실험 보고서](../benchmarks/repair/RESULTS.md)에 있다. 36개 개발 과제에 조건별 1회씩 실행한
탐색 실험이다. 같은 과제에서 검색기를 개선한 이력이 있어 독립 검증이나 통계적 우월성으로 해석하지 않는다.
보고서는 2026-10-03의 1.1.27 기반 측정값이다. 1.1.30 통합 시 유료 실험을 다시 실행하지 않았으며,
기존 기록을 새 버전의 측정값으로 바꾸지 않는다.
실패 비용도 해결당 비용에 포함한다. 인용 검토자는 코드 작성 AI이며 사람의 독립적인 검토를 대신하지 않는다.

## 고객 계정의 문서 갱신

`infra/repair-documents.yaml`은 private/versioned S3와 Lambda를 구성한다. 일일 스케줄은 기본 비활성이다.
`source-manifest.json`은 승인된 저장소·원문 경로·ref·LICENSE SHA-256을 명시한다. 추적 ref의 새 commit에서
이용 조건이 같을 때만 갱신하고, 이용 조건이 바뀌거나 다운로드가 실패하면 기존 색인을 유지한다.
AWS의 보관된 공개 문서는 고정 snapshot으로 남는다. 라이선스 전문도 S3에 함께 보존한다.

```sh
PYTHONPATH=core python -m grounded_repair.refresh benchmarks/repair/source-manifest.json work/corpus.json --update
sam build -t infra/repair-documents.yaml
sam deploy --guided
# 생성된 버킷에 sources/manifest.json으로 source-manifest.json 업로드 후 일정 활성화
```

이번 검증에서는 클라우드 자원을 배포하지 않았다. Lambda 패키징·소스 갱신·실패 시 이전 색인 보존을 검증했다.
S3의 새 색인은 내려받은 뒤 설정의 corpus 경로에 연결한다. 문서 갱신 비용은 모델 비교 비용과 별개다.

## API와 검증

기존 세션 토큰으로 보호된 `POST /api/repair/prepare`, `GET /api/repair/{id}`,
`POST /api/repair/{id}/approve`를 사용한다. 승인 본문은 `workspace_path`를 받는다.
승인 API는 미검증·만료·다른 프로젝트·파일이 변경된 요청을 거부한다.
캐시는 동일 프로젝트/파일/오류/문서/모델/가격/검증기에서만 24시간 재사용하며 다시 빌드한다.

검증 명령:

```sh
python -m pytest core/tests -q
npm --prefix extension test
npm --prefix extension run lint
npm --prefix extension run build
npm --prefix extension run verify:package
python scripts/smoke_grounded_repair.py
```

파일 편집 범위는 기존 텍스트 파일이며 복사본은 32 MB/4,000파일로 제한한다.
비밀 설정 파일, 심볼릭 링크와 워크스페이스 밖의 경로는 제외한다. 해당 환경이 필요한 프로젝트는 별도 검증 환경이 필요하다.
