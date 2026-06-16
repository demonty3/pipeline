#!/usr/bin/env bash
# ============================================================
#  CCHQ Pipeline - start the web app (macOS / Linux)
#  Then open http://localhost:5050
# ============================================================
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
if [ ! -x "$ROOT/.venv/bin/python" ]; then
  echo "Local environment not found. Run ./setup.sh first."
  exit 1
fi
cd "$ROOT/app"   # run from app/ so load_dotenv() finds app/.env
echo "Starting the app... open http://localhost:5050  (Ctrl+C to stop)"
exec "$ROOT/.venv/bin/python" app.py
