#!/usr/bin/env bash
# 아이온2 뷰어 서버 재시작
#   restart.sh            # 기본 포트(8770)로 재시작
#   restart.sh 9000       # 지정 포트로 재시작
# 기존 server.py 인스턴스를 종료한 뒤 백그라운드로 새로 띄운다(로그 적재).
set -euo pipefail

DIR="/home/ysnam/projects/aion2"
PORT="${1:-8770}"
PY="$(command -v python3)"
LOG="$DIR/logs/server.log"
PIDFILE="$DIR/.server.pid"

mkdir -p "$DIR/logs"
cd "$DIR"

# 1) 기존 인스턴스 종료 (PID 파일 + 안전망으로 명령줄 매칭)
if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  kill "$(cat "$PIDFILE")" 2>/dev/null || true
fi
pkill -f "python3 .*server\.py" 2>/dev/null || true

# 2) 새로 기동 (백그라운드, 로그 append)
echo "[$(date '+%F %T')] restart port=$PORT" >>"$LOG"
nohup "$PY" server.py --port "$PORT" >>"$LOG" 2>&1 &
echo $! > "$PIDFILE"

sleep 1
if kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  echo "아이온2 뷰어 재시작됨: http://127.0.0.1:$PORT  (PID $(cat "$PIDFILE"), 로그 $LOG)"
else
  echo "기동 실패 — 로그 확인: $LOG" >&2
  tail -n 20 "$LOG" >&2 || true
  exit 1
fi
