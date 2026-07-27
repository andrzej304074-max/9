#!/usr/bin/env bash
# Uruchamia backtester lokalnie: instaluje zależności i startuje serwer.
set -euo pipefail

cd "$(dirname "$0")"

PORT="${PORT:-8000}"
HOST="${HOST:-127.0.0.1}"

if [ ! -f data/GBPUSD_15m_sample.csv ]; then
  echo "==> Generuję dane demo..."
  python3 tools/make_sample_data.py
fi

echo "==> Instaluję zależności..."
python3 -m pip install --quiet --disable-pip-version-check -r requirements.txt

echo "==> Backtester działa na http://${HOST}:${PORT}"
exec python3 -m uvicorn app.main:app --host "$HOST" --port "$PORT"
