#!/usr/bin/env bash
set -euo pipefail

SOURCE="${BASH_SOURCE[0]}"
while [ -L "$SOURCE" ]; do
    DIR="$(cd -P "$(dirname "$SOURCE")" >/dev/null 2>&1 && pwd)"
    SOURCE="$(readlink "$SOURCE")"
    [[ "$SOURCE" != /* ]] && SOURCE="$DIR/$SOURCE"
done
SCRIPT_DIR="$(cd -P "$(dirname "$SOURCE")" >/dev/null 2>&1 && pwd)"
TARGET="$SCRIPT_DIR/moodle_sync.py"

PYTHON_BIN=""
for candidate in \
    "$SCRIPT_DIR/.venv/Scripts/python.exe" \
    "$SCRIPT_DIR/.venv/bin/python" \
    "$HOME/.venv/Scripts/python.exe" \
    "$HOME/.venv/bin/python"
do
    if [ -x "$candidate" ]; then
        PYTHON_BIN="$candidate"
        break
    fi
done

if [ -z "$PYTHON_BIN" ]; then
    if command -v python3 >/dev/null 2>&1; then
        PYTHON_BIN="$(command -v python3)"
    elif command -v python >/dev/null 2>&1; then
        PYTHON_BIN="$(command -v python)"
    else
        echo "[FEHLER] Keine Python-Umgebung gefunden (.venv, python3 oder python im PATH)." >&2
        exit 1
    fi
fi

exec "$PYTHON_BIN" "$TARGET" "$@"
