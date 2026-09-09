#!/usr/bin/env bash
# DVForge farm — stop whatever is bound to :8765 (DVForge app) and :8766 (queue.py)
#
# --with-app / --with-queue leave those processes running on purpose (so other
# machines keep claiming after you Ctrl+C the worker). Use this when you
# actually want them gone — e.g. before testing a fresh --notification-webhook
# run, or to free the ports for a clean restart.
#
# Usage:
#   ./farm/stop-farm.sh            # stop :8765 and :8766 (graceful, then force after 5s)
#   ./farm/stop-farm.sh 8766       # stop only the queue
#   ./farm/stop-farm.sh --force    # skip the graceful wait, kill -9 immediately
set -e
cd "$(dirname "$0")"

FORCE=false
PORTS=()
for arg in "$@"; do
  case "$arg" in
    --force) FORCE=true ;;
    *) PORTS+=("$arg") ;;
  esac
done
[ ${#PORTS[@]} -eq 0 ] && PORTS=(8765 8766)

if ! command -v lsof >/dev/null 2>&1; then
  echo "lsof not found — install it (e.g. 'sudo apt install lsof') or stop the"
  echo "processes manually: ps aux | grep -E 'app\.py|queue\.py'"
  exit 1
fi

NAME_FOR_PORT() {
  case "$1" in
    8765) echo "DVForge app (app.py)" ;;
    8766) echo "farm queue (queue.py)" ;;
    *) echo "process" ;;
  esac
}

stopped_any=false
for port in "${PORTS[@]}"; do
  pids=$(lsof -ti "tcp:${port}" -sTCP:LISTEN 2>/dev/null || true)
  label="$(NAME_FOR_PORT "$port")"
  if [ -z "$pids" ]; then
    echo "  · :$port — nothing listening ($label)"
    continue
  fi
  for pid in $pids; do
    cmd=$(ps -p "$pid" -o command= 2>/dev/null | cut -c1-80)
    if $FORCE; then
      kill -9 "$pid" 2>/dev/null && echo "  ✓ :$port — killed pid $pid ($label): $cmd"
    else
      kill "$pid" 2>/dev/null || true
      for i in 1 2 3 4 5; do
        kill -0 "$pid" 2>/dev/null || break
        sleep 1
      done
      if kill -0 "$pid" 2>/dev/null; then
        kill -9 "$pid" 2>/dev/null
        echo "  ✓ :$port — pid $pid ($label) did not exit, force-killed: $cmd"
      else
        echo "  ✓ :$port — stopped pid $pid ($label): $cmd"
      fi
    fi
    stopped_any=true
  done
done

# Stale worker locks are harmless (acquire_lock() checks liveness), but clean
# them up anyway so a leftover lock file never causes confusion.
shopt -s nullglob
locks=(.worker-*.lock)
if [ ${#locks[@]} -gt 0 ]; then
  rm -f "${locks[@]}"
  echo "  ✓ removed ${#locks[@]} worker lock file(s)"
fi

if ! $stopped_any; then
  echo "Nothing was running on: ${PORTS[*]}"
fi
