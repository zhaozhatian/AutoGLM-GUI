#!/usr/bin/env bash
# AutoGLM-GUI local service launcher for Git Bash.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ADB_DIR="$HOME/.cache/autoglm/platform-tools"

if [ ! -f "$ADB_DIR/adb.exe" ]; then
  echo "[AutoGLM] ADB not found, downloading Android platform-tools..."
  "$ROOT/.venv/Scripts/python.exe" -c "from AutoGLM_GUI.adb_manager import ensure_adb; print(ensure_adb())"
fi

export PATH="$ADB_DIR:$PATH"
export AUTOGLM_ADB_PATH="$ADB_DIR/adb.exe"
export AUTOGLM_GRID_OVERLAY="${AUTOGLM_GRID_OVERLAY:-1}"
export AUTOGLM_PORT="${AUTOGLM_PORT:-8765}"

cd "$ROOT"
echo "[AutoGLM] starting on http://127.0.0.1:${AUTOGLM_PORT}"
exec "$ROOT/.venv/Scripts/autoglm-gui.exe" --no-browser --port "$AUTOGLM_PORT" "$@"