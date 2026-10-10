"""AI 가 만드는 결제 앱에 ReCoder 가 **고정 파일**로 넣는 Stripe 결제 모듈.

왜 고정 파일인가
    결제 약속(모의 결제 모드·서명 웹훅)을 프롬프트로만 지시하면 AI 가 자기 방식(JWT 비밀로 만든 HMAC 웹훅,
    직접 만든 모의 결제 서버)을 만들었다(실기기 2026-10-10 TEMP 쇼핑몰). 그 앱은 로컬 배포의 모의 결제 서버와
    맞지 않아 결제가 끝나지 않았고, 일관성 점검이 "웹훅 없음" 을 끝까지 고치지 못했다.
    그래서 Stripe 클라이언트·결제 의도 만들기·웹훅 서명 검증은 ReCoder 가 검증한 코드로 넣고, AI 는 이 모듈을
    **불러서 쓰기만** 한다(주문 저장·상태 변경 같은 앱 로직만 AI 가 쓴다).

약속(deploy_settings 의 모의 결제 서버·데모 모드와 같다)
    · process.env.PAYMENT_MODE · STRIPE_MOCK_HOST · STRIPE_MOCK_PORT · STRIPE_SECRET_KEY · STRIPE_WEBHOOK_SECRET · NODE_ENV
    · NODE_ENV=test 이고 PAYMENT_MODE=mock 일 때만 모의 결제 서버(http)로 보낸다. 운영에서 모의 설정이 있으면 시작을 거부한다.
    · 결제 완료는 POST /api/webhooks/stripe 의 서명 검증된 payment_intent.succeeded 로만 처리한다.
"""
from __future__ import annotations

import posixpath
import re

WEBHOOK_PATH = "/api/webhooks/stripe"
_SERVER_DIRS = ("backend", "server", "api")
#: AI 가 따로 만들던 모의 결제 서버 — 로컬 배포가 모의 결제 서버를 함께 띄우므로 만들지 않는다.
_AI_MOCK = re.compile(r"^(?:[\w.-]+/)?(?:mock[-_]?(?:payment|stripe|pay)[\w-]*|(?:payment|stripe)[-_]?mock[\w-]*|fake[-_]?(?:payment|stripe)[\w-]*)/", re.I)
_CODE = (".js", ".ts", ".mjs", ".cjs", ".tsx", ".jsx")

TS_KIT = r'''/**
 * ReCoder 결제 모듈 — Stripe(PaymentIntents) 클라이언트·결제 만들기·웹훅 서명 검증.
 * 이 파일은 ReCoder 가 넣은 검증된 코드입니다. 앱 코드는 여기서 불러 쓰기만 하세요(직접 고치지 않음).
 *
 *  - 환경변수: STRIPE_SECRET_KEY, STRIPE_WEBHOOK_SECRET, NODE_ENV, PAYMENT_MODE, STRIPE_MOCK_HOST, STRIPE_MOCK_PORT
 *  - NODE_ENV=test 이고 PAYMENT_MODE=mock 일 때만 모의 결제 서버(http)로 보냅니다(로컬 데모). 운영에서는 거부합니다.
 *  - 결제 완료는 POST /api/webhooks/stripe 의 서명 검증된 payment_intent.succeeded 로만 처리합니다.
 */
import Stripe from 'stripe';
import express, { Router, Request, Response } from 'express';

export const STRIPE_WEBHOOK_PATH = '/api/webhooks/stripe';

/** 모의 결제(로컬 데모) 모드인가 — 화면이 "테스트 결제" 를 보여 줄지 정할 때 씁니다. */
export function isTestPaymentMode(): boolean {
  return process.env.NODE_ENV === 'test' && process.env.PAYMENT_MODE === 'mock';
}

/** 서버 시작 때 한 번 부르세요 — 잘못된 결제 설정이면 시작을 거부합니다. */
export function assertPaymentConfig(): void {
  if (process.env.PAYMENT_MODE === 'mock' && process.env.NODE_ENV !== 'test') {
    throw new Error('PAYMENT_MODE=mock 은 NODE_ENV=test 에서만 쓸 수 있습니다');
  }
  if (process.env.NODE_ENV === 'production' && process.env.STRIPE_MOCK_HOST) {
    throw new Error('운영(NODE_ENV=production)에서는 STRIPE_MOCK_HOST 를 설정할 수 없습니다');
  }
}

let client: Stripe | null = null;

/** Stripe 클라이언트(한 번만 만듭니다). */
export function getStripe(): Stripe {
  if (client) return client;
  assertPaymentConfig();
  const key = process.env.STRIPE_SECRET_KEY;
  if (!key) {
    throw new Error('STRIPE_SECRET_KEY 가 없습니다');
  }
  if (isTestPaymentMode()) {
    client = new Stripe(key, {
      host: process.env.STRIPE_MOCK_HOST || 'localhost',
      port: parseInt(process.env.STRIPE_MOCK_PORT || '12111', 10),
      protocol: 'http',
    });
  } else {
    client = new Stripe(key);
  }
  return client;
}

export interface CreatedPayment {
  id: string;
  clientSecret: string | null;
  status: string;
  testMode: boolean;
}

/**
 * 결제 의도를 만듭니다. amount 는 통화의 가장 작은 단위 정수(원화는 원, 달러는 센트)입니다.
 * 같은 주문을 다시 결제하면 idempotencyKey(예: `order-<id>`)로 같은 결제를 돌려받습니다.
 */
export async function createPaymentIntent(params: {
  amount: number;
  currency: string;
  orderId: string | number;
  idempotencyKey?: string;
}): Promise<CreatedPayment> {
  const amount = Math.round(Number(params.amount));
  if (!Number.isFinite(amount) || amount <= 0) {
    throw new Error('결제 금액이 올바르지 않습니다');
  }
  const intent = await getStripe().paymentIntents.create(
    {
      amount,
      currency: String(params.currency || 'krw').toLowerCase(),
      metadata: { orderId: String(params.orderId) },
      automatic_payment_methods: { enabled: true },
    },
    { idempotencyKey: params.idempotencyKey || `order-${params.orderId}` },
  );
  return { id: intent.id, clientSecret: intent.client_secret, status: intent.status, testMode: isTestPaymentMode() };
}

export interface PaymentEvent {
  eventId: string;
  orderId: string;
  paymentIntentId: string;
  amount: number;
  currency: string;
}

export interface PaymentEventHandlers {
  /** 결제 완료 — 주문을 결제 완료로 바꾸세요. 같은 eventId 로 두 번 와도 한 번만 반영되게(멱등) 만드세요. */
  onPaymentSucceeded(event: PaymentEvent): Promise<void>;
  /** 결제 실패(선택) */
  onPaymentFailed?(event: PaymentEvent): Promise<void>;
}

/**
 * 결제 웹훅 라우터. **express.json() 보다 먼저** 이렇게 등록하세요:
 *   app.use(STRIPE_WEBHOOK_PATH, createStripeWebhookRouter({ onPaymentSucceeded, onPaymentFailed }));
 */
export function createStripeWebhookRouter(handlers: PaymentEventHandlers): Router {
  const router = express.Router();
  router.post('/', express.raw({ type: 'application/json' }), async (req: Request, res: Response) => {
    const secret = process.env.STRIPE_WEBHOOK_SECRET;
    const signature = req.headers['stripe-signature'];
    if (!secret || typeof signature !== 'string') {
      res.status(400).json({ error: 'missing webhook signature' });
      return;
    }
    let event: Stripe.Event;
    try {
      event = getStripe().webhooks.constructEvent(req.body as Buffer, signature, secret);
    } catch {
      res.status(400).json({ error: 'invalid webhook signature' });
      return;
    }
    const intent = event.data.object as Stripe.PaymentIntent;
    const orderId = intent && intent.metadata ? intent.metadata.orderId : undefined;
    if (!orderId) {
      res.json({ received: true, ignored: 'no orderId' });
      return;
    }
    const payload: PaymentEvent = {
      eventId: event.id,
      orderId: String(orderId),
      paymentIntentId: intent.id,
      amount: intent.amount,
      currency: intent.currency,
    };
    try {
      if (event.type === 'payment_intent.succeeded') {
        await handlers.onPaymentSucceeded(payload);
      } else if (event.type === 'payment_intent.payment_failed' && handlers.onPaymentFailed) {
        await handlers.onPaymentFailed(payload);
      }
    } catch (err) {
      console.error('[payments] webhook handler failed', err);
      res.status(500).json({ error: 'webhook handling failed' });
      return;
    }
    res.json({ received: true });
  });
  return router;
}
'''

CJS_KIT = r'''/**
 * ReCoder 결제 모듈 — Stripe(PaymentIntents) 클라이언트·결제 만들기·웹훅 서명 검증.
 * 이 파일은 ReCoder 가 넣은 검증된 코드입니다. 앱 코드는 여기서 불러 쓰기만 하세요(직접 고치지 않음).
 * CommonJS(.cjs) 라서 require 로도, ESM 의 import 로도 불러올 수 있습니다.
 *
 *  - 환경변수: STRIPE_SECRET_KEY, STRIPE_WEBHOOK_SECRET, NODE_ENV, PAYMENT_MODE, STRIPE_MOCK_HOST, STRIPE_MOCK_PORT
 *  - NODE_ENV=test 이고 PAYMENT_MODE=mock 일 때만 모의 결제 서버(http)로 보냅니다(로컬 데모). 운영에서는 거부합니다.
 *  - 결제 완료는 POST /api/webhooks/stripe 의 서명 검증된 payment_intent.succeeded 로만 처리합니다.
 */
'use strict';
const Stripe = require('stripe');
const express = require('express');

const STRIPE_WEBHOOK_PATH = '/api/webhooks/stripe';

function isTestPaymentMode() {
  return process.env.NODE_ENV === 'test' && process.env.PAYMENT_MODE === 'mock';
}

function assertPaymentConfig() {
  if (process.env.PAYMENT_MODE === 'mock' && process.env.NODE_ENV !== 'test') {
    throw new Error('PAYMENT_MODE=mock 은 NODE_ENV=test 에서만 쓸 수 있습니다');
  }
  if (process.env.NODE_ENV === 'production' && process.env.STRIPE_MOCK_HOST) {
    throw new Error('운영(NODE_ENV=production)에서는 STRIPE_MOCK_HOST 를 설정할 수 없습니다');
  }
}

let client = null;

function getStripe() {
  if (client) return client;
  assertPaymentConfig();
  const key = process.env.STRIPE_SECRET_KEY;
  if (!key) {
    throw new Error('STRIPE_SECRET_KEY 가 없습니다');
  }
  if (isTestPaymentMode()) {
    client = new Stripe(key, {
      host: process.env.STRIPE_MOCK_HOST || 'localhost',
      port: parseInt(process.env.STRIPE_MOCK_PORT || '12111', 10),
      protocol: 'http',
    });
  } else {
    client = new Stripe(key);
  }
  return client;
}

/** amount 는 통화의 가장 작은 단위 정수(원화는 원, 달러는 센트). 같은 주문은 idempotencyKey 로 같은 결제를 돌려받습니다. */
async function createPaymentIntent({ amount, currency, orderId, idempotencyKey }) {
  const value = Math.round(Number(amount));
  if (!Number.isFinite(value) || value <= 0) {
    throw new Error('결제 금액이 올바르지 않습니다');
  }
  const intent = await getStripe().paymentIntents.create(
    {
      amount: value,
      currency: String(currency || 'krw').toLowerCase(),
      metadata: { orderId: String(orderId) },
      automatic_payment_methods: { enabled: true },
    },
    { idempotencyKey: idempotencyKey || `order-${orderId}` },
  );
  return { id: intent.id, clientSecret: intent.client_secret, status: intent.status, testMode: isTestPaymentMode() };
}

/**
 * 결제 웹훅 라우터. **express.json() 보다 먼저** 등록하세요:
 *   app.use(STRIPE_WEBHOOK_PATH, createStripeWebhookRouter({ onPaymentSucceeded, onPaymentFailed }));
 * onPaymentSucceeded({ eventId, orderId, paymentIntentId, amount, currency }) — 같은 eventId 가 두 번 와도 한 번만 반영(멱등).
 */
function createStripeWebhookRouter(handlers) {
  const router = express.Router();
  router.post('/', express.raw({ type: 'application/json' }), async (req, res) => {
    const secret = process.env.STRIPE_WEBHOOK_SECRET;
    const signature = req.headers['stripe-signature'];
    if (!secret || typeof signature !== 'string') {
      res.status(400).json({ error: 'missing webhook signature' });
      return;
    }
    let event;
    try {
      event = getStripe().webhooks.constructEvent(req.body, signature, secret);
    } catch (err) {
      res.status(400).json({ error: 'invalid webhook signature' });
      return;
    }
    const intent = event.data.object || {};
    const orderId = intent.metadata ? intent.metadata.orderId : undefined;
    if (!orderId) {
      res.json({ received: true, ignored: 'no orderId' });
      return;
    }
    const payload = { eventId: event.id, orderId: String(orderId), paymentIntentId: intent.id,
                      amount: intent.amount, currency: intent.currency };
    try {
      if (event.type === 'payment_intent.succeeded') {
        await handlers.onPaymentSucceeded(payload);
      } else if (event.type === 'payment_intent.payment_failed' && handlers.onPaymentFailed) {
        await handlers.onPaymentFailed(payload);
      }
    } catch (err) {
      console.error('[payments] webhook handler failed', err);
      res.status(500).json({ error: 'webhook handling failed' });
      return;
    }
    res.json({ received: true });
  });
  return router;
}

module.exports = { STRIPE_WEBHOOK_PATH, isTestPaymentMode, assertPaymentConfig, getStripe, createPaymentIntent,
                   createStripeWebhookRouter };
'''


def _norm(path: str) -> str:
    return re.sub(r"^(\./)+", "", str(path or "").replace("\\", "/")).lstrip("/")


def is_ai_mock(path: str) -> bool:
    """AI 가 따로 만드는 모의 결제 서버 파일인가(로컬 배포가 모의 결제 서버를 함께 띄우므로 만들지 않는다)."""
    return bool(_AI_MOCK.match(_norm(path) + ("/" if "/" not in _norm(path) else "")))


def server_folder(paths: list[str]) -> str | None:
    """서버 코드 폴더("" = 루트). 서버 코드가 없으면 None."""
    norm = [_norm(p) for p in paths]
    for folder in _SERVER_DIRS:
        if any(p.startswith(folder + "/") and p.endswith(_CODE) for p in norm):
            return folder
    if any(re.match(r"^(?:src/)?(?:server|app|index)\.(?:js|ts|mjs|cjs)$", p) for p in norm):
        return ""
    return None


def kit_path(paths: list[str]) -> str | None:
    """결제 모듈을 넣을 경로. 서버가 TypeScript 면 .ts, 아니면 .cjs(require·import 둘 다 됨)."""
    folder = server_folder(paths)
    if folder is None:
        return None
    prefix = f"{folder}/" if folder else ""
    norm = [_norm(p) for p in paths]
    mine = [p for p in norm if p.startswith(prefix) and p.endswith(_CODE) and not is_ai_mock(p)
            and not re.match(r"^(?:frontend|client|web)/", p)]
    ts = any(p.endswith(".ts") for p in mine)
    src = any(p.startswith(prefix + "src/") for p in mine)
    base = f"{prefix}{'src/' if src else ''}payments/stripe"
    return base + (".ts" if ts else ".cjs")


def kit_content(path: str) -> str:
    return TS_KIT if path.endswith(".ts") else CJS_KIT


def import_hint(path: str) -> str:
    """서버 파일에서 이 모듈을 불러오는 방법(약속 문서에 넣는다)."""
    if path.endswith(".ts"):
        return ("TypeScript: `import { createPaymentIntent, createStripeWebhookRouter, STRIPE_WEBHOOK_PATH, assertPaymentConfig, "
                "isTestPaymentMode } from '<상대 경로>/payments/stripe.js'` (ESM 이면 .js 확장자, CommonJS 로 컴파일하면 확장자 없이)")
    return ("CommonJS: `const { createPaymentIntent, createStripeWebhookRouter, STRIPE_WEBHOOK_PATH, assertPaymentConfig, "
            "isTestPaymentMode } = require('<상대 경로>/payments/stripe.cjs')` · ESM: `import payments from '<상대 경로>/payments/stripe.cjs'` "
            "뒤 `const { … } = payments`")


def contract(path: str) -> str:
    return f"""

[결제 모듈 — ReCoder 가 넣는 고정 파일, 반드시 이것만 사용]
- {path} 는 ReCoder 가 이미 작성했습니다(파일 목록에 있음, 다시 쓰거나 고치지 마세요). Stripe SDK·웹훅 서명 검증·모의 결제 연결을 다른 곳에서 직접 만들지 마세요.
- 불러오기: {import_hint(path)}.
- 서버 시작 때 assertPaymentConfig() 를 부르세요.
- 주문 결제: 주문을 '결제 대기'(pending) 로 저장한 뒤 `await createPaymentIntent({{ amount, currency, orderId, idempotencyKey: 'order-' + orderId }})`
  를 부르고, 결과의 clientSecret·testMode 와 주문 id 를 응답으로 돌려주세요. amount 는 통화 최소 단위 정수(원화는 원).
- 결제 완료: 서버 진입 파일에서 **express.json() 보다 먼저** `app.use(STRIPE_WEBHOOK_PATH, createStripeWebhookRouter({{ onPaymentSucceeded, onPaymentFailed }}))`.
  onPaymentSucceeded({{ eventId, orderId, paymentIntentId, amount, currency }}) 에서 같은 eventId 는 한 번만 반영(DB 에 처리한 이벤트 id 저장)하고,
  금액이 주문 합계와 같을 때 주문을 'paid' 로 바꾸세요. 결제 완료는 이 웹훅으로만 바꿉니다(화면·다른 API 가 직접 paid 로 바꾸지 않음).
- 상태 확인 응답(/health 또는 /api/health)에 test_payment_mode: isTestPaymentMode() 를 넣으세요. 화면은 test_payment_mode 이면 카드 입력 대신
  '테스트 결제 진행 중' 을 보여 주고 주문 상태가 paid 가 될 때까지 몇 초 간격으로 확인합니다(무한 반복 금지 — 최대 30번).
- 모의 결제 서버·결제 페이지(mock-payment-server 등)를 만들지 마세요 — 로컬 배포가 모의 결제 서버를 함께 띄웁니다.
- 서버 package.json 의 dependencies 에 "stripe": "^14.25.0" 과 "express" 를 넣으세요."""


def plan_with_kit(files: list[dict]) -> tuple[list[dict], list[dict], str]:
    """설계 목록에 결제 모듈을 넣는다 → (새 목록, 미리 만든 ops, 약속 문서에 붙일 글). 서버가 없으면 그대로."""
    kept = [f for f in files if not is_ai_mock(f.get("file", ""))]
    path = kit_path([f["file"] for f in kept])
    if not path:
        return files, [], ""
    kept = [f for f in kept if _norm(f["file"]) != path]
    entry = {"file": path, "layer": 0, "size": "medium", "fixed": True,
             "purpose": "ReCoder 결제 모듈(고정) — Stripe 클라이언트·createPaymentIntent·createStripeWebhookRouter"}
    op = {"action": "create", "file": path, "content": kit_content(path), "language": "typescript" if path.endswith(".ts") else "javascript",
          "rationale": "ReCoder 가 넣은 검증된 결제 모듈(모의 결제·서명 웹훅)", "fixed": True}
    #: 기반(layer 0) 맨 앞 — 다른 파일이 이 모듈을 보며 쓴다.
    return [entry] + kept, [op], contract(path)


def is_kit(path: str) -> bool:
    return bool(re.search(r"(?:^|/)payments/stripe\.(?:ts|cjs)$", _norm(path)))


def wiring_issues(ops: list[dict]) -> list[dict]:
    """결제 모듈을 넣었는데 서버가 쓰지 않는다 — 결제가 실제로 일어나지 않는다."""
    kit = next((op for op in ops if is_kit(op.get("file", "")) and op.get("action") != "delete"), None)
    if kit is None:
        return []
    server = server_folder([op.get("file", "") for op in ops]) or ""
    prefix = f"{server}/" if server else ""
    sources = {_norm(op["file"]): str(op.get("content") or "") for op in ops
               if _norm(op.get("file", "")).startswith(prefix) and _norm(op.get("file", "")).endswith(_CODE)
               and not is_kit(op.get("file", "")) and op.get("action") != "delete"}
    users = {p: t for p, t in sources.items() if re.search(r"payments/stripe(?:\.(?:js|ts|cjs))?['\"]", t)}
    entry = next((p for p, t in sources.items() if re.search(r"\bexpress\(\s*\)", t)), "") \
        or min((p for p in sources if re.match(r"(?:server|app|index|main)\.", posixpath.basename(p))),
               key=lambda p: (p.count("/"), ["server", "app", "index", "main"].index(posixpath.basename(p).split(".")[0])), default="") \
        or next(iter(sources), "")
    issues: list[dict] = []
    text = "\n".join(users.values())
    if "createStripeWebhookRouter" not in text:
        issues.append({"code": "PAYMENT_WEBHOOK_MISSING", "severity": "error", "file": entry,
                       "message": f"결제 완료 웹훅이 등록되지 않았습니다 — {kit['file']} 의 createStripeWebhookRouter 를 쓰지 않습니다",
                       "fix": f"{entry} 에서 express.json() 보다 먼저 app.use(STRIPE_WEBHOOK_PATH, createStripeWebhookRouter({{ onPaymentSucceeded }})) "
                              "로 등록하고, onPaymentSucceeded 에서 주문을 paid 로 바꾸세요(같은 eventId 는 한 번만)."})
    if "createPaymentIntent" not in text:
        target = next((p for p in sources if re.search(r"order|checkout|payment", p, re.I)), entry)
        issues.append({"code": "PAYMENT_INTENT_MISSING", "severity": "error", "file": target,
                       "message": f"주문 결제가 {kit['file']} 의 createPaymentIntent 를 부르지 않습니다 — 결제가 시작되지 않습니다",
                       "fix": "주문을 pending 으로 저장한 뒤 createPaymentIntent({ amount, currency, orderId, idempotencyKey }) 를 부르고 "
                              "clientSecret·testMode 를 응답에 넣으세요."})
    for path, body in sources.items():
        if path in users:
            continue
        if re.search(r"""from\s+['"]stripe['"]|require\(\s*['"]stripe['"]\s*\)|constructEvent\(|createHmac\([^)]*\)[^;]*webhook""", body, re.I):
            issues.append({"code": "PAYMENT_DUPLICATE_STRIPE", "severity": "error", "file": path,
                           "message": f"{path} 가 결제 모듈을 쓰지 않고 Stripe·웹훅 검증을 따로 만들었습니다 — 모의 결제·서명 검증이 어긋납니다",
                           "fix": f"Stripe 호출·웹훅 검증을 지우고 {kit['file']} 의 createPaymentIntent·createStripeWebhookRouter 를 쓰세요."})
    return issues
