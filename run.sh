#!/bin/sh
set -eu
cd "$(dirname "$0")"
if ! command -v ffmpeg >/dev/null 2>&1 || ! command -v ffprobe >/dev/null 2>&1; then
  echo "FFmpeg and FFprobe are required. Install them with: brew install ffmpeg" >&2
  exit 1
fi
if [ ! -d .venv ]; then python3 -m venv .venv; fi
.venv/bin/python -m pip install -r requirements.txt
exec .venv/bin/uvicorn app:app --host 127.0.0.1 --port 8765
