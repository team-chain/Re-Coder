#!/usr/bin/env python3
"""짝퉁 패키지 판정에 쓰는 '유명 npm 패키지' 목록을 갱신한다.

npm 검색 API(registry.npmjs.org/-/v1/search)는 결과마다 주간 다운로드 수를 준다.
흔한 주제어로 여러 번 검색해 모은 뒤, 주간 다운로드가 기준 이상인 것만 남긴다.
버전을 올릴 때 한 번 돌려 core/data/popular_npm.json 을 커밋한다.

  python scripts/update_popular_npm.py            # 기본: 주간 5만 이상, 최대 4000개
"""
from __future__ import annotations

import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "core" / "data" / "popular_npm.json"
TOPICS = """react vue angular svelte next nuxt express koa fastify nest http server client api rest graphql
database sql postgres mysql mongodb redis orm prisma sequelize typeorm knex auth jwt oauth passport session cookie
crypto hash bcrypt uuid id date time moment dayjs format validation schema zod yup joi ajv json yaml xml csv parse
string text color chalk log logger debug cli command args yargs commander prompt inquirer file fs path glob watch
stream buffer zip compress image sharp canvas pdf excel email mail smtp sms queue job cron schedule worker cache
test jest mocha vitest chai sinon mock assert coverage lint eslint prettier stylelint format typescript types babel
webpack rollup vite esbuild bundler loader plugin postcss css sass less tailwind styled emotion ui component icon
chart d3 animation router state redux mobx zustand query fetch axios request websocket socket ws proxy cors helmet
rate limit security env dotenv config aws azure google firebase stripe payment upload multer storage s3 docker
kubernetes cloud serverless lambda util lodash underscore ramda async promise event emitter rxjs observable
markdown html template ejs handlebars pug i18n locale intl math number big decimal random faker seed graph tree
react-native electron mobile desktop terminal ansi progress spinner table semver version git github npm package
monorepo workspace build compile minify polyfill core-js regenerator source-map test-utils testing-library
storybook docs swagger openapi grpc protobuf kafka rabbitmq amqp mqtt sse http2 compression multipart form""".split()


def search(text: str, size: int = 250, frm: int = 0) -> list[dict]:
    url = "https://registry.npmjs.org/-/v1/search?" + urllib.parse.urlencode({"text": text, "size": size, "from": frm})
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.loads(r.read().decode("utf-8")).get("objects", [])


#: 검색 주제어로 안 잡힐 수 있는, 생성 앱이 자주 쓰는 이름 — 이름 그대로 한 번씩 더 찾는다.
SEEDS = """nodemon sqlite3 better-sqlite3 express-validator express-session connect-redis bcryptjs bcrypt jsonwebtoken
cookie-parser morgan compression helmet cors body-parser multer sharp nodemailer socket.io socket.io-client
mysql2 pg pg-pool mongoose mongodb redis ioredis prisma @prisma/client sequelize typeorm knex drizzle-orm
react react-dom react-router-dom react-router @tanstack/react-query swr zustand redux @reduxjs/toolkit
vite @vitejs/plugin-react tailwindcss postcss autoprefixer typescript ts-node tsx @types/node @types/express
@types/react @types/react-dom @types/cors @types/bcryptjs @types/jsonwebtoken @types/multer @types/pg
jest vitest supertest @testing-library/react eslint prettier concurrently cross-env dotenv uuid nanoid zod yup joi
axios node-fetch stripe @stripe/stripe-js @stripe/react-stripe-js @tosspayments/payment-sdk
dayjs date-fns moment lodash classnames clsx lucide-react react-icons react-hot-toast framer-motion
recharts chart.js react-chartjs-2 express-rate-limit rate-limiter-flexible passport passport-local
passport-jwt winston pino pino-http serve-static http-proxy-middleware next nuxt vue vue-router pinia
@nestjs/core @nestjs/common fastify koa hono graphql apollo-server @apollo/client""".split()


def main(min_weekly: int = 50_000, limit: int = 20000) -> int:
    found: dict[str, int] = {}
    for name in SEEDS:
        try:
            for o in search(name, 5):
                if (o.get("package") or {}).get("name") == name:
                    found[name] = int((o.get("downloads") or {}).get("weekly") or 0)
        except Exception as exc:  # noqa: BLE001
            print(f"skip seed {name}: {exc}", file=sys.stderr)
    for i, topic in enumerate(TOPICS):
        for frm in (0, 250):
            try:
                objs = search(topic, 250, frm)
            except Exception as exc:  # noqa: BLE001
                print(f"skip {topic}@{frm}: {exc}", file=sys.stderr)
                continue
            for o in objs:
                name = (o.get("package") or {}).get("name")
                weekly = (o.get("downloads") or {}).get("weekly") or 0
                if name and weekly >= min_weekly:
                    found[name] = max(found.get(name, 0), int(weekly))
            time.sleep(0.2)
        print(f"[{i + 1}/{len(TOPICS)}] {topic}: {len(found)}", file=sys.stderr)
    top = dict(sorted(found.items(), key=lambda kv: -kv[1])[:limit])
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"source": "registry.npmjs.org/-/v1/search", "updated": time.strftime("%Y-%m-%d"),
                               "min_weekly": min_weekly, "weekly": top}, ensure_ascii=False, indent=0, sort_keys=False) + "\n",
                   encoding="utf-8")
    print(f"{len(top)} packages → {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
