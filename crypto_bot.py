import os, time, json, threading, queue, requests
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

TOKEN = os.environ["TELEGRAM_TOKEN"].strip()
CHAT = os.environ["TELEGRAM_CHAT_ID"].strip()
NTFY = os.environ.get("NTFY_TOPIC", "").strip()
WA_PHONE = os.environ.get("WA_PHONE", "").strip()    # jaise +971501234567
WA_APIKEY = os.environ.get("WA_APIKEY", "").strip()  # CallMeBot se mili key
SYMBOLS = ["BTCUSDT", "XAUUSDT"]
TFS = {"15": 900, "5": 300, "1": 60}  # interval -> seconds (15m pehle, trend ke liye)
RUN_SECONDS = 5 * 3600 + 45 * 60
LOOKBACK = {"1": 60, "5": 80, "15": 100}  # kitni purani candles tak OB dhoondhe
IMPULSE = 1.2   # impulse body >= 1.2 x ATR
SL_BUF = 0.3    # SL = zone ke bahar 0.3 x ATR
PUSH_MAX = 1.5  # pichli 3 candles ka ulta push > 1.5 x ATR ho to OB signal skip
ADX_MIN = 20    # 1m/5m pe isse neeche = sideways, alert band
HTF_FILTER = True      # 1m/5m signal sirf 15m trend ki side me
SESSION_FILTER = False  # True = 1m/5m alerts sirf London/NY session me
SESSION_ALERTS = True  # session shuru/band hone pe alert
# (naam, city timezone, open hour, close hour) - local time, DST khud adjust
SESSIONS = [("Tokyo", "Asia/Tokyo", 9, 18),
            ("London", "Europe/London", 8, 17),
            ("New York", "America/New_York", 8, 17)]
SESS_FLAG = {"Tokyo": "🇯🇵", "London": "🇬🇧", "New York": "🇺🇸"}
LIMIT_EXPIRE = 10      # limit entry itni candles me fill na ho to cancel
STATE_FILE = "crypto_state.json"
DXB = timezone(timedelta(hours=4))

OKX_IDS = {"BTCUSDT": "BTC-USDT-SWAP", "XAUUSDT": "XAU-USDT-SWAP"}

def get_candles(sym, tf, sec):
    r = requests.get(
        "https://www.okx.com/api/v5/market/candles",
        params={"instId": OKX_IDS[sym], "bar": tf + "m", "limit": 300},
        timeout=15)
    r.raise_for_status()
    j = r.json()
    if j.get("code") != "0":
        raise Exception("OKX: " + str(j.get("msg")))
    rows = j["data"][::-1]  # purani se nayi
    return [{"t": int(x[0]) // 1000, "o": float(x[1]), "h": float(x[2]),
             "l": float(x[3]), "c": float(x[4])}
            for x in rows if x[8] == "1"]  # sirf closed candles

def ema(vals, n):
    k = 2 / (n + 1)
    out = [vals[0]]
    for v in vals[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out

def atr_list(c, n=14):
    tr = [c[0]["h"] - c[0]["l"]]
    for i in range(1, len(c)):
        tr.append(max(c[i]["h"] - c[i]["l"],
                      abs(c[i]["h"] - c[i - 1]["c"]),
                      abs(c[i]["l"] - c[i - 1]["c"])))
    out = []
    for i in range(len(tr)):
        w = tr[max(0, i - n + 1): i + 1]
        out.append(sum(w) / len(w))
    return out

def adx_last(c, n=14):
    tr, pdm, mdm = [], [], []
    for i in range(1, len(c)):
        up = c[i]["h"] - c[i - 1]["h"]
        dn = c[i - 1]["l"] - c[i]["l"]
        pdm.append(up if up > dn and up > 0 else 0)
        mdm.append(dn if dn > up and dn > 0 else 0)
        tr.append(max(c[i]["h"] - c[i]["l"],
                      abs(c[i]["h"] - c[i - 1]["c"]),
                      abs(c[i]["l"] - c[i - 1]["c"])))
    if len(tr) < n * 2:
        return 0
    s_tr, s_p, s_m = sum(tr[:n]), sum(pdm[:n]), sum(mdm[:n])
    dx = []
    for i in range(n, len(tr) + 1):
        if i > n:
            s_tr = s_tr - s_tr / n + tr[i - 1]
            s_p = s_p - s_p / n + pdm[i - 1]
            s_m = s_m - s_m / n + mdm[i - 1]
        pdi = 100 * s_p / s_tr if s_tr else 0
        mdi = 100 * s_m / s_tr if s_tr else 0
        dx.append(100 * abs(pdi - mdi) / (pdi + mdi) if (pdi + mdi) else 0)
    if len(dx) < n:
        return 0
    a = sum(dx[:n]) / n
    for d in dx[n:]:
        a = (a * (n - 1) + d) / n
    return a

def find_ob(c, tf="1"):
    n = len(c)
    if n < 210:
        return None
    closes = [x["c"] for x in c]
    e20, e50, e200 = ema(closes, 20), ema(closes, 50), ema(closes, 200)
    atr = atr_list(c)
    a = atr[-1]
    last = c[-1]
    if tf in ("1", "5"):
        bull = e20[-1] > e50[-1] > e200[-1] and last["c"] > e50[-1]
        bear = e20[-1] < e50[-1] < e200[-1] and last["c"] < e50[-1]
    else:  # 15m: thoda relax, sirf EMA 50 vs 200
        bull = e50[-1] > e200[-1] and last["c"] > e50[-1]
        bear = e50[-1] < e200[-1] and last["c"] < e50[-1]
    if not (bull or bear):
        return None
    # pichli 3 candles (last se pehle) ka push
    push = sum(x["c"] - x["o"] for x in c[-4:-1])
    for i in range(n - 3, n - LOOKBACK[tf], -1):
        o = c[i]
        nx = c[i + 1]
        body = abs(nx["c"] - nx["o"])
        strong = body >= IMPULSE * atr[i]
        mid = c[i + 2: n - 1]
        if bull and o["c"] < o["o"] and nx["c"] > o["h"] and strong:
            lo, hi = o["l"], o["h"]
            if any(x["l"] <= hi for x in mid):
                continue
            # rejection: wick zone me, green candle, close zone ke upar
            if last["l"] <= hi and last["c"] > hi and last["c"] > last["o"]:
                if push < -PUSH_MAX * a:  # neeche ka strong push, buy risky
                    return None
                entry = hi  # limit entry: zone top
                sl = lo - SL_BUF * a
                risk = entry - sl
                if risk <= 0:
                    return None
                return ("OB Retest", "BUY", entry, sl,
                        [entry + risk * m for m in (1, 2, 3)],
                        f"OB zone: {lo:.2f} - {hi:.2f}\n"
                        f"Abhi price: {last['c']:.2f} (limit order zone top pe)")
        if bear and o["c"] > o["o"] and nx["c"] < o["l"] and strong:
            lo, hi = o["l"], o["h"]
            if any(x["h"] >= lo for x in mid):
                continue
            if last["h"] >= lo and last["c"] < lo and last["c"] < last["o"]:
                if push > PUSH_MAX * a:  # upar ka strong push, sell risky
                    return None
                entry = lo  # limit entry: zone bottom
                sl = hi + SL_BUF * a
                risk = sl - entry
                if risk <= 0:
                    return None
                return ("OB Retest", "SELL", entry, sl,
                        [entry - risk * m for m in (1, 2, 3)],
                        f"OB zone: {lo:.2f} - {hi:.2f}\n"
                        f"Abhi price: {last['c']:.2f} (limit order zone bottom pe)")
    return None

def find_cross(c, tf="1"):
    if len(c) < 210:
        return []
    closes = [x["c"] for x in c]
    e20, e50, e200 = ema(closes, 20), ema(closes, 50), ema(closes, 200)
    atr = atr_list(c)
    out = []
    pairs = [("EMA 20/50 Cross", e20, e50),
             ("EMA 50/200 Cross", e50, e200)]
    for name, a, b in pairs:
        side = None
        if a[-2] <= b[-2] and a[-1] > b[-1]:
            side = "BUY"
        elif a[-2] >= b[-2] and a[-1] < b[-1]:
            side = "SELL"
        if not side:
            continue
        entry = c[-1]["c"]
        # 1m/5m pe 20/50 cross sirf EMA 200 trend ki side me
        if tf in ("1", "5") and name == "EMA 20/50 Cross":
            if side == "BUY" and entry < e200[-1]:
                continue
            if side == "SELL" and entry > e200[-1]:
                continue
        recent = c[-10:]
        if side == "BUY":
            sl = min(x["l"] for x in recent) - 0.1 * atr[-1]
            risk = entry - sl
            tps = [entry + risk * m for m in (1, 2, 3)]
        else:
            sl = max(x["h"] for x in recent) + 0.1 * atr[-1]
            risk = sl - entry
            tps = [entry - risk * m for m in (1, 2, 3)]
        if risk <= 0:
            continue
        out.append((name, side, entry, sl, tps, ""))
    return out

_q = queue.Queue()

def _post(text):
    for attempt in range(1, 9):
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                data={"chat_id": CHAT, "text": text}, timeout=40)
            if r.status_code == 200:
                print("Telegram OK", flush=True)
                return True
            print("Telegram error:", r.status_code, r.text, flush=True)
            if r.status_code == 429:  # Telegram ne roka hai, utna ruk ke dobara
                try:
                    wait = int(r.json().get("parameters", {}).get("retry_after", 10))
                except Exception:
                    wait = 10
                time.sleep(wait + 1)
                continue
            if r.status_code in (400, 401, 403, 404):
                return False  # token ya chat id ka masla, retry se fayda nahi
        except Exception as ex:
            print("Telegram error (try", attempt, "):", ex, flush=True)
        time.sleep(min(5 * attempt, 20))
    print("Telegram: message chhod diya (8 koshish ke baad)", flush=True)
    return False

def _worker():
    while True:
        text = _q.get()
        try:
            _post(text)
        finally:
            time.sleep(1.2)  # Telegram limit: ~1 message/second
            _q.task_done()

threading.Thread(target=_worker, daemon=True).start()

_nq = queue.Queue()

def _ntfy_worker():
    # ntfy.sh: free push notification, Telegram band ho tab bhi alert aaye
    while True:
        text = _nq.get()
        try:
            for attempt in range(1, 4):
                try:
                    r = requests.post(f"https://ntfy.sh/{NTFY}",
                                      data=text.encode("utf-8"),
                                      headers={"Title": "Crypto Alert", "Priority": "high"},
                                      timeout=20)
                    if r.status_code == 200:
                        print("ntfy OK", flush=True)
                        break
                    print("ntfy error:", r.status_code, r.text[:200], flush=True)
                except Exception as ex:
                    print("ntfy error (try", attempt, "):", ex, flush=True)
                time.sleep(3 * attempt)
        finally:
            time.sleep(0.5)
            _nq.task_done()

if NTFY:
    threading.Thread(target=_ntfy_worker, daemon=True).start()

_wq = queue.Queue()

def _wa_worker():
    # CallMeBot (free WhatsApp API) - sirf zaroori alerts, dheere dheere
    while True:
        text = _wq.get()
        try:
            for attempt in range(1, 4):
                try:
                    r = requests.get("https://api.callmebot.com/whatsapp.php",
                                     params={"phone": WA_PHONE, "text": text,
                                             "apikey": WA_APIKEY},
                                     timeout=30)
                    if r.status_code == 200:
                        print("WhatsApp OK", flush=True)
                        break
                    print("WhatsApp error:", r.status_code, r.text[:200], flush=True)
                except Exception as ex:
                    print("WhatsApp error (try", attempt, "):", ex, flush=True)
                time.sleep(10 * attempt)
        finally:
            time.sleep(8)  # free API: messages ke beech gap
            _wq.task_done()

if WA_PHONE and WA_APIKEY:
    threading.Thread(target=_wa_worker, daemon=True).start()

def send(text, wa=False):
    _q.put(text)  # main loop block nahi hota, alert background me jata hai
    if NTFY:
        _nq.put(text)
    if wa and WA_PHONE and WA_APIKEY:  # WhatsApp pe sirf zaroori alerts
        _wq.put(text)

def get_candles_long(sym, tf, pages=3):
    # status ke liye zyada history (3 x 300 candles), EMA 200 sahi bane
    out, after = [], None
    for _ in range(pages):
        params = {"instId": OKX_IDS[sym], "bar": tf + "m", "limit": 300}
        if after:
            params["after"] = after
        r = requests.get("https://www.okx.com/api/v5/market/candles",
                         params=params, timeout=15)
        r.raise_for_status()
        j = r.json()
        if j.get("code") != "0" or not j["data"]:
            break
        out += j["data"]
        after = j["data"][-1][0]  # sabse purani candle ka time
    rows = sorted({x[0]: x for x in out}.values(), key=lambda x: int(x[0]))
    return [{"t": int(x[0]) // 1000, "o": float(x[1]), "h": float(x[2]),
             "l": float(x[3]), "c": float(x[4])}
            for x in rows if x[8] == "1"]

def last_cross(c, a, b, start):
    for i in range(len(c) - 1, start, -1):
        if (a[i - 1] <= b[i - 1]) != (a[i] <= b[i]):
            side = "BUY" if a[i] > b[i] else "SELL"
            hrs = (c[-1]["t"] - c[i]["t"]) / 3600
            return f"{side}, {hrs:.1f} ghante pehle" if hrs < 48 else f"{side}, {hrs / 24:.1f} din pehle"
    days = (c[-1]["t"] - c[start]["t"]) / 86400
    return f"pichle ~{days:.0f} din me nahi"

def status_15m():
    # bot start hone pe 15m ka haal: trend, EMA 200, last cross kab hua
    lines = ["📊 15m status"]
    for sym in SYMBOLS:
        try:
            try:
                c = get_candles_long(sym, "15")
            except Exception:
                c = get_candles(sym, "15", 900)
            closes = [x["c"] for x in c]
            e20, e50, e200 = ema(closes, 20), ema(closes, 50), ema(closes, 200)
            px = closes[-1]
            if e50[-1] > e200[-1] and px > e50[-1]:
                trend = "Bullish"
            elif e50[-1] < e200[-1] and px < e50[-1]:
                trend = "Bearish"
            else:
                trend = "Mixed"
            dist = (px - e200[-1]) / e200[-1] * 100
            pos = "UPAR ⬆️" if px > e200[-1] else "NEECHE ⬇️"
            start = min(200, len(c) - 2)  # EMA 200 warm-up ke baad hi cross gino
            lines.append(
                f"\n{sym}: {trend} | Price {px:.2f}\n"
                f"EMA 20: {e20[-1]:.2f} | EMA 50: {e50[-1]:.2f} | EMA 200: {e200[-1]:.2f}\n"
                f"Price EMA 200 se {pos} ({dist:+.2f}%)\n"
                f"Last 20/50 cross: {last_cross(c, e20, e50, 60)}\n"
                f"Last 50/200 cross: {last_cross(c, e50, e200, start)}\n"
                f"Last price/EMA 200 cross: {last_cross(c, closes, e200, start)}")
        except Exception as ex:
            lines.append(f"{sym}: error {ex}")
    return "\n".join(lines)

def ema200_break(c):
    # 15m candle EMA 200 ke aar-paar close hui?
    if len(c) < 210:
        return None
    closes = [x["c"] for x in c]
    e200 = ema(closes, 200)
    if closes[-2] <= e200[-2] and closes[-1] > e200[-1]:
        return "UPAR", e200[-1]
    if closes[-2] >= e200[-2] and closes[-1] < e200[-1]:
        return "NEECHE", e200[-1]
    return None

# ---------- trade tracking + daily summary ----------
def load_state():
    try:
        with open(STATE_FILE) as f:
            st = json.load(f)
    except Exception:
        st = {}
    st.setdefault("trades", [])
    st.setdefault("day", datetime.now(DXB).strftime("%Y-%m-%d"))
    st.setdefault("stats", {})
    return st

def save_state(st):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(st, f, indent=1)
    except Exception as ex:
        print("state save error:", ex, flush=True)

def bump(st, key, field):
    d = st["stats"].setdefault(key, {"signals": 0, "filled": 0, "tp1": 0,
                                     "tp2": 0, "tp3": 0, "sl": 0, "cancel": 0})
    d[field] += 1

def summary_text(day, stats):
    if not stats:
        return f"📅 Daily summary {day}\nKoi signal nahi aaya."
    lines = [f"📅 Daily summary {day}"]
    tot = {"signals": 0, "tp1": 0, "sl": 0}
    for key in sorted(stats):
        d = stats[key]
        for k in tot:
            tot[k] += d[k]
        done = d["tp1"] + d["sl"]
        wr = f"{100 * d['tp1'] / done:.0f}%" if done else "-"
        lines.append(f"{key}: {d['signals']} signal | TP1 {d['tp1']} TP2 {d['tp2']} "
                     f"TP3 {d['tp3']} | SL {d['sl']} | cancel {d['cancel']} | win {wr}")
    done = tot["tp1"] + tot["sl"]
    wr = f"{100 * tot['tp1'] / done:.0f}%" if done else "-"
    lines.append(f"\nTotal: {tot['signals']} signal, win rate {wr} (TP1 pehle hit = win)")
    return "\n".join(lines)

def check_day(st):
    today = datetime.now(DXB).strftime("%Y-%m-%d")
    if today != st["day"]:
        send(summary_text(st["day"], st["stats"]), wa=True)
        st["day"], st["stats"] = today, {}
        save_state(st)

def track(st, sym, c1):
    # 1m candles se har open trade ka TP/SL check
    keep = []
    for t in st["trades"]:
        if t["sym"] != sym:
            keep.append(t)
            continue
        key = f"{sym} {t['tf']}m"
        head = f"{sym} ({t['tf']}m) {t['side']} @ {t['entry']:.2f}"
        buy = t["side"] == "BUY"
        alive = True
        for x in c1:
            if x["t"] <= t["chk"]:
                continue
            t["chk"] = x["t"]
            if not t["filled"]:
                if (x["l"] <= t["entry"]) if buy else (x["h"] >= t["entry"]):
                    t["filled"] = True
                    bump(st, key, "filled")
                    send(f"📥 Limit fill: {head}")
                elif x["t"] > t["expire"]:
                    bump(st, key, "cancel")
                    send(f"⌛ Cancel (fill nahi hua): {head}")
                    alive = False
                    break
                else:
                    continue
            # SL pehle check (ek hi candle me dono ho to SL maana)
            if (x["l"] <= t["sl"]) if buy else (x["h"] >= t["sl"]):
                if t["hit"] == 0:
                    bump(st, key, "sl")
                    send(f"🛑 SL hit: {head}", wa=True)
                else:
                    send(f"↩️ TP{t['hit']} ke baad SL hit: {head}", wa=True)
                alive = False
                break
            for n in range(t["hit"] + 1, 4):
                tp = t["tps"][n - 1]
                if (x["h"] >= tp) if buy else (x["l"] <= tp):
                    t["hit"] = n
                    bump(st, key, f"tp{n}")
                    send(f"✅ TP{n} hit: {head} → {tp:.2f}", wa=True)
            if t["hit"] == 3:
                alive = False
                break
        if alive:
            keep.append(t)
    st["trades"] = keep

def sess_times(tzname, oh, ch, now):
    # aaj ka open/close (session city ke time me), Dubai time me convert
    loc = now.astimezone(ZoneInfo(tzname))
    op = loc.replace(hour=oh, minute=0, second=0, microsecond=0)
    cl = loc.replace(hour=ch, minute=0, second=0, microsecond=0)
    return op, cl

def active_sessions(now=None):
    now = now or datetime.now(timezone.utc)
    act = []
    for name, tz, oh, ch in SESSIONS:
        op, cl = sess_times(tz, oh, ch, now)
        if op.weekday() < 5 and op <= now < cl:
            act.append(name)
    return act

def in_session():
    a = active_sessions()
    return "London" in a or "New York" in a

def dxb(t):
    return t.astimezone(DXB).strftime("%I:%M %p").lstrip("0")

def sessions_text():
    now = datetime.now(timezone.utc)
    lines = ["🕒 Sessions (Dubai time)"]
    for name, tz, oh, ch in SESSIONS:
        op, cl = sess_times(tz, oh, ch, now)
        on = "🟢 chalu" if name in active_sessions(now) else "⚪ band"
        lines.append(f"{SESS_FLAG[name]} {name}: {dxb(op)} - {dxb(cl)}  {on}")
    if now.astimezone(DXB).weekday() >= 5:
        lines.append("Weekend: forex sessions band (crypto chalu)")
    return "\n".join(lines)

def session_alerts(st):
    if not SESSION_ALERTS:
        return
    now = datetime.now(timezone.utc)
    sent = st.setdefault("sess_sent", [])
    for name, tz, oh, ch in SESSIONS:
        op, cl = sess_times(tz, oh, ch, now)
        if op.weekday() >= 5:
            continue
        day = op.strftime("%Y-%m-%d")
        for kind, t in (("open", op), ("close", cl)):
            k = f"{name}|{day}|{kind}"
            # boundary ke 20 min ke andar hi alert (restart pe purana alert na aaye)
            if k in sent or not (t <= now < t + timedelta(minutes=20)):
                continue
            sent.append(k)
            if kind == "open":
                act = [a for a in active_sessions(now) if a != name]
                ov = f"\nOverlap: {name} + {', '.join(act)} (volume zyada)" if act else ""
                send(f"🔔 {SESS_FLAG[name]} {name} session SHURU\n"
                     f"Time: {dxb(op)} (Dubai)\nBand hoga: {dxb(cl)}{ov}")
            else:
                act = active_sessions(now)
                nx = f"\nAbhi chalu: {', '.join(act)}" if act else "\nAbhi koi bada session chalu nahi"
                send(f"🔕 {SESS_FLAG[name]} {name} session BAND\n"
                     f"Time: {dxb(cl)} (Dubai){nx}")
    st["sess_sent"] = sent[-30:]

def main():
    start = time.time()
    print("bot started", flush=True)
    st = load_state()
    send("✅ Crypto bot chalu hai: BTC + Gold (1m, 5m, 15m)\n\n"
         + status_15m() + "\n\n" + sessions_text()
         + ("\n\nWhatsApp: ON" if WA_PHONE and WA_APIKEY else "\n\nWhatsApp: OFF (secrets nahi mile)"))
    seen = {}
    htf = {}  # sym -> 15m trend "BUY"/"SELL"
    last_save = time.time()
    while time.time() - start < RUN_SECONDS:
        check_day(st)
        session_alerts(st)
        for sym in SYMBOLS:
            for tf, sec in TFS.items():
                try:
                    c = get_candles(sym, tf, sec)
                    if tf == "1" and c:
                        track(st, sym, c)
                    key = (sym, tf)
                    if not c or seen.get(key) == c[-1]["t"]:
                        continue
                    seen[key] = c[-1]["t"]
                    if tf == "15":
                        br = ema200_break(c)
                        if br:
                            send(f"⚡ {sym} (15m): price EMA 200 ke {br[0]} close hua\n"
                                 f"Close: {c[-1]['c']:.2f} | EMA 200: {br[1]:.2f}\n"
                                 f"{'Trend bullish ho sakta hai' if br[0] == 'UPAR' else 'Trend bearish ho sakta hai'}", wa=True)
                        cl = [x["c"] for x in c]
                        htf[sym] = "BUY" if ema(cl, 20)[-1] > ema(cl, 50)[-1] else "SELL"
                    sigs = []
                    ob = find_ob(c, tf)
                    if ob:
                        sigs.append(ob)
                    sigs += find_cross(c, tf)
                    if tf in ("1", "5") and sigs:
                        # sideways market me alert nahi
                        if adx_last(c) < ADX_MIN:
                            sigs = []
                        if SESSION_FILTER and not in_session():
                            sigs = []
                        if HTF_FILTER and sym in htf:
                            sigs = [s for s in sigs if s[1] == htf[sym]]
                    label = tf + "m"
                    close_t = c[-1]["t"] + sec
                    for kind, side, e, sl, tp, extra in sigs:
                        msg = (f"{'🟢' if side == 'BUY' else '🔴'} {side} {sym} ({label})\n"
                               f"Type: {kind}\n"
                               f"{'Entry (limit)' if kind == 'OB Retest' else 'Entry'}: {e:.2f}\nSL: {sl:.2f}\n"
                               f"TP1: {tp[0]:.2f}\nTP2: {tp[1]:.2f}\n"
                               f"TP3: {tp[2]:.2f}")
                        if extra:
                            msg += "\n" + extra
                        if tf in ("1", "5") and HTF_FILTER:
                            msg += f"\n15m trend: {side} ✔"
                        send(msg, wa=True)
                        limit = kind == "OB Retest"
                        st["trades"].append({
                            "sym": sym, "tf": tf, "side": side, "entry": e,
                            "sl": sl, "tps": tp, "hit": 0, "filled": not limit,
                            "chk": close_t - 60,
                            "expire": close_t + LIMIT_EXPIRE * sec})
                        bump(st, f"{sym} {tf}m", "signals")
                        if not limit:
                            bump(st, f"{sym} {tf}m", "filled")
                except Exception as ex:
                    print(sym, tf, "error:", ex, flush=True)
        if time.time() - last_save > 300:
            save_state(st)
            last_save = time.time()
        time.sleep(10)
    save_state(st)

main()
end = time.time() + 180
while (_q.unfinished_tasks or _nq.unfinished_tasks or _wq.unfinished_tasks) and time.time() < end:
    time.sleep(1)
