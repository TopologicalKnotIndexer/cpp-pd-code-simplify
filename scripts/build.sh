#!/usr/bin/env sh
set -eu

BUILD_DIR="${BUILD_DIR:-build}"
CONFIG="${CONFIG:-release}"
PYTHON="${PYTHON:-}"

if [ -z "$PYTHON" ]; then
    if command -v python3 >/dev/null 2>&1; then
        PYTHON=python3
    else
        PYTHON=python
    fi
fi

"$PYTHON" tools/package.py test --build-dir "$BUILD_DIR" --config "$(printf '%s' "$CONFIG" | tr '[:upper:]' '[:lower:]')"
