#!/usr/bin/env bash
# Start the F&O Live Screener (no dependencies, plain Python 3.9+)
cd "$(dirname "$0")"
PORT="${PORT:-8000}"
echo "F&O Live Screener starting on http://localhost:$PORT"
# Optional: UPSTOX_ACCESS_TOKEN=xxxx ./run.sh   <- skips pasting the token in the UI
exec python3 -u server.py
