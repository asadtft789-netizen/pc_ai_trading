#!/bin/bash
# Startup script for running the trader inside a Pterodactyl container.
# Pterodactyl has no systemd; the panel supervises this process itself.
#
# Solar / generic Python egg startup command:
#   bash /home/container/deploy/pterodactyl/start.sh

set -euo pipefail

cd /home/container

PY=$(command -v python3 || command -v python)
echo "[start] using interpreter: $PY"

# Dependencies are installed once per container rebuild; the marker file
# keeps later boots fast.
if [ ! -f .deps_ok ]; then
    echo "[start] installing dependencies (first boot, may take a few minutes)..."
    $PY -m pip install --no-cache-dir -r requirements.txt
    touch .deps_ok
else
    echo "[start] dependencies already installed"
fi

mkdir -p data

echo "[start] launching trader daemon"
exec $PY -u main.py
