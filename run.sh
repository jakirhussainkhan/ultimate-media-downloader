#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

PYTHON_BIN="${PYTHON_BIN:-python3}"

if [ ! -x ".venv/bin/python" ]; then
    echo "[1/4] Creating isolated virtual environment..."
    "$PYTHON_BIN" -m venv .venv
fi

# shellcheck disable=SC1091
source .venv/bin/activate

echo "[2/4] Installing dependencies..."
python -m pip install --disable-pip-version-check -q -r requirements.txt

if command -v ffmpeg >/dev/null 2>&1; then
    echo "[3/4] ffmpeg detected. MP3 extraction is available."
else
    echo "[3/4] WARNING: ffmpeg not found. MP3 extraction will be disabled."
    echo "      Install it with: sudo apt install ffmpeg (or your package manager)."
fi

export HOST="${HOST:-127.0.0.1}"
export PORT="${PORT:-5000}"

echo "[4/4] Starting Ultimate Media Downloader v1.0.0..."
echo "      Open http://${HOST}:${PORT} in your browser."
echo "      For LAN access: HOST=0.0.0.0 ./run.sh"
echo "========================================================"

exec python app.py
