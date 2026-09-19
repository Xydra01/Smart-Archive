#!/usr/bin/env bash
# Start the Next.js frontend from anywhere.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FRONTEND_DIR="$SCRIPT_DIR/frontend"

if [ ! -d "$FRONTEND_DIR/node_modules" ]; then
  echo "Frontend deps not installed. Installing now..."
  (cd "$FRONTEND_DIR" && npm install)
fi

echo "Starting frontend on http://localhost:3000 ..."
cd "$FRONTEND_DIR"
exec npm run dev "$@"
