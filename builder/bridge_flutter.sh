#!/bin/sh
if [ "$1" = pub ] && [ "$2" = run ] && [ "$3" = ffigen ]; then
    exec "$ECZ_REAL_DART" "--packages=$ECZ_DART_PACKAGES" "$ECZ_FFIGEN_WRAPPER" "$ECZ_REAL_FLUTTER" "$@"
fi
exec "$ECZ_REAL_FLUTTER" "$@"
