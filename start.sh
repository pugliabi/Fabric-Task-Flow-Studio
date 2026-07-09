#!/usr/bin/env bash
# ============================================================
#  Fabric Task Flows Studio — clone-and-run launcher (macOS/Linux)
#  Creates a virtual environment + installs deps on first run,
#  then starts the app and opens http://127.0.0.1:8000
# ============================================================
set -e
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"

if [ ! -x ".venv/bin/python" ]; then
  echo "Creating virtual environment (.venv) ..."
  "$PY" -m venv .venv
  echo "Installing dependencies ..."
  ./.venv/bin/python -m pip install -q --upgrade pip
  ./.venv/bin/python -m pip install -q -r app/requirements-app.txt
fi

echo
echo "  Starting Fabric Task Flows Studio ..."
echo "  A browser tab will open at http://127.0.0.1:8000"
echo "  Press Ctrl+C to stop the server."
echo
exec ./.venv/bin/python run-app.py "$@"
