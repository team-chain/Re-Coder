FROM python:3.11-slim

# 작업 디렉토리 설정
WORKDIR /app

# 보안을 위해 권한이 제한된 사용자 생성
RUN adduser --disabled-password --gecos "" appuser

# 포트 설정
EXPOSE 8000

# 프로젝트 소스 코드 복사
COPY . .

# 의존성 파일이 없는 경우 생략, 필요한 경우 pip install 명령 사용
# RUN pip install --no-cache-dir <package-name>

# 권한 변경 및 사용자 전환
RUN chown -R appuser:appuser /app
USER appuser

# 애플리케이션 실행
CMD ["python", "main.py"]