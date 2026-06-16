#!/usr/bin/env bash
# ============================================================
#  CCHQ Pipeline - one-time setup (macOS / Linux)
#  Builds a local Python environment and installs deps.
# ============================================================
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

echo "=== CCHQ Pipeline setup ==="

# Prefer a known-good Python (3.10-3.13; avoid brand-new 3.14 wheel gaps).
PY=""
for c in python3.12 python3.13 python3.11 python3.10; do
  if command -v "$c" >/dev/null 2>&1; then PY="$c"; break; fi
done
if [ -z "$PY" ]; then
  echo "Need Python 3.10-3.13. Install from https://www.python.org/downloads/  (or: brew install python@3.12)"
  exit 1
fi
echo "Using $PY ($("$PY" --version))"

[ -d .venv ] || "$PY" -m venv .venv
.venv/bin/python -m pip install --upgrade pip >/dev/null
.venv/bin/python -m pip install -r app/requirements.txt

[ -f app/.env ] || cp app/.env.example app/.env

echo
echo "=== Setup complete ==="
echo "1. Edit  app/.env  and add your CH_API_KEY."
echo "2. Run  ./run.sh  to start the app, then open http://localhost:5050"
