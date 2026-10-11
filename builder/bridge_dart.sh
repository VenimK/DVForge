#!/bin/sh
if [ "$1" = run ] && [ "$2" = ffigen ]; then
    exec "$ECZ_REAL_DART" "--packages=$ECZ_DART_PACKAGES" "$ECZ_FFIGEN_WRAPPER" "$ECZ_REAL_DART" "$@"
fi
exec "$ECZ_REAL_DART" "$@"
