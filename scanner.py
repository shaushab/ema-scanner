import os, io, json, time, html, traceback, requests, pyotp
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import quote
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from SmartApi import SmartConnect

# ================= SETTINGS (sirf yahan badlav karein) =================
FIXED       = ["LODHA"]      # ye shares hamesha scan honge
TOP_N       = 5              # kitne top gainers aur kitne top losers
TIMEFRAMES  = [5, 15]        # minutes me: 1, 3, 5, 10, 15, 30
FAST, SLOW  = 9, 21          # EMA periods
RR          = 2              # target = risk x 2  (1:2)
REFRESH_MIN = 30             # gainers/losers list har kitne minute me update ho
BIG_EMOJI   = True           # alert se pehle bada 🚀 / 🔻 emoji
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
TG_URL   = f"https://api.telegram.org/bot{TG_TOKEN}"


# ----------------------------- TELEGRAM -----------------------------
def tg(text, as_html=False, reply_to=None, buttons=None):
    """Text message bhejo (lamba ho toh hisson me). message_id return karta hai."""
    parts, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > 3500:
            parts.append(cur)
            cur = ""
        cur += line + "\n"
    if cur.strip():
        parts.append(cur)
    mid = None
    for i, p in enumerate(parts):
        data = {"chat_id": TG_CHAT, "text": p, "disable_web_page_preview": True}
        if as_html:
            data["parse_mode"] = "HTML"
        if reply_to:
            data["reply_to_message_id"] = reply_to
            data["allow_sending_without_reply"] = True
        if buttons and i == len(parts) - 1:
            data["reply_markup"] = json.dumps({"inline_keyboard": buttons})
        try:
            r = requests.post(f"{TG_URL}/sendMessage", data=data, timeout=15).json()
            if not r.get("ok") and as_html:              # HTML galat ho toh plain text bhejo
                data.pop("parse_mode", None)
                r = requests.post(f"{TG_URL}/sendMessage", data=data, timeout=15).json()
            mid = mid or (r.get("result") or {}).get("message_id")
        except Exception as e:
            print("Telegram error:", e)
    return mid


def tg_photo(png, caption, buttons=None):
    data = {"chat_id": TG_CHAT, "caption": caption[:1024], "parse_mode": "HTML"}
    if buttons:
        data["reply_markup"] = json.dumps({"inline_keyboard": buttons})
    try:
        r = requests.post(f"{TG_URL}/sendPhoto", data=data,
                          files={"photo": ("chart.png", png, "image/png")}, timeout=30).json()
        if r.get("ok"):
            return r["result"]["message_id"]
        print("sendPhoto failed:", r)
    except Exception as e:
        print("Telegram photo error:", e)
    return tg(caption, as_html=True, buttons=buttons)     # photo fail ho toh text bhejo


# ----------------------------- ANGEL ONE -----------------------------
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
            out.append((t, float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])))
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


# ----------------------------- INDICATORS -----------------------------
def ema(values, n):
    k, out, e = 2 / (n + 1), [], None
    for v in values:
        e = v if e is None else v * k + e * (1 - k)
        out.append(e)
    return out


def rsi(values, n=14):
    if len(values) <= n:
        return 50.0
    gains = [max(values[i] - values[i - 1], 0) for i in range(1, len(values))]
    losses = [max(values[i - 1] - values[i], 0) for i in range(1, len(values))]
    ag, al = sum(gains[:n]) / n, sum(losses[:n]) / n
    for g, l in zip(gains[n:], losses[n:]):
        ag, al = (ag * (n - 1) + g) / n, (al * (n - 1) + l) / n
    return 100.0 if al == 0 else 100 - 100 / (1 + ag / al)


def tv_link(sym):
    return f"https://www.tradingview.com/chart/?symbol=NSE:{sym.replace('&', '_').replace('-', '_')}"


# ----------------------------- CHART -----------------------------
def make_chart(sym, tf, c, f, s, side, entry, sl, tgt):
    n = min(60, len(c))
    c, f, s = c[-n:], f[-n:], s[-n:]
    bg, up, dn = "#0f1420", "#26a69a", "#ef5350"
    fig, ax = plt.subplots(figsize=(8, 4.6), dpi=110)
    fig.patch.set_facecolor(bg)
    ax.set_facecolor(bg)
    for i, (t, o, h, l, cl, v) in enumerate(c):
        col = up if cl >= o else dn
        ax.vlines(i, l, h, color=col, linewidth=1)
        ax.add_patch(plt.Rectangle((i - 0.35, min(o, cl)), 0.7, max(abs(cl - o), 1e-6),
                                   color=col))
    x = list(range(n))
    ax.plot(x, f, color="#4fc3f7", linewidth=1.6, label=f"EMA {FAST}")
    ax.plot(x, s, color="#ffd54f", linewidth=1.6, label=f"EMA {SLOW}")
    last = n - 1
    rng = max(h for _, _, h, _, _, _ in c) - min(l for _, _, _, l, _, _ in c)
    if side == "BUY":
        ax.annotate("BUY", xy=(last, c[-1][3]), xytext=(last, c[-1][3] - rng * 0.3),
                    color="white", fontsize=10, fontweight="bold", ha="center",
                    arrowprops=dict(facecolor=up, edgecolor=up, width=6, headwidth=16),
                    bbox=dict(boxstyle="round", fc=up, ec=up))
    else:
        ax.annotate("SELL", xy=(last, c[-1][2]), xytext=(last, c[-1][2] + rng * 0.3),
                    color="white", fontsize=10, fontweight="bold", ha="center",
                    arrowprops=dict(facecolor=dn, edgecolor=dn, width=6, headwidth=16),
                    bbox=dict(boxstyle="round", fc=dn, ec=dn))
    for y, col, lab in ((entry, "#90caf9", "Entry"), (sl, dn, "SL"), (tgt, up, "Target")):
        ax.hlines(y, last - 12, last + 6, colors=col, linestyles="--", linewidth=1.2)
        ax.text(last + 6.3, y, f"{lab} {y:.2f}", color=col, fontsize=8, va="center")
    ax.set_xlim(-1, last + 14)
    ticks = list(range(0, n, max(1, n // 6)))
    ax.set_xticks(ticks)
    ax.set_xticklabels([c[i][0].strftime("%H:%M") for i in ticks], color="#9aa4b2", fontsize=8)
    ax.tick_params(axis="y", colors="#9aa4b2", labelsize=8)
    for sp in ax.spines.values():
        sp.set_color("#2a3142")
    ax.grid(color="#1e2533", linewidth=0.6)
    ax.set_title(f"{sym} • {tf}m • NSE   EMA {FAST}/{SLOW} crossover",
                 color="white", fontsize=11, loc="left")
    ax.legend(loc="upper left", facecolor=bg, edgecolor="#2a3142", labelcolor="white", fontsize=8)
    buf = io.BytesIO()
    fig.tight_layout()
    fig.savefig(buf, format="png", facecolor=bg)
    plt.close(fig)
    return buf.getvalue()


# ----------------------------- SIGNALS -----------------------------
def check(api, sym, token, tf, seen, trades):
    hs = html.escape(sym)
    c = candles(api, token, tf)
    if len(c) < SLOW + 5:
        return
    closes = [x[4] for x in c]
    f, s = ema(closes, FAST), ema(closes, SLOW)

    # --- khule trades ka target / SL check ---
    for tr in trades:
        if tr["sym"] != sym or tr["tf"] != tf or tr["status"] != "OPEN":
            continue
        for t, o, h, l, cl, v in c:
            if t <= tr["t"]:
                continue
            buy = tr["side"] == "BUY"
            if (buy and l <= tr["sl"]) or (not buy and h >= tr["sl"]):
                tr["status"] = "SL"
            elif (buy and h >= tr["tgt"]) or (not buy and l <= tr["tgt"]):
                tr["status"] = "TARGET"
            if tr["status"] != "OPEN":
                if tr["status"] == "TARGET":
                    msg = (f"🎯 <b>TARGET HIT!</b> {hs} {tf}m\n"
                           f"Entry {tr['entry']:.2f} → Target {tr['tgt']:.2f}\n"
                           f"✅ Profit: {abs(tr['tgt'] - tr['entry']):.2f} / share")
                else:
                    msg = (f"🛑 <b>STOP LOSS HIT</b> {hs} {tf}m\n"
                           f"Entry {tr['entry']:.2f} → SL {tr['sl']:.2f}\n"
                           f"❌ Loss: {abs(tr['entry'] - tr['sl']):.2f} / share")
                tg(msg, as_html=True, reply_to=tr["mid"])
                break
        if tr["status"] == "OPEN":
            opp = (tr["side"] == "BUY" and f[-1] < s[-1]) or (tr["side"] == "SELL" and f[-1] > s[-1])
            if opp:
                tr["status"] = "EXIT"
                pnl = (closes[-1] - tr["entry"]) * (1 if tr["side"] == "BUY" else -1)
                tg(f"⚠️ <b>EXIT SIGNAL</b> {hs} {tf}m\nUlta crossover ho gaya\n"
                   f"Entry {tr['entry']:.2f} → Abhi {closes[-1]:.2f}  ({pnl:+.2f} / share)",
                   as_html=True, reply_to=tr["mid"])

    # --- naya crossover ---
    t, o, h, l, cl, vol = c[-1]
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
        side, sl = "BUY", min(l, cl - min_risk)
        tgt = cl + RR * (cl - sl)
    else:
        side, sl = "SELL", max(h, cl + min_risk)
        tgt = cl - RR * (sl - cl)

    # --- signal strength ---
    r = rsi(closes)
    vols = [x[5] for x in c[-21:-1]]
    vol_x = vol / (sum(vols) / len(vols)) if vols and sum(vols) > 0 else 1
    e200 = ema(closes, 200)[-1] if len(closes) >= 200 else None
    stars, notes = 1, []
    if e200:
        if (bull and cl > e200) or (bear and cl < e200):
            stars += 1
            notes.append("✅ EMA200 trend ke saath")
        else:
            notes.append("⚠️ EMA200 trend ke ulta")
    if vol_x >= 1.5:
        stars += 1
        notes.append(f"✅ Volume {vol_x:.1f}x zyada")
    if (bull and 50 <= r <= 70) or (bear and 30 <= r <= 50):
        stars += 1
        notes.append(f"✅ RSI {r:.0f} sahi zone me")
    else:
        notes.append(f"ℹ️ RSI {r:.0f}")
    star_txt = "⭐" * stars + "☆" * (4 - stars)

    risk = abs(cl - sl)
    pct = lambda a: abs(a - cl) / cl * 100
    if bull:
        head = "🟢🚀 <b>BUY SIGNAL</b> ⬆️"
        line = f"EMA{FAST} ne EMA{SLOW} ko ⬆️ upar cross kiya"
    else:
        head = "🔴🔻 <b>SELL SIGNAL</b> ⬇️"
        line = f"EMA{FAST} ne EMA{SLOW} ko ⬇️ neeche cross kiya"
    caption = (f"{head}\n"
               f"<b>{hs}</b> • {tf}m • {t:%d-%b %H:%M}\n"
               f"{line}\n\n"
               f"💰 Entry:  <code>{cl:.2f}</code>\n"
               f"🛑 SL:     <code>{sl:.2f}</code>  (-{pct(sl):.2f}%)\n"
               f"🎯 Target: <code>{tgt:.2f}</code>  (+{pct(tgt):.2f}%)\n"
               f"⚖️ Risk:Reward = 1:{RR}  |  Risk {risk:.2f}/share\n\n"
               f"Strength: {star_txt}\n" + "\n".join(notes) +
               "\n\n<i>Sirf technical signal hai, advice nahi.</i>")
    share_txt = (f"{'🟢 BUY' if bull else '🔴 SELL'} {sym} ({tf}m)\n"
                 f"Entry {cl:.2f} | SL {sl:.2f} | Target {tgt:.2f}\n"
                 f"EMA {FAST}/{SLOW} crossover • {t:%d-%b %H:%M}")
    buttons = [[{"text": "📤 Share Signal",
                 "url": f"https://t.me/share/url?url={quote(tv_link(sym))}&text={quote(share_txt)}"},
                {"text": "📈 Chart dekho", "url": tv_link(sym)}]]

    if BIG_EMOJI:
        tg("🚀" if bull else "🔻")
    try:
        png = make_chart(sym, tf, c, f, s, side, cl, sl, tgt)
        mid = tg_photo(png, caption, buttons)
    except Exception as e:
        print("Chart error:", e)
        mid = tg(caption, as_html=True, buttons=buttons)
    trades.append({"sym": sym, "tf": tf, "side": side, "entry": cl, "sl": sl,
                   "tgt": tgt, "t": t, "mid": mid, "status": "OPEN"})


def build_watch(api, tokens, title):
    g, l = movers(api, tokens)
    watch = [s for s in FIXED if s in tokens] + [x[0] for x in g] + [x[0] for x in l]
    watch = list(dict.fromkeys(watch))
    msg = f"{title}\n📌 <b>Fixed:</b> {html.escape(', '.join(FIXED))}\n\n📈 <b>Top Gainers</b>\n"
    msg += "\n".join(f"🟢 {html.escape(s)}  <code>{p:+.2f}%</code>" for s, p in g)
    msg += "\n\n📉 <b>Top Losers</b>\n" + "\n".join(f"🔴 {html.escape(s)}  <code>{p:+.2f}%</code>" for s, p in l)
    return watch, msg


def summary(trades):
    if not trades:
        return "📊 <b>Session Summary</b>\nAaj is session me koi signal nahi aaya."
    cnt = {k: sum(1 for t in trades if t["status"] == k) for k in ("TARGET", "SL", "EXIT", "OPEN")}
    lines = [f"📊 <b>Session Summary</b>  ({len(trades)} signals)",
             f"🎯 Target: {cnt['TARGET']}   🛑 SL: {cnt['SL']}   ⚠️ Exit: {cnt['EXIT']}   ⏳ Open: {cnt['OPEN']}", ""]
    icon = {"TARGET": "🎯", "SL": "🛑", "EXIT": "⚠️", "OPEN": "⏳"}
    for t in trades:
        lines.append(f"{icon[t['status']]} {'🟢' if t['side'] == 'BUY' else '🔴'} {html.escape(t['sym'])} {t['tf']}m "
                     f"@ {t['entry']:.2f} ({t['t']:%H:%M})")
    return "\n".join(lines)


# ----------------------------- MAIN -----------------------------
def main():
    start = time.time()
    tokens = get_tokens(FIXED + UNIVERSE)
    missing = [s for s in FIXED + UNIVERSE if s.upper() not in tokens]
    api = login()
    seen, trades = set(), []
    watch, msg = build_watch(api, tokens, "✅ <b>EMA scanner chalu ho gaya</b>")
    if missing:
        msg += "\n\n⚠️ Ye symbol nahi mile: " + ", ".join(missing)
    tg(msg, as_html=True)
    last_refresh = datetime.now(IST)
    traded_today = False

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
        traded_today = True

        first_open = mopen + timedelta(minutes=5)
        if now >= first_open and (last_refresh < first_open or
                                  now - last_refresh >= timedelta(minutes=REFRESH_MIN)):
            try:
                new_watch, msg = build_watch(api, tokens, "🔄 <b>Gainers/Losers list update</b>")
                if set(new_watch) != set(watch):
                    tg(msg, as_html=True)
                watch = new_watch
            except Exception as e:
                print("Movers update error:", e)
            last_refresh = now

        open_syms = [t["sym"] for t in trades if t["status"] == "OPEN"]
        scan = list(dict.fromkeys(watch + open_syms))
        mins = int((now - mopen).total_seconds() // 60)
        for tf in TIMEFRAMES:
            if mins % tf:
                continue
            for sym in scan:
                try:
                    check(api, sym, tokens[sym], tf, seen, trades)
                except Exception as e:
                    print("Error", sym, tf, e)
                    try:
                        api = login()
                    except Exception as e2:
                        print("Re-login failed:", e2)
                time.sleep(0.5)

    if traded_today:
        tg(summary(trades), as_html=True)
    print("Scanner band.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        tg(f"❌ EMA scanner error:\n{e}")
        traceback.print_exc()
        raise
