#!/bin/bash
# start.sh — one-command deploy on any Linux VPS
set -e
cd "$(dirname "$0")"
echo "=== installing deps ==="
if command -v apt-get >/dev/null 2>&1; then
  sudo apt-get update -y >/dev/null 2>&1 || true
  sudo apt-get install -y python3 python3-pip >/dev/null 2>&1 || true
fi
pip3 install --quiet requests urllib3 2>/dev/null || pip3 install --user --quiet requests urllib3
export PATH="$HOME/.local/bin:$PATH"

if [ -z "$ADIL_BOT_TOKEN" ]; then
  echo "!! ADIL_BOT_TOKEN not set."
  echo "   export ADIL_BOT_TOKEN='123456:ABC...'   (from @BotFather)"
  echo "   optional: export ADIL_CHAT_ID='123456789'"
  exit 1
fi

mkdir -p state
echo "=== starting 24/7 bot (rescan + refund + leaderboard + spin + telegram) ==="
nohup python3 -u adil_bot.py > adil_bot.out 2>&1 &
echo "  PID $!"
sleep 6
tail -n 12 adil_bot.log 2>/dev/null || tail -n 12 adil_bot.out
echo ""
echo "Live:  tail -f adil_bot.log"
echo "Stop:  pkill -f adil_bot.py"
echo "Keep alive after logout: this used nohup; for reboot-persistence add:"
echo "  (crontab -l; echo '@reboot cd $(pwd) && ADIL_BOT_TOKEN=\$ADIL_BOT_TOKEN nohup python3 -u adil_bot.py >> adil_bot.out 2>&1 &') | crontab -"
