FROM python:3.11-slim
WORKDIR /app

# Docker CLI 설치 (호스트 Docker 소켓 사용)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl gnupg && \
    install -m 0755 -d /etc/apt/keyrings && \
    curl -fsSL https://download.docker.com/linux/debian/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg && \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/debian bookworm stable" > /etc/apt/sources.list.d/docker.list && \
    apt-get update && apt-get install -y --no-install-recommends docker-ce-cli && \
    rm -rf /var/lib/apt/lists/*

RUN adduser --disabled-password --gecos "" appuser
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

# Docker 소켓 접근을 위해 appuser를 docker 그룹에 추가
RUN groupadd -f docker && usermod -aG docker appuser

USER appuser
EXPOSE 8000
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
