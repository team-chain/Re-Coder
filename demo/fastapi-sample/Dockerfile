# ReCoder 배포 검증용 샘플 앱
#
# 플랫폼은 배포 코드가 `--platform linux/amd64` 로 고정해 빌드한다.
# Fargate 기본 아키텍처가 X86_64 여서, Apple Silicon 에서 그냥 빌드하면
# 태스크가 exec format error 로 죽는데 그 로그로는 원인을 알기 어렵다.
FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

# 루트로 돌리지 않는다.
RUN useradd --create-home --shell /bin/false appuser && chown -R appuser /app
USER appuser

EXPOSE 8000

# curl 을 쓰지 않는다 — 이 이미지에 없다. python 은 반드시 있다.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)" || exit 1

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
