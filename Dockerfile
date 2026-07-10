FROM python:3.12-slim

# 앱은 순수 파이썬 표준 라이브러리 → 설치할 의존성 없음.
# 헬스체크에 쓸 curl 만 넣는다.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 앱 전체 복사 — index.html·server.py·이미지·크래프트 DB 가 전부 리포에 포함돼 있다.
# (불필요 항목은 .dockerignore 로 제외)
COPY . /app

# 비루트 사용자로 실행. server.py 가 기동 시 뷰 재생성/price_overrides 테이블 생성으로
# DB 에 써야 하므로 /app 를 쓰기 가능하게 소유권을 넘긴다.
RUN useradd -u 1000 -m -d /home/app app \
 && chown -R app:app /app
USER app

ENV PYTHONUNBUFFERED=1
EXPOSE 8770

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD curl -fsS http://localhost:8770/ >/dev/null 2>&1 || exit 1

CMD ["python", "server.py", "--host", "0.0.0.0", "--port", "8770"]
