FROM node:22-bookworm-slim@sha256:43ac6c60b8f89723f746e8a92ce91abd5017e627ce1ddfe4238355d3a30b772c
WORKDIR /app
COPY package*.json ./
COPY registry.cjs ./
RUN node registry.cjs & sleep 1; npm install --registry=http://127.0.0.1:4873 --fetch-retries=0 --ignore-scripts --no-audit --no-fund
COPY . .
CMD ["node", "app.cjs"]
