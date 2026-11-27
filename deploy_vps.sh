#!/usr/bin/env bash
printf "%s\n" "Legacy deployment disabled. See DEPLOYMENT.md for release gates." >&2
exit 78
# ==============================================================================
# PHOENIX DEPLOYMENT SCRIPT — Google Cloud Compute Engine
# ==============================================================================
# Makes the trading daemon fully autonomous on a GCP e2-micro VM (Free Tier).
# Run this once on a fresh Ubuntu 22.04 VM instance.
# ==============================================================================

set -e

echo "╔══════════════════════════════════════════════════════════════╗"
echo "║   PHOENIX AUTONOMOUS TRADING DAEMON — VPS DEPLOYMENT      ║"
echo "╚══════════════════════════════════════════════════════════════╝"

# ---- 1. System dependencies ----
echo "[1/7] Installing system dependencies..."
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3-pip python3-venv git sqlite3 cron vim nano

# ---- 2. Add swap (crash protection for e2-micro 1GB RAM) ----
echo "[2/7] Adding 2GB swap file..."
if [ ! -f /swapfile ]; then
    sudo fallocate -l 2G /swapfile
    sudo chmod 600 /swapfile
    sudo mkswap /swapfile
    sudo swapon /swapfile
    echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
fi

# ---- 3. Clone repository ----
echo "[3/7] Cloning repository from GitHub..."
cd ~
if [ ! -d pc_ai_trading ]; then
    git clone https://github.com/asadtft789-netizen/pc_ai_trading.git
fi
cd pc_ai_trading
git checkout review/hourly-paper
git pull origin review/hourly-paper

# ---- 4. Python environment ----
echo "[4/7] Setting up Python virtual environment..."
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip setuptools wheel
pip install -r requirements.txt

# ---- 5. Credentials placeholder ----
echo "[5/7] Credentials configuration..."
if [ ! -f .env ]; then
    echo "ALPACA_API_KEY=YOUR_API_KEY_HERE" > .env
    echo "ALPACA_API_SECRET=YOUR_SECRET_KEY_HERE" >> .env
    echo "ALPACA_BASE_URL=https://paper-api.alpaca.markets" >> .env
    echo "PAPER_ENTRIES_ENABLED=YES" >> .env
    chmod 600 .env
    echo "  >> .env created — PLEASE EDIT WITH YOUR REAL ALPACA PAPER KEYS <<"
fi

# ---- 6. Systemd service files ----
echo "[6/7] Installing systemd services..."

USER_NAME=$(whoami)
HOME_DIR=$(eval echo ~$USER_NAME)
PROJECT_DIR="$HOME_DIR/pc_ai_trading"
PYTHON_BIN="$PROJECT_DIR/venv/bin/python"

cat > /tmp/trading-research.service <<EOF
[Unit]
Description=Trading AI Research Daemon
After=network.target

[Service]
User=$USER_NAME
WorkingDirectory=$PROJECT_DIR
ExecStart=$PYTHON_BIN main.py research
Restart=always
RestartSec=10
Environment=HOME=$HOME_DIR

[Install]
WantedBy=multi-user.target
EOF

cat > /tmp/trading-supervise.service <<EOF
[Unit]
Description=Trading AI Supervisor
After=network.target

[Service]
User=$USER_NAME
WorkingDirectory=$PROJECT_DIR
ExecStart=$PYTHON_BIN main.py supervise
Restart=always
RestartSec=10
Environment=HOME=$HOME_DIR

[Install]
WantedBy=multi-user.target
EOF

sudo cp /tmp/trading-research.service /etc/systemd/system/trading-research.service
sudo cp /tmp/trading-supervise.service /etc/systemd/system/trading-supervise.service

# ---- 7. Enable and start ----
echo "[7/7] Enabling and starting autonomous services..."
sudo systemctl daemon-reload
sudo systemctl enable trading-research trading-supervise
sudo systemctl start trading-research trading-supervise

echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║   DEPLOYMENT COMPLETE                                       ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""
echo "The daemon is now running autonomously on this VPS."
echo ""
echo "IMPORTANT STEPS REMAINING:"
echo "  1. Edit .env with your real Alpaca PAPER keys:"
echo "     nano $PROJECT_DIR/.env"
echo "  2. Restart services after editing keys:"
echo "     sudo systemctl restart trading-research trading-supervise"
echo ""
echo "  Check logs:"
echo "     sudo journalctl -u trading-research -f"
echo "     sudo journalctl -u trading-supervise -f"
echo ""
echo "  Check status:"
echo "     sudo systemctl status trading-research trading-supervise"
echo ""
echo "The daemon will auto-restart on crash or VM reboot."
