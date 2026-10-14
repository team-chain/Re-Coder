# 원문 이용 조건 검토 기록

검토일: 2026-10-03. 이 기록은 포함된 원문의 저장소 LICENSE, 지정 commit,
출처 경로에 대한 배포 조건 검토다. 웹사이트 전체나 앞으로 추가할 문서에 대한 허가로 확대하지 않는다.

| 원문 | 적용 조건 | 수집 결정 |
|---|---|---|
| Docker docs | Apache-2.0 | `content/manuals/build/concepts/context.md`만 포함. 저작권·라이선스 전문 보존 |
| npm CLI 문서 | Artistic-2.0 | npm 웹사이트의 다른 라이선스와 혼동하지 않고 CLI 저장소 원문·전문을 사용 |
| Node.js v22 문서 | MIT | `doc/api` 문서 포함. Node의 전체 LICENSE도 함께 보존 |
| Vite 문서 | MIT | 공식 저장소 문서 포함, 저작권·전문 보존 |
| Express 웹 문서 | CC-BY-4.0 | 공식 main 브랜치 LICENSE.md 기준. 기여자·원문 링크·발췌 변경 명시 |
| AWS ECS·ECR·S3 공개 저장소 | CC-BY-SA-4.0 | 2023-06 마지막 공개 snapshot만 포함. 발췌에도 같은 조건 적용 |
| AWS IAM 공개 저장소 | MIT-0 | 저장소의 문서 사용 허가 확인. 전문과 출처 보존 |
| multilingual-e5-small 모델 | MIT | Hugging Face 모델 카드의 license=mit 확인. 가중치는 명시적 설치, Git 미포함 |

정확한 LICENSE 링크와 원문 commit은 `source-manifest.json`에 있다. 원문 LICENSE 파일은
`licenses/`에 그대로 보존했다. npm CLI 문서에 npm/documentation의 CC-BY 라이선스를 대신 적용하지 않았다.
Docker의 외부 vendored BuildKit 문서는 이번 데이터셋에 포함하지 않았다.

`corpus.json`은 서로 다른 원문을 모은 데이터 **컬렉션**이다. 각 발췌의 라이선스는
`license`, `license_url`, `attribution`, `source_url`, `source_date`로 구분하며, Re-Coder의 MIT 코드
라이선스로 다시 허가하지 않는다. CC-BY-SA 원문의 발췌·문단 분할물은 CC-BY-SA-4.0으로 제공한다.
본문 번역이나 의미 수정은 하지 않았고, 선택한 절을 1,600자 이하 단락으로 나눴다.
직접 작성한 요약은 `authored_summary`, 복제한 원문은 `excerpt`로 구분한다.

AWS 공개 문서 저장소는 2023년에 보관 상태가 됐다. 이 라이선스를 현재 docs.aws.amazon.com 원문에
자동 적용하지 않는다. 현재 AWS 웹 원문의 전문 색인은 제외하며, UI에 snapshot 날짜를 표시한다.
현재 페이지는 참조 링크로 제공한다. 최신 서비스 동작과 IAM 시뮬레이터의 범위는 따로 검증해야 한다.

Docker/npm/Node/Vite/Express 갱신은 승인된 repository/ref만 추적한다. LICENSE SHA-256이
바뀌면 새 원문 수집을 중단하고 기존 색인을 유지한다. AWS snapshot은 자동으로 최신 문서라고 바꾸지 않는다.
Lambda가 배포된 경우 배포 데이터와 함께 `index/licenses/<sha256>.txt`에 이용 조건 전문을 보존한다.

모델 카드: https://huggingface.co/intfloat/multilingual-e5-small
모델 고정 revision: 614241f622f53c4eeff9890bdc4f31cfecc418b3
