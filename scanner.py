import os, time, traceback, requests, pyotp
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from SmartApi import SmartConnect

# ================= SETTINGS (sirf yahan badlav karein) =================
WATCHLIST  = ["LODHA"]       # NSE symbols, jaise ["LODHA", "RELIANCE", "TCS"]
TIMEFRAMES = [5, 15]         # minutes me: 1, 3, 5, 10, 15, 30
FAST, SLOW = 9, 21           # EMA periods
# =======================================================================

IST = ZoneInfo("Asia/Kolkata")
INTERVALS = {1: "ONE_MINUTE", 3: "THREE_MINUTE", 5: "FIVE_MINUTE",
             10: "TEN_MINUTE", 15: "FIFTEEN_MINUTE", 30: "THIRTY_MINUTE"}
HISTORY_DAYS = {1: 3, 3: 5, 5: 7, 10: 10, 15: 15, 30: 25}
MAX_RUN_MIN = 345
SCRIP_URL = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"


def secret(name):
    # space, nayi line, tab sab hata deta hai
    return "".join(os.environ.get(name, "").split())


API_KEY   = secret("ANGEL_API_KEY")
CLIENT    = secret("ANGEL_CLIENT_CODE").upper()
PIN       = secret("ANGEL_PIN")
TOTP_KEY  = secret("ANGEL_TOTP_SECRET").upper()
TG_TOKEN  = secret("TG_BOT_TOKEN")
TG_CHAT   = secret("TG_CHAT_ID")


def tg(text):
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                      data={"chat_id": TG_CHAT, "text": text[:4000]}, timeout=15)
    except Exception as e:
        print("Telegram error:", e)


def login():
    api = SmartConnect(api_key=API_KEY)
    res = api.generateSession(CLIENT, PIN, pyotp.TOTP(TOTP_KEY).now())
    if not res or not res.get("status"):
        raise RuntimeError(f"Angel One login failed: {res}")
    return api


def get_tokens():
    data = requests.get(SCRIP_URL, timeout=90).json()
    want = {f"{s.upper()}-EQ": s.upper() for s in WATCHLIST}
    return {want[r["symbol"]]: r["token"] for r in data
            if r.get("exch_seg") == "NSE" and r.get("symbol") in want}


def ema(values, n):
    k, out, e = 2 / (n + 1), [], None
    for v in values:
        e = v if e is None else v * k + e * (1 - k)
        out.append(e)
    return out


def candles(api, token, tf):
    now = datetime.now(IST)
    res = api.getCandleData({
        "exchange": "NSE", "symboltoken": token, "interval": INTERVALS[tf],
        "fromdate": (now - timedelta(days=HISTORY_DAYS[tf])).strftime("%Y-%m-%d %H:%M"),
        "todate": now.strftime("%Y-%m-%d %H:%M")})
    rows = (res or {}).get("data") or []
    out = []
    for r in rows:
        t = datetime.fromisoformat(r[0])
        if t + timedelta(minutes=tf) <= now:      # sirf poori bani candles
            out.append((t, float(r[4])))
    return out


def check(api, sym, token, tf, seen, startup=False):
    c = candles(api, token, tf)
    if len(c) < SLOW + 5:
        return None
    closes = [x[1] for x in c]
    f, s = ema(closes, FAST), ema(closes, SLOW)
    t = c[-1][0]
    signal = None
    if f[-2] <= s[-2] and f[-1] > s[-1]:
        signal = f"🟢 BULLISH: EMA{FAST} ne EMA{SLOW} ko upar cross kiya"
    elif f[-2] >= s[-2] and f[-1] < s[-1]:
        signal = f"🔴 BEARISH: EMA{FAST} ne EMA{SLOW} ko neeche cross kiya"
    key = (sym, tf, t)
    if signal and key not in seen:
        seen.add(key)
        if not startup:
            tg(f"{signal}\n{sym} | {tf}m candle {t:%d-%b %H:%M}\n"
               f"Close: {closes[-1]:.2f}\nEMA{FAST}: {f[-1]:.2f} | EMA{SLOW}: {s[-1]:.2f}")
    side = ">" if f[-1] > s[-1] else "<"
    return f"{sym} {tf}m: close {closes[-1]:.2f} | EMA{FAST} {f[-1]:.2f} {side} EMA{SLOW} {s[-1]:.2f}"


def main():
    start = time.time()
    tokens = get_tokens()
    missing = [s for s in WATCHLIST if s.upper() not in tokens]
    api = login()
    seen, lines = set(), []
    for sym, tok in tokens.items():
        for tf in TIMEFRAMES:
            try:
                lines.append(check(api, sym, tok, tf, seen, startup=True) or f"{sym} {tf}m: data nahi mila")
            except Exception as e:
                lines.append(f"{sym} {tf}m: error {e}")
            time.sleep(0.5)
    msg = "✅ EMA scanner chalu ho gaya\n" + "\n".join(lines)
    if missing:
        msg += "\n⚠️ Ye symbol nahi mile: " + ", ".join(missing)
    tg(msg)

    while time.time() - start < MAX_RUN_MIN * 60:
        now = datetime.now(IST)
        mopen = now.replace(hour=9, minute=15, second=0, microsecond=0)
        mclose = now.replace(hour=15, minute=30, second=0, microsecond=0)
        if now.weekday() >= 5 or now > mclose + timedelta(minutes=2):
            break
        nxt = (now + timedelta(minutes=1)).replace(second=5, microsecond=0)
        time.sleep(max(1, (nxt - datetime.now(IST)).total_seconds()))
        now = datetime.now(IST)
        if now < mopen + timedelta(minutes=1):
            continue
        mins = int((now - mopen).total_seconds() // 60)
        for tf in TIMEFRAMES:
            if mins % tf:
                continue
            for sym, tok in tokens.items():
                try:
                    check(api, sym, tok, tf, seen)
                except Exception as e:
                    print("Error", sym, tf, e)
                    try:
                        api = login()
                    except Exception as e2:
                        print("Re-login failed:", e2)
                time.sleep(0.5)
    print("Scanner band.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        tg(f"❌ EMA scanner error:\n{e}")
        traceback.print_exc()
        raise
