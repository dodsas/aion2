#!/usr/bin/env bash
# 호스트(dodsas)에서 실행되는 배포 스크립트 — Jenkins 가 scp 로 소스를 푼 뒤 SSH 로 호출한다.
#   podman-compose 로 이미지 빌드 → 재기동 → 헬스체크 → 오래된 이미지 정리.
# 앱은 stdlib 전용이라 .env·시크릿·데이터 볼륨이 없다(크래프트 DB 는 이미지에 포함).
set -euo pipefail

APP_NAME="${APP_NAME:-aion2}"
APP_DIR="${APP_DIR:-/home/dodsas/work/${APP_NAME}}"
HOST_PORT="${HOST_PORT:-9092}"
COMPOSE_FILE="${COMPOSE_FILE:-compose.yml}"
IMAGE_TAG="${IMAGE_TAG:-latest}"
IMAGE_RETAIN="${IMAGE_RETAIN:-3}"
IMAGE_NAME="localhost/${APP_NAME}"

log() { printf '[deploy] %s\n' "$*"; }

[ -d "$APP_DIR" ] || { log "APP_DIR 없음: $APP_DIR"; exit 1; }
cd "$APP_DIR"

command -v podman-compose >/dev/null 2>&1 || {
  log "podman-compose 없음. 설치: pip install --user podman-compose (또는 dnf install podman-compose)"
  exit 1
}

export HOST_PORT IMAGE_TAG
log "이미지: ${IMAGE_NAME}:${IMAGE_TAG}  (호스트 포트 ${HOST_PORT})"

log "빌드"
podman-compose -f "$COMPOSE_FILE" build
# compose 기본(latest) 태그도 함께 부여
if [ "$IMAGE_TAG" != "latest" ]; then
  podman tag "${IMAGE_NAME}:${IMAGE_TAG}" "${IMAGE_NAME}:latest"
fi

log "기존 컨테이너 중지/제거"
podman-compose -f "$COMPOSE_FILE" down --remove-orphans || true

log "기동"
podman-compose -f "$COMPOSE_FILE" up -d

log "헬스체크 (최대 30초 대기)"
ok=0
for _ in $(seq 1 30); do
  if curl -fsS -o /dev/null "http://127.0.0.1:${HOST_PORT}/"; then ok=1; break; fi
  sleep 1
done
if [ "$ok" -ne 1 ]; then
  log "✗ 헬스체크 실패. 최근 로그:"
  podman-compose -f "$COMPOSE_FILE" logs --tail 50 || true
  exit 1
fi
log "✓ 헬스체크 통과 — http://$(hostname):${HOST_PORT}"

# aion2 이미지만 정리 — 호스트의 다른 서비스(yclaude 등)엔 손대지 않음
log "이미지 정리 (최근 ${IMAGE_RETAIN}개 유지)"
podman images --filter "dangling=true" --filter "reference=${IMAGE_NAME}" --format "{{.ID}}" \
  | xargs -r podman rmi 2>/dev/null || true
podman images "${IMAGE_NAME}" --format "{{.Tag}}\t{{.ID}}\t{{.CreatedAt}}" \
  | grep -v -E "^latest\b" | sort -k3 -r \
  | awk -v keep="$IMAGE_RETAIN" 'NR > keep {print $2}' \
  | xargs -r podman rmi 2>/dev/null || true

log "완료."
