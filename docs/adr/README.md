# ADR (Architecture Decision Records)

이 폴더는 ReCoder 가 생성하는 **설계 결정 기록(ADR)**이 쌓이는 곳이다.

## 흐름 (AI-DLC 회차1)

1. 사용자가 요청하면 `POST /api/code/plan` 이 코드 대신 **설계 결정 선택지**를 제시한다.
2. 사용자가 결정 카드에서 옵션을 선택·승인한다.
3. 그 결정을 담아 `POST /api/code/generate` 를 호출하면,
   - 생성 코드가 그 결정을 따르고,
   - **동시에** 각 결정이 `docs/adr/ADR-NNN-<슬러그>.md` 파일로 영속화된다.

즉 "코드 + ADR 동시 산출" (FR-02-03/04).

## 파일 규칙

- 파일명: `ADR-<3자리 번호>-<슬러그>.md` (예: `ADR-001-storage.md`)
- 번호는 이 폴더의 기존 ADR 을 스캔해 자동 증가한다.
  대상 폴더(`target_folder`)를 지정해 생성한 경우 그 폴더 아래의 `docs/adr` 을 기준으로 센다.
- 구조: 상태 · 날짜 · 요청 · 결정 · 근거 · 검토한 대안 · 영향

## 구현 위치

- `core/adr.py` — 결정 정규화(`normalize_decisions`) + ADR 생성(`build_adr_ops`)
- `core/code_agent.py` — 정규화 결과를 프롬프트에 주입(`_decisions_prompt_block`) 후 ADR ops 를 함께 반환

결정 파싱은 `normalize_decisions` **한 곳에서만** 수행한다.
프롬프트가 말하는 결정과 ADR 이 기록한 결정이 어긋나면 ADR 의 존재 의미가 없어지기 때문이다.
