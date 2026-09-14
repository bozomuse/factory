#!/bin/sh
set -eu

factory_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)

run_as_root() {
    if [ "$(id -u)" -eq 0 ]; then
        "$@"
    elif command -v sudo >/dev/null 2>&1; then
        sudo "$@"
    else
        echo "factory setup requires root or sudo to install curl" >&2
        exit 1
    fi
}

install_curl() {
    if command -v apt-get >/dev/null 2>&1; then
        run_as_root apt-get update
        run_as_root apt-get install -y curl ca-certificates
    elif command -v dnf >/dev/null 2>&1; then
        run_as_root dnf install -y curl ca-certificates
    elif command -v yum >/dev/null 2>&1; then
        run_as_root yum install -y curl ca-certificates
    elif command -v apk >/dev/null 2>&1; then
        run_as_root apk add curl ca-certificates
    elif command -v pacman >/dev/null 2>&1; then
        run_as_root pacman -Sy --needed --noconfirm curl ca-certificates
    elif command -v zypper >/dev/null 2>&1; then
        run_as_root zypper install -y curl ca-certificates
    elif command -v brew >/dev/null 2>&1; then
        brew install curl
    else
        echo "factory setup could not find a supported package manager" >&2
        exit 1
    fi
}

if ! command -v uv >/dev/null 2>&1; then
    command -v curl >/dev/null 2>&1 || install_curl
    installer=$(mktemp "${TMPDIR:-/tmp}/factory-uv.XXXXXX")
    trap 'rm -f "$installer"' EXIT HUP INT TERM
    curl -LsSf https://astral.sh/uv/install.sh -o "$installer"
    UV_NO_MODIFY_PATH=1 UV_INSTALL_DIR="$HOME/.local/bin" sh "$installer"
    uv_binary="$HOME/.local/bin/uv"
else
    uv_binary=$(command -v uv)
fi

"$uv_binary" tool install --force "$factory_root"
factory_bin_dir=$("$uv_binary" tool dir --bin)
cd "$factory_root"
"$factory_bin_dir/factory" setup "$@"
