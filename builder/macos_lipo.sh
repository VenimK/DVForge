#!/bin/bash
set -e
real_lipo="${ECZ_REAL_LIPO:?ECZ_REAL_LIPO is required}"

# Flutter 3.24 passes multiple architectures to verify_arch; newer lipo rejects that.
if [ "$#" -ge 3 ] && [ "$2" = "-verify_arch" ]; then
    binary="$1"
    shift 2
    for architecture in "$@"; do
        "$real_lipo" "$binary" -verify_arch "$architecture"
    done
    exit 0
fi

exec "$real_lipo" "$@"
