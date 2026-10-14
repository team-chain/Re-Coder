"""2.0.22 — 함수·스키마로 읽는 필수 환경변수 감지, 실행 점검의 설정 누락 처리.

실기기 2.0.21 생성 앱(d22de705): `getEnv('JWT_SECRET')` 로만 읽어 ReCoder 가 JWT_SECRET 을 넣지 않았고,
앱이 시작 즉시 종료 → AI 교정이 코드 문제로 알고 6회 헛돌았다.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import deploy_settings as ds  # noqa: E402
import runtime_check as rc  # noqa: E402

ENV_TS = """
function getEnv(key: string, defaultValue?: string): string {
  const value = process.env[key];
  if (value !== undefined) { return value; }
  if (defaultValue !== undefined) { return defaultValue; }
  throw new Error(`Environment variable ${key} is required.`);
}
function getEnvOptional(key: string, defaultValue?: string): string {
  const value = process.env[key];
  return value ?? defaultValue ?? '';
}
export function validateEnv() {
  const databaseUrl = getEnv('DATABASE_URL');
  const jwtSecret = getEnv('JWT_SECRET');
  if (jwtSecret.length < 32) { throw new Error('JWT_SECRET must be at least 32 characters long.'); }
  const expires = getEnv('JWT_EXPIRES_IN', '7d');
  const bucket = getEnvOptional('S3_BUCKET_NAME');
  return { databaseUrl, jwtSecret, expires, bucket };
}
"""


def _ws(tmp_path, files):
    for rel, text in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return str(tmp_path)


def test_accessor_function_required_and_length(tmp_path):
    ws = _ws(tmp_path, {"backend/src/env.ts": ENV_TS})
    req = ds.required_env(ws)
    assert req.get("JWT_SECRET") == 32 and "DATABASE_URL" in req
    # 기본값을 준 호출·throw 없는 함수로 읽는 값은 필수가 아니다
    assert "JWT_EXPIRES_IN" not in req and "S3_BUCKET_NAME" not in req
    names = ds.all_env_names(ws)
    assert {"JWT_SECRET", "JWT_EXPIRES_IN", "S3_BUCKET_NAME", "DATABASE_URL"} <= names


def test_accessor_used_from_another_file_and_arrow(tmp_path):
    ws = _ws(tmp_path, {
        "src/config.js": "export const requireEnv = (name) => {\n  const v = process.env[name];\n  if (!v) throw new Error(name + ' missing');\n  return v;\n};\n",
        "src/server.js": "import { requireEnv } from './config.js';\nconst secret = requireEnv('SESSION_SECRET');\n",
    })
    assert "SESSION_SECRET" in ds.required_env(ws)


def test_zod_and_envalid_schemas(tmp_path):
    ws = _ws(tmp_path, {
        "src/env.ts": "import { z } from 'zod';\nconst schema = z.object({\n  JWT_SECRET: z.string().min(48),\n  REDIS_URL: z.string().optional(),\n  PORT: z.coerce.number().default(3000),\n  API_TOKEN_SECRET: z.string(),\n});\nexport const env = schema.parse(process.env);\n",
        "src/env2.js": "const { cleanEnv, str, port } = require('envalid');\nmodule.exports = cleanEnv(process.env, { COOKIE_SECRET: str(), LOG_LEVEL: str({ default: 'info' }) });\n",
    })
    req = ds.required_env(ws)
    assert req.get("JWT_SECRET") == 48 and "API_TOKEN_SECRET" in req and "COOKIE_SECRET" in req
    assert "REDIS_URL" not in req and "LOG_LEVEL" not in req


def test_plain_process_env_detection_unchanged(tmp_path):
    ws = _ws(tmp_path, {"server.js": "if (!process.env.JWT_SECRET) { throw new Error('JWT_SECRET required'); }\nconst x = process.env.OPTIONAL_THING;\n"})
    assert ds.required_env(ws) == {"JWT_SECRET": 0}


def test_missing_env_from_startup_log():
    log = ("Error: Environment variable JWT_SECRET is required.\n"
           "Missing required environment variables: DATABASE_URL, COOKIE_SECRET\n"
           "S3_BUCKET_NAME is required\nSTRIPE_SECRET_KEY 가 없습니다\n"
           "TypeError: x is not a function\nERROR something failed\n")
    assert rc.missing_env(log) == ["JWT_SECRET", "DATABASE_URL", "COOKIE_SECRET", "S3_BUCKET_NAME", "STRIPE_SECRET_KEY"]
    assert rc.missing_env("TypeError: Cannot read properties of undefined") == []


def test_config_missing_is_not_a_code_repair():
    result = {"status": "failed", "config_missing": ["S3_BUCKET_NAME"],
              "failures": [{"request": "앱 시작(/health)", "status": None, "lines": ["S3_BUCKET_NAME is required"],
                            "files": [], "config_missing": ["S3_BUCKET_NAME"]}]}
    issues = rc.issues(result)
    assert [i["code"] for i in issues] == ["GENERATED_RUNTIME_CONFIG"] and issues[0]["severity"] == "warning"
