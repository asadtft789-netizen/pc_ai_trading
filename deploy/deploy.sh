#!/bin/bash
printf "%s\n" "Legacy deployment disabled. See DEPLOYMENT.md for release gates." >&2
exit 78
# Deploy the self-evolving trading AI to a remote Linux server.
# Usage: ./deploy.sh <user>@<host> [path_on_server]
set -euo pipefail

TARGET="${1:?Usage: $0 <user>@<host> [remote_path]}"
REMOTE_PATH="${2:-/opt/self_evolving_trader}"
LOCAL_DIR="$(cd "$(dirname "$0")/.." && pwd)"

echo "==> Deploying to $TARGET:$REMOTE_PATH ..."

# Create remote directory
ssh "$TARGET" "mkdir -p $REMOTE_PATH"

# Copy project files (excluding secrets, data, cache)
rsync -avz --delete \
  --exclude='.env' \
  --exclude='data/' \
  --exclude='__pycache__/' \
  --exclude='.git/' \
  --exclude='daemon.log' \
  --exclude='*.pyc' \
  "$LOCAL_DIR/" "$TARGET:$REMOTE_PATH/"

# Copy systemd unit file
scp "$LOCAL_DIR/deploy/systemd/self_evolving_trader.service" "$TARGET:/etc/systemd/system/self_evolving_trader.service"

echo ""
echo "==> Done. Next steps on the remote host:"
echo "  1. scp your .env file:  scp .env $TARGET:$REMOTE_PATH/"
echo "  2. ssh $TARGET"
echo "  3. cd $REMOTE_PATH && python3 -m venv .venv && source .venv/bin/activate"
echo "  4. pip install -r requirements.txt"
echo "  5. sudo systemctl enable self_evolving_trader.service"
echo "  6. sudo systemctl start self_evolving_trader.service"
echo ""
echo "View logs with: ssh $TARGET 'journalctl -u self_evolving_trader -f'"
