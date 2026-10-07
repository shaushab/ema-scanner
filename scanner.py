import os, time, traceback, requests, pyotp
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from SmartApi import SmartConnect

# ================= SETTINGS (sirf yahan badlav karein) =================
FIXED       = ["LODHA"]      # ye shares hamesha scan honge
TOP_N       = 5              # kitne top gainers aur kitne top losers
TIMEFRAMES  = [5, 15]        # minutes me: 1, 3, 5, 10, 15, 30
FAST, SLOW  = 9, 21          # EMA periods
RR          = 2              # target = risk x 2  (1:2)
REFRESH_MIN = 30             # gainers/losers list har kitne minute me update ho
UNIVERSE    = ["ADANIENT", "ADANIPORTS", "APOLLOHOSP", "ASIANPAINT", "AXISBANK", "BAJAJ-AUTO", "BAJFINANCE", "BAJAJFINSV", "BEL", "BHARTIARTL", "BSE", "CIPLA", "COALINDIA", "DRREDDY", "EICHERMOT", "ETERNAL", "GRASIM", "HCLTECH", "HDFCBANK", "HDFCLIFE", "HINDALCO", "HINDUNILVR", "ICICIBANK", "INDIGO", "INFY", "ITC", "JIOFIN", "JSWSTEEL", "KOTAKBANK", "LT", "M&M", "MARUTI", "MAXHEALTH", "NESTLEIND", "NTPC", "ONGC", "POWERGRID", "RELIANCE", "SBILIFE", "SBIN", "SHRIRAMFIN", "SUNPHARMA", "TATACONSUM", "TMPV", "TATASTEEL", "TCS", "TECHM", "TITAN", "TRENT", "ULTRACEMCO"]
# =======================================================================

IST = ZoneInfo("Asia/Kolkata")
INTERVALS = {1: "ONE_MINUTE", 3: "THREE_MINUTE", 5: "FIVE_MINUTE",
             10: "TEN_MINUTE", 15: "FIFTEEN_MINUTE", 30: "THIRTY_MINUTE"}
HISTORY_DAYS = {1: 3, 3: 5, 5: 7, 10: 10, 15: 15, 30: 25}
MAX_RUN_MIN = 345
SCRIP_URL = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"


def secret(name):
    return "".join(os.environ.get(name, "").split())


API_KEY  = secret("ANGEL_API_KEY")
CLIENT   = secret("ANGEL_CLIENT_CODE").upper()
PIN      = secret("ANGEL_PIN")
TOTP_KEY = secret("ANGEL_TOTP_SECRET").upper()
TG_TOKEN = secret("TG_BOT_TOKEN")
TG_CHAT  = secret("TG_CHAT_ID")


def tg(text):
    parts, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > 3500:
            parts.append(cur)
            cur = ""
        cur += line + "\n"
    if cur.strip():
        parts.append(cur)
    for p in parts:
        try:
            requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                          data={"chat_id": TG_CHAT, "text": p}, timeout=15)
        except Exception as e:
            print("Telegram error:", e)


def login():
    api = SmartConnect(api_key=API_KEY)
    res = api.generateSession(CLIENT, PIN, pyotp.TOTP(TOTP_KEY).now())
    if not res or not res.get("status"):
        raise RuntimeError(f"Angel One login failed: {res}")
    return api


def get_tokens(symbols):
    data = requests.get(SCRIP_URL, timeout=90).json()
    want = {f"{s.upper()}-EQ": s.upper() for s in symbols}
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
            out.append((t, float(r[1]), float(r[2]), float(r[3]), float(r[4])))
    return out


def movers(api, tokens):
    """Nifty 50 me se top gainers aur top losers (% change)"""
    pct = {}
    try:
        res = api.getMarketData("FULL", {"NSE": [tokens[s] for s in UNIVERSE if s in tokens]})
        for d in ((res or {}).get("data") or {}).get("fetched") or []:
            sym = str(d.get("tradingSymbol", ""))
            if sym.endswith("-EQ"):
                sym = sym[:-3]
            if sym in UNIVERSE and d.get("percentChange") is not None:
                pct[sym] = float(d["percentChange"])
    except Exception as e:
        print("getMarketData failed:", e)
    if len(pct) < 2 * TOP_N:                      # backup tarika: daily candles
        now = datetime.now(IST)
        for sym in UNIVERSE:
            if sym not in tokens or sym in pct:
                continue
            try:
                r = api.getCandleData({
                    "exchange": "NSE", "symboltoken": tokens[sym], "interval": "ONE_DAY",
                    "fromdate": (now - timedelta(days=10)).strftime("%Y-%m-%d %H:%M"),
                    "todate": now.strftime("%Y-%m-%d %H:%M")})
                rows = (r or {}).get("data") or []
                if len(rows) >= 2:
                    pct[sym] = (float(rows[-1][4]) / float(rows[-2][4]) - 1) * 100
            except Exception as e:
                print("Daily candle error", sym, e)
            time.sleep(0.4)
    ranked = sorted(pct.items(), key=lambda x: x[1], reverse=True)
    return ranked[:TOP_N], list(reversed(ranked[-TOP_N:]))


def check(api, sym, token, tf, seen):
    c = candles(api, token, tf)
    if len(c) < SLOW + 5:
        return
    closes = [x[4] for x in c]
    f, s = ema(closes, FAST), ema(closes, SLOW)
    t, o, h, l, cl = c[-1]
    now = datetime.now(IST)
    if now - (t + timedelta(minutes=tf)) > timedelta(minutes=3):
        return                                    # purani candle, alert nahi
    bull = f[-2] <= s[-2] and f[-1] > s[-1]
    bear = f[-2] >= s[-2] and f[-1] < s[-1]
    key = (sym, tf, t)
    if not (bull or bear) or key in seen:
        return
    seen.add(key)
    min_risk = cl * 0.003                         # risk kam se kam 0.3%
    if bull:
        sl = min(l, cl - min_risk)
        tgt = cl + RR * (cl - sl)
        head = f"🟢 BULLISH (BUY): EMA{FAST} ne EMA{SLOW} ko upar cross kiya"
    else:
        sl = max(h, cl + min_risk)
        tgt = cl - RR * (sl - cl)
        head = f"🔴 BEARISH (SELL): EMA{FAST} ne EMA{SLOW} ko neeche cross kiya"
    tg(f"{head}\n{sym} | {tf}m candle {t:%d-%b %H:%M}\n"
       f"Entry: {cl:.2f}\nStop Loss: {sl:.2f}\nTarget (1:{RR}): {tgt:.2f}")


def build_watch(api, tokens, title):
    g, l = movers(api, tokens)
    watch = [s for s in FIXED if s in tokens] + [x[0] for x in g] + [x[0] for x in l]
    watch = list(dict.fromkeys(watch))
    msg = f"{title}\n📌 Fixed: {', '.join(FIXED)}\n\n📈 Top Gainers:\n"
    msg += "\n".join(f"{s} {p:+.2f}%" for s, p in g)
    msg += "\n\n📉 Top Losers:\n" + "\n".join(f"{s} {p:+.2f}%" for s, p in l)
    return watch, msg


def main():
    start = time.time()
    tokens = get_tokens(FIXED + UNIVERSE)
    missing = [s for s in FIXED + UNIVERSE if s.upper() not in tokens]
    api = login()
    seen = set()
    watch, msg = build_watch(api, tokens, "✅ EMA scanner chalu ho gaya")
    if missing:
        msg += "\n\n⚠️ Ye symbol nahi mile: " + ", ".join(missing)
    tg(msg)
    last_refresh = datetime.now(IST)

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

        # gainers/losers list update (9:20 ke baad, phir har REFRESH_MIN minute)
        first_open = mopen + timedelta(minutes=5)
        if now >= first_open and (last_refresh < first_open or
                                  now - last_refresh >= timedelta(minutes=REFRESH_MIN)):
            try:
                new_watch, msg = build_watch(api, tokens, "🔄 Gainers/Losers list update")
                if set(new_watch) != set(watch):
                    tg(msg)
                watch = new_watch
            except Exception as e:
                print("Movers update error:", e)
            last_refresh = now

        mins = int((now - mopen).total_seconds() // 60)
        for tf in TIMEFRAMES:
            if mins % tf:
                continue
            for sym in watch:
                try:
                    check(api, sym, tokens[sym], tf, seen)
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
