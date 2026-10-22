# 선택형 Reranker 비교 — 2.0.39

결론: 이 개발 데이터에서는 품질 향상 없이 지연이 커졌다. **기본 꺼짐**을 유지하며,
실험용 선택 기능으로만 제공한다. 오류 수정 성공률이 높아졌다고 주장하지 않는다.

| 검색 지표 | BM25 + E5 | 동일 검색 + BGE Reranker |
| --- | ---: | ---: |
| Hit@1 (20개) | 1.000 | 0.950 |
| Hit@4 | 1.000 | 1.000 |
| MRR@4 | 1.000 | 0.975 |
| 중앙값 | 36.592 ms | 10,988.312 ms |
| p95 | 51.401 ms | 12,440.630 ms |
| 버전 부적합 문서 포함 | 0 | 0 |

2026-10-20, macOS arm64, Python 3.12.11, CPU, `OMP_NUM_THREADS=4`.
sentence-transformers 5.7.0, torch 2.14.1, transformers 5.20.1, huggingface-hub 2.2.0.
각 변형 1회 warm-up 제외, 질의별 실행 순서 교대, 각 1회 측정. 지연은 환경 의존적이다.

- 임베딩: `intfloat/multilingual-e5-small`, revision `614241f622f53c4eeff9890bdc4f31cfecc418b3`.
- 재순위화: `BAAI/bge-reranker-v2-m3`, revision `953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e`.
- 기존 오류 코드 우선·버전 필터 유지, 후보 최대 20개, 출력 4개, CPU/배치 4/512 토큰.
- 한국어·영어 개발 질의 20개와 Express 4 버전 차단 대조군 1개. 모델 결과 확인 전에 고정했다.
- 기존 smoke 질의 4개를 포함한다. 독립 holdout이 아니며, 문서 주제 단위 label이다.
  적중은 해당 주제 문서 ID/prefix 일치로 판정하므로 문단이 수정에 충분한지는 평가하지 않는다.
- 다운로드 이후 오프라인으로 실행했다. 유료 LLM 호출 및 오류 수정 생성·실행은 없다.

[원본 결과](evidence/reranker-comparison-2.0.39.json)에 각 질의의 순위·시간과 corpus/dataset SHA-256을 보존했다.
라이브 모델 결과를 유닛 테스트 기대값으로 고정하지 않는다. CI에서는 후보 한도·버전 차단·
잘못된 점수 거부·옵션/캐시 분리·지표 계산을 비용 없이 검증한다.

재실행 명령은 [RAG 운영 문서](../../docs/document-grounded-repair.md#선택형-reranker와-비교-2039)에 있다.
향후 더 큰 독립 질의 세트와 실제 수정 성공률을 평가한 뒤 기본값 변경을 검토한다.
