#!/bin/bash
# deploy_oracle.sh — one-command deploy of the 24/7 adil bot to any Ubuntu/Debian VPS
#
# Usage (from Windows git-bash or Linux):
#   bash deploy_oracle.sh <VM_IP> <SSH_KEY_PATH> [ssh_user]
#
# Example:
#   bash deploy_oracle.sh 140.238.12.34 ~/.ssh/oracle_adil ubuntu
#
# What it does:
#   * copies the bot folder to the VM
#   * installs python + deps
#   * writes /etc/systemd/system/adil-bot.service  (auto-restart, auto-start on boot)
#   * starts it and tails the log
set -e

IP="${1:?usage: deploy_oracle.sh <VM_IP> <SSH_KEY_PATH> [ssh_user]}"
KEY="${2:?usage: deploy_oracle.sh <VM_IP> <SSH_KEY_PATH> [ssh_user]}"
USER="${3:-ubuntu}"
TOKEN="${ADIL_BOT_TOKEN:-9108751382:AAHRtqKXM0nCxlCeQt0T1DoCHQvigZ2LBUs}"
CHAT="${ADIL_CHAT_ID:-5681654051}"
REMOTE=~/adil_cloud

echo "=== 1/5  packing ==="
cd "$(dirname "$0")"
TAR=/tmp/adil_cloud.tgz
tar czf "$TAR" \
  --exclude='.git' --exclude='__pycache__' --exclude='.ghtok' \
  --exclude='.gistid' --exclude='*.log' \
  adil_bot.py requirements.txt state 2>/dev/null || \
tar czf "$TAR" --exclude='.git' --exclude='__pycache__' --exclude='.ghtok' \
  --exclude='.gistid' --exclude='*.log' adil_bot.py requirements.txt
ls -la "$TAR"

echo "=== 2/5  uploading to $USER@$IP ==="
scp -i "$KEY" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "$TAR" "$USER@$IP:/tmp/"

echo "=== 3/5  installing on the VM ==="
ssh -i "$KEY" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "$USER@$IP" bash -s <<REMOTE_SCRIPT
set -e
export DEBIAN_FRONTEND=noninteractive
echo "--- apt deps ---"
sudo apt-get update -qq
sudo apt-get install -y -qq python3 python3-pip python3-venv >/dev/null

echo "--- unpack ---"
rm -rf $REMOTE
mkdir -p $REMOTE
tar xzf /tmp/adil_cloud.tgz -C $REMOTE
cd $REMOTE
mkdir -p state

echo "--- venv + pip ---"
python3 -m venv venv
./venv/bin/pip install -q --upgrade pip
./venv/bin/pip install -q requests urllib3

echo "--- config ---"
cat > state/config.json <<JSON
{"bot_token":"$TOKEN","chat_id":"$CHAT","threshold":5000.0,"owner_only":true}
JSON

echo "--- systemd unit ---"
sudo tee /etc/systemd/system/adil-bot.service >/dev/null <<UNIT
[Unit]
Description=adil 24/7 harvest bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$REMOTE
Environment=PYTHONUNBUFFERED=1
Environment=ADIL_BOT_TOKEN=$TOKEN
Environment=ADIL_CHAT_ID=$CHAT
Environment=ADIL_TOP_REFRESH=1
Environment=ADIL_TOP_N=15
Environment=ADIL_MASK_THREADS=4
ExecStart=$REMOTE/venv/bin/python -u $REMOTE/adil_bot.py
Restart=always
RestartSec=5
StandardOutput=append:$REMOTE/adil_bot.log
StandardError=append:$REMOTE/adil_bot.log

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable adil-bot >/dev/null 2>&1
sudo systemctl restart adil-bot
sleep 8
echo "--- status ---"
sudo systemctl is-active adil-bot
echo "--- log tail ---"
tail -n 15 $REMOTE/adil_bot.log 2>/dev/null || true
REMOTE_SCRIPT

echo "=== 4/5  done ==="
echo "=== 5/5  live log (Ctrl-C to stop watching) ==="
ssh -i "$KEY" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "$USER@$IP" \
  "tail -f $REMOTE/adil_bot.log" || true
