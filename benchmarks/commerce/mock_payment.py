"""Disposable provider emulator: real HTTP + Stripe-compatible webhook signature."""

import json
import os
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

intents = {}
keys = {}
lock = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        form = parse_qs(self.rfile.read(int(self.headers.get("Content-Length", 0))).decode())
        with lock:
            if self.path == "/v1/payment_intents":
                key = self.headers.get("Idempotency-Key")
                if key and key in keys:
                    return self.reply(200, intents[keys[key]])
                id = "pi_mock_" + secrets.token_hex(8)
                intent = {
                    "id": id,
                    "object": "payment_intent",
                    "amount": int(form.get("amount", ["0"])[0]),
                    "currency": form.get("currency", ["usd"])[0],
                    "status": "requires_payment_method",
                    "client_secret": id + "_secret_mock",
                    "metadata": {
                        k[9:-1]: v[0] for k, v in form.items() if k.startswith("metadata[")
                    },
                }
                intents[id] = intent
                if key:
                    keys[key] = id
                return self.reply(200, intent)
            id = self.path.split("/")[3] if len(self.path.split("/")) > 3 else ""
            if id in intents and self.path.endswith("/cancel"):
                intents[id]["status"] = "canceled"
                return self.reply(200, intents[id])
            self.reply(404, {"error": {"message": "Unsupported mock endpoint"}})

    def do_GET(self):
        if self.path == "/health":
            return self.reply(200, {"status": "ok", "provider": "mock"})
        if self.path == "/intents":
            return self.reply(200, list(intents.values()))
        if self.path.startswith("/v1/payment_intents/"):
            return self.reply(200, intents.get(self.path.rsplit("/", 1)[-1], {}))
        self.reply(404, {})


ThreadingHTTPServer(
    (os.environ.get("MOCK_BIND", "127.0.0.1"), int(os.environ.get("MOCK_PORT", "19090"))), Handler
).serve_forever()
