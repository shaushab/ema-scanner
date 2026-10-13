import os, io, sys, json, time, html, traceback, requests, pyotp
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import quote
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from SmartApi import SmartConnect

sys.stdout.reconfigure(line_buffering=True)   # logs turant GitHub pe dikhen

# ================= SETTINGS (sirf yahan badlav karein) =================
FIXED       = ["LODHA"]      # ye shares hamesha scan honge
TOP_N       = 5              # kitne top gainers aur kitne top losers
TIMEFRAMES  = [5]            # minutes me: 1, 3, 5, 10, 15, 30
FAST, SLOW  = 9, 21          # EMA periods
RR          = 2              # Target 2 = risk x 2  (1:2); Target 1 = 1:1
MIN_RISK_PCT = 0.15          # SL kam se kam itna % door
MAX_RISK_PCT = 0.5           # SL zyada se zyada itna % door (target realistic rahe)
REFRESH_MIN = 30             # gainers/losers list har kitne minute me update ho
BIG_EMOJI   = True           # alert se pehle bada 🚀 / 🔻 emoji
MAX_LATE    = 20             # data late aaye toh bhi itne minute tak ka crossover pakdo
STRATEGY_EMA      = True     # EMA 9/21 crossover signals
STRATEGY_REVERSAL = True     # Reversal (bottom/top) signals
RSI_LOW, RSI_HIGH = 30, 70   # oversold / overbought
REV_LOOKBACK = 6             # pichhli kitni candles me RSI oversold/overbought dekha jaye
MAX_REV_RISK_PCT = 0.8       # reversal ka SL (swing low/high) isse door ho toh signal skip
TRADE_FROM, TRADE_TILL = "09:25", "15:00"   # sirf is time ke beech naye signals
STRATEGY_SMC      = True     # Smart Money Concepts (sweep + CHoCH + FVG/OB) signals
SMC_FROM, SMC_TILL = "09:30", "14:45"       # SMC plans sirf is time me
SMC_SWEEP_LOOKBACK = 15      # CHoCH se pehle kitni candles me liquidity sweep dhundhe
SMC_EXPIRY = 6               # plan ke baad kitni candles tak entry zone ka intezaar
MAX_SMC_RISK_PCT = 0.8       # SMC SL isse door ho toh plan skip
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
    params = {"exchange": "NSE", "symboltoken": token, "interval": INTERVALS[tf],
              "fromdate": (now - timedelta(days=HISTORY_DAYS[tf])).strftime("%Y-%m-%d %H:%M"),
              "todate": now.strftime("%Y-%m-%d %H:%M")}
    res = None
    for attempt in range(2):
        try:
            res = api.getCandleData(params)
            if res and res.get("status") is False:
                raise RuntimeError(f"{res.get('errorcode')} {res.get('message')}")
            break
        except Exception:
            if attempt == 1:
                raise
            time.sleep(2)
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


def rsi_series(values, n=14):
    out = [50.0] * len(values)
    if len(values) <= n:
        return out
    gains = [0.0] + [max(values[i] - values[i - 1], 0) for i in range(1, len(values))]
    losses = [0.0] + [max(values[i - 1] - values[i], 0) for i in range(1, len(values))]
    ag, al = sum(gains[1:n + 1]) / n, sum(losses[1:n + 1]) / n
    out[n] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    for i in range(n + 1, len(values)):
        ag, al = (ag * (n - 1) + gains[i]) / n, (al * (n - 1) + losses[i]) / n
        out[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


def vwap_today(c, idx):
    d = c[idx][0].date()
    pv = vv = 0.0
    for t, o, h, l, cl, v in c[:idx + 1]:
        if t.date() == d:
            pv += (h + l + cl) / 3 * v
            vv += v
    return pv / vv if vv else c[idx][4]


def in_trade_time(t, tf):
    hm_from = tuple(map(int, TRADE_FROM.split(":")))
    hm_till = tuple(map(int, TRADE_TILL.split(":")))
    close_t = t + timedelta(minutes=tf)
    return (t.hour, t.minute) >= hm_from and (close_t.hour, close_t.minute) <= hm_till


LAST_REV = {}


def tv_link(sym):
    return f"https://www.tradingview.com/chart/?symbol=NSE:{sym.replace('&', '_').replace('-', '_')}"


# ----------------------------- CHART -----------------------------
def make_chart(sym, tf, c, f, s, side, entry, sl, t1, tgt, title=None):
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
    for y, col, lab in ((entry, "#90caf9", "Entry"), (sl, dn, "SL"), (t1, "#80cbc4", "T1"), (tgt, up, "T2")):
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
    ax.set_title(f"{sym} • {tf}m • NSE   " + (title or f"EMA {FAST}/{SLOW} crossover"),
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

    # --- khule trades ka T1 / T2 / SL check ---
    for tr in trades:
        if tr["sym"] != sym or tr["tf"] != tf or tr["status"] != "OPEN":
            continue
        buy = tr["side"] == "BUY"
        for t, o, h, l, cl, v in c:
            if t <= tr["last"]:
                continue
            tr["last"] = t
            hit_sl = (buy and l <= tr["sl"]) or (not buy and h >= tr["sl"])
            hit_t1 = (buy and h >= tr["t1"]) or (not buy and l <= tr["t1"])
            hit_t2 = (buy and h >= tr["tgt"]) or (not buy and l <= tr["tgt"])
            if hit_sl:
                if tr["t1hit"]:
                    tr["status"] = "BE"
                    tg(f"🔒 <b>BREAKEVEN EXIT</b> {hs} {tf}m\nT1 ke baad price entry {tr['entry']:.2f} pe wapas aaya",
                       as_html=True, reply_to=tr["mid"])
                else:
                    tr["status"] = "SL"
                    tg(f"🛑 <b>STOP LOSS HIT</b> {hs} {tf}m\nEntry {tr['entry']:.2f} → SL {tr['sl']:.2f}\n"
                       f"❌ Loss: {abs(tr['entry'] - tr['sl']):.2f} / share", as_html=True, reply_to=tr["mid"])
                break
            if hit_t2:
                tr["status"] = "TARGET"
                tg(f"🎯🎯 <b>TARGET 2 HIT!</b> {hs} {tf}m\nEntry {tr['entry']:.2f} → T2 {tr['tgt']:.2f}\n"
                   f"✅ Profit: {abs(tr['tgt'] - tr['entry']):.2f} / share", as_html=True, reply_to=tr["mid"])
                break
            if hit_t1 and not tr["t1hit"]:
                tr["t1hit"] = True
                tr["sl"] = tr["entry"]                    # SL ab entry pe (risk-free)
                tg(f"🎯 <b>TARGET 1 HIT</b> {hs} {tf}m\nEntry {tr['entry']:.2f} → T1 {tr['t1']:.2f}\n"
                   f"💡 Aadha profit book karo, SL entry ({tr['entry']:.2f}) pe le aao",
                   as_html=True, reply_to=tr["mid"])
        if tr["status"] == "OPEN" and tr.get("kind", "EMA") == "EMA":
            opp = (buy and f[-1] < s[-1]) or (not buy and f[-1] > s[-1])
            if opp:
                tr["status"] = "EXIT"
                pnl = (closes[-1] - tr["entry"]) * (1 if buy else -1)
                tg(f"⚠️ <b>EXIT SIGNAL</b> {hs} {tf}m\nUlta crossover ho gaya\n"
                   f"Entry {tr['entry']:.2f} → Abhi {closes[-1]:.2f}  ({pnl:+.2f} / share)",
                   as_html=True, reply_to=tr["mid"])

    now = datetime.now(IST)
    if STRATEGY_SMC:
        smc_track(sym, tf, c, trades, now)
    rs = rsi_series(closes)
    for idx in range(max(REV_LOOKBACK + 1, len(c) - 4), len(c)):
        t = c[idx][0]
        if t.date() != now.date() or not in_trade_time(t, tf):
            continue
        if now - (t + timedelta(minutes=tf)) > timedelta(minutes=MAX_LATE):
            continue

        # --- 0) SMC PLAN (sweep + CHoCH + FVG/OB) ---
        if STRATEGY_SMC and hm_ok(t, tf, SMC_FROM, SMC_TILL) and (sym, tf, t, "SMC") not in seen:
            plan = smc_at(c, idx)
            if plan:
                seen.add((sym, tf, t, "SMC"))
                busy = any(p["sym"] == sym and p["tf"] == tf and p["status"] == "WAIT" for p in SMC_PLANS) or \
                    any(tr["sym"] == sym and tr["tf"] == tf and tr["status"] == "OPEN" and tr.get("kind") == "SMC"
                        for tr in trades)
                if not busy:
                    smc_new_plan(sym, tf, c[:idx + 1], plan, now)
                    seen.add((sym, tf, t, "REV"))          # same candle pe duplicate reversal nahi

        # --- 1) REVERSAL (bottom / top pe entry) ---
        if STRATEGY_REVERSAL and (sym, tf, t, "REV") not in seen:
            rev = reversal_at(c, f, rs, idx)
            if rev:
                seen.add((sym, tf, t, "REV"))          # har candle ka faisla sirf ek baar
                last = LAST_REV.get((sym, tf, rev))
                cool = last is not None and t - last < timedelta(minutes=tf * REV_LOOKBACK)
                smc_busy = any(p["sym"] == sym and p["tf"] == tf and p["status"] == "WAIT" and p["side"] == rev
                               for p in SMC_PLANS) or \
                    any(tr["sym"] == sym and tr["tf"] == tf and tr["status"] == "OPEN" and tr["side"] == rev
                        and tr.get("kind") == "SMC" for tr in trades)
                if not cool and not smc_busy:
                    LAST_REV[(sym, tf, rev)] = t
                    send_reversal(sym, tf, c[:idx + 1], f[:idx + 1], s[:idx + 1], rs[:idx + 1],
                                  rev == "BUY", trades, c[-1][4], now)

        # --- 2) EMA CROSSOVER ---
        if STRATEGY_EMA:
            bull = f[idx - 1] <= s[idx - 1] and f[idx] > s[idx]
            bear = f[idx - 1] >= s[idx - 1] and f[idx] < s[idx]
            key = (sym, tf, t)
            if (bull or bear) and key not in seen:
                seen.add(key)
                side = "BUY" if bull else "SELL"
                rev_open = [tr for tr in trades if tr["sym"] == sym and tr["tf"] == tf
                            and tr["status"] == "OPEN" and tr.get("kind") in ("REV", "SMC") and tr["side"] == side]
                if rev_open:                       # reversal trade ko confirm karo, naya trade nahi
                    tg(f"✅ <b>CONFIRM</b> {hs} {tf}m\nEMA{FAST}/{SLOW} crossover ne {side} trade ko confirm kiya "
                       f"@ {c[idx][4]:.2f}\n💡 Trade hold karo (ya add karo)", as_html=True, reply_to=rev_open[0]["mid"])
                else:
                    send_signal(sym, tf, c[:idx + 1], f[:idx + 1], s[:idx + 1], bull, trades, c[-1][4], now)


def reversal_at(c, f, rs, idx):
    """Reversal candle: pehle RSI oversold/overbought, phir strong ulti candle."""
    t, o, h, l, cl, v = c[idx]
    prev = c[idx - 1]
    window = range(idx - REV_LOOKBACK, idx + 1)
    oversold = min(rs[j] for j in window) < RSI_LOW
    overbought = max(rs[j] for j in window) > RSI_HIGH
    if oversold and cl > o and cl > prev[2] and rs[idx] > rs[idx - 1] and cl > f[idx]:
        return "BUY"      # green candle ne pichhla High toda + EMA9 ke upar close
    if overbought and cl < o and cl < prev[3] and rs[idx] < rs[idx - 1] and cl < f[idx]:
        return "SELL"     # red candle ne pichhla Low toda + EMA9 ke neeche close
    return None


def send_reversal(sym, tf, c, f, s, rs, bull, trades, ltp, now):
    hs = html.escape(sym)
    t, o, h, l, cl, vol = c[-1]
    win = c[-(REV_LOOKBACK + 1):]
    buf = cl * 0.0005
    if bull:
        swing = min(x[3] for x in win)
        sl = swing - buf
        risk = cl - sl
    else:
        swing = max(x[2] for x in win)
        sl = swing + buf
        risk = sl - cl
    if risk > cl * MAX_REV_RISK_PCT / 100:
        print(f"Reversal skip {sym}: SL bahut door ({risk:.2f})")
        return
    if risk < cl * MIN_RISK_PCT / 100:
        risk = cl * MIN_RISK_PCT / 100
        sl = cl - risk if bull else cl + risk
    side = "BUY" if bull else "SELL"
    t1 = cl + risk if bull else cl - risk
    tgt = cl + RR * risk if bull else cl - RR * risk
    vw = vwap_today(c, len(c) - 1)
    vols = [x[5] for x in c[-21:-1]]
    vol_x = vol / (sum(vols) / len(vols)) if vols and sum(vols) > 0 else 1
    extreme = min(rs[-(REV_LOOKBACK + 1):]) if bull else max(rs[-(REV_LOOKBACK + 1):])

    stars, notes = 1, []
    if vol_x >= 1.5:
        stars += 1
        notes.append(f"✅ Volume {vol_x:.1f}x zyada")
    if (bull and extreme < 25) or (not bull and extreme > 75):
        stars += 1
        notes.append(f"✅ RSI bahut extreme tha ({extreme:.0f})")
    else:
        notes.append(f"ℹ️ RSI {extreme:.0f} se palta")
    if (bull and l <= swing + buf * 2) or (not bull and h >= swing - buf * 2):
        stars += 1
        notes.append("✅ Bottom/top ke bilkul paas entry")
    if bull and vw > cl:
        notes.append(f"🎯 VWAP {vw:.2f} upar hai — wahan resistance aa sakta hai")
    elif not bull and vw < cl:
        notes.append(f"🎯 VWAP {vw:.2f} neeche hai — wahan support aa sakta hai")
    star_txt = "⭐" * stars + "☆" * (4 - stars)

    close_time = t + timedelta(minutes=tf)
    late = int((now - close_time).total_seconds() // 60)
    timing = f"⏱ Candle close {close_time:%H:%M} | Alert {now:%H:%M}"
    if late >= 3:
        timing += f"\n⚠️ Data {late} min late aaya — abhi price {ltp:.2f}"
    pct = lambda a: abs(a - cl) / cl * 100
    if bull:
        head = "🔄🟢 <b>REVERSAL BUY</b> ⬆️"
        why = f"RSI oversold ({extreme:.0f}) se palta + green candle ne pichhla High toda"
    else:
        head = "🔄🔴 <b>REVERSAL SELL</b> ⬇️"
        why = f"RSI overbought ({extreme:.0f}) se palta + red candle ne pichhla Low toda"
    caption = (f"{head}\n<b>{hs}</b> • {tf}m • {t:%d-%b %H:%M}\n{why}\n{timing}\n\n"
               f"💰 Entry:  <code>{cl:.2f}</code>\n"
               f"🛑 SL:     <code>{sl:.2f}</code>  (-{pct(sl):.2f}%)  swing {'low' if bull else 'high'}\n"
               f"🎯 T1:     <code>{t1:.2f}</code>  (+{pct(t1):.2f}%)  1:1\n"
               f"🎯 T2:     <code>{tgt:.2f}</code>  (+{pct(tgt):.2f}%)  1:{RR}\n\n"
               f"Strength: {star_txt}\n" + "\n".join(n for n in notes if n) +
               "\n\n⚠️ <i>Reversal = trend ke ulta trade. Chhoti quantity rakho, SL zaroor lagao.</i>")
    share_txt = (f"🔄 {'BUY' if bull else 'SELL'} {sym} ({tf}m) reversal\n"
                 f"Entry {cl:.2f} | SL {sl:.2f} | T1 {t1:.2f} | T2 {tgt:.2f}")
    buttons = [[{"text": "📤 Share Signal",
                 "url": f"https://t.me/share/url?url={quote(tv_link(sym))}&text={quote(share_txt)}"},
                {"text": "📈 Chart dekho", "url": tv_link(sym)}]]
    if BIG_EMOJI:
        tg("🔄")
    try:
        png = make_chart(sym, tf, c, f, s, side, cl, sl, t1, tgt, title="RSI Reversal")
        mid = tg_photo(png, caption, buttons)
    except Exception as e:
        print("Chart error:", e)
        mid = tg(caption, as_html=True, buttons=buttons)
    trades.append({"sym": sym, "tf": tf, "side": side, "entry": cl, "sl": sl, "t1": t1, "tgt": tgt,
                   "t": t, "last": t, "t1hit": False, "mid": mid, "status": "OPEN", "kind": "REV"})


def send_signal(sym, tf, c, f, s, bull, trades, ltp, now):
    hs = html.escape(sym)
    closes = [x[4] for x in c]
    t, o, h, l, cl, vol = c[-1]
    bear = not bull
    # --- SL / Targets (ATR based, realistic) ---
    trs = [max(c[i][2] - c[i][3], abs(c[i][2] - c[i - 1][4]), abs(c[i][3] - c[i - 1][4]))
           for i in range(max(1, len(c) - 14), len(c))]
    atr = sum(trs) / len(trs) if trs else cl * 0.003
    candle_risk = (cl - l) if bull else (h - cl)
    risk = min(candle_risk, 1.2 * atr)            # badi candle ho toh ATR se limit
    risk = max(risk, cl * MIN_RISK_PCT / 100)
    risk = min(risk, cl * MAX_RISK_PCT / 100)
    if bull:
        side, sl, t1, tgt = "BUY", cl - risk, cl + risk, cl + RR * risk
    else:
        side, sl, t1, tgt = "SELL", cl + risk, cl - risk, cl - RR * risk
    today = [x for x in c if x[0].date() == t.date()]
    day_hi = max(x[2] for x in today) if today else h
    day_lo = min(x[3] for x in today) if today else l

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

    close_time = t + timedelta(minutes=tf)
    late = int((now - close_time).total_seconds() // 60)
    timing = f"⏱ Candle close {close_time:%H:%M} | Alert {now:%H:%M}"
    if late >= 3:
        timing += f"\n⚠️ Data {late} min late aaya — abhi price {ltp:.2f}"

    pct = lambda a: abs(a - cl) / cl * 100
    level_note = ""
    if bull and tgt > day_hi:
        level_note = f"\n⚠️ T2 aaj ke High {day_hi:.2f} ke upar hai — T1 pe profit book karna behtar"
    elif bear and tgt < day_lo:
        level_note = f"\n⚠️ T2 aaj ke Low {day_lo:.2f} ke neeche hai — T1 pe profit book karna behtar"
    if bull:
        head = "🟢🚀 <b>BUY SIGNAL</b> ⬆️"
        line = f"EMA{FAST} ne EMA{SLOW} ko ⬆️ upar cross kiya"
    else:
        head = "🔴🔻 <b>SELL SIGNAL</b> ⬇️"
        line = f"EMA{FAST} ne EMA{SLOW} ko ⬇️ neeche cross kiya"
    caption = (f"{head}\n"
               f"<b>{hs}</b> • {tf}m • {t:%d-%b %H:%M}\n"
               f"{line}\n{timing}\n\n"
               f"💰 Entry:  <code>{cl:.2f}</code>\n"
               f"🛑 SL:     <code>{sl:.2f}</code>  (-{pct(sl):.2f}%)\n"
               f"🎯 T1:     <code>{t1:.2f}</code>  (+{pct(t1):.2f}%)  1:1\n"
               f"🎯 T2:     <code>{tgt:.2f}</code>  (+{pct(tgt):.2f}%)  1:{RR}\n"
               f"⚖️ Risk {risk:.2f}/share{level_note}\n\n"
               f"Strength: {star_txt}\n" + "\n".join(notes) +
               "\n\n<i>Sirf technical signal hai, advice nahi.</i>")
    share_txt = (f"{'🟢 BUY' if bull else '🔴 SELL'} {sym} ({tf}m)\n"
                 f"Entry {cl:.2f} | SL {sl:.2f} | T1 {t1:.2f} | T2 {tgt:.2f}\n"
                 f"EMA {FAST}/{SLOW} crossover • {t:%d-%b %H:%M}")
    buttons = [[{"text": "📤 Share Signal",
                 "url": f"https://t.me/share/url?url={quote(tv_link(sym))}&text={quote(share_txt)}"},
                {"text": "📈 Chart dekho", "url": tv_link(sym)}]]

    if BIG_EMOJI:
        tg("🚀" if bull else "🔻")
    try:
        png = make_chart(sym, tf, c, f, s, side, cl, sl, t1, tgt)
        mid = tg_photo(png, caption, buttons)
    except Exception as e:
        print("Chart error:", e)
        mid = tg(caption, as_html=True, buttons=buttons)
    trades.append({"sym": sym, "tf": tf, "side": side, "entry": cl, "sl": sl, "t1": t1,
                   "tgt": tgt, "t": t, "last": t, "t1hit": False, "mid": mid, "status": "OPEN", "kind": "EMA"})


# ----------------------------- SMC (Smart Money Concepts) -----------------------------
SMC_PLANS = []
PIV_K = 2


def hm_ok(t, tf, frm, till):
    a = tuple(map(int, frm.split(":")))
    b = tuple(map(int, till.split(":")))
    ct = t + timedelta(minutes=tf)
    return (t.hour, t.minute) >= a and (ct.hour, ct.minute) <= b


def pivots(c, upto, lookback=150, k=PIV_K):
    """Confirmed swing highs/lows (index list) jinke right side ki k candles 'upto' tak ban chuki hain."""
    hs, ls = [], []
    for i in range(max(k, upto - lookback), upto - k + 1):
        rng = range(i - k, i + k + 1)
        if all(c[i][2] >= c[j][2] for j in rng):
            hs.append(i)
        if all(c[i][3] <= c[j][3] for j in rng):
            ls.append(i)
    return hs, ls


def day_ctx(c, idx):
    d = c[idx][0].date()
    today = [i for i in range(max(0, idx - 90), idx + 1) if c[i][0].date() == d]
    prev = [i for i in range(max(0, idx - 200), idx + 1) if c[i][0].date() < d]
    pdh = pdl = None
    if prev:
        pd = c[prev[-1]][0].date()
        pdi = [i for i in prev if c[i][0].date() == pd]
        pdh = max(c[i][2] for i in pdi)
        pdl = min(c[i][3] for i in pdi)
    return today, pdh, pdl


def avg(vals):
    vals = list(vals)
    return sum(vals) / len(vals) if vals else 0.0


def smc_side(c, idx, bull, today, pdh, pdl):
    cl, op = c[idx][4], c[idx][1]
    t0 = today[0]
    if len(today) < 4:
        return None
    or_end = today[2]
    orh = max(c[i][2] for i in today[:3])
    orl = min(c[i][3] for i in today[:3])
    hs, ls = pivots(c, idx - 1)

    # --- 1) CHoCH: aakhri swing high (bull) / swing low (bear) ka pehli baar toot-na ---
    piv = hs if bull else ls
    piv = [i for i in piv if i >= t0 - 12 and i < idx - 1]
    if not piv:
        return None
    ci = piv[-1]
    lvl = c[ci][2] if bull else c[ci][3]
    if bull and not (cl > lvl and c[idx - 1][4] <= lvl):
        return None
    if not bull and not (cl < lvl and c[idx - 1][4] >= lvl):
        return None

    # --- 2) Liquidity sweep: CHoCH level aur ab ke beech ka extreme ---
    seg = list(range(ci + 1, idx))
    if not seg or idx - seg[0] > SMC_SWEEP_LOOKBACK + 5:
        seg = list(range(max(ci + 1, idx - SMC_SWEEP_LOOKBACK), idx))
    if not seg:
        return None
    sj = min(seg, key=lambda i: c[i][3]) if bull else max(seg, key=lambda i: c[i][2])
    sweep_px = c[sj][3] if bull else c[sj][2]
    levels = []
    if bull:
        if pdl is not None:
            levels.append((pdl, "PDL"))
        if sj > or_end:
            levels.append((orl, "OR Low"))
        levels += [(c[i][3], "Swing low") for i in ls if i + PIV_K < sj and i >= t0 - 40]
        swept = [(L, nm) for L, nm in levels if sweep_px < L and
                 any(c[k][4] > L for k in range(sj, min(sj + 3, idx + 1)))]
    else:
        if pdh is not None:
            levels.append((pdh, "PDH"))
        if sj > or_end:
            levels.append((orh, "OR High"))
        levels += [(c[i][2], "Swing high") for i in hs if i + PIV_K < sj and i >= t0 - 40]
        swept = [(L, nm) for L, nm in levels if sweep_px > L and
                 any(c[k][4] < L for k in range(sj, min(sj + 3, idx + 1)))]
    if not swept:
        return None
    prio = {"PDL": 0, "PDH": 0, "OR Low": 1, "OR High": 1, "Swing low": 2, "Swing high": 2}
    L, liq_name = sorted(swept, key=lambda x: (prio[x[1]], -abs(x[0] - sweep_px)))[0]

    # --- 3) Displacement (badi candle + volume) ---
    body = abs(cl - op)
    avg_body = avg(abs(c[i][4] - c[i][1]) for i in range(idx - 10, idx)) or body
    avg_vol = avg(c[i][5] for i in range(idx - 20, idx)) or 1
    choch_vx = c[idx][5] / avg_vol
    sweep_vx = c[sj][5] / avg_vol
    if not ((bull and cl > op) or (not bull and cl < op)):
        return None
    if body < 1.2 * avg_body or choch_vx < 1.1:
        return None

    # --- 4) Entry zone: FVG (pehle) ya Order Block ---
    zone = ztype = None
    for k in range(idx - 1, sj, -1):
        if bull and c[k + 1][3] > c[k - 1][2]:
            zone, ztype = (c[k - 1][2], c[k + 1][3]), "FVG"
            break
        if not bull and c[k + 1][2] < c[k - 1][3]:
            zone, ztype = (c[k + 1][2], c[k - 1][3]), "FVG"
            break
    if zone is None:
        for k in range(idx - 1, sj - 1, -1):
            if (bull and c[k][4] < c[k][1]) or (not bull and c[k][4] > c[k][1]):
                zone, ztype = (c[k][3], c[k][2]), "Order Block"
                break
    if zone is None:
        return None
    zlo, zhi = zone
    if bull and zlo >= cl or not bull and zhi <= cl:
        return None
    zhi, zlo = (min(zhi, cl), zlo) if bull else (zhi, max(zlo, cl))

    # --- 5) Bias: VWAP side ya discount/premium ---
    vw = vwap_today(c, idx)
    dh = max(c[i][2] for i in today)
    dl = min(c[i][3] for i in today)
    mid_day = dl + (dh - dl) / 2
    bias = (cl > vw or zhi <= mid_day) if bull else (cl < vw or zlo >= mid_day)
    if not bias:
        return None

    # --- 6) SL / Targets (liquidity based) ---
    buf = cl * 0.0005
    mid = (zlo + zhi) / 2
    sl = sweep_px - buf if bull else sweep_px + buf
    risk = mid - sl if bull else sl - mid
    if risk <= 0 or risk > mid * MAX_SMC_RISK_PCT / 100:
        return None
    if risk < mid * MIN_RISK_PCT / 100:
        risk = mid * MIN_RISK_PCT / 100
        sl = mid - risk if bull else mid + risk
    if bull:
        cands = [(c[i][2], "Swing high") for i in hs if i >= t0 - 60]
        if pdh is not None:
            cands.append((pdh, "PDH"))
        if idx > or_end:
            cands.append((orh, "OR High"))
        cands = sorted({(round(p, 2), n) for p, n in cands if p > mid + risk})
    else:
        cands = [(c[i][3], "Swing low") for i in ls if i >= t0 - 60]
        if pdl is not None:
            cands.append((pdl, "PDL"))
        if idx > or_end:
            cands.append((orl, "OR Low"))
        cands = sorted({(round(p, 2), n) for p, n in cands if p < mid - risk}, reverse=True)
    sgn = 1 if bull else -1
    if cands and abs(cands[0][0] - mid) <= 2.5 * risk:
        t1, t1n = cands[0]
    else:
        t1, t1n = mid + sgn * 1.5 * risk, "1.5R"
    rest = [(p, n) for p, n in cands if sgn * (p - t1) > 0.5 * risk]
    if rest and abs(rest[0][0] - mid) <= 4 * risk:
        t2, t2n = rest[0]
    else:
        t2, t2n = mid + sgn * max(RR * risk, abs(t1 - mid) + risk), f"{RR}R+"

    stars = 0
    stars += sweep_vx >= 1.5
    stars += choch_vx >= 1.5
    stars += liq_name in ("PDL", "PDH", "OR Low", "OR High")
    stars += (cl > vw) if bull else (cl < vw)
    return {"side": "BUY" if bull else "SELL", "liq": L, "liq_name": liq_name, "sweep_px": sweep_px,
            "sweep_t": c[sj][0], "sweep_vx": sweep_vx, "choch": lvl, "choch_vx": choch_vx,
            "zlo": zlo, "zhi": zhi, "ztype": ztype, "sl": sl, "t1": t1, "t1n": t1n, "t2": t2, "t2n": t2n,
            "vwap": vw, "stars": stars, "t": c[idx][0], "disp_vol": c[idx][5]}


def smc_at(c, idx):
    today, pdh, pdl = day_ctx(c, idx)
    if not today:
        return None
    return smc_side(c, idx, True, today, pdh, pdl) or smc_side(c, idx, False, today, pdh, pdl)


def make_smc_chart(sym, tf, c, plan, entry=None):
    n = min(60, len(c))
    c = c[-n:]
    bg, up, dn = "#0f1420", "#26a69a", "#ef5350"
    fig, ax = plt.subplots(figsize=(8, 4.6), dpi=110)
    fig.patch.set_facecolor(bg)
    ax.set_facecolor(bg)
    tix = {x[0]: i for i, x in enumerate(c)}
    for i, (t, o, h, l, cl, v) in enumerate(c):
        col = up if cl >= o else dn
        ax.vlines(i, l, h, color=col, linewidth=1)
        ax.add_patch(plt.Rectangle((i - 0.35, min(o, cl)), 0.7, max(abs(cl - o), 1e-6), color=col))
    last = n - 1
    pi = tix.get(plan["t"], last)
    si = tix.get(plan["sweep_t"], max(0, pi - 5))
    ax.add_patch(plt.Rectangle((si, plan["zlo"]), last + 6 - si, plan["zhi"] - plan["zlo"],
                               color="#42a5f5", alpha=0.18))
    ax.text(si, plan["zhi"], f" {plan['ztype']}", color="#90caf9", fontsize=8, va="bottom")
    ax.hlines(plan["liq"], max(0, si - 15), si + 3, colors="#bdbdbd", linestyles=":", linewidth=1.2)
    ax.text(max(0, si - 15), plan["liq"], f"{plan['liq_name']} liquidity", color="#bdbdbd", fontsize=7,
            va="bottom")
    ax.annotate("Sweep", xy=(si, plan["sweep_px"]),
                xytext=(si, plan["sweep_px"] + (-1 if plan["side"] == "BUY" else 1) * (plan["zhi"] - plan["zlo"] + 0.001) * 2),
                color="#ffd54f", fontsize=8, ha="center", arrowprops=dict(arrowstyle="->", color="#ffd54f"))
    ax.hlines(plan["choch"], max(0, pi - 12), pi, colors="#ff9800", linestyles="--", linewidth=1.2)
    ax.text(max(0, pi - 12), plan["choch"], "CHoCH", color="#ff9800", fontsize=8, va="bottom")
    lines = [(plan["sl"], dn, "SL"), (plan["t1"], "#80cbc4", "T1"), (plan["t2"], up, "T2")]
    if entry is not None:
        lines.insert(0, (entry, "#90caf9", "Entry"))
    for y, col, lab in lines:
        ax.hlines(y, last - 10, last + 6, colors=col, linestyles="--", linewidth=1.2)
        ax.text(last + 6.3, y, f"{lab} {y:.2f}", color=col, fontsize=8, va="center")
    ax.set_xlim(-1, last + 14)
    ticks = list(range(0, n, max(1, n // 6)))
    ax.set_xticks(ticks)
    ax.set_xticklabels([c[i][0].strftime("%H:%M") for i in ticks], color="#9aa4b2", fontsize=8)
    ax.tick_params(axis="y", colors="#9aa4b2", labelsize=8)
    for sp in ax.spines.values():
        sp.set_color("#2a3142")
    ax.grid(color="#1e2533", linewidth=0.6)
    ax.set_title(f"{sym} • {tf}m • SMC {plan['side']} " + ("ENTRY" if entry else "PLAN"),
                 color="white", fontsize=11, loc="left")
    buf = io.BytesIO()
    fig.tight_layout()
    fig.savefig(buf, format="png", facecolor=bg)
    plt.close(fig)
    return buf.getvalue()


def smc_new_plan(sym, tf, c, plan, now):
    hs = html.escape(sym)
    bull = plan["side"] == "BUY"
    mid = (plan["zlo"] + plan["zhi"]) / 2
    pct = lambda a: abs(a - mid) / mid * 100
    stars = "⭐" * max(1, plan["stars"]) + "☆" * (4 - max(1, plan["stars"]))
    cap = (f"📋 <b>SMC PLAN — {plan['side']}</b> {'⬆️' if bull else '⬇️'}\n"
           f"<b>{hs}</b> • {tf}m • {plan['t']:%d-%b %H:%M}\n\n"
           f"💧 Liquidity sweep: {plan['liq_name']} {plan['liq']:.2f} "
           f"({'low' if bull else 'high'} {plan['sweep_px']:.2f}, vol {plan['sweep_vx']:.1f}x)\n"
           f"🔀 CHoCH: {plan['choch']:.2f} toda (vol {plan['choch_vx']:.1f}x)\n"
           f"📦 Entry zone ({plan['ztype']}): <code>{plan['zlo']:.2f} – {plan['zhi']:.2f}</code>\n"
           f"🛑 SL: <code>{plan['sl']:.2f}</code>  ({pct(plan['sl']):.2f}%)\n"
           f"🎯 T1: <code>{plan['t1']:.2f}</code>  ({plan['t1n']})\n"
           f"🎯 T2: <code>{plan['t2']:.2f}</code>  ({plan['t2n']})\n\n"
           f"Strength: {stars}\n"
           f"⏳ Price zone me aaye + {'green' if bull else 'red'} candle bane → ENTRY alert\n"
           f"⌛ {SMC_EXPIRY * tf} min me zone me na aaye toh plan cancel\n\n"
           f"<i>SMC = price + volume se andaaza, guarantee nahi.</i>")
    share = (f"🧠 SMC {plan['side']} PLAN {sym} ({tf}m)\nZone {plan['zlo']:.2f}-{plan['zhi']:.2f} | "
             f"SL {plan['sl']:.2f} | T1 {plan['t1']:.2f} | T2 {plan['t2']:.2f}")
    buttons = [[{"text": "📤 Share Plan",
                 "url": f"https://t.me/share/url?url={quote(tv_link(sym))}&text={quote(share)}"},
                {"text": "📈 Chart dekho", "url": tv_link(sym)}]]
    try:
        mid_msg = tg_photo(make_smc_chart(sym, tf, c, plan), cap, buttons)
    except Exception as e:
        print("SMC chart error:", e)
        mid_msg = tg(cap, as_html=True, buttons=buttons)
    plan.update({"sym": sym, "tf": tf, "status": "WAIT", "last": plan["t"], "bars": 0, "mid": mid_msg})
    SMC_PLANS.append(plan)


def smc_track(sym, tf, c, trades, now):
    hs = html.escape(sym)
    for p in SMC_PLANS:
        if p["sym"] != sym or p["tf"] != tf or p["status"] != "WAIT":
            continue
        bull = p["side"] == "BUY"
        for i, (t, o, h, l, cl, v) in enumerate(c):
            if t <= p["last"]:
                continue
            p["last"] = t
            p["bars"] += 1
            stale = now - (t + timedelta(minutes=tf)) > timedelta(minutes=MAX_LATE)
            if (bull and l <= p["sl"]) or (not bull and h >= p["sl"]):
                p["status"] = "CANCEL"
                tg(f"❌ <b>SMC plan cancel</b> {hs}\nEntry se pehle hi SL level toot gaya", as_html=True,
                   reply_to=p["mid"])
                break
            touched = (l <= p["zhi"] and cl >= p["zlo"]) if bull else (h >= p["zlo"] and cl <= p["zhi"])
            good = (cl > o) if bull else (cl < o)
            room = (cl < p["t1"]) if bull else (cl > p["t1"])
            if touched and good and room and not stale:
                p["status"] = "ENTERED"
                avg_vol = avg(x[5] for x in c[max(0, i - 20):i]) or 1
                pvx = v / avg_vol
                vol_note = (f"✅ Pullback pe volume kam ({pvx:.1f}x) — selling/buying pressure khatam"
                            if pvx < 1 else f"ℹ️ Pullback volume {pvx:.1f}x")
                risk = abs(cl - p["sl"])
                cap = (f"🎯🧠 <b>SMC ENTRY — {p['side']}</b> {'⬆️' if bull else '⬇️'}\n"
                       f"<b>{hs}</b> • {tf}m • {t:%d-%b %H:%M}\n"
                       f"Price {p['ztype']} zone me aaya + {'green' if bull else 'red'} candle\n\n"
                       f"💰 Entry:  <code>{cl:.2f}</code>\n"
                       f"🛑 SL:     <code>{p['sl']:.2f}</code>  (risk {risk:.2f}/share)\n"
                       f"🎯 T1:     <code>{p['t1']:.2f}</code>  ({p['t1n']})\n"
                       f"🎯 T2:     <code>{p['t2']:.2f}</code>  ({p['t2n']})\n\n{vol_note}")
                try:
                    msg_id = tg_photo(make_smc_chart(sym, tf, c[:i + 1], p, entry=cl), cap,
                                      [[{"text": "📈 Chart dekho", "url": tv_link(sym)}]])
                except Exception as e:
                    print("SMC chart error:", e)
                    msg_id = tg(cap, as_html=True, reply_to=p["mid"])
                trades.append({"sym": sym, "tf": tf, "side": p["side"], "entry": cl, "sl": p["sl"],
                               "t1": p["t1"], "tgt": p["t2"], "t": t, "last": t, "t1hit": False,
                               "mid": msg_id, "status": "OPEN", "kind": "SMC"})
                break
            if p["bars"] >= SMC_EXPIRY or stale:
                p["status"] = "EXPIRED"
                tg(f"⌛ SMC plan expire {hs} — price entry zone me nahi aaya", reply_to=p["mid"])
                break


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
    cnt = {k: sum(1 for t in trades if t["status"] == k) for k in ("TARGET", "BE", "SL", "EXIT", "OPEN")}
    t1n = sum(1 for t in trades if t.get("t1hit"))
    lines = [f"📊 <b>Session Summary</b>  ({len(trades)} signals)",
             f"🎯 T1 hit: {t1n}   🎯🎯 T2: {cnt['TARGET']}   🔒 BE: {cnt['BE']}",
             f"🛑 SL: {cnt['SL']}   ⚠️ Exit: {cnt['EXIT']}   ⏳ Open: {cnt['OPEN']}", ""]
    icon = {"TARGET": "🎯", "BE": "🔒", "SL": "🛑", "EXIT": "⚠️", "OPEN": "⏳"}
    for t in trades:
        lines.append(f"{icon[t['status']]} { {'REV': '🔄', 'SMC': '🧠'}.get(t.get('kind'), '') }{'🟢' if t['side'] == 'BUY' else '🔴'} {html.escape(t['sym'])} {t['tf']}m "
                     f"@ {t['entry']:.2f} ({t['t']:%H:%M})")
    return "\n".join(lines)


# ----------------------------- MAIN -----------------------------
def main():
    start = time.time()
    tokens = get_tokens(FIXED + UNIVERSE)
    missing = [s for s in FIXED + UNIVERSE if s.upper() not in tokens]
    api = login()
    seen, trades = set(), []
    watch, msg = build_watch(api, tokens, "✅ <b>Scanner chalu ho gaya</b> (SMC + Reversal + EMA)")
    if missing:
        msg += "\n\n⚠️ Ye symbol nahi mile: " + ", ".join(missing)
    tg(msg, as_html=True)
    last_refresh = datetime.now(IST)
    traded_today = False
    err_count, last_err, last_err_alert = 0, "", 0.0

    while time.time() - start < MAX_RUN_MIN * 60:
        now = datetime.now(IST)
        mopen = now.replace(hour=9, minute=15, second=0, microsecond=0)
        mclose = now.replace(hour=15, minute=30, second=0, microsecond=0)
        if now.weekday() >= 5 or now > mclose + timedelta(minutes=2):
            break
        nxt = (now + timedelta(minutes=1)).replace(second=20, microsecond=0)
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

        open_syms = [t["sym"] for t in trades if t["status"] == "OPEN"] + \
            [p["sym"] for p in SMC_PLANS if p["status"] == "WAIT"]
        scan = list(dict.fromkeys(watch + open_syms))
        for tf in TIMEFRAMES:                     # har minute check (data late ho toh bhi pakde)
            for sym in scan:
                try:
                    check(api, sym, tokens[sym], tf, seen, trades)
                except Exception as e:
                    err_count += 1
                    last_err = f"{sym}: {e!r}"
                    print("Error", last_err)
                    low = str(e).lower()
                    if any(k in low for k in ("token", "session", "unauthor", "ag8001", "ag8002", "ag8003")):
                        try:
                            api = login()
                            print("Re-login OK")
                        except Exception as e2:
                            print("Re-login failed:", e2)
                    else:
                        time.sleep(2)                 # rate limit ho toh thoda ruko
                    if err_count >= 15 and time.time() - last_err_alert > 1800:
                        last_err_alert = time.time()
                        tg(f"⚠️ Angel One se data lene me dikkat ({err_count} errors)\n"
                           f"Last: {last_err[:200]}\nScanner chal raha hai, retry kar raha hai.")
                else:
                    err_count = 0
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
