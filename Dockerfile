FROM python:3.11-slim

# 작업 디렉토리 설정
WORKDIR /app

# 보안을 위해 권한이 제한된 사용자 생성
RUN adduser --disabled-password --gecos "" appuser

# 포트 노출
EXPOSE 8000

# 소스 코드 복사
COPY . .

# 의존성 파일이 없으므로 필요 시 pip install 단계 생략
# 만약 설치할 패키지가 있다면 requirements.txt 생성 후 주석 해제
# RUN pip install --no-cache-dir -r requirements.txt

# 사용자 권한 설정
USER appuser

# 애플리케이션 실행
CMD ["python", "main.py"]