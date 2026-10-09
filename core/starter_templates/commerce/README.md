# 검증된 쇼핑몰 기반 v1

Re-Coder가 제공하는 React·Express·PostgreSQL·Stripe SDK 시작 프로젝트입니다. 설계 단계에서 이 구성을 선택했을 때 생성됩니다. AI 자유 생성 결과와 구분하며, 변경 후에는 다시 검증해야 합니다.

## 포함 기능과 범위

가입·로그인, 상품 검색·상세, 장바구니, 주문, 재고 예약, 서명된 결제 웹훅, 관리자 상품·재고·주문 화면을 제공합니다. 가격은 서버에서 계산하고 주문과 웹훅의 중복 요청, 동시 재고 경쟁, 타인 주문 접근을 방어합니다. 관리자 권한을 회수하면 기존 토큰도 관리자 API에 접근할 수 없습니다.

미결제 주문은 15분 뒤 만료 대상이 됩니다. 서버는 1분마다 최대 50개를 정리합니다. 결제사 취소가 확인된 주문만 재고를 반환하며, 여러 서버가 동시에 실행해도 행 잠금으로 중복 반환을 막습니다. 결제사 응답이 불확실하면 다음 시도까지 재고를 유지합니다.

기본 통화는 USD입니다. 실물 배송·세금 계산·환불·정산·이메일 알림·비밀번호 재설정은 구현 범위에 포함되지 않습니다. 사업에 필요한 항목을 구현하고 실제 결제사 테스트 계정으로 검증하기 전에는 일반 고객에게 판매를 개시하지 마세요. 자동 검사는 모의 결제 서버를 사용했으며 실제 카드 승인이나 정산의 검증 결과가 아닙니다.

## 실행

검증 환경은 Node.js 22, PostgreSQL 17입니다. React 18, React Router 7, Vite 8을 사용합니다. 저장소 루트에서 다음을 실행합니다.

```sh
npm --prefix backend ci
npm --prefix frontend ci
```

루트 `.env`에 `DATABASE_URL`, `JWT_SECRET`, `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `STRIPE_PUBLIC_KEY`, `FRONTEND_URL`을 설정합니다. JWT 키는 `node -e "console.log(require('crypto').randomBytes(48).toString('hex'))"`처럼 무작위로 생성하세요. 비밀값을 코드·Git·브라우저에 넣지 않습니다.

| 환경변수 | 용도 |
| --- | --- |
| `DATABASE_URL` | PostgreSQL 연결 문자열 |
| `JWT_SECRET` | 32자 이상의 무작위 서명 키. 운영 모드는 예시 키를 거절합니다. |
| `STRIPE_SECRET_KEY` | 서버 결제 API 키 |
| `STRIPE_WEBHOOK_SECRET` | 웹훅 서명 키 |
| `STRIPE_PUBLIC_KEY` | 브라우저용 공개 키. `/api/config`가 공개 키만 전달합니다. |
| `FRONTEND_URL` | CORS를 허용할 실제 HTTPS 주소 |
| `NODE_ENV` | 운영은 `production` |
| `PORT` | 기본 `3001` |
| `DB_SSL` | TLS 검증을 사용할 때 `true` |
| `NODE_EXTRA_CA_CERTS` | RDS 사용 시 `/app/certs/rds-ca.pem` 등 신뢰할 CA 경로 |
| `SEED_DEMO` | `true`일 때만 빈 상품 테이블에 데모 상품을 넣습니다. |

```sh
npm run init-db
npm run build
npm start
```

`/health`는 DB 연결까지 검사합니다. 개발 시에는 루트 환경변수를 export하거나 `backend/.env`로 설정한 후 `npm run dev:backend`와 `npm run dev:frontend`를 각각 실행합니다. 관리자 기본 계정은 없습니다. 가입한 본인 계정의 `users.role`을 DB 관리자가 `admin`으로 변경한 뒤 다시 로그인합니다.

기존 DB를 삭제해 업데이트하지 마세요. 초기화 SQL은 최초 설치용이며, 스키마 변경은 백업 후 버전별 마이그레이션과 복구 시험을 거쳐 적용합니다.

## Docker와 ECS

```sh
docker build -t shopping-mall:local .
# schema 초기화는 배포 전에 같은 이미지의 일회성 작업으로 실행합니다.
docker run --rm --env-file .env shopping-mall:local node backend/init-db.js
docker run --rm --env-file .env -p 127.0.0.1:3001:3001 shopping-mall:local
```

이미지는 비루트 사용자로 실행합니다. ECS에서는 `DATABASE_URL`, `JWT_SECRET`, `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`을 Secrets Manager ARN으로 전달합니다. 공개 키·HTTPS 주소 등은 일반 환경변수로 설정할 수 있습니다. Node의 CA 검증을 끄지 마세요.

운영용 HTTPS는 Re-Coder의 `infra/production-edge.yaml`로 구성한 CloudFront·비공개 ALB와 연결할 수 있습니다. 애플리케이션 보안 그룹은 ALB에서 오는 3001번 포트만 허용하고, RDS는 애플리케이션 보안 그룹만 허용합니다. 백업 보존, 경보, 비밀값 교체, 복구 절차는 사용하는 AWS 계정에서 설정합니다.

Stripe 웹훅 경로는 `https://<서비스 주소>/api/webhooks/stripe`입니다. `payment_intent.succeeded`, `payment_intent.payment_failed`, `payment_intent.canceled` 이벤트를 설정합니다. 공개 키가 없으면 실제 결제 UI를 활성화하지 않습니다.

## 모의 결제 검증

Re-Coder 저장소의 `benchmarks/commerce`에서 Docker Compose와 블랙박스 검사를 실행할 수 있습니다. `NODE_ENV=test`와 `PAYMENT_MODE=mock`, `STRIPE_MOCK_HOST`, `STRIPE_MOCK_PORT`는 격리된 테스트에서만 사용합니다. 이 조합에서는 카드 대신 테스트 주문 버튼을 표시하며, 결제 완료는 서명된 웹훅을 받은 뒤에만 기록합니다. 운영 모드는 모의 결제 설정을 거절합니다.

## API

- 인증: `POST /api/auth/register`, `POST /api/auth/login`
- 상품: `GET /api/products`, `GET /api/products/:id`
- 장바구니: `GET /api/cart`, `POST /api/cart`, `POST /api/cart/:product_id`, `DELETE /api/cart/:product_id`
- 주문: `POST /api/orders` (`idempotency_key` 필수), `GET /api/orders`, `GET /api/orders/:id`, `POST /api/orders/:id/cancel`
- 관리자: `POST /api/admin/products`, `PUT /api/admin/products/:id`, `DELETE /api/admin/products/:id`, `GET /api/admin/orders`
- 공개 설정: `GET /api/config`; 상태: `GET /health`

라이선스: ISC. `certs/rds-ca.pem`은 공개 RDS 신뢰 인증서이며 개인 키가 아닙니다.
