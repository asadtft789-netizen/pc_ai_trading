#!/bin/bash
# One-shot setup for the self-evolving trader on a fresh VPS/sandbox.
#
# Usage:
#   bash deploy/setup_trader.sh
#
# What it does:
#   1. Clones the repo if not present
#   2. Installs Python dependencies
#   3. Creates .env if missing (from env vars or prompts)
#   4. Starts the daemon 24/7 (systemd on a VPS, nohup in a container)

set -euo pipefail

REPO_URL="https://github.com/asadtft789-netizen/pc_ai_trading.git"
DEFAULT_DIR="/root/pc_ai_trading"
REPO_DIR="${1:-$DEFAULT_DIR}"

# --- 1. Clone repo if needed ------------------------------------------------
if [ ! -d "$REPO_DIR/.git" ]; then
    echo "[1/4] Cloning repo into $REPO_DIR ..."
    mkdir -p "$REPO_DIR"
    git clone --depth 1 "$REPO_URL" "$REPO_DIR"
else
    echo "[1/4] Repo already present — pulling latest..."
    cd "$REPO_DIR"
    git pull
fi

cd "$REPO_DIR"

# --- 2. Install dependencies ------------------------------------------------
echo "[2/4] Installing Python dependencies..."
python3 -m pip install --quiet --upgrade pip
python3 -m pip install --quiet -r requirements.txt

# --- 3. Create .env if missing ----------------------------------------------
mkdir -p data

if [ ! -f .env ]; then
    echo ""
    echo "============================================================"
    echo "  Creating .env — paste your Alpaca paper-trading keys:"
    echo "============================================================"
    echo ""

    read -p "  ALPACA_API_KEY: " ALPACA_KEY
    read -s -p "  ALPACA_API_SECRET: " ALPACA_SECRET
    echo ""

    cat > .env << EOF
ALPACA_API_KEY=${ALPACA_KEY}
ALPACA_API_SECRET=${ALPACA_SECRET}
ALPACA_BASE_URL=https://paper-api.alpaca.markets

LLM_PROVIDER=google
LLM_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai/
LLM_API_KEY=${LLM_API_KEY:-}
LLM_MODEL=gemini-flash-lite-latest

LLM_FALLBACK_PROVIDER=openai
LLM_FALLBACK_BASE_URL=https://openrouter.ai/api/v1
LLM_FALLBACK_API_KEY=${LLM_FALLBACK_API_KEY:-}
LLM_FALLBACK_MODEL=openai/gpt-4o-mini

TRADE_UNIVERSE=AAPL,MSFT,NVDA,TSLA,BTC/USD,ETH/USD
MAX_CAPITAL_USD=10
MAX_POSITION_USD=1
MAX_DAILY_LOSS_USD=0.50

DATA_DIR=data
DB_PATH=data/memory.sqlite
LOG_LEVEL=INFO
EOF
    echo "  .env created."
fi

# --- 4. Start the daemon ----------------------------------------------------
echo "[3/4] Starting the daemon..."
bash deploy/install_systemd.sh "$REPO_DIR"