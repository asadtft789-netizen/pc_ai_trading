#!/bin/bash
# Install the self-evolving trader as a systemd service so it runs 24/7
# and auto-restarts on crash. Run this once on your VPS.
#
# Usage:
#   bash deploy/install_systemd.sh
#
# Prerequisites:
#   - Repo already cloned at /root/pc_ai_trading
#   - .env created with your Alpaca keys
#   - requirements.txt installed

set -euo pipefail

REPO_DIR="/root/pc_ai_trading"
SERVICE_NAME="self_evolving_trader"
SERVICE_FILE="deploy/systemd/${SERVICE_NAME}.service"

cd "$REPO_DIR"

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

# 5. Install systemd service
echo "[4/5] Installing systemd service..."
cp "$SERVICE_FILE" "/etc/systemd/system/${SERVICE_NAME}.service"
systemctl daemon-reload
systemctl enable "${SERVICE_NAME}.service"

# 6. Start the service
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