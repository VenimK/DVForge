#!/usr/bin/env bash
# Run precheck.py on macOS/Linux from the project root.
set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"
python3 builder/precheck.py "$@"
