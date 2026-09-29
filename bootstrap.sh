#!/bin/sh
# Install only pinned, project-local language tools. Git and Docker stay manual prerequisites.
set -eu
cd "$(dirname "$0")"
root=$(pwd -P)
store="$root/.cache/host-tools"
manifest="$root/scripts/host-tools.manifest"

fail() { printf 'Host tools: %s\n' "$*" >&2; exit 2; }
command -v git >/dev/null 2>&1 || fail 'Install Git before using this repository.'
command -v curl >/dev/null 2>&1 || fail 'curl is required on macOS.'
command -v shasum >/dev/null 2>&1 || fail 'shasum is required on macOS.'
[ "$(uname -s)" = Darwin ] || fail 'This entry point supports macOS. Use bootstrap.ps1 on Windows.'
case $(uname -m) in
  arm64) platform=darwin-arm64 ;;
  x86_64) platform=darwin-x64 ;;
  *) fail 'Unsupported Mac architecture.' ;;
esac
mkdir -p "$store"

entry() { awk -F '|' -v p="$platform" -v t="$1" '$1==p && $2==t {print $3 "|" $4 "|" $5}' "$manifest"; }
expected_path() {
  case "$1" in
    node) printf '%s/bin/node' "$2" ;;
    npm) printf '%s/bin/npm' "$2" ;;
    python) printf '%s/python/bin/python3.13' "$2" ;;
    uv) printf '%s/uv' "$2" ;;
  esac
}
verify() {
  tool=$1; dir=$2; digest=$3
  [ -f "$dir/.archive-sha256" ] && [ "$(cat "$dir/.archive-sha256")" = "$digest" ] || return 1
  case "$tool" in
    node)
      [ -x "$dir/bin/node" ] && [ -f "$dir/bin/npm" ] || return 1
      [ "$("$dir/bin/node" --version 2>/dev/null)" = v24.21.0 ] || return 1
      [ "$(PATH="$dir/bin:$PATH" "$dir/bin/npm" --version 2>/dev/null)" = 11.19.0 ] || return 1 ;;
    python) [ "$("$dir/python/bin/python3.13" --version 2>&1)" = 'Python 3.13.15' ] || return 1 ;;
    uv) [ "$("$dir/uv" --version 2>/dev/null | cut -d ' ' -f 1,2)" = 'uv 0.11.19' ] || return 1 ;;
  esac
  [ -f "$dir/.binary-sha256" ] || return 1
  binary=$(expected_path "$tool" "$dir")
  [ "$(shasum -a 256 "$binary" | cut -d ' ' -f 1)" = "$(cat "$dir/.binary-sha256")" ]
}
install_one() {
  name=$1
  case "$name" in
    node) override=${B71_NODE:-${B71_NPM:-}} ;;
    python) override=${B71_PYTHON:-} ;;
    uv) override=${B71_UV:-} ;;
  esac
  if [ -n "$override" ]; then printf 'Host tools: %s override retained.\n' "$name"; return; fi
  record=$(entry "$name")
  [ -n "$record" ] || fail "No pinned $name artifact for $platform."
  version=${record%%|*}; rest=${record#*|}; digest=${rest%%|*}; url=${rest#*|}
  target="$store/$name-$version-$platform"
  if verify "$name" "$target" "$digest"; then printf 'Host tools: %s %s reused.\n' "$name" "$version"; return; fi
  [ ! -e "$target" ] || fail "$name install exists but failed verification: $target. Move it aside manually before retrying."
  stage=$(mktemp -d "$store/.stage.XXXXXXXX") || fail 'Cannot create staging directory.'
  # The trap always removes only our private staging path.
  trap 'rm -rf "$stage"' EXIT HUP INT TERM
  archive="$stage/download"
  curl --fail --location --retry 2 --connect-timeout 10 --max-time 180 --silent --show-error "$url" -o "$archive" || fail "$name download failed. Retry when network access is available."
  [ "$(shasum -a 256 "$archive" | cut -d ' ' -f 1)" = "$digest" ] || fail "$name archive checksum mismatch."
  mkdir "$stage/out"
  tar -xzf "$archive" -C "$stage/out" || fail "$name archive extraction failed."
  case "$name" in
    node) source_dir="$stage/out/node-v24.21.0-$platform"
          [ -n "$source_dir" ] || fail 'Node archive layout changed.'
          [ -d "$source_dir" ] || fail 'Node archive layout changed.'
          mv "$source_dir" "$stage/ready" ;;
    python) [ -d "$stage/out/python" ] || fail 'Python archive layout changed.'; mv "$stage/out" "$stage/ready" ;;
    uv) case "$platform" in darwin-arm64) uv_arch=aarch64 ;; darwin-x64) uv_arch=x86_64 ;; esac
        source_dir="$stage/out/uv-$uv_arch-apple-darwin"
        [ -d "$source_dir" ] || fail 'uv archive layout changed.'; mv "$source_dir" "$stage/ready" ;;
  esac
  printf '%s\n' "$digest" > "$stage/ready/.archive-sha256"
  shasum -a 256 "$(expected_path "$name" "$stage/ready")" | cut -d ' ' -f 1 > "$stage/ready/.binary-sha256"
  verify "$name" "$stage/ready" "$digest" || fail "$name version verification failed."
  mv "$stage/ready" "$target" || fail "$name publication failed."
  rm -rf "$stage"
  trap - EXIT HUP INT TERM
  printf 'Host tools: %s %s installed.\n' "$name" "$version"
}
install_one node
install_one python
install_one uv
printf 'Host tools ready. Docker Compose and a running daemon are required for setup.\n'
