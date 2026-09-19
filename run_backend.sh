#!/usr/bin/env bash
# Start the FastAPI backend from anywhere. Resolves paths relative to this
# script, so it works no matter what directory you run it from.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$SCRIPT_DIR/backend"
VENV_UVICORN="$BACKEND_DIR/.venv/bin/uvicorn"

# --- Sanity checks ---
if [ ! -x "$VENV_UVICORN" ]; then
  echo "Backend venv not found. Create it first:"
  echo "  python3 -m venv backend/.venv"
  echo "  backend/.venv/bin/pip install -r backend/requirements.txt"
  exit 1
fi

if ! curl -s http://localhost:11434/api/tags >/dev/null 2>&1; then
  echo "Warning: Ollama doesn't appear to be running on :11434."
  echo "Start it (open the Ollama app or run 'ollama serve') before asking questions."
fi

echo "Starting backend on http://localhost:8000 ..."
cd "$BACKEND_DIR"
exec "$VENV_UVICORN" app.main:app --port 8000 "$@"
