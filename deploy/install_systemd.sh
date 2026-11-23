#!/bin/bash
# Install the self-evolving trader so it runs 24/7 and auto-restarts on crash.
# Run this once on your VPS.
#
# Usage:
#   bash deploy/install_systemd.sh
#
# Prerequisites:
#   - Repo already cloned at /root/pc_ai_trading
#   - .env created with your Alpaca keys
#   - requirements.txt installed

set -euo pipefail

# Auto-detect repo location: use the directory containing this script's parent,
# or the current directory if run from inside the repo.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_DIR="${1:-$SCRIPT_DIR}"
SERVICE_NAME="self_evolving_trader"
SERVICE_FILE="deploy/systemd/${SERVICE_NAME}.service"

cd "$REPO_DIR" || {
    echo "ERROR: cannot find repo at $REPO_DIR"
    exit 1
}

# 1. Pull latest code
echo "[1/5] Pulling latest code..."
git pull

# 2. Install dependencies
echo "[2/5] Installing Python dependencies..."
python3 -m pip install -q -r requirements.txt

# 3. Create data directory
echo "[3/5] Creating data directory..."
mkdir -p data

# 4. Verify .env exists
if [ ! -f .env ]; then
    echo ""
    echo "============================================================"
    echo "  MISSING .env — create it with your Alpaca API keys:"
    echo "    ALPACA_API_KEY=..."
    echo "    ALPACA_API_SECRET=..."
    echo "============================================================"
    echo ""
    exit 1
fi

# 5. Install service (systemd on a real VPS, background process in a container)
if command -v systemctl >/dev/null 2>&1 && systemctl is-system-running >/dev/null 2>&1; then
    echo "[4/5] Installing systemd service..."
    cp "$SERVICE_FILE" "/etc/systemd/system/${SERVICE_NAME}.service"
    systemctl daemon-reload
    systemctl enable "${SERVICE_NAME}.service"

    echo "[5/5] Starting the daemon..."
    systemctl restart "${SERVICE_NAME}.service"

    echo ""
    echo "============================================================"
    echo "  Self-Evolving Trader is now running 24/7!"
    echo "============================================================"
    echo ""
    echo "  Check status:   systemctl status ${SERVICE_NAME}.service"
    echo "  Watch logs:     journalctl -u ${SERVICE_NAME}.service -f"
    echo "  Local log:      ${REPO_DIR}/daemon.log"
    echo "  Restart:        systemctl restart ${SERVICE_NAME}.service"
    echo "  Stop:           systemctl stop ${SERVICE_NAME}.service"
    echo ""
else
    echo "[4/5] systemd not available (container) — starting as background process..."
    # Kill any existing instance
    pkill -f "python3 -u ${REPO_DIR}/main.py" 2>/dev/null || true
    sleep 1

    # Start in background with nohup so it survives shell exit
    nohup python3 -u "${REPO_DIR}/main.py" >> "${REPO_DIR}/daemon.log" 2>&1 &
    echo "  Started daemon with PID: $!"
    echo ""
    echo "============================================================"
    echo "  Self-Evolving Trader is now running 24/7!"
    echo "============================================================"
    echo ""
    echo "  Check status:   ps aux | grep main.py"
    echo "  Watch logs:     tail -f ${REPO_DIR}/daemon.log"
    echo "  Restart:        pkill -f main.py && nohup python3 -u ${REPO_DIR}/main.py >> ${REPO_DIR}/daemon.log 2>&1 &"
    echo "  Stop:           pkill -f main.py"
    echo ""
fi