#!/bin/sh
set -eu
cd "$(dirname "$0")"
if [ -n "${B71_PYTHON:-}" ]; then
  exec "$B71_PYTHON" scripts/local.py "$@"
fi
exec python3.13 scripts/local.py "$@"
