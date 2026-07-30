FROM python:3.12-slim

# 앱은 순수 파이썬 표준 라이브러리 → 설치할 의존성 없음.
# 헬스체크에 쓸 curl 만 넣는다.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 앱 전체 복사 — index.html·server.py·이미지가 포함되며, 제작 데이터는 Turso에서 읽는다.
# (불필요 항목은 .dockerignore 로 제외)
COPY . /app

# 비루트 사용자로 실행.
RUN useradd -u 1000 -m -d /home/app app \
 && chown -R app:app /app
USER app

ENV PYTHONUNBUFFERED=1
EXPOSE 8770

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD curl -fsS http://localhost:8770/ >/dev/null 2>&1 || exit 1

# 포트는 하드코딩하지 않는다: Render 는 PORT 를 주입하며 server.py 가 이를 읽는다
# (없으면 8770). host 만 0.0.0.0 으로 고정해 컨테이너 외부에서 접속되게 한다.
CMD ["python", "server.py", "--host", "0.0.0.0"]
