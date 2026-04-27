# Python 3.11 버전을 기반으로 하는 슬림 이미지를 사용합니다.
FROM python:3.11-slim

# 작업 디렉토리를 /app으로 설정합니다.
WORKDIR /app

# 'appuser'라는 이름의 비활성화된 비밀번호를 가진 사용자를 생성합니다.
RUN adduser --disabled-password --gecos "" appuser

# requirements.txt 파일을 컨테이너로 복사합니다.
COPY requirements.txt .

# requirements.txt에 명시된 패키지를 캐시 없이 설치합니다.
RUN pip install --no-cache-dir -r requirements.txt

# 프로젝트의 모든 파일을 컨테이너의 /app 디렉토리로 복사합니다.
COPY . .

# 생성된 'appuser'로 전환하여 컨테이너를 실행합니다.
USER appuser

# 애플리케이션이 수신 대기할 포트를 명시합니다.
EXPOSE 8000

# uvicorn을 사용하여 main.py 파일에 있는 'app' 객체를 0.0.0.0:8000으로 실행합니다.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]