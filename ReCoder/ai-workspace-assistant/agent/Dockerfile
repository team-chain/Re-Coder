FROM python:3.11-slim

# 작업 디렉토리 설정
WORKDIR /app

# 비권한 사용자 생성 및 권한 설정
RUN adduser --disabled-password --gecos "" appuser

# 의존성 설치
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 소스 코드 복사
COPY . .
RUN chown -R appuser:appuser /app

# 비권한 사용자로 실행
USER appuser

# 포트 노출
EXPOSE 8000

# 애플리케이션 실행
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]