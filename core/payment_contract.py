"""결제 시작 방식(모의 결제 / 실제 키) — AI 가 처음부터 만드는 앱에도 같은 약속을 적용한다.

검증된 쇼핑몰 기반은 처음부터 이 약속을 지킨다. AI 자유 생성 앱은 이 약속을 프롬프트로 지시하고,
만든 결과가 실제로 지켰는지 검사해 빠졌으면 일관성 점검이 고치게 한다.

약속(로컬 배포의 모의 결제 서버 · deploy_settings 가 기대하는 것):
  · 환경변수 PAYMENT_MODE · STRIPE_MOCK_HOST · STRIPE_MOCK_PORT 를 process.env.이름 으로 읽는다.
  · NODE_ENV=test 이고 PAYMENT_MODE=mock 일 때만 Stripe SDK 를 그 주소(http)로 보낸다. 운영에서는 거부한다.
  · 결제는 PaymentIntents API, 완료는 POST /api/webhooks/stripe 의 서명 검증된 웹훅으로만 처리한다.
"""

from __future__ import annotations

import re

from commerce_starter import PAYMENT_ID, payment_choice, payment_decision  # noqa: F401 — 한 곳에서 가져다 쓰게

MARKERS = ("PAYMENT_MODE", "STRIPE_MOCK_HOST", "STRIPE_MOCK_PORT")
WEBHOOK_PATH = "/api/webhooks/stripe"

#: 결제가 들어가는 요청인가.
_WANTS = re.compile(r"(결제|payment|checkout|체크아웃|stripe|스트라이프|쇼핑몰|e-?commerce|shopping\s*(mall|store)|온라인\s*(스토어|상점)|online\s*store)", re.I)
#: 다른 결제사를 콕 집었으면 Stripe 기준 모의 결제를 강요하지 않는다(사용자의 선택이 우선).
_OTHER_PROVIDER = re.compile(r"(토스|toss|카카오\s*페이|kakao\s*pay|네이버\s*페이|naver\s*pay|포트원|portone|아임포트|iamport|"
                             r"페이팔|paypal|이니시스|inicis|나이스\s*페이|nicepay|paddle|razorpay|braintree|square)", re.I)
_JS_EXT = (".js", ".mjs", ".cjs", ".ts", ".mts", ".cts")
_SKIP_DIRS = ("node_modules/", "dist/", "build/", "client/", "frontend/", "web/", "public/")


def wants_payment(instruction: str) -> bool:
    return bool(_WANTS.search(instruction or ""))


def other_provider(instruction: str) -> bool:
    return bool(_OTHER_PROVIDER.search(instruction or ""))


def applies(instruction: str) -> bool:
    """이 요청에 결제 시작 방식 결정을 물어야 하는가."""
    return wants_payment(instruction) and not other_provider(instruction)


PLAN_NOTE = (
    "\n\n[결제] 이 앱의 결제사는 Stripe(PaymentIntents) 로 진행합니다. 결제사 선택이나 '결제를 키 없이 시작할지' 결정은 "
    "만들지 마세요 — ReCoder 가 결제 시작 방식을 따로 묻습니다. 다른 기술 결정만 제시하세요."
)


def code_contract(choice: str) -> str:
    """코드 생성 프롬프트에 붙이는 결제 약속. choice: "mock" | "keys"."""
    first = ("사용자는 '키 없이 모의 결제로 바로 실행' 을 골랐습니다. 첫 로컬 배포는 Stripe 키 없이 모의 결제 서버로 뜹니다."
             if choice == "mock" else
             "사용자는 'Stripe 테스트 키를 넣고 실행' 을 골랐습니다. 그래도 아래 모의 결제 모드를 함께 지원해야 나중에 키 없이도 실행할 수 있습니다.")
    return f"""

[결제 약속 — 반드시 지킬 것] {first}
- 서버가 Node/Express 이면 ReCoder 가 결제 모듈(payments/stripe)을 고정 파일로 넣습니다. 정확한 경로·함수는 설계 약속의 [결제 모듈] 을
  따르고, 그 모듈이 있으면 아래의 Stripe 클라이언트·웹훅 검증을 직접 만들지 말고 그 모듈을 불러 쓰세요. 모의 결제 서버도 만들지 마세요.
- 결제는 서버(Node/Express)에서 npm 패키지 `stripe` 의 PaymentIntents API 로만 만듭니다: stripe.paymentIntents.create({{ amount, currency, metadata: {{ orderId }} }}, {{ idempotencyKey }}).
- 서버 결제 모듈 한 곳에서 환경변수를 **process.env.이름 형태 그대로** 읽습니다(구조 분해 금지): process.env.STRIPE_SECRET_KEY, process.env.STRIPE_WEBHOOK_SECRET, process.env.NODE_ENV, process.env.PAYMENT_MODE, process.env.STRIPE_MOCK_HOST, process.env.STRIPE_MOCK_PORT.
- process.env.NODE_ENV === 'test' 이고 process.env.PAYMENT_MODE === 'mock' 일 때만 Stripe 클라이언트를 new Stripe(key, {{ host: STRIPE_MOCK_HOST, port: parseInt(STRIPE_MOCK_PORT, 10), protocol: 'http' }}) 로 만듭니다. PAYMENT_MODE=mock 인데 NODE_ENV 가 test 가 아니면 시작을 거부하고, NODE_ENV=production 에서 STRIPE_MOCK_HOST 가 있으면 시작을 거부합니다.
- 결제 완료는 POST {WEBHOOK_PATH} 웹훅에서만 처리합니다. 이 라우트는 express.raw({{ type: 'application/json' }}) 로 JSON 파서보다 먼저 등록하고, stripe.webhooks.constructEvent(rawBody, req.headers['stripe-signature'], STRIPE_WEBHOOK_SECRET) 로 서명을 검증합니다. payment_intent.succeeded 이벤트의 metadata.orderId 로 주문을 결제 완료로 바꾸고, 이벤트 id 로 중복 처리를 막습니다.
- 브라우저 쪽 Stripe 공개 키는 모의 결제 모드에서 없어도 앱이 시작되고 결제 화면이 동작해야 합니다(공개 키가 없을 때 시작을 거부하지 마세요). 모의 결제 모드에서는 카드 입력 대신 "테스트 결제 진행 중" 을 보여 주고 주문 상태가 결제 완료가 될 때까지 확인합니다. 서버의 상태 확인 응답에 test_payment_mode(true/false)를 담아 화면이 알 수 있게 합니다.
- 로컬 데모는 NODE_ENV=test 로 실행됩니다. NODE_ENV 가 test 여도 서버는 반드시 포트를 열어야 합니다(테스트용이라며 app.listen 을 건너뛰지 마세요).
- process.env.SEED_DEMO === 'true' 이면 서버가 시작할 때 상품이 하나도 없을 경우에만 데모 상품 몇 개를 넣습니다(여러 번 시작해도 중복되지 않게).
- README·.env.example 에는 실제 키 값을 쓰지 말고 STRIPE_SECRET_KEY=sk_test_your_key 같은 자리표시만 씁니다.
"""


def _server_sources(ops: list[dict]) -> dict[str, str]:
    out: dict[str, str] = {}
    for op in ops or []:
        path = re.sub(r"^(\./)+", "", str(op.get("file") or "").replace("\\", "/")).lstrip("/")
        if not path.lower().endswith(_JS_EXT) or op.get("action") == "delete":
            continue
        low = path.lower()
        if any(low.startswith(d) or f"/{d}" in low for d in _SKIP_DIRS) or "/src/components/" in low or "/pages/" in low:
            continue
        out[path] = str(op.get("content") or "")
    return out


def issues(ops: list[dict]) -> list[dict]:
    """만든 결과가 결제 약속을 지켰는가 — 안 지켰으면 일관성 점검이 고칠 오류 목록."""
    import payment_kit
    if any(payment_kit.is_kit(op.get("file", "")) for op in ops or []):
        #: ReCoder 결제 모듈을 넣었다 — 모의 결제·서명 검증은 그 모듈이 지킨다. 서버가 그것을 쓰는지만 본다.
        return payment_kit.wiring_issues(ops) + _no_listen(_server_sources(ops))
    sources = _server_sources(ops)
    if not sources:
        return []
    text = "\n".join(sources.values())
    found = set(re.findall(r"process\.env\.([A-Z_][A-Z0-9_]*)", text))
    problems: list[dict] = []
    missing = [m for m in MARKERS if m not in found]
    payment_file = next((p for p, c in sources.items() if re.search(r"""from\s+['"]stripe['"]|require\(\s*['"]stripe['"]\s*\)""", c)), "")
    #: 고칠 파일을 꼭 지목한다 — 지목한 파일이 없으면 부분 교정이 건너뛰어진다.
    entry = next((p for p, c in sources.items() if re.search(r"\bexpress\(\s*\)|\.listen\(", c)), "") or next(iter(sources))
    if missing:
        problems.append({
            "code": "PAYMENT_MOCK_CONTRACT_MISSING", "severity": "error", "file": payment_file or entry,
            "message": "모의 결제 모드가 빠졌습니다 — 서버가 process.env." + ", process.env.".join(missing) + " 를 읽지 않습니다",
            "fix": "서버 결제 모듈에서 결제 약속대로 NODE_ENV=test·PAYMENT_MODE=mock 일 때 Stripe 클라이언트를 "
                   "STRIPE_MOCK_HOST·STRIPE_MOCK_PORT(http)로 보내세요. 실제 키 경로는 그대로 둡니다.",
        })
    if not re.search(r"webhooks/stripe|['\"]/stripe['\"]", text) or "constructEvent" not in text:
        problems.append({
            "code": "PAYMENT_WEBHOOK_MISSING", "severity": "error", "file": entry,
            "message": f"결제 완료 웹훅 POST {WEBHOOK_PATH} (서명 검증 constructEvent) 이 없습니다",
            "fix": f"POST {WEBHOOK_PATH} 를 express.raw 로 JSON 파서보다 먼저 등록하고 stripe.webhooks.constructEvent 로 "
                   "검증한 뒤 payment_intent.succeeded 로 주문을 결제 완료로 바꾸세요.",
        })
    return problems + _no_listen(sources)


def _no_listen(sources: dict[str, str]) -> list[dict]:
    for path, content in sources.items():
        if re.search(r"NODE_ENV\s*!==?\s*['\"]test['\"][^;{}]{0,40}\)?\s*\{?[^{}]{0,200}?\.listen\(", content):
            return [{
                "code": "PAYMENT_DEMO_NO_LISTEN", "severity": "error", "file": path,
                "message": "NODE_ENV=test 이면 서버가 포트를 열지 않습니다 — 로컬 데모(모의 결제)는 NODE_ENV=test 로 실행됩니다",
                "fix": "NODE_ENV 와 관계없이 서버가 listen 하게 하세요. 테스트가 필요하면 app 을 내보내는 모듈과 listen 하는 진입 파일을 나누세요.",
            }]
    return []
