# 쇼핑몰 기반의 반복 가능한 검증

`core/starter_templates/commerce`는 AI가 인증·결제 로직을 매번 다시 쓰는 대신 사용자가 설계 카드에서 명시적으로 선택하는 시작 프로젝트입니다. React, Express, PostgreSQL, Stripe SDK를 사용합니다. 자유 생성과 구분해 `foundation=commerce-v1`, `provider=starter`로 기록합니다.

```sh
docker compose -f benchmarks/commerce/compose.yml -p recoder-commerce-check up --build -d --wait
SHOP_ALLOW_DISPOSABLE_TESTS=1 python benchmarks/commerce/acceptance.py
docker compose -f benchmarks/commerce/compose.yml -p recoder-commerce-check down -v
```

Python에는 `httpx`가 필요합니다. API 검사는 새 DB와 HTTP 모의 결제 서버만 사용하며, 운영 DB에 실행할 수 없도록 localhost와 명시적인 테스트 설정을 요구합니다. 테스트 전용 계정 생성, 역할 변경, 장애 주입 트리거를 포함합니다.

50개 검사 범위: 가입·로그인, 비밀번호 길이, 순차·동시 로그인 실패 잠금, UTF-8 비밀번호 바이트 제한, 동시 중복 가입, 역할 상승 방지, 주문 소유권, 서버 금액 계산, 재고 경쟁, 결제 서명·금액·통화 검증, 중복 이벤트, 취소의 멱등성, DB 실패 후 웹훅 재시도, 관리자 권한 회수, 재고 입력 범위, 공개 설정에서 비밀값 제외, 사용자 재방문 없이 미결제 주문 만료 처리, 중복 재고 반환 방지, 결제사 취소 결과 불명 시 예약 유지.

실제 카드 결제·결제사 계정·환불·정산·배송사·세금 정책은 이 검사의 대상이 아닙니다. 계정과 사업 정책을 설정한 다음 별도 인수 검증을 해야 합니다. 기본 데모 상품은 `SEED_DEMO=true`일 때만 삽입합니다. 운영 모드에서는 모의 결제를 사용할 수 없습니다.
