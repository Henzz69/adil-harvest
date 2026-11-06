# Oracle Cloud Always Free — 24/7 host (free forever)

## Step 1 — sign up (5 min)
1. Go to **https://oracle.com/cloud/free**
2. Click **Start for free**
3. Country: your country · Email + password
4. **Credit/debit card** — used for identity verification only, **$0 charged** on Always Free
   (a $1 temporary hold may appear, then disappears)
5. Verify phone, pick **home region** (choose one close to Ethiopia: *Germany Central (Frankfurt)*
   or *UAE East (Dubai)* — latency matters for the adil.bet API)

## Step 2 — create the VM
1. Console → **Compute → Instances → Create instance**
2. Name: `adil-bot`
3. **Image**: *Canonical Ubuntu 22.04* (or 24.04)
4. **Shape**: click *Change shape* → **Ampere → VM.Standard.A1.Flex**
   - OCPUs: **1** · Memory: **6 GB** (all within Always Free: 4 OCPU / 24 GB total)
   - ⚠️ If it says *"out of capacity"*, retry later or pick **VM.Standard.E2.1.Micro** (AMD, always available)
5. **Networking**: keep the default VCN, ensure *Assign a public IPv4 address* = **Yes**
6. **SSH keys**: choose **Generate a key pair** → **download the PRIVATE key** (save as `oracle_adil.key`)
7. Click **Create** → wait ~1 min → copy the **Public IP address**

## Step 3 — deploy (one command, from this machine)
```bash
cd C:/Users/h/ptai-setup/adil_cloud
bash deploy_oracle.sh <PUBLIC_IP> /c/Users/h/oracle_adil.key ubuntu
```

That script:
- uploads the bot
- creates a venv + installs deps
- writes `/etc/systemd/system/adil-bot.service` with `Restart=always` and boot autostart
- starts it and shows the log

## Step 4 — verify
Send `/status` to **@anbesaalertbot** → it replies instantly (not 5-min cron lag).

On the VM:
```bash
ssh -i oracle_adil.key ubuntu@<IP>
sudo systemctl status adil-bot
tail -f ~/adil_cloud/adil_bot.log
sudo systemctl restart adil-bot
```

## What runs there (true 24/7, no cron lag)
| worker | behaviour |
|---|---|
| **rescan** | endless — the next sweep starts the instant the previous ends |
| **toplive** | re-checks the top 15 balances **every 1 second**, writes `state/top_live.json` |
| **refund** | watchlist poll every 2 min, auto-withdraw + code push |
| **lb** | leaderboard brute (4 live + 15 rotating IDs/cycle) |
| **spin** | spin-wheel + top-winners |
| **mask** | top-winner mask cracker (4 threads) |
| **telegram** | instant command replies (`/top`, `/withdraw`, `/avi`, …) |

## Notes
- Oracle's firewall: the VM's *Security List* only allows port 22 by default — the bot needs **no inbound ports** (all outbound), so nothing to open.
- Free tier gives 10 TB/month egress — plenty.
- If the instance is ever reclaimed for inactivity, Oracle emails you; just restart it.
- Keep the GitHub Actions setup as a **backup** — it keeps running independently.
