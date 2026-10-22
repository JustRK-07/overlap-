#!/bin/bash
# run.sh — start the overlay web app on http://localhost:8000
set -e
cd "$(dirname "$0")"
exec python3 -m uvicorn app:app --host 0.0.0.0 --port 8000 --reload
