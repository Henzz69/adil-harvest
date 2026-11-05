# adil 24/7 harvest bot

Single daemon, five always-on workers, Telegram control.

## Workers (run non-stop, auto-restart on error)
| worker | job |
|---|---|
| **rescan** | infinite full balance sweep of every known phone → `state/balances.json` |
| **refund** | polls watchlist; on `withdrawable >= threshold` auto-withdraws, reads the **claim code from the payment gateway** (no browser needed) and pushes it to Telegram |
| **lb** | leaderboard brute — all tournament days, paginated (4 known-live + 15 rotating IDs/cycle) |
| **spin** | spin-wheel + top-winners harvest |
| **mask** | top-winner **mask cracker** — brute the 6 hidden digits of `+251******176` style masks, adds hits to master |

## Telegram commands
```
/status            health + totals
/top [n]           top n by withdrawable  ← sends AVIATOR LINK BUTTONS
/avi [n]           plain Aviator links
/jetx [n]          JetX links
/withdraw <phone>  withdraw + return code immediately
/codes             recent captured codes
/mask              mask cracker status
/watch             watchlist + threshold
/add <phone>  /del <phone>
/threshold <amt>   auto-withdraw threshold (default 5000)
/rescan            restart sweep cycle
```

## 1. Create the bot (30 seconds, on your phone)
1. Telegram → search **@BotFather** → send `/newbot`
2. Pick a name, pick a username ending in `bot`
3. Copy the token `123456789:ABCdef...`
4. Send `/start` to your new bot

## 2. Run it

### Windows (this machine, 24/7)
```bat
set_token.bat        :: paste the BotFather token
run_bot.bat          :: starts detached
```
Log: `adil_bot.log`   Stop: `taskkill /f /im pythonw.exe`

### Linux VPS (any provider)
```bash
export ADIL_BOT_TOKEN='123456:ABC...'
bash start.sh
```

### Fly.io
```bash
fly launch --no-deploy
fly secrets set ADIL_BOT_TOKEN=123456:ABC...
fly volumes create adil_state --size 1
fly deploy
fly logs
```

### Oracle Cloud Always Free (best free 24/7, free forever)
1. https://oracle.com/cloud/free → sign up (card verifies, $0)
2. Create **Always Free** instance: `VM.Standard.A1.Flex` (ARM 4 OCPU/24GB) or AMD micro — Ubuntu 22.04
3. Open port 22, save the SSH key
4. From this machine:
```bash
scp -r adil_cloud ubuntu@<VM_IP>:~/
ssh ubuntu@<VM_IP>
cd ~/adil_cloud
export ADIL_BOT_TOKEN='123456:ABC...'
bash start.sh
# survive reboots:
(crontab -l 2>/dev/null; echo "@reboot cd \$HOME/adil_cloud && ADIL_BOT_TOKEN=$ADIL_BOT_TOKEN nohup python3 -u adil_bot.py >> adil_bot.out 2>&1 &") | crontab -
```

### Google Cloud e2-micro (also free forever)
Same as Oracle: create `e2-micro` in `us-central1`, scp the folder, `bash start.sh`.

## 3. Connect
Open your bot in Telegram → `/start` → `/status`

The bot remembers the first chat that messages it (or set `ADIL_CHAT_ID`).

## Files
```
adil_bot.py            the daemon
state/master_phones.txt    every known phone (12,749 seeded)
state/balances.json        last sweep results
state/watchlist.json       re-funder watchlist (30 seeded)
state/codes.json           captured withdrawal codes
state/mask_targets.json    mask crack queue
state/mask_found.json      cracked accounts
state/config.json          bot_token, chat_id, threshold
```

## Notes
- Claim codes come from `zplayzone.com/api/gateway/pay/<token>` → `claimCode`
- Redeem codes with **your own agent** — never the shared "Withdraw with Betconnect" pool
  (that pool auto-assigns to any agent; it stole two payouts already)
