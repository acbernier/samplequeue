#!/usr/bin/env bash
# Start SampleQueue. Creates the venv on first run.
set -euo pipefail
cd "$(dirname "$0")"

PORT="${PORT:-8787}"
HOST="${HOST:-127.0.0.1}"   # set HOST=0.0.0.0 to let the rest of the lab reach it

if [ ! -d .venv ]; then
  echo "Creating virtualenv..."
  python3 -m venv .venv
  .venv/bin/pip install --quiet --upgrade pip
  .venv/bin/pip install --quiet -r requirements.txt
fi

# Auto-reload while developing on loopback; never when serving the lab.
RELOAD=""
if [ "$HOST" = "127.0.0.1" ] || [ "$HOST" = "localhost" ]; then
  RELOAD="--reload"
fi

echo "SampleQueue on http://${HOST}:${PORT}"
exec .venv/bin/uvicorn app.main:app --host "$HOST" --port "$PORT" $RELOAD
