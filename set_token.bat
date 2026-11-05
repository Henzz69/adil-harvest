@echo off
REM set_token.bat — paste your BotFather token here to connect the bot
cd /d "%~dp0"
set /p TOK=BotFather token (123456:ABC...): 
python -c "import json,os;os.makedirs('state',exist_ok=True);p='state/config.json';d=json.load(open(p)) if os.path.exists(p) else {};d['bot_token']=%r if False else None" 2>nul
python - "%TOK%" << 'PY'
import json,os,sys
os.makedirs('state',exist_ok=True)
p='state/config.json'
d=json.load(open(p)) if os.path.exists(p) else {}
d['bot_token']=sys.argv[1].strip()
json.dump(d,open(p,'w'),indent=1)
print('saved bot_token to state/config.json')
PY
echo.
echo Now run: run_bot.bat
