#!/bin/bash
# run.sh — start the overlay web app on http://localhost:8000
#
#   ./run.sh                  → http://localhost:8000
#   PORT=8765 ./run.sh        → http://localhost:8765
#   HOST=127.0.0.1 ./run.sh   → loopback only (safer on a shared network)
#
# The README documents the PORT override — it used to be ignored, and the
# port was hard-coded to 8000.
set -e
cd "$(dirname "$0")"
exec python3 -m uvicorn app:app \
    --host "${HOST:-0.0.0.0}" \
    --port "${PORT:-8000}" \
    --reload
