# Running the Self-Evolving Trading AI Online (Headless / Laptop Closed)

This project is designed to run as an unattended daemon on an always-on machine — a VPS, Raspberry Pi, or a second PC. Your laptop only needs to stay open long enough to deploy it.

## Prerequisites on the remote machine

- **Linux** (Ubuntu/Debian preferred), macOS, or Windows with WSL2
- **Python 3.10+**
- **Alpaca Paper Trading API** account — get your API key & secret at https://app.alpaca.markets
- **(Optional)** OpenAI-compatible LLM API key for autonomous strategy invention (the system falls back to built-in strategies without it)

## 1. Prepare the `.env` file (on your laptop)

Copy `.env.example` to `.env` and fill in your Alpaca paper-trading credentials:

```bash
cp .env.example .env
# Edit .env with your real keys:
#   ALPACA_API_KEY=...
#   ALPACA_API_SECRET=...
#   LLM_API_KEY=...   (optional; system falls back to built-in strategy)
```

Never commit `.env` — it's already in `.gitignore`.

## 2. Deploy to your remote machine

From the project directory on your laptop, run:

```bash
chmod +x deploy/deploy.sh
./deploy.sh user@remote-host /opt/self_evolving_trader
```

This copies all project files (except `.env`, `data/`, cache, and git history) to the remote host.

Then on the remote machine:

```bash
ssh user@remote-host
cd /opt/self_evolving_trader

# Transfer your secrets (never via git)
scp user@laptop:/path/to/self_evolving_trader/.env .

# Create a dedicated user (recommended)
sudo useradd -m -s /bin/bash trader
sudo chown -R trader:trader /opt/self_evolving_trader

# Install Python dependencies
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Install and start as a systemd service
sudo cp deploy/systemd/self_evolving_trader.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable self_evolving_trader.service
sudo systemctl start self_evolving_trader.service
```

## 3. Verify it's running

```bash
# On the remote machine:
sudo systemctl status self_evolving_trader.service
sudo journalctl -u self_evolving_trader -f   # watch live logs
cat /opt/self_evolving_trader/daemon.log      # local log file
```

You should see the dashboard printout each hour:

```
TRADING AI DASHBOARD
============================================================
Time:         2026-11-23 14:00:00
Strategy:     ...
Market Regime: Smooth
Daily PnL:    $+0.00

PRICE ACTION & MARKET STRUCTURE:
------------------------------------------------------------
  AAPL: hammer, momentum_candle
  ...

TRADE SIGNALS:
------------------------------------------------------------
=== AAPL - BUY SIGNAL ===
Entry:  $155.00
Stop:   $147.25
Target: $170.50
...
```

## 4. Management commands

```bash
# Check status
sudo systemctl status self_evolving_trader.service

# Restart after code changes
sudo systemctl restart self_evolving_trader.service

# View live logs
sudo journalctl -u self_evolving_trader -f

# Stop the daemon
sudo systemctl stop self_evolving_trader.service

# View trade history
cat /opt/self_evolving_trader/daemon.log
```

## How it stays online

- **systemd** restarts the daemon automatically if it crashes (`Restart=always`).
- **`PYTHONUNBUFFERED=1`** ensures logs stream immediately to journald.
- The main loop runs once per hour (`time.sleep(3600)`), fetching real market data from Alpaca and executing paper trades via the Alpaca API.
- The SQLite database (`data/memory.sqlite`) persists strategies, trades, and learned rules across restarts.
- Your laptop can be closed once the remote service is started — the daemon runs independently.

## Pterodactyl Panel setup

If you're using a Pterodactyl panel (not a full VPS), systemd does not exist inside the container — the panel itself supervises and restarts the process.

1. Create a new **Python** server in Pterodactyl.
2. **Startup command:** `bash /home/container/deploy/pterodactyl/start.sh`
3. **Before first boot**, upload your `.env` and edit these paths for Linux:
   ```
   DATA_DIR=/home/container/data
   DB_PATH=/home/container/data/memory.sqlite
   ```
   (Your laptop's `.env` uses Windows paths like `C:/...`; those will crash inside the container.)
4. **Upload project files** via the panel's File Manager (or SFTP/FTP): copy everything **except** `.git`, `__pycache__`, `data/`, and `daemon.log`.
5. Start the server. First boot installs Python dependencies (~2 minutes). Check the console to confirm `main.py` launched.

## Security notes

- `.env` contains API secrets — transfer it directly (scp / SFTP), never commit it to git.
- The system uses Alpaca **paper trading** by default (`paper=True` in `executor.py`), so no real money is at risk unless you change the base URL.
- The daemon runs as a non-root `trader` user for safety on a real VPS; inside Pterodactyl it runs as the container's default user.
