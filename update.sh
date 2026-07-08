#!/usr/bin/env bash
# 아이온2 제작 DB 갱신 래퍼 (cron용)
#   update.sh prices   # 시세만 갱신 (자주)
#   update.sh full     # 레시피+이미지+시세 전체 재구축 (가끔)
# 중복 실행 방지(flock) + 로그 적재.
set -euo pipefail

DIR="/home/ysnam/projects/aion2"
MODE="${1:-prices}"
PY="$(command -v python3)"
LOG="$DIR/logs/update.log"
LOCK="$DIR/.update.lock"

mkdir -p "$DIR/logs"
cd "$DIR"

case "$MODE" in
  prices) ARGS="--prices-only" ;;
  full)   ARGS="" ;;
  *) echo "usage: update.sh [prices|full]" >&2; exit 2 ;;
esac

{
  echo "=========================================="
  echo "[$(date '+%F %T')] START mode=$MODE"
  # 이미 도는 인스턴스가 있으면 스킵(-n: non-blocking)
  flock -n 9 || { echo "[$(date '+%F %T')] SKIP: another run holds the lock"; exit 0; }
  if "$PY" crawl_craft.py $ARGS; then
    echo "[$(date '+%F %T')] OK mode=$MODE"
  else
    echo "[$(date '+%F %T')] FAIL mode=$MODE (exit $?)"
  fi
} 9>"$LOCK" >>"$LOG" 2>&1

# 로그가 너무 커지면 최근 2000줄만 유지
if [ -f "$LOG" ] && [ "$(wc -l <"$LOG")" -gt 2000 ]; then
  tail -n 2000 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi
