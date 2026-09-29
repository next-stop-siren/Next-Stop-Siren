#!/bin/sh
set -eu
cd "$(dirname "$0")"
if [ "${1:-}" = setup ]; then ./bootstrap.sh; fi
case $(uname -m) in arm64) arch=arm64 ;; x86_64) arch=x64 ;; *) arch=unsupported ;; esac
tools_dir="$(pwd -P)/.cache/host-tools"
if [ -z "${B71_NODE:-}" ] && [ -x "$tools_dir/node-24.21.0-darwin-$arch/bin/node" ]; then
  B71_NODE="$tools_dir/node-24.21.0-darwin-$arch/bin/node"; export B71_NODE
fi
if [ -z "${B71_NPM:-}" ] && [ -n "${B71_NODE:-}" ] && [ -f "$(dirname "$B71_NODE")/npm" ]; then
  B71_NPM="$(dirname "$B71_NODE")/npm"; export B71_NPM
fi
if [ -z "${B71_UV:-}" ] && [ -x "$tools_dir/uv-0.11.19-darwin-$arch/uv" ]; then
  B71_UV="$tools_dir/uv-0.11.19-darwin-$arch/uv"; export B71_UV
fi
if [ -z "${B71_PYTHON:-}" ] && [ -x "$tools_dir/python-3.13.15-darwin-$arch/python/bin/python3.13" ]; then
  B71_PYTHON="$tools_dir/python-3.13.15-darwin-$arch/python/bin/python3.13"; export B71_PYTHON
fi
if [ -n "${B71_NODE:-}" ]; then PATH="$(dirname "$B71_NODE"):$PATH"; export PATH; fi
if [ -z "${UV_CACHE_DIR:-}" ]; then UV_CACHE_DIR="$tools_dir/uv-cache"; export UV_CACHE_DIR; fi
if [ -n "${B71_PYTHON:-}" ]; then
  exec "$B71_PYTHON" scripts/local.py "$@"
fi
exec python3.13 scripts/local.py "$@"
