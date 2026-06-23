# 클라우드타입 배포용 Dockerfile
# 빌드팩 자동인식 대신 이 Dockerfile로 배포하면 파이썬 버전·패키지가 고정되어
# kiwipiepy 설치 변수(휠 호환 등)가 사라진다.

FROM python:3.12-slim

# kiwipiepy 는 OpenMP(libgomp)를 런타임에 사용한다. slim 이미지엔 없으므로 추가.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 의존성 먼저 복사·설치(레이어 캐시 활용)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 앱 코드 복사
COPY main.py blog_seo_service.py ./

# 클라우드타입은 PORT 환경변수를 주입할 수 있다. 없으면 8000.
ENV PORT=8000
EXPOSE 8000

# sh -c 로 감싸야 ${PORT} 가 런타임에 치환된다.
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT}"]
