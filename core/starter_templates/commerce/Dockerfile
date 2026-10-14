# Stage 1: Build frontend
FROM node:22-alpine AS frontend-builder

WORKDIR /app/frontend

COPY frontend/package*.json ./
RUN npm ci

COPY frontend/ ./
RUN npm run build

# Stage 2: Build backend runtime
FROM node:22-alpine AS backend-builder

WORKDIR /app/backend

COPY backend/package*.json ./
RUN npm ci --omit=dev

# Stage 3: Runtime
FROM node:22-alpine

WORKDIR /app

# Remove npm, corepack, yarn from runtime image to reduce tool transitivity vulnerabilities
RUN apk upgrade --no-cache zlib \
    && rm -rf /usr/local/lib/node_modules /opt/yarn* /usr/local/bin/npm /usr/local/bin/npx /usr/local/bin/corepack /usr/local/bin/yarn /usr/local/bin/yarnpkg

# Create non-root user
# The official Node image already provides UID/GID 1000: node.

# Copy backend dependencies from builder
COPY --from=backend-builder --chown=node:node /app/backend/node_modules ./backend/node_modules

# Copy backend source
COPY --chown=node:node backend/src ./backend/src
COPY --chown=node:node backend/init-db.js ./backend/init-db.js
COPY --chown=node:node backend/package.json ./backend/package.json
COPY --chown=node:node certs ./certs

# Copy frontend dist
COPY --from=frontend-builder --chown=node:node /app/frontend/dist ./frontend/dist

USER 1000:1000

EXPOSE 3001

# Health check: verify database connectivity with 5s timeout
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["node", "-e", "const http = require('http'); http.get('http://127.0.0.1:3001/health', (r) => { if (r.statusCode === 200) process.exit(0); else process.exit(1); }).on('error', () => process.exit(1)).setTimeout(5000, () => process.exit(1));"]

CMD ["node", "backend/src/server.js"]
