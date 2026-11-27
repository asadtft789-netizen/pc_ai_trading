#!/bin/bash
printf "%s\n" "Legacy deployment disabled. See DEPLOYMENT.md for release gates." >&2
exit 78
# Pull the project straight from GitHub and start the trader.
#
# Pterodactyl startup command:
#   bash /home/container/deploy/pterodactyl/setup.sh
#
# Required: a .env file with your Alpaca paper-trading API keys.
# The repo itself is private — the panel must have a way to authenticate:
#   * HTTPS + personal access token (set GITHUB_TOKEN), OR
#   * SSH deploy key (add your public key to the repo settings)
#
# The .env is NEVER committed to git; you upload it separately via the
# panel's File Manager or SFTP.

set -euo pipefail

REPO_URL="${GITHUB_REPO_URL:-https://github.com/asadtft789-netizen/pc_ai_trading.git}"
WORKDIR="/home/container"

echo "[setup] working directory: $WORKDIR"

# --- 1. Clone (or update) the repo -----------------------------------------
if [ -d "$WORKDIR/.git" ]; then
    echo "[setup] repo already present — pulling latest..."
    cd "$WORKDIR"
    git fetch --quiet origin main
    git reset --hard origin/main
    git clean -fdx -- ':!.env' ':!data' ':!.deps_ok'
else
    echo "[setup] cloning repo..."
    cd "$WORKDIR"
    if [ -n "${GITHUB_TOKEN:-}" ]; then
        AUTH_URL="https://${GITHUB_TOKEN}@github.com/asadtft789-netizen/pc_ai_trading.git"
        git clone --depth 1 --branch main "$AUTH_URL" .
    else
        git clone --depth 1 --branch main "$REPO_URL" .
    fi
fi

# --- 2. Python + dependencies ----------------------------------------------
PY=$(command -v python3 || command -v python)
echo "[setup] using interpreter: $PY"

if [ ! -f .deps_ok ]; then
    echo "[setup] installing dependencies (first boot, may take a few minutes)..."
    $PY -m pip install --no-cache-dir -r requirements.txt
    touch .deps_ok
else
    echo "[setup] dependencies already installed"
fi

# --- 3. Runtime state directory --------------------------------------------
mkdir -p data

# --- 4. Sanity check: secrets must exist -----------------------------------
if [ ! -f .env ]; then
    echo ""
    echo "============================================================"
    echo "  MISSING .env — create it now with your Alpaca API keys:"
    echo "    ALPACA_API_KEY=..."
    echo "    ALPACA_API_SECRET=..."
    echo "    DATA_DIR=data"
    echo "    DB_PATH=data/memory.sqlite"
    echo "============================================================"
    echo ""
    sleep 5
fi

# --- 5. Launch -------------------------------------------------------------
echo "[setup] launching trader daemon"
exec $PY -u main.py