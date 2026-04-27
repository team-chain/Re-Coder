# Python 3.11 슬림 이미지 사용
FROM python:3.11-slim

# 작업 디렉토리 설정
WORKDIR /app

# 보안을 위해 비활성화된 비밀번호를 가진 'appuser' 생성
RUN adduser --disabled-password --gecos "" appuser

# requirements.txt 복사
COPY requirements.txt .

# pip로 의존성 설치 (캐시 비활성화)
RUN pip install --no-cache-dir -r requirements.txt

# 프로젝트 파일 복사
COPY . .

# 생성된 사용자로 전환
USER appuser

# 애플리케이션이 사용할 포트 노출
EXPOSE 8000

# 애플리케이션 실행 명령
# main.py 파일의 'app' 객체를 uvicorn으로 실행
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]