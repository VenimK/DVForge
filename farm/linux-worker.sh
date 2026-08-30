#!/bin/bash
# Run on the Linux box. DVForge (./run.sh or python3 app.py --no-browser) must
# already be listening on 127.0.0.1:8765.
#
# The INBOX must be the same NAS folder the Mac/Windows workers use.
# Edit DVFORGE_FARM if this machine mounts the share somewhere else.

set -e
export DVFORGE_URL="${DVFORGE_URL:-http://127.0.0.1:8765}"
export DVFORGE_FARM="${DVFORGE_FARM:-/mnt/downloads/MusicLover/RustDesk/Buildwithconfig/test/dvforge/farm}"

DIR="$(cd "$(dirname "$0")" && pwd)"
echo "DVForge API: $DVFORGE_URL"
echo "Shared farm: $DVFORGE_FARM"
echo "This worker claims linux-* and android-* jobs."
echo
if [ ! -d "$DVFORGE_FARM/inbox" ]; then
  echo "inbox not found at $DVFORGE_FARM/inbox"
  echo "Mount the NAS share, then:"
  echo "  export DVFORGE_FARM=/path/to/dvforge/farm"
  echo "  $0"
  exit 1
fi
exec python3 "$DIR/worker.py" "$@"
