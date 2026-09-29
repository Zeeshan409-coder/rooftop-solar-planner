#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
[ -d .venv ] || python3 -m venv .venv
. .venv/bin/activate
echo "Checking packages (the first time takes a few minutes)..."
python -m pip install -q -r requirements.txt
echo "open http://localhost:8000  (Ctrl+C to stop)"
python -m uvicorn server.app:app --port 8000
