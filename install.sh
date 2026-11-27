#!/bin/sh
# Installs Threadline and registers it with the coding agents on this computer.
#   curl -LsSf <url of this file> | sh
# Settings (optional environment variables):
#   THREADLINE_SOURCE      where to install from (a package name, git URL or folder)
#   THREADLINE_LITE=1      skip search by meaning (smaller install, words-only search)
#   THREADLINE_NO_SETUP=1  install only; do not register with agents
set -eu

SOURCE="${THREADLINE_SOURCE:-git+https://github.com/temic137/threadline}"
if [ "${THREADLINE_LITE:-0}" = "1" ]; then
    PACKAGE="threadline @ $SOURCE"
else
    PACKAGE="threadline[meaning] @ $SOURCE"
fi
case "$SOURCE" in
    git+*|http*|/*|.*) ;;
    *) PACKAGE="$SOURCE"; [ "${THREADLINE_LITE:-0}" = "1" ] || PACKAGE="$SOURCE[meaning]" ;;
esac

if ! command -v uv >/dev/null 2>&1; then
    echo "Installing uv (a Python package installer from astral.sh)..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
fi

echo "Installing Threadline..."
uv tool install --force --python 3.13 "$PACKAGE"
uv tool update-shell >/dev/null 2>&1 || true
BIN="$(uv tool dir --bin)/threadline"

if [ "${THREADLINE_NO_SETUP:-0}" != "1" ]; then
    echo
    "$BIN" setup
fi
echo
echo "Done. Open a project in any coding agent and ask: where did we stop?"
echo "Open a new terminal to use the 'threadline' command."
