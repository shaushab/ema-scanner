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
                        f"Current price: {last['c']:.2f} (limit order at zone top)")
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
                        f"Current price: {last['c']:.2f} (limit order at zone bottom)")
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

# ---------- Telegram: stylish message + chart + sticker ----------
import io, html, random, math
try:
    import matplotlib
    matplotlib.use("Agg")
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.patches import Polygon, Circle, Rectangle
    from PIL import Image
    CHARTS = True
except Exception as _ex:
    print("chart libs nahi mili, sirf text jayega:", _ex, flush=True)
    CHARTS = False

BRAND = "SHAUSHAB"
ANIMATION = True  # False = sirf static chart image
FOOT = "\n━━━━━━━━━━━━━━\n⚡ <i>Shaushab Crypto Bot</i>"
BG, GRID, TXT = "#0b0e11", "#1e2329", "#d1d4dc"
UP, DN = "#0ecb81", "#f6465d"

def tg_plain(text):
    # simple text ko Telegram ke liye thoda stylish banao
    lines = html.escape(text).split("\n")
    lines[0] = f"<b>{lines[0]}</b>"
    return "\n".join(lines) + FOOT

def _limits(d):
    c = d["c"]
    ymin = min(x["l"] for x in c)
    ymax = max(x["h"] for x in c)
    for key in ("e20", "e50", "e200"):  # EMA 200 bhi hamesha chart me dikhe
        if d.get(key):
            ymin, ymax = min(ymin, min(d[key])), max(ymax, max(d[key]))
    for price, _, _ in d.get("levels", []):
        ymin, ymax = min(ymin, price), max(ymax, price)
    for _, blo, bhi, _, _ in d.get("boxes", []):
        ymin, ymax = min(ymin, blo), max(ymax, bhi)
    pad = (ymax - ymin) * 0.08 or 1
    return ymin - pad, ymax + pad

def _frame(d, k, show_levels, size=(9, 5.4), dpi=100):
    # pehli k candles tak ka chart ek PIL image me
    c = d["c"]
    n = len(c)
    fig = Figure(figsize=size, dpi=dpi, facecolor=BG)
    FigureCanvasAgg(fig)
    ax = fig.add_axes([0.03, 0.06, 0.78, 0.85], facecolor=BG)
    ax.set_ylim(*_limits(d))
    ax.set_xlim(-1, n + 0.5)
    w = 0.6
    for i, x in enumerate(c[:k]):
        col = UP if x["c"] >= x["o"] else DN
        ax.vlines(i, x["l"], x["h"], color=col, linewidth=1)
        lo, hi = sorted((x["o"], x["c"]))
        ax.add_patch(Rectangle((i - w / 2, lo), w, max(hi - lo, (x["h"] - x["l"]) * 0.02 + 1e-9),
                               color=col))
    tags = []
    for key, col, lab in (("e20", "#ff2e88", "EMA 20"), ("e50", "#3d7dff", "EMA 50"),
                          ("e200", "#ffd400", "EMA 200")):
        if d.get(key):
            ax.plot(range(k), d[key][:k], color=col, linewidth=1.8, label=lab)
            tags.append((d[key][k - 1], lab, col, "black" if key == "e200" else "white"))
    if k:
        last = c[k - 1]["c"]
        tags.append((last, "PRICE", UP if c[k - 1]["c"] >= c[k - 1]["o"] else DN, "white"))
        ax.axhline(last, color="#848e9c", linewidth=0.8, linestyle=":")
    for i0, blo, bhi, bcol, blab in d.get("boxes", []):  # FVG / OB boxes
        x0 = max(i0, -0.5)
        ax.add_patch(Rectangle((x0, blo), n - x0, bhi - blo, color=bcol, alpha=0.22, lw=0))
        ax.text(x0 + 0.3, bhi, blab, color=bcol, fontsize=7, va="bottom", fontweight="bold")
    for mi, mp, mt, mc in d.get("marks", []):  # BOS / CHoCH / swing marks
        if 0 <= mi < n:
            ax.annotate(mt, (mi, mp), color=mc, fontsize=7, ha="center", fontweight="bold",
                        xytext=(0, 6 if mt.endswith("H") or "▲" in mt else -10),
                        textcoords="offset points")
    for x0, x1, y, slab, scol in d.get("segs", []):  # BOS/CHoCH line: swing se break tak
        if x1 >= 0:
            ax.hlines(y, max(x0, -0.5), x1, color=scol, linewidth=1.2, linestyle="--")
            ax.text((max(x0, 0) + x1) / 2, y, slab, color=scol, fontsize=7, ha="center",
                    va="bottom", fontweight="bold")
    for y, ylab, ycol in d.get("hlines", []):  # liquidity / PDH / PDL lines
        ax.axhline(y, color=ycol, linewidth=0.9, linestyle="-.", alpha=0.8)
        ax.text(0.5, y, f" {ylab}", color=ycol, fontsize=7, va="bottom")
    if show_levels:
        if d.get("zone"):
            zl, zh = d["zone"]
            ax.axhspan(zl, zh, color="#a78bfa", alpha=0.18)
            ax.text(1, zh, " OB zone", color="#c4b5fd", fontsize=8, va="bottom")
        for price, lab, col in d.get("levels", []):
            ax.axhline(price, color=col, linewidth=1.2, linestyle="--", alpha=0.9)
            tags.append((price, lab, col, "white"))
    lo_y, hi_y = ax.get_ylim()
    gap = (hi_y - lo_y) * 0.048  # tags ek dusre pe na chadhe
    tags.sort(key=lambda x: x[0])
    ys = []
    for price, *_ in tags:
        y = max(price, ys[-1] + gap) if ys else price
        ys.append(y)
    over = ys[-1] - (hi_y - gap / 2) if ys else 0
    if over > 0:
        ys = [y - over for y in ys]
    for (price, lab, col, fc), y in zip(tags, ys):  # right side value tags (app jaisa)
        ax.text(n + 0.8, y, f" {lab} {price:.2f}", color=fc, fontsize=7.5,
                va="center", ha="left", clip_on=False,
                bbox=dict(boxstyle="round,pad=0.25", fc=col, ec="none"))
    ax.set_yticks([])
    ax.set_xticks([])
    for sp in ax.spines.values():
        sp.set_color(GRID)
    ax.grid(color=GRID, linewidth=0.6)
    ax.text(0.5, 0.5, BRAND, transform=ax.transAxes, fontsize=54, color="white",
            alpha=0.05, ha="center", va="center", fontweight="bold")
    ax.set_title(d.get("title", ""), color="white", fontsize=12, loc="left",
                 fontweight="bold")
    leg = ax.legend(loc="upper left", fontsize=8, facecolor=BG, edgecolor=GRID)
    for t in leg.get_texts():
        t.set_color(TXT)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=BG)
    return Image.open(io.BytesIO(buf.getvalue())).convert("RGB")

def make_chart(d):
    # SMC style chart: structure labels, BOS/CHoCH, FVG, Order Block, EMA, Entry/SL/TP arrows
    from matplotlib.patches import FancyBboxPatch
    from matplotlib.colors import LinearSegmentedColormap
    full = d.get("full") or d["c"]
    N = len(full)
    n = min(len(d["c"]), 55)
    off = N - n
    c = full[off:]
    G_UP, G_DN = "#8fe36b", "#e5484d"
    fig = Figure(figsize=(10, 7.4), dpi=200, facecolor="#06120b")  # HD: 2000x1480
    FigureCanvasAgg(fig)
    bgx = fig.add_axes([0, 0, 1, 1])
    bgx.axis("off")
    cmap = LinearSegmentedColormap.from_list("g", ["#0f2e1d", "#071510", "#040a07"])
    bgx.imshow([[0], [0.5], [1]], aspect="auto", cmap=cmap, extent=[0, 1, 0, 1],
               interpolation="bicubic")
    action = d.get("action")
    ax = fig.add_axes([0.04, 0.22, 0.80, 0.61] if action else [0.04, 0.09, 0.80, 0.74])
    ax.patch.set_alpha(0)
    RX = n + 27  # right side labels ki jagah

    # ---- SMC data pehle nikaalo (taaki y-range me zones bhi aayein) ----
    px = c[-1]["c"]
    cand_lo = min(x["l"] for x in c)
    cand_hi = max(x["h"] for x in c)
    span = (cand_hi - cand_lo) or 1
    near = lambda lo_, hi_: lo_ > cand_lo - span * 0.8 and hi_ < cand_hi + span * 0.8
    smc = {"ev": [], "hs": [], "ls": [], "fvg": [], "ob": [], "eqh": [], "eql": []}
    try:
        if d.get("ema_only"):
            raise StopIteration  # /ema chart: sirf EMA + cross points
        atr = atr_list(full)
        trend, ev, hs, ls = structure(full)
        smc.update(ev=ev, hs=hs, ls=ls)
        if not d.get("no_auto_fvg"):
            fv = [f for f in fvgs(full, atr) if near(f["lo"], f["hi"])]
            bull = sorted([f for f in fv if f["side"] == "bull"], key=lambda f: abs(px - f["hi"]))[:1]
            bear = sorted([f for f in fv if f["side"] == "bear"], key=lambda f: abs(f["lo"] - px))[:1]
            smc["fvg"] = bull + bear
        if not d.get("no_auto_ob"):
            obs = [o for o in order_blocks(full, atr) if near(o["lo"], o["hi"])]
            ob_b = min([o for o in obs if o["side"] == "bull"], key=lambda o: abs(px - o["hi"]), default=None)
            ob_s = min([o for o in obs if o["side"] == "bear"], key=lambda o: abs(o["lo"] - px), default=None)
            smc["ob"] = [o for o in (ob_b, ob_s) if o]
        eqh, eql = liquidity(full, hs, ls, atr)
        smc["eqh"] = [v for v in eqh if near(v[1], v[1])][-1:]
        smc["eql"] = [v for v in eql if near(v[1], v[1])][-1:]
    except StopIteration:
        pass
    except Exception as ex:
        print("smc calc error:", ex, flush=True)

    # ---- y limits (candles + EMA + levels + zones) ----
    ys = [cand_lo, cand_hi]
    emas = {}
    cl = [x["c"] for x in full]
    for key, per in (("e20", 20), ("e50", 50), ("e200", 200)):
        if N >= per:
            emas[key] = ema(cl, per)[off:]
    for key in ("e20", "e50"):
        if key in emas:
            ys += emas[key]
    ys += [p for p, _, _ in d.get("levels", [])]
    for z in smc["fvg"] + smc["ob"]:
        ys += [z["lo"], z["hi"]]
    ys += [v for _, v in smc["eqh"] + smc["eql"]]
    lo_y, hi_y = min(ys), max(ys)
    if "e200" in emas:  # EMA 200 bahut door na ho to range me lo
        e2 = emas["e200"]
        if min(e2) > lo_y - (hi_y - lo_y) * 0.6 and max(e2) < hi_y + (hi_y - lo_y) * 0.6:
            lo_y, hi_y = min(lo_y, min(e2)), max(hi_y, max(e2))
    pad = (hi_y - lo_y) * 0.10 or 1
    ax.set_ylim(lo_y - pad, hi_y + pad * 1.3)
    ax.set_xlim(-1, RX)

    # ---- candles ----
    for i, x in enumerate(c):
        col = G_UP if x["c"] >= x["o"] else G_DN
        ax.vlines(i, x["l"], x["h"], color=col, linewidth=1.1, zorder=3)
        lo, hi = sorted((x["o"], x["c"]))
        ax.add_patch(Rectangle((i - 0.33, lo), 0.66, max(hi - lo, (hi_y - lo_y) * 0.002),
                               facecolor=col, edgecolor="#1a1a1a", linewidth=0.6, zorder=4))

    # ---- EMA lines ----
    for key, col, lab in (("e20", "#ff4fa3", "EMA 20"), ("e50", "#4f8dff", "EMA 50"),
                          ("e200", "#ffd400", "EMA 200")):
        if key in emas:
            ax.plot(range(n), emas[key], color=col, linewidth=1.5, alpha=0.9, zorder=2, label=lab)

    # ---- EMA cross points ----
    for xi, xp, xl, xc in d.get("xmarks", []):
        if xi >= off:
            ax.scatter([xi - off], [xp], s=110, facecolor=xc, edgecolor="white", linewidth=1.5, zorder=9)
            ax.annotate(xl, (xi - off, xp), xytext=(0, 14), textcoords="offset points", color=xc,
                        fontsize=8, ha="center", fontweight="bold", zorder=9,
                        bbox=dict(boxstyle="round,pad=0.2", fc="#0d2418", ec=xc, alpha=0.95))
    # ---- Equilibrium (range ka 50%) ----
    eqp = (cand_lo + cand_hi) / 2
    ax.hlines(eqp, -0.5, n, color="#9fb5a8", linewidth=0.8, linestyle=(0, (2, 4)), alpha=0.6, zorder=1)

    # ---- label placer: labels ek dusre pe na chadhein ----
    yl0, yh0 = ax.get_ylim()
    axw_in = 8.0
    axh_in = 7.4 * (0.61 if action else 0.74)
    placed = []

    def put(x, y, text, fs=8, ha="center", va="center", **kw):
        lines_ = text.split("\n")
        w = max(len(t_) for t_ in lines_) * fs * 0.0085 / axw_in * (RX + 1)
        h = len(lines_) * fs * 0.0185 / axh_in * (yh0 - yl0)
        x0_ = x - w / 2 if ha == "center" else (x if ha == "left" else x - w)
        y0_ = y - h / 2 if va == "center" else (y if va == "bottom" else y - h)
        if x0_ < -0.8:  # chart ke bahar na jaaye
            x += -0.8 - x0_
            x0_ = -0.8
        for dy in (0, h * 1.1, -h * 1.1, h * 2.2, -h * 2.2, h * 3.3, -h * 3.3):
            box = (x0_, y0_ + dy, x0_ + w, y0_ + dy + h)
            if box[1] < yl0 or box[3] > yh0:
                continue
            if all(box[2] < b_[0] or box[0] > b_[2] or box[3] < b_[1] or box[1] > b_[3]
                   for b_ in placed):
                placed.append(box)
                return ax.text(x, y + dy, text, fontsize=fs, ha=ha, va=va, **kw)
        return None  # jagah nahi mili to label skip

    # ---- SMC overlay drawing ----
    shown = []
    try:
        ev, hs, ls = smc["ev"], smc["hs"], smc["ls"]
        # swing labels: Higher High / Lower High / Higher Low / Lower Low
        prev = None
        labs = []
        for i, p in hs:
            if prev is not None and i >= off:
                labs.append((i, p, "Higher High" if p > prev else "Lower High", "top"))
            prev = p
        prev = None
        for i, p in ls:
            if prev is not None and i >= off:
                labs.append((i, p, "Higher Low" if p > prev else "Lower Low", "bot"))
            prev = p
        labs.sort(key=lambda z: z[0])
        for i, p, t, pos in labs[-6:]:
            dy = pad * 0.35
            put(i - off, p + dy if pos == "top" else p - dy, t, fs=8.5, color="white",
                ha="center", va="bottom" if pos == "top" else "top", fontweight="bold", zorder=6)
        if labs:
            shown.append("HH/HL/LH/LL")
        # BOS / CHoCH lines
        nb = 0
        for i, side, kind, lvl, si in ev[-3:]:
            if i < off:
                continue
            nb += 1
            x0, x1 = max(si - off, 0), i - off
            col = "#b6f09c" if kind == "BOS" else "#7dff7a"
            ax.hlines(lvl, x0, x1, color=col, linewidth=1.3, zorder=5)
            sub = "(Break of Structure)" if kind == "BOS" else "(Change of Character)"
            put((x0 + x1) / 2, lvl - (pad * 0.08 if side == "bull" else -pad * 0.08),
                f"{kind}\n{sub}", fs=7.5, color=col if kind == "CHoCH" else "white",
                ha="center", va="top" if side == "bull" else "bottom", fontweight="bold",
                linespacing=1.1, zorder=6)
        shown.append(f"BOS/CHoCH {nb}" if nb else "BOS/CHoCH: none recent")
        # FVG boxes (purane bhi, agar abhi tak fill nahi hue)
        callout_done = d.get("no_callout", False)
        for f in smc["fvg"]:
            col = "#5fd35f" if f["side"] == "bull" else "#ff6b6b"
            x0 = max(f["i"] - off - 0.5, -0.5)
            ax.add_patch(Rectangle((x0, f["lo"]), n - x0 - 0.5, f["hi"] - f["lo"], facecolor=col,
                                   alpha=0.22, edgecolor=col, linewidth=0.9, zorder=1))
            tag = "Bullish FVG" if f["side"] == "bull" else "Bearish FVG"
            put(max(x0, 0) + 0.4, (f["lo"] + f["hi"]) / 2, tag, fs=7.5, color=col, ha="left",
                va="center", fontweight="bold", zorder=7,
                bbox=dict(boxstyle="round,pad=0.15", fc="#0d2418", ec="none", alpha=0.85))
            if not callout_done:
                callout_done = True
                yl, yh = ax.get_ylim()
                best = None
                for cx in (n * 0.2, n * 0.4, n * 0.6):
                    for cy in (yh - (yh - yl) * 0.12, yl + (yh - yl) * 0.12,
                               (f["lo"] + f["hi"]) / 2 + (yh - yl) * 0.25,
                               (f["lo"] + f["hi"]) / 2 - (yh - yl) * 0.25):
                        if not (yl < cy < yh):
                            continue
                        band = (yh - yl) * 0.08
                        hit = sum(1 for j, x in enumerate(c) if abs(j - cx) < 7
                                  and x["l"] < cy + band and x["h"] > cy - band)
                        score = (hit * 10 + abs(cx - (x0 + 2)) * 0.3
                                 + abs(cy - (f["lo"] + f["hi"]) / 2) / (yh - yl) * 25)
                        if best is None or score < best[0]:
                            best = (score, cx, cy)
                if best:
                    ax.annotate("Fair Value Gap (FVG)\nInefficiency Zone - price\nlikely to return to fill",
                                xy=(max(x0, 0) + 1.5, (f["lo"] + f["hi"]) / 2), xytext=(best[1], best[2]),
                                color="white", fontsize=7.5, ha="center", va="center", zorder=8,
                                bbox=dict(boxstyle="round,pad=0.45", fc="#0d2418", ec=col, lw=1.2),
                                arrowprops=dict(arrowstyle="-", color=col, lw=1))
        if not d.get("no_auto_fvg"):
            shown.append(f"FVG {len(smc['fvg'])}" if smc["fvg"] else "FVG: none open near price")
        # Order Blocks (bullish + bearish dono)
        for ob in smc["ob"]:
            x0 = max(ob["i"] - off - 0.6, -0.6)
            col = "#5fd35f" if ob["side"] == "bull" else "#ff9f1c"
            w = n - x0 - 0.4
            ax.add_patch(FancyBboxPatch((x0, ob["lo"]), w, ob["hi"] - ob["lo"],
                                        boxstyle="round,pad=0,rounding_size=0.6",
                                        facecolor=col, alpha=0.14, edgecolor=col, lw=1.6, zorder=1))
            put(n * 0.55, (ob["lo"] + ob["hi"]) / 2,
                "Bullish Order Block" if ob["side"] == "bull" else "Bearish Order Block",
                fs=8, color="white", ha="center", va="center", fontweight="bold", zorder=7,
                bbox=dict(boxstyle="round,pad=0.2", fc="#0d2418", ec=col, alpha=0.95))
        if not d.get("no_auto_ob"):
            shown.append(f"OB {len(smc['ob'])}" if smc["ob"] else "OB: none valid near price")
        # Liquidity: equal highs / lows
        for (_, v), lab, col in [(z, "$$$ EQH - Buy-side Liquidity", "#ff8a8a") for z in smc["eqh"]] + \
                                [(z, "$$$ EQL - Sell-side Liquidity", "#8fe36b") for z in smc["eql"]]:
            ax.hlines(v, -0.5, n, color=col, linewidth=1, linestyle="-.", alpha=0.9, zorder=2)
            put(n * 0.3, v, lab, fs=7.5, color=col, ha="center", va="bottom",
                fontweight="bold", zorder=6)
        nl = len(smc["eqh"]) + len(smc["eql"])
        shown.append(f"Liquidity {nl}" if nl else "Liquidity: none")
    except Exception as ex:
        print("smc overlay error:", ex, flush=True)
    put(n - 0.5, eqp, "Equilibrium 50%", fs=7, color="#9fb5a8", ha="right", va="bottom", zorder=6)
    if d.get("ema_only"):
        shown = ["● = EMA cross point", "EMA 20 / 50 / 200"]
    # chart pe kya kya dikh raha hai (info strip)
    ax.text(0.995, 0.985, "  ·  ".join(shown), transform=ax.transAxes, color="#cfd8d3",
            fontsize=7.5, ha="right", va="top", zorder=9,
            bbox=dict(boxstyle="round,pad=0.35", fc="#0d2418", ec="#2f5c43", alpha=0.9))

    # ---- extra boxes / lines (commands se) ----
    for i0, blo, bhi, bcol, blab in d.get("boxes", []):
        x0 = max(i0 - (len(d["c"]) - n), -0.5)
        ax.add_patch(Rectangle((x0, blo), n - x0, bhi - blo, color=bcol, alpha=0.18, lw=0, zorder=1))
        ax.text(x0 + 0.3, bhi, blab, color=bcol, fontsize=7.5, va="bottom", fontweight="bold")
    for y, ylab, ycol in d.get("hlines", []):
        ax.hlines(y, -0.5, n, color=ycol, linewidth=1, linestyle="-.", alpha=0.9, zorder=2)
        ax.text(0, y, f" {ylab}", color=ycol, fontsize=7.5, va="bottom", fontweight="bold")

    # ---- Entry / SL / TP: arrows + labels (right side) ----
    names = {"ENTRY": "Potential Entry", "SL": "Stop Loss", "TP1": "TP1",
             "TP2": "TP2", "TP3": "Target / Take Profit", "EMA200": "EMA 200"}
    merged = {}  # same price wale levels ek label me (jaise SL = Entry breakeven)
    for p_, lab_, col_ in d.get("levels", []):
        k_ = round(p_, 6)
        if k_ in merged:
            merged[k_] = (p_, merged[k_][1] + "+" + lab_, merged[k_][2])
        else:
            merged[k_] = (p_, lab_, col_)
    lv = sorted(merged.values(), key=lambda z: z[0])
    ylo, yhi = ax.get_ylim()
    gap = (yhi - ylo) * 0.045
    yt, last = [], None
    for p, _, _ in lv:
        y = p if last is None else max(p, last + gap)
        yt.append(y)
        last = y
    for (p, lab, col), y in zip(lv, yt):
        lc = "#ff5c5c" if lab.startswith("SL") else "#8fe36b" if lab.startswith("TP") else "white"
        ax.hlines(p, n - 10, n + 0.3, color=lc, linewidth=1, linestyle="--", alpha=0.6, zorder=2)
        ax.annotate("", xy=(n + 4.2, p), xytext=(n + 0.3, p),
                    arrowprops=dict(arrowstyle="-|>", color=lc, lw=1.6, mutation_scale=15), zorder=7)
        nm = " / ".join(names.get(x_, x_) for x_ in lab.split("+"))
        ax.annotate(f"{nm}  {p:,.2f}", xy=(n + 4.6, y), color=lc, fontsize=8.5,
                    va="center", ha="left", fontweight="bold", annotation_clip=False, zorder=8)

    # ---- axis style ----
    ax.yaxis.tick_right()
    ax.tick_params(axis="y", colors="#cfd8d3", labelsize=8.5, length=0)
    for sp in ("top", "left", "bottom"):
        ax.spines[sp].set_visible(False)
    ax.spines["right"].set_color("#8a9a90")
    ax.spines["right"].set_position(("data", RX))
    ax.axhline(c[-1]["c"], xmax=(n + 1) / (RX + 1), color="#8a9a90", lw=0.7, ls=":", zorder=1)
    step = max(1, n // 8)
    ticks = list(range(0, n, step))
    ax.set_xticks(ticks)
    ax.set_xticklabels([datetime.fromtimestamp(c[i]["t"], DXB).strftime("%H:%M") for i in ticks],
                       color="#cfd8d3", fontsize=8.5)
    ax.tick_params(axis="x", length=0)
    leg = ax.legend(loc="upper left", fontsize=8, frameon=False, ncol=3)
    for t in leg.get_texts():
        t.set_color("#cfd8d3")
    title = d.get("title", "")
    fig.text(0.04, 0.925, title, color="white", fontsize=19, fontweight="bold")
    fig.text(0.04, 0.885, "Smart Money Concepts · EMA 20 / 50 / 200 · Dubai time", color="#9fb5a8",
             fontsize=10)
    if action:  # bada NEXT ACTION note
        atxt, acol = action
        parts = atxt.split(" - ")
        lines_, cur = [], ""
        for prt in parts:
            nxt = (cur + " - " + prt) if cur else prt
            if len(nxt) > 52 and cur:
                lines_.append(cur)
                cur = prt
            else:
                cur = nxt
        lines_.append(cur)
        fg = "#06120b" if acol in ("#0ecb81", "#8fe36b", "#ffd400") else "white"
        fig.text(0.44, 0.085, "▶ NEXT ACTION\n" + "\n".join(lines_), color=fg, fontsize=13.5,
                 fontweight="bold", ha="center", va="center", linespacing=1.25,
                 bbox=dict(boxstyle="round,pad=0.6", fc=acol, ec="white", lw=1.5))
    fig.text(0.96, 0.025, "SHAUSHAB", color="#8fe36b", fontsize=15,
             fontweight="bold", ha="right")
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=fig.get_facecolor())
    return buf.getvalue()

def make_anim(d, steps=24):
    # GIF: candles ek ek karke bante hain, phir Entry/SL/TP lines aati hain
    n = len(d["c"])
    start = max(1, n - steps)
    frames, durs = [], []
    for k in range(start, n + 1):
        frames.append(_frame(d, k, False, size=(7.2, 4.32)))
        durs.append(110)
    final = _frame(d, n, True, size=(7.2, 4.32))
    frames.append(final)
    durs.append(3500)  # aakhri frame thodi der ruke
    pal = [f.quantize(colors=128, method=Image.Quantize.FASTOCTREE) for f in frames]
    buf = io.BytesIO()
    pal[0].save(buf, format="GIF", save_all=True, append_images=pal[1:],
                duration=durs, loop=0, optimize=True)
    return buf.getvalue()

_stickers = {}

def make_sticker(kind):
    # static fallback: rocket animation ka aakhri frame, WEBP sticker
    if kind in _stickers:
        return _stickers[kind]
    make_sticker_anim(kind)
    img = _anims["last_" + kind].resize((512, 512))
    out = io.BytesIO()
    img.save(out, format="WEBP")
    _stickers[kind] = out.getvalue()
    return _stickers[kind]

_anims = {}
ROCKET_B64 = "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"

def _font(size):
    from PIL import ImageFont
    try:
        path = os.path.join(matplotlib.get_data_path(), "fonts", "ttf", "DejaVuSans-Bold.ttf")
        return ImageFont.truetype(path, size)
    except Exception:
        return ImageFont.load_default()

def _rocket_img():
    # rocket + mask: dark background hata ke sirf rocket
    import base64
    from PIL import ImageFilter
    im = Image.open(io.BytesIO(base64.b64decode(ROCKET_B64))).convert("RGB")
    lum = im.convert("L")
    mask = lum.point(lambda v: 0 if v < 38 else min(255, (v - 38) * 6))
    mask = mask.filter(ImageFilter.GaussianBlur(1.2))
    return im, mask

def make_sticker_anim(kind):
    # Rocket animation (aapki rocket image): upar udta rocket, jalti flame, chalte stars,
    # SHAUSHAB naam letter by letter
    if kind in _anims:
        return _anims[kind]
    from PIL import ImageDraw, ImageChops, ImageFilter
    top = {"rocket": "TO THE MOON", "brand": "CRYPTO BOT"}.get(kind, "TO THE MOON")
    W = H = 480
    rk, rmask = _rocket_img()
    rnd = random.Random(5)
    stars = [(rnd.uniform(0, W), rnd.uniform(0, H), rnd.uniform(1, 2.6), rnd.uniform(4, 11),
              rnd.choice([(255, 255, 255), (180, 120, 255), (140, 90, 255)])) for _ in range(70)]
    bg = Image.new("RGB", (W, H))
    bd = ImageDraw.Draw(bg)
    for y in range(H):  # dark purple gradient
        f = y / H
        bd.line([(0, y), (W, y)], fill=(int(14 - 10 * f), int(6 - 4 * f), int(28 - 20 * f)))
    f_big, f_top = _font(46), _font(30)
    nf = 24
    frames = []
    for i in range(nf):
        t = i / (nf - 1)
        im = bg.copy()
        d = ImageDraw.Draw(im)
        for sx, sy, r, sp, col in stars:  # stars neeche-left jaate hain = rocket upar
            x = (sx - i * sp * 0.45) % W
            y = (sy + i * sp) % H
            d.ellipse([x - r, y - r, x + r, y + r], fill=col)
        ease = 1 - (1 - min(1, t * 1.5)) ** 3
        ox = int(-60 + 60 * ease + rnd.uniform(-2, 2))
        oy = int(140 - 110 * ease + rnd.uniform(-2, 2))

        # flame glow (flicker)
        glow = Image.new("RGB", (W, H))
        gd = ImageDraw.Draw(glow)
        fx, fy = 90 + ox + 112, 20 + oy + 330
        fl = 1 + 0.35 * abs(math.sin(i * 1.9))
        gd.ellipse([fx - 34 * fl, fy - 20, fx + 34 * fl, fy + 70 * fl], fill=(255, 120, 30))
        gd.ellipse([fx - 16 * fl, fy - 8, fx + 16 * fl, fy + 40 * fl], fill=(255, 220, 90))
        glow = glow.filter(ImageFilter.GaussianBlur(14))
        im = ImageChops.screen(im, glow)
        im.paste(rk, (90 + ox, 20 + oy), rmask)  # sirf rocket, background nahi
        d = ImageDraw.Draw(im)
        if t > 0.35:
            a = min(1, (t - 0.35) * 3)
            col = (int(255 * a), int(212 * a), 0)
            w = d.textlength(top, font=f_top)
            d.text(((W - w) / 2, 18), top, font=f_top, fill=col)
        d.rounded_rectangle([40, 392, 440, 462], radius=18, fill=(14, 203, 129))
        k = min(len(BRAND), int(t * 2.4 * len(BRAND)) + 1)
        txt = BRAND[:k]
        w = d.textlength(BRAND, font=f_big)
        d.text(((W - w) / 2, 400), txt, font=f_big, fill=(255, 255, 255))
        frames.append(im.resize((400, 400), Image.LANCZOS)
                      .quantize(colors=96, method=Image.Quantize.FASTOCTREE))
    durs = [70] * nf
    durs[-1] = 1200
    buf = io.BytesIO()
    frames[0].save(buf, format="GIF", save_all=True, append_images=frames[1:],
                   duration=durs, loop=0, optimize=True)
    _anims[kind] = buf.getvalue()
    _anims["last_" + kind] = frames[-1].convert("RGB")
    return _anims[kind]

_q = queue.Queue()

def _tg_call(method, data, files=None):
    for attempt in range(1, 9):
        try:
            r = requests.post(f"https://api.telegram.org/bot{TOKEN}/{method}",
                              data=data, files=files, timeout=60)
            if r.status_code == 200:
                print("Telegram OK", method, flush=True)
                return True
            print("Telegram error:", r.status_code, r.text[:300], flush=True)
            if r.status_code == 429:  # Telegram ne roka hai, utna ruk ke dobara
                try:
                    wait = int(r.json().get("parameters", {}).get("retry_after", 10))
                except Exception:
                    wait = 10
                time.sleep(wait + 1)
                continue
            if r.status_code in (400, 401, 403, 404):
                return False  # galat format / token ka masla, retry se fayda nahi
        except Exception as ex:
            print("Telegram error (try", attempt, "):", ex, flush=True)
        time.sleep(min(5 * attempt, 20))
    print("Telegram: message chhod diya (8 koshish ke baad)", flush=True)
    return False

def _buttons(tg):
    # Telegram copy buttons: tap karte hi value copy ho jati hai
    rows = tg.get("copy") or []
    if not rows:
        return {}
    kb = [[{"text": f"📋 {lab}: {val}", "copy_text": {"text": val}} for lab, val in row]
          for row in rows]
    return {"reply_markup": json.dumps({"inline_keyboard": kb})}

def _post(item):
    text, tg = item
    tg = tg or {}
    cap = tg.get("html") or tg_plain(text)
    chat = tg.get("chat") or CHAT
    anim_ok = False  # chart animation band (sirf static SMC chart)
    sent = False
    for rm in ([_buttons(tg), {}] if tg.get("copy") else [{}]):  # buttons fail ho to bina buttons
        if sent:
            break
        if CHARTS and tg.get("chart") and anim_ok:
            try:  # moving chart (GIF)
                gif = make_anim(tg["chart"])
                sent = _tg_call("sendAnimation", {"chat_id": chat, "caption": cap[:1024],
                                                  "parse_mode": "HTML", **rm},
                                files={"animation": ("chart.gif", gif, "image/gif")})
            except Exception as ex:
                print("animation error:", ex, flush=True)
        if CHARTS and tg.get("chart") and not sent:
            try:
                png = make_chart(tg["chart"])
                long_ = len(cap) > 1024  # Telegram caption limit: lamba text alag message me
                pcap = cap.split("\n")[0] if long_ else cap
                sent = _tg_call("sendPhoto", {"chat_id": chat, "caption": pcap,
                                              "parse_mode": "HTML", **({} if long_ else rm)},
                                files={"photo": ("chart.png", png, "image/png")})
                if sent and long_:
                    time.sleep(1.0)
                    _tg_call("sendMessage", {"chat_id": chat, "text": cap, "parse_mode": "HTML", **rm})
            except Exception as ex:
                print("chart error:", ex, flush=True)
        if not sent:
            sent = _tg_call("sendMessage", {"chat_id": chat, "text": cap, "parse_mode": "HTML", **rm})
    if not sent:  # HTML me masla ho to plain text
        _tg_call("sendMessage", {"chat_id": chat, "text": text})
    if CHARTS and tg.get("sticker"):
        try:
            time.sleep(1.2)
            ok = False
            if ANIMATION:
                try:
                    ok = _tg_call("sendAnimation", {"chat_id": chat},
                                  files={"animation": ("shaushab.gif",
                                                       make_sticker_anim(tg["sticker"]), "image/gif")})
                except Exception as ex:
                    print("sticker anim error:", ex, flush=True)
            if not ok:
                _tg_call("sendSticker", {"chat_id": chat},
                         files={"sticker": ("s.webp", make_sticker(tg["sticker"]), "image/webp")})
        except Exception as ex:
            print("sticker error:", ex, flush=True)

def _worker():
    while True:
        item = _q.get()
        try:
            _post(item)
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

def send(text, wa=False, tg=None):
    # tg = {"html": stylish Telegram text, "chart": chart data, "sticker": "rocket"/"brand"}
    _q.put((text, tg))  # main loop block nahi hota, alert background me jata hai
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
            return f"{side}, {hrs:.1f} hours ago" if hrs < 48 else f"{side}, {hrs / 24:.1f} days ago"
    days = (c[-1]["t"] - c[start]["t"]) / 86400
    return f"none in last ~{days:.0f} days"

def status_15m():
    # bot start hone pe 15m ka haal: trend, EMA 200, last cross kab hua
    lines = ["📊 15m Status"]
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
            pos = "ABOVE ⬆️" if px > e200[-1] else "BELOW ⬇️"
            start = min(200, len(c) - 2)  # EMA 200 warm-up ke baad hi cross gino
            lines.append(
                f"\n{sym}: {trend} | Price {px:.2f}\n"
                f"EMA 20: {e20[-1]:.2f} | EMA 50: {e50[-1]:.2f} | EMA 200: {e200[-1]:.2f}\n"
                f"Price is {pos} EMA 200 ({dist:+.2f}%)\n"
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
        return "ABOVE", e200[-1]
    if closes[-2] >= e200[-2] and closes[-1] < e200[-1]:
        return "BELOW", e200[-1]
    return None

def chart_data(c, title, levels=(), zone=None, n=70):
    cl = [x["c"] for x in c]
    e20, e50, e200 = ema(cl, 20), ema(cl, 50), ema(cl, 200)
    return {"full": c, "c": c[-n:], "e20": e20[-n:], "e50": e50[-n:],
            "e200": e200[-n:] if len(c) >= 200 else None,
            "levels": list(levels), "zone": zone, "title": title}

def signal_html(sym, label, kind, side, e, sl, tp, extra, trend_ok, late_t, act=""):
    risk = abs(e - sl)
    pct = risk / e * 100 if e else 0
    h = (f"{'🟢' if side == 'BUY' else '🔴'} <b>{side} · {sym}</b>  <i>({label})</i>\n"
         f"━━━━━━━━━━━━━━\n"
         f"▶️ <b>NEXT ACTION: {html.escape(act)}</b>\n"
         f"━━━━━━━━━━━━━━\n"
         f"📌 <b>Type:</b> {kind}\n"
         f"🎯 <b>Entry:</b> <code>{e:.2f}</code>{' (limit)' if kind == 'OB Retest' else ''}\n"
         f"🛑 <b>SL:</b> <code>{sl:.2f}</code>  <i>(risk {pct:.2f}%)</i>\n"
         f"✅ <b>TP1:</b> <code>{tp[0]:.2f}</code>  <i>1R</i>\n"
         f"✅ <b>TP2:</b> <code>{tp[1]:.2f}</code>  <i>2R</i>\n"
         f"🚀 <b>TP3:</b> <code>{tp[2]:.2f}</code>  <i>3R</i>\n")
    for ln in (extra or "").split("\n"):
        if ln:
            h += f"📦 {html.escape(ln)}\n"
    if trend_ok:
        h += f"📈 15m trend: {side} ✔\n"
    if late_t:
        h += f"⏰ <i>Late alert: {late_t} candle (closed during bot restart)</i>\n"
    return h.rstrip("\n") + FOOT

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
        return f"📅 Daily Summary {day}\nNo signals today."
    lines = [f"📅 Daily Summary {day}"]
    tot = {"signals": 0, "tp1": 0, "sl": 0}
    for key in sorted(stats):
        d = stats[key]
        for k in tot:
            tot[k] += d[k]
        done = d["tp1"] + d["sl"]
        wr = f"{100 * d['tp1'] / done:.0f}%" if done else "-"
        lines.append(f"{key}: {d['signals']} signals | TP1 {d['tp1']} TP2 {d['tp2']} "
                     f"TP3 {d['tp3']} | SL {d['sl']} | cancel {d['cancel']} | win {wr}")
    done = tot["tp1"] + tot["sl"]
    wr = f"{100 * tot['tp1'] / done:.0f}%" if done else "-"
    lines.append(f"\nTotal: {tot['signals']} signals, win rate {wr} (win = TP1 hit before SL)")
    return "\n".join(lines)

def check_day(st):
    today = datetime.now(DXB).strftime("%Y-%m-%d")
    if today != st["day"]:
        send(summary_text(st["day"], st["stats"]), wa=True, tg={"sticker": "brand"})
        st["day"], st["stats"] = today, {}
        save_state(st)

def track(st, sym, c1):
    # 1m candles se har open trade ka TP/SL check (TP1 ke baad SL entry pe, TP2 ke baad TP1 pe)
    keep = []
    for t in st["trades"]:
        if t["sym"] != sym:
            keep.append(t)
            continue
        key = f"{sym} {t['tf']}m"
        head = f"{sym} ({t['tf']}m) {t['side']} @ {t['entry']:.2f}"

        def tgm(icon, title, extra_line, action, acol, sticker=None):
            lv = [(t["entry"], "ENTRY", "#5e6673"), (t["sl"], "SL", "#f6465d")] + \
                 [(p, f"TP{i + 1}", "#0a9e66") for i, p in enumerate(t["tps"])]
            ch = chart_data(c1, f"{sym} · 1m · {title}", lv, n=60)
            ch["action"] = (action, acol)
            return {"html": (f"{icon} <b>{title}</b>\n━━━━━━━━━━━━━━\n"
                             f"▶️ <b>NEXT ACTION: {html.escape(action)}</b>\n━━━━━━━━━━━━━━\n"
                             f"🪙 <b>{sym}</b>  <i>({t['tf']}m)</i>  ·  {t['side']}\n"
                             f"🎯 Entry: <code>{t['entry']:.2f}</code>\n{extra_line}" + FOOT),
                    "chart": ch, "sticker": sticker}
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
                    send(f"📥 Limit order filled: {head}",
                         tg=tgm("📥", "LIMIT ORDER FILLED", f"🛑 SL: <code>{t['sl']:.2f}</code>",
                                f"TRADE ACTIVE - HOLD, KEEP SL AT {t['sl']:.2f}", "#4f8dff"))
                elif x["t"] > t["expire"]:
                    bump(st, key, "cancel")
                    send(f"⌛ Limit not filled - CANCEL ORDER: {head}",
                         tg={"html": (f"⌛ <b>LIMIT NOT FILLED</b>\n━━━━━━━━━━━━━━\n"
                                      f"▶️ <b>NEXT ACTION: CANCEL THE LIMIT ORDER - NO TRADE</b>\n"
                                      f"🪙 {html.escape(head)}" + FOOT)})
                    alive = False
                    break
                else:
                    continue
            # SL pehle check (ek hi candle me dono ho to SL maana)
            if (x["l"] <= t["sl"]) if buy else (x["h"] >= t["sl"]):
                if t["hit"] == 0:
                    bump(st, key, "sl")
                    send(f"🛑 Stop loss hit: {head}", wa=True,
                         tg=tgm("🛑", "STOP LOSS HIT", f"❌ SL: <code>{t['sl']:.2f}</code>",
                                "TRADE CLOSED - WAIT FOR THE NEXT SETUP", "#e5484d"))
                else:
                    where = "BREAKEVEN" if t["hit"] == 1 else "TP1 (PROFIT LOCKED)"
                    send(f"↩️ Stopped at {where} after TP{t['hit']}: {head}", wa=True,
                         tg=tgm("↩️", f"STOPPED AT {where}", f"✅ TP{t['hit']} was already hit",
                                "TRADE CLOSED - PROFIT PROTECTED, WAIT FOR NEXT SETUP", "#8fe36b"))
                alive = False
                break
            for n in range(t["hit"] + 1, 4):
                tp = t["tps"][n - 1]
                if (x["h"] >= tp) if buy else (x["l"] <= tp):
                    t["hit"] = n
                    bump(st, key, f"tp{n}")
                    if n == 1:
                        t["sl"] = t["entry"]  # breakeven
                        act = f"BOOK 50% PROFIT - MOVE SL TO ENTRY {t['entry']:.2f} (BREAKEVEN)"
                    elif n == 2:
                        t["sl"] = t["tps"][0]
                        act = f"BOOK 30% MORE - MOVE SL TO TP1 {t['tps'][0]:.2f}"
                    else:
                        act = "CLOSE FULL TRADE - TARGET DONE"
                    send(f"✅ TP{n} hit: {head} -> {tp:.2f} | {act}", wa=True,
                         tg=tgm("🚀" if n == 3 else "✅", f"TP{n} HIT" + (" · FULL TARGET" if n == 3 else ""),
                                f"💰 TP{n}: <code>{tp:.2f}</code>  <i>(+{n}R)</i>", act, "#0ecb81",
                                "rocket" if n >= 2 else None))
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
        on = "🟢 OPEN" if name in active_sessions(now) else "⚪ CLOSED"
        lines.append(f"{SESS_FLAG[name]} {name}: {dxb(op)} - {dxb(cl)}  {on}")
    if now.astimezone(DXB).weekday() >= 5:
        lines.append("Weekend: forex sessions closed (crypto open)")
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
                ov = f"\nOverlap: {name} + {', '.join(act)} (high volume)" if act else ""
                send(f"🔔 {SESS_FLAG[name]} {name} session OPEN\n"
                     f"Time: {dxb(op)} (Dubai)\nCloses at: {dxb(cl)}{ov}")
            else:
                act = active_sessions(now)
                nx = f"\nOpen now: {', '.join(act)}" if act else "\nNo major session open now"
                send(f"🔕 {SESS_FLAG[name]} {name} session CLOSED\n"
                     f"Time: {dxb(cl)} (Dubai){nx}")
    st["sess_sent"] = sent[-30:]

CATCHUP = {"15": 8, "5": 6, "1": 0}  # restart ke gap me chhuti candles (15m: 2 ghante tak)

def process(st, htf, sym, tf, sec, c, late=False):
    # ek closed candle (c[-1]) pe saare signals check karo
    label = tf + "m"
    tag = ""
    t = ""
    if late:
        t = datetime.fromtimestamp(c[-1]["t"] + sec, DXB).strftime("%I:%M %p").lstrip("0")
        tag = f"\n⏰ Late alert: {t} candle (closed during bot restart)"
    if tf == "15":
        br = ema200_break(c)
        if br:
            up = br[0] == "ABOVE"
            act = ("WAIT - LET PRICE RETEST EMA 200 FROM ABOVE, THEN LOOK FOR BUY" if up else
                   "WAIT - LET PRICE RETEST EMA 200 FROM BELOW, THEN LOOK FOR SELL")
            ch = chart_data(c, f"{sym} · 15m · EMA 200 BREAK", [(br[1], "EMA200", "#b8a000")])
            ch["action"] = (act, "#ffd400")
            send(f"⚡ {sym} (15m): price closed {br[0]} EMA 200\n"
                 f"Close: {c[-1]['c']:.2f} | EMA 200: {br[1]:.2f}\n"
                 f"{'Trend may turn bullish' if up else 'Trend may turn bearish'}\n"
                 f"NEXT ACTION: {act}" + tag, wa=True,
                 tg={"html": (f"⚡ <b>EMA 200 BREAK · {sym}</b>  <i>(15m)</i>\n━━━━━━━━━━━━━━\n"
                              f"▶️ <b>NEXT ACTION: {act}</b>\n━━━━━━━━━━━━━━\n"
                              f"Price closed <b>{br[0]}</b> EMA 200 {'⬆️' if up else '⬇️'}\n"
                              f"💰 Close: <code>{c[-1]['c']:.2f}</code>\n"
                              f"🟡 EMA 200: <code>{br[1]:.2f}</code>\n"
                              f"{'📈 Trend may turn bullish' if up else '📉 Trend may turn bearish'}"
                              + (f"\n⏰ <i>Late alert: {t} candle</i>" if late else "") + FOOT),
                     "chart": ch})
        cl = [x["c"] for x in c]
        htf[sym] = "BUY" if ema(cl, 20)[-1] > ema(cl, 50)[-1] else "SELL"
    sigs = []
    ob = find_ob(c, tf)
    if ob:
        sigs.append(ob)
    sigs += find_cross(c, tf)
    if tf in ("1", "5") and sigs:
        if adx_last(c) < ADX_MIN:  # sideways market me alert nahi
            sigs = []
        if SESSION_FILTER and not in_session():
            sigs = []
        if HTF_FILTER and sym in htf:
            sigs = [s for s in sigs if s[1] == htf[sym]]
    close_t = c[-1]["t"] + sec
    for kind, side, e, sl, tp, extra in sigs:
        if kind == "OB Retest":
            act = (f"PLACE {side} LIMIT @ {e:.2f} - SL {sl:.2f} - "
                   f"CANCEL IF NOT FILLED IN {LIMIT_EXPIRE} CANDLES")
        else:
            act = f"ENTER {side} NOW @ {e:.2f} - SL {sl:.2f} - BOOK 50% AT TP1"
        msg = (f"{'🟢' if side == 'BUY' else '🔴'} {side} {sym} ({label})\n"
               f"NEXT ACTION: {act}\n"
               f"Type: {kind}\n"
               f"{'Entry (limit)' if kind == 'OB Retest' else 'Entry'}: {e:.2f}\nSL: {sl:.2f}\n"
               f"TP1: {tp[0]:.2f}\nTP2: {tp[1]:.2f}\n"
               f"TP3: {tp[2]:.2f}")
        if extra:
            msg += "\n" + extra
        if tf in ("1", "5") and HTF_FILTER:
            msg += f"\n15m trend: {side} ✔"
        lv = [(e, "ENTRY", "#5e6673"), (sl, "SL", "#f6465d"),
              (tp[0], "TP1", "#0a9e66"), (tp[1], "TP2", "#0a9e66"), (tp[2], "TP3", "#0a9e66")]
        zone = None
        if kind == "OB Retest" and extra.startswith("OB zone:"):
            try:
                zl, zh = extra.split("\n")[0].replace("OB zone:", "").split(" - ")
                zone = (float(zl), float(zh))
            except Exception:
                zone = None
        stk = "brand" if tf == "15" else ("brand" if random.random() < 0.2 else None)
        send(msg + tag, wa=True, tg={
            "html": signal_html(sym, label, kind, side, e, sl, tp, extra,
                                tf in ("1", "5") and HTF_FILTER, t, act),
            "chart": dict(chart_data(c, f"{sym} · {label} · {side} SIGNAL · {kind}", lv, zone),
                          action=(act, "#0ecb81" if side == "BUY" else "#e5484d")),
            "sticker": stk,
            "copy": [[("Entry", f"{e:.2f}"), ("SL", f"{sl:.2f}")],
                     [("TP1", f"{tp[0]:.2f}"), ("TP2", f"{tp[1]:.2f}"), ("TP3", f"{tp[2]:.2f}")]]})
        limit = kind == "OB Retest"
        st["trades"].append({
            "sym": sym, "tf": tf, "side": side, "entry": e,
            "sl": sl, "tps": tp, "hit": 0, "filled": not limit,
            "chk": close_t - 60,
            "expire": close_t + LIMIT_EXPIRE * sec})
        bump(st, f"{sym} {tf}m", "signals")
        if not limit:
            bump(st, f"{sym} {tf}m", "filled")

# ---------- SMC analysis + Telegram commands (/fvg, /smc, /trend ...) ----------
BARS = {"1m": "1m", "3m": "3m", "5m": "5m", "15m": "15m", "30m": "30m",
        "1h": "1H", "2h": "2H", "4h": "4H", "1d": "1Dutc"}
SYM_ALIAS = {"btc": "BTCUSDT", "bitcoin": "BTCUSDT", "btcusdt": "BTCUSDT",
             "gold": "XAUUSDT", "xau": "XAUUSDT", "xauusdt": "XAUUSDT", "xauusd": "XAUUSDT"}

def fetch(sym, bar, limit=300):
    r = requests.get("https://www.okx.com/api/v5/market/candles",
                     params={"instId": OKX_IDS[sym], "bar": BARS.get(bar, bar), "limit": limit},
                     timeout=15)
    r.raise_for_status()
    j = r.json()
    if j.get("code") != "0":
        raise Exception("OKX: " + str(j.get("msg")))
    return [{"t": int(x[0]) // 1000, "o": float(x[1]), "h": float(x[2]),
             "l": float(x[3]), "c": float(x[4])} for x in j["data"][::-1]]

def fmt(p):
    return f"{p:,.2f}"

def swings(c, k=3):
    # fractal swing highs / lows (dono taraf k candles)
    hs, ls = [], []
    for i in range(k, len(c) - k):
        win = c[i - k:i + k + 1]
        if c[i]["h"] == max(x["h"] for x in win):
            hs.append((i, c[i]["h"]))
        if c[i]["l"] == min(x["l"] for x in win):
            ls.append((i, c[i]["l"]))
    return hs, ls

def structure(c, k=3):
    # BOS / CHoCH: close ne last swing high/low toda
    hs, ls = swings(c, k)
    events, trend = [], None
    hi_i = lo_i = 0
    last_h = last_l = None
    used_h = used_l = None
    for i in range(len(c)):
        while hi_i < len(hs) and hs[hi_i][0] + k <= i:
            last_h = hs[hi_i]
            hi_i += 1
        while lo_i < len(ls) and ls[lo_i][0] + k <= i:
            last_l = ls[lo_i]
            lo_i += 1
        if last_h and last_h != used_h and c[i]["c"] > last_h[1]:
            kind = "BOS" if trend == "bull" else "CHoCH"
            events.append((i, "bull", kind, last_h[1], last_h[0]))
            trend, used_h = "bull", last_h
        if last_l and last_l != used_l and c[i]["c"] < last_l[1]:
            kind = "BOS" if trend == "bear" else "CHoCH"
            events.append((i, "bear", kind, last_l[1], last_l[0]))
            trend, used_l = "bear", last_l
    return trend, events, hs, ls

def fvgs(c, atr):
    # Fair Value Gaps jo abhi tak poore fill nahi hue
    out = []
    for i in range(2, len(c)):
        a, b = c[i - 2], c[i]
        if a["h"] < b["l"] and b["l"] - a["h"] >= 0.15 * atr[i]:
            lo, hi, side = a["h"], b["l"], "bull"
        elif a["l"] > b["h"] and a["l"] - b["h"] >= 0.15 * atr[i]:
            lo, hi, side = b["h"], a["l"], "bear"
        else:
            continue
        after = c[i + 1:]
        if side == "bull":
            if any(x["l"] <= lo for x in after):
                continue
            touched = min([x["l"] for x in after], default=hi)
            fill = max(0.0, min(1.0, (hi - touched) / (hi - lo)))
        else:
            if any(x["h"] >= hi for x in after):
                continue
            touched = max([x["h"] for x in after], default=lo)
            fill = max(0.0, min(1.0, (touched - lo) / (hi - lo)))
        out.append({"i": i - 1, "side": side, "lo": lo, "hi": hi, "fill": fill})
    return out

def order_blocks(c, atr):
    # OB: impulse se pehle ki ulti candle. Valid jab tak price uske paar close na kare.
    out = []
    for i in range(1, len(c) - 1):
        o, nx = c[i], c[i + 1]
        body = abs(nx["c"] - nx["o"])
        if body < 1.0 * atr[i + 1]:
            continue
        after = c[i + 2:]
        if o["c"] < o["o"] and nx["c"] > o["h"]:
            if not any(x["c"] < o["l"] for x in after):
                out.append({"i": i, "side": "bull", "lo": o["l"], "hi": o["h"],
                            "tested": any(x["l"] <= o["h"] for x in after)})
        elif o["c"] > o["o"] and nx["c"] < o["l"]:
            if not any(x["c"] > o["h"] for x in after):
                out.append({"i": i, "side": "bear", "lo": o["l"], "hi": o["h"],
                            "tested": any(x["h"] >= o["l"] for x in after)})
    return out

def liquidity(c, hs, ls, atr):
    # equal highs / equal lows (liquidity pools) jo abhi sweep nahi hue
    tol = 0.12 * atr[-1]
    eqh, eql = [], []
    for j in range(len(hs)):
        for m in range(j + 1, len(hs)):
            if abs(hs[j][1] - hs[m][1]) <= tol:
                lvl = max(hs[j][1], hs[m][1])
                if not any(x["h"] > lvl for x in c[hs[m][0] + 1:]):
                    eqh.append((hs[m][0], lvl))
    for j in range(len(ls)):
        for m in range(j + 1, len(ls)):
            if abs(ls[j][1] - ls[m][1]) <= tol:
                lvl = min(ls[j][1], ls[m][1])
                if not any(x["l"] < lvl for x in c[ls[m][0] + 1:]):
                    eql.append((ls[m][0], lvl))
    return eqh[-2:], eql[-2:]

def ema_trend(c):
    cl = [x["c"] for x in c]
    e20, e50, e200 = ema(cl, 20)[-1], ema(cl, 50)[-1], ema(cl, 200)[-1]
    p = cl[-1]
    if e20 > e50 > e200 and p > e50:
        return "🟢 Bullish", e20, e50, e200
    if e20 < e50 < e200 and p < e50:
        return "🔴 Bearish", e20, e50, e200
    return "⚪ Sideways", e20, e50, e200

def parse_args(args, d_sym="BTCUSDT", d_tf="15m"):
    sym, tf = d_sym, d_tf
    for a in args:
        a = a.lower()
        if a in SYM_ALIAS:
            sym = SYM_ALIAS[a]
        elif a in BARS:
            tf = a
    return sym, tf

def smc_pack(sym, tf):
    c = fetch(sym, tf)
    atr = atr_list(c)
    trend, ev, hs, ls = structure(c)
    return c, atr, trend, ev, hs, ls

def chart_for(c, title, n=80, boxes=(), marks=(), hlines=(), segs=()):
    d = chart_data(c, title, n=n)
    off = len(c) - n
    d["segs"] = [(a - off, b - off, y, t, col) for a, b, y, t, col in segs]
    d["boxes"] = [(i - off, lo, hi, col, lab) for i, lo, hi, col, lab in boxes]
    d["marks"] = [(i - off, p, t, col) for i, p, t, col in marks]
    d["hlines"] = list(hlines)
    return d

def head(sym, tf, title):
    return f"<b>{title}</b>\n🪙 <b>{sym}</b> · <i>{tf}</i>\n━━━━━━━━━━━━━━\n"

def cmd_price(args):
    lines = ["<b>💰 Live Price</b>\n━━━━━━━━━━━━━━"]
    for sym in SYMBOLS:
        c = fetch(sym, "1d", 2)
        p, o = c[-1]["c"], c[-1]["o"]
        ch = (p - o) / o * 100
        lines.append(f"{'🟢' if ch >= 0 else '🔴'} <b>{sym}</b>: <code>{p:.2f}</code>  ({ch:+.2f}% today)")
    return {"html": "\n".join(lines) + FOOT}

def cmd_trend(args):
    sym, _ = parse_args(args)
    h = head(sym, "MTF", "📊 Multi-Timeframe Trend")
    score = 0
    for tf in ("5m", "15m", "1h", "4h", "1d"):
        c = fetch(sym, tf)
        lab, e20, e50, e200 = ema_trend(c)
        st, ev, _, _ = structure(c)
        stl = {"bull": "HH/HL ⬆️", "bear": "LH/LL ⬇️"}.get(st, "-")
        score += (1 if "Bull" in lab else -1 if "Bear" in lab else 0) + \
                 (1 if st == "bull" else -1 if st == "bear" else 0)
        h += f"<b>{tf:>3}</b>: {lab} | Structure {stl}\n"
    bias = ("🟢 BUY SIDE - LOOK FOR BUYS ON PULLBACKS" if score >= 3 else
            "🔴 SELL SIDE - LOOK FOR SELLS ON PULLBACKS" if score <= -3 else
            "⚪ MIXED - WAIT, NO TRADE")
    h += f"━━━━━━━━━━━━━━\n🧭 <b>Overall bias:</b> {bias}  <i>(score {score:+d})</i>"
    return {"html": h + FOOT}

TF_SEC = {"1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "2h": 7200,
          "4h": 14400, "1d": 86400}

def fetch_long(sym, tf, pages=3):
    # zyada history (EMA 200 sahi banane ke liye): 3 x 300 candles
    out, after = [], None
    for _ in range(pages):
        params = {"instId": OKX_IDS[sym], "bar": BARS.get(tf, tf), "limit": 300}
        if after:
            params["after"] = after
        r = requests.get("https://www.okx.com/api/v5/market/candles", params=params, timeout=15)
        r.raise_for_status()
        j = r.json()
        if j.get("code") != "0" or not j["data"]:
            break
        out += j["data"]
        after = j["data"][-1][0]
    rows = sorted({x[0]: x for x in out}.values(), key=lambda x: int(x[0]))
    return [{"t": int(x[0]) // 1000, "o": float(x[1]), "h": float(x[2]),
             "l": float(x[3]), "c": float(x[4])} for x in rows]

def _when(ts):
    dt = datetime.fromtimestamp(ts, DXB)
    today = datetime.now(DXB).date()
    if dt.date() == today:
        return dt.strftime("%I:%M %p").lstrip("0") + " today"
    if dt.date() == today - timedelta(days=1):
        return dt.strftime("%I:%M %p").lstrip("0") + " yesterday"
    if dt.date() == today + timedelta(days=1):
        return dt.strftime("%I:%M %p").lstrip("0") + " tomorrow"
    return dt.strftime("%d %b %I:%M %p")

def _dur(sec_):
    m = int(sec_ // 60)
    if m < 60:
        return f"{m} min"
    h_, m = divmod(m, 60)
    if h_ < 48:
        return f"{h_}h {m}m"
    return f"{h_ // 24} days"

def cross_report(c, a, b, start, sec, name_a, name_b):
    # last cross kab hua + next cross kab ho sakta hai (estimate)
    diff = [x - y for x, y in zip(a, b)]
    last = None
    for i in range(len(c) - 1, max(start, 1), -1):
        if (diff[i - 1] <= 0) != (diff[i] <= 0):
            last = (i, "BULLISH" if diff[i] > 0 else "BEARISH")
            break
    now_close = c[-1]["t"] + sec
    res = {"last": None, "next": None, "above": diff[-1] > 0, "gap": diff[-1]}
    if last:
        i, kind = last
        res["last"] = (kind, c[i]["t"] + sec, i)
    k = 5
    slope = (diff[-1] - diff[-1 - k]) / k if len(diff) > k else 0
    if diff[-1] != 0 and slope != 0 and (diff[-1] > 0) != (slope > 0):
        bars = abs(diff[-1]) / abs(slope)
        if bars <= 150:
            kind = "BEARISH" if diff[-1] > 0 else "BULLISH"
            res["next"] = (kind, now_close + bars * sec, bars)
    return res

def cmd_ema(args):
    sym, tf = parse_args(args)
    sec = TF_SEC.get(tf, 900)
    try:
        c = fetch_long(sym, tf)
    except Exception:
        c = fetch(sym, tf)
    cl = [x["c"] for x in c]
    e20, e50, e200 = ema(cl, 20), ema(cl, 50), ema(cl, 200)
    lab, _, _, _ = ema_trend(c)
    p = cl[-1]
    h = head(sym, tf, "📈 EMA Cross Report")
    for n_, v in (("EMA 20", e20[-1]), ("EMA 50", e50[-1]), ("EMA 200", e200[-1])):
        h += f"{n_}: <code>{v:.2f}</code>  (price {(p - v) / v * 100:+.2f}%)\n"
    h += f"Price: <code>{p:.2f}</code> · Trend: {lab}\n━━━━━━━━━━━━━━\n"
    pairs = [("EMA 20", e20, "EMA 50", e50, 60), ("EMA 50", e50, "EMA 200", e200, 210),
             ("EMA 20", e20, "EMA 200", e200, 210), ("Price", cl, "EMA 200", e200, 210)]
    nexts, xmarks = [], []
    for na, a, nb, b, st0 in pairs:
        r = cross_report(c, a, b, st0, sec, na, nb)
        name = f"{na}/{nb.replace('EMA ', '')}" if na != "Price" else "Price/EMA 200"
        tagline = {"EMA 50/200": " (Golden/Death cross)"}.get(name, "")
        h += f"<b>🔀 {name}{tagline}</b>\n"
        h += f"  Now: {na} is <b>{'ABOVE ⬆️' if r['above'] else 'BELOW ⬇️'}</b> {nb}\n"
        if r["last"]:
            kind, ts, i = r["last"]
            ago = c[-1]["t"] + sec - ts
            h += (f"  Last cross: {'🟢' if kind == 'BULLISH' else '🔴'} {kind} at "
                  f"<b>{_when(ts)}</b> ({_dur(ago)} ago)\n")
            if name in ("EMA 20/50", "EMA 50/200"):
                xmarks.append((i, a[i], f"{'▲' if kind == 'BULLISH' else '▼'} {name}",
                               UP if kind == "BULLISH" else DN))
        else:
            h += "  Last cross: none in loaded history\n"
        if r["next"]:
            kind, ts, bars = r["next"]
            h += (f"  Next cross: {'🟢' if kind == 'BULLISH' else '🔴'} possible {kind} around "
                  f"<b>{_when(ts)}</b> (in ~{_dur(ts - (c[-1]['t'] + sec))}) ⏳\n")
            nexts.append((ts, kind, name))
        else:
            h += "  Next cross: not expected soon (lines moving apart)\n"
    if nexts:
        ts, kind, name = min(nexts)
        act = (f"WATCH {name} - POSSIBLE {kind} CROSS AROUND "
               f"{datetime.fromtimestamp(ts, DXB).strftime('%I:%M %p').lstrip('0')} - "
               f"WAIT FOR CANDLE CLOSE TO CONFIRM")
    else:
        act = ("TREND IS STABLE - NO CROSS EXPECTED SOON - TRADE WITH THE TREND"
               if "Sideways" not in lab else "NO CLEAR TREND - WAIT")
    h += (f"━━━━━━━━━━━━━━\n▶️ <b>NEXT ACTION: {act}</b>\n"
          f"<i>Next cross time is an estimate from current EMA speed - it can change.</i>")
    ch = chart_data(c, f"{sym} · {tf} · EMA CROSS")
    ch["xmarks"] = xmarks
    ch["ema_only"] = True
    ch["action"] = (act, "#ffd400")
    return {"html": h + FOOT, "chart": ch, "static": True}

def cmd_fvg(args):
    sym, tf = parse_args(args)
    c, atr, *_ = smc_pack(sym, tf)
    p = c[-1]["c"]
    fv = fvgs(c, atr)
    bull = sorted([f for f in fv if f["side"] == "bull"], key=lambda f: p - f["hi"])[:3]
    bear = sorted([f for f in fv if f["side"] == "bear"], key=lambda f: f["lo"] - p)[:3]
    h = head(sym, tf, "🕳 Fair Value Gaps (unfilled)")
    h += "🟢 <b>Bullish FVG</b> (support, below price):\n"
    h += "".join(f"  • <code>{f['lo']:.2f} - {f['hi']:.2f}</code>  ({f['fill'] * 100:.0f}% filled)\n"
                 for f in bull) or "  none\n"
    h += "🔴 <b>Bearish FVG</b> (resistance, above price):\n"
    h += "".join(f"  • <code>{f['lo']:.2f} - {f['hi']:.2f}</code>  ({f['fill'] * 100:.0f}% filled)\n"
                 for f in bear) or "  none\n"
    h += f"Price: <code>{p:.2f}</code>"
    boxes = [(f["i"], f["lo"], f["hi"], UP if f["side"] == "bull" else DN, "FVG")
             for f in bull + bear if f["i"] >= len(c) - 80]
    ch = chart_for(c, f"{sym} · {tf} · FVG", boxes=boxes)
    ch["no_auto_fvg"] = True
    return {"html": h + FOOT, "static": True, "chart": ch}

def cmd_ob(args):
    sym, tf = parse_args(args)
    c, atr, *_ = smc_pack(sym, tf)
    p = c[-1]["c"]
    obs = order_blocks(c, atr)
    bull = sorted([o for o in obs if o["side"] == "bull"], key=lambda o: p - o["hi"])[:3]
    bear = sorted([o for o in obs if o["side"] == "bear"], key=lambda o: o["lo"] - p)[:3]
    h = head(sym, tf, "🧱 Order Blocks (unmitigated)")
    h += "🟢 <b>Bullish OB</b> (demand):\n"
    h += "".join(f"  • <code>{o['lo']:.2f} - {o['hi']:.2f}</code>\n" for o in bull) or "  none\n"
    h += "🔴 <b>Bearish OB</b> (supply):\n"
    h += "".join(f"  • <code>{o['lo']:.2f} - {o['hi']:.2f}</code>\n" for o in bear) or "  none\n"
    h += f"Price: <code>{p:.2f}</code>"
    boxes = [(o["i"], o["lo"], o["hi"], "#3d7dff" if o["side"] == "bull" else "#ff9f1c", "OB")
             for o in bull + bear if o["i"] >= len(c) - 80]
    ch = chart_for(c, f"{sym} · {tf} · Order Blocks", boxes=boxes)
    ch["no_auto_ob"] = True
    return {"html": h + FOOT, "static": True, "chart": ch}

def cmd_bos(args):
    sym, tf = parse_args(args)
    c, atr, trend, ev, hs, ls = smc_pack(sym, tf)
    h = head(sym, tf, "🏗 Market Structure (BOS / CHoCH)")
    h += f"Trend: <b>{'🟢 Bullish' if trend == 'bull' else '🔴 Bearish' if trend == 'bear' else '⚪ -'}</b>\n"
    for i, side, kind, lvl, _ in ev[-5:][::-1]:
        ago = (c[-1]["t"] - c[i]["t"]) / 3600
        h += f"{'⬆️' if side == 'bull' else '⬇️'} <b>{kind}</b> @ <code>{lvl:.2f}</code>  <i>{ago:.1f} hours ago</i>\n"
    if hs:
        h += f"Last swing high: <code>{hs[-1][1]:.2f}</code>\n"
    if ls:
        h += f"Last swing low: <code>{ls[-1][1]:.2f}</code>"
    segs = [(si, i, lvl, kind, UP if side == "bull" else DN) for i, side, kind, lvl, si in ev[-6:]]
    return {"html": h + FOOT, "static": True,
            "chart": chart_for(c, f"{sym} · {tf} · Structure", segs=segs)}

def day_levels(sym):
    d = fetch(sym, "1d", 3)
    return d[-2]["h"], d[-2]["l"], d[-1]["o"], d[-1]["t"]

def asia_range(sym):
    c = fetch(sym, "15m", 96)
    now = datetime.now(timezone.utc)
    op, cl = sess_times("Asia/Tokyo", 9, 18, now)
    if now < op:
        op, cl = op - timedelta(days=1), cl - timedelta(days=1)
    w = [x for x in c if op.timestamp() <= x["t"] < cl.timestamp()]
    if not w:
        return None
    return max(x["h"] for x in w), min(x["l"] for x in w)

def cmd_liq(args):
    sym, tf = parse_args(args)
    c, atr, trend, ev, hs, ls = smc_pack(sym, tf)
    p = c[-1]["c"]
    eqh, eql = liquidity(c, hs, ls, atr)
    pdh, pdl, dop, dstart = day_levels(sym)
    today = [x for x in c if x["t"] >= dstart] or c[-1:]
    ar = asia_range(sym)
    h = head(sym, tf, "💧 Liquidity Levels")
    h += f"📅 PDH (previous day high): <code>{pdh:.2f}</code> {'✅ swept today' if max(x['h'] for x in today) > pdh else ''}\n"
    h += f"📅 PDL (previous day low): <code>{pdl:.2f}</code> {'✅ swept today' if min(x['l'] for x in today) < pdl else ''}\n"
    h += f"🕛 Today open: <code>{dop:.2f}</code>\n"
    if ar:
        h += f"🇯🇵 Asia high/low: <code>{ar[0]:.2f}</code> / <code>{ar[1]:.2f}</code>\n"
    h += "🔺 Equal highs (buy-side liq): " + (", ".join(f"<code>{v:.2f}</code>" for _, v in eqh) or "-") + "\n"
    h += "🔻 Equal lows (sell-side liq): " + (", ".join(f"<code>{v:.2f}</code>" for _, v in eql) or "-") + "\n"
    h += f"Price: <code>{p:.2f}</code>"
    hl = [(pdh, "PDH", "#ffd400"), (pdl, "PDL", "#ffd400")] + \
         [(v, "EQH", "#f6465d") for _, v in eqh] + [(v, "EQL", "#0ecb81") for _, v in eql]
    if ar:
        hl += [(ar[0], "Asia H", "#c4b5fd"), (ar[1], "Asia L", "#c4b5fd")]
    lo_c, hi_c = min(x["l"] for x in c[-80:]), max(x["h"] for x in c[-80:])
    rng = hi_c - lo_c
    hl = [x for x in hl if lo_c - rng * 0.5 <= x[0] <= hi_c + rng * 0.5]
    return {"html": h + FOOT, "static": True,
            "chart": chart_for(c, f"{sym} · {tf} · Liquidity", hlines=hl)}

def cmd_pd(args):
    sym, tf = parse_args(args)
    c = fetch(sym, tf)
    w = c[-100:]
    hi, lo = max(x["h"] for x in w), min(x["l"] for x in w)
    eq = (hi + lo) / 2
    p = c[-1]["c"]
    pos = (p - lo) / (hi - lo) * 100 if hi > lo else 50
    zone = "🔴 PREMIUM (expensive - SELL zone)" if pos > 55 else "🟢 DISCOUNT (cheap - BUY zone)" if pos < 45 else "⚪ EQUILIBRIUM (middle)"
    pd_act = ("LOOK FOR SELLS - PRICE IN PREMIUM" if pos > 55 else
              "LOOK FOR BUYS - PRICE IN DISCOUNT" if pos < 45 else "WAIT - PRICE AT EQUILIBRIUM")
    h = head(sym, tf, "⚖️ Premium / Discount")
    h += (f"Range high: <code>{hi:.2f}</code>\nEquilibrium (50%): <code>{eq:.2f}</code>\n"
          f"Range low: <code>{lo:.2f}</code>\n"
          f"OTE (62–79%): <code>{hi - (hi - lo) * 0.62:.2f}</code> – <code>{hi - (hi - lo) * 0.79:.2f}</code> (buy) | "
          f"<code>{lo + (hi - lo) * 0.62:.2f}</code> – <code>{lo + (hi - lo) * 0.79:.2f}</code> (sell)\n"
          f"Price: <code>{p:.2f}</code> → <b>{pos:.0f}%</b> of range\n{zone}\n"
          f"▶️ <b>NEXT ACTION: {pd_act}</b>")
    hl = [(hi, "Range H", "#848e9c"), (eq, "EQ 50%", "#ffd400"), (lo, "Range L", "#848e9c")]
    boxes = [(len(c) - 100, eq, hi, DN, "PREMIUM"), (len(c) - 100, lo, eq, UP, "DISCOUNT")]
    return {"html": h + FOOT, "static": True,
            "chart": dict(chart_for(c, f"{sym} · {tf} · Premium/Discount", boxes=boxes, hlines=hl),
                          action=(pd_act, "#e5484d" if pos > 55 else "#0ecb81" if pos < 45 else "#848e9c"))}

def cmd_smc(args):
    sym, tf = parse_args(args)
    c, atr, trend, ev, hs, ls = smc_pack(sym, tf)
    p = c[-1]["c"]
    lab, e20, e50, e200 = ema_trend(c)
    fv = fvgs(c, atr)
    obs = order_blocks(c, atr)
    eqh, eql = liquidity(c, hs, ls, atr)
    w = c[-100:]
    hi, lo = max(x["h"] for x in w), min(x["l"] for x in w)
    pos = (p - lo) / (hi - lo) * 100 if hi > lo else 50
    nb_f = min([f for f in fv if f["side"] == "bull"], key=lambda f: p - f["hi"], default=None)
    ns_f = min([f for f in fv if f["side"] == "bear"], key=lambda f: f["lo"] - p, default=None)
    nb_o = min([o for o in obs if o["side"] == "bull"], key=lambda o: p - o["hi"], default=None)
    ns_o = min([o for o in obs if o["side"] == "bear"], key=lambda o: o["lo"] - p, default=None)
    h = head(sym, tf, "🧠 SMC Full Report")
    h += f"📈 EMA trend: {lab}\n"
    h += f"🏗 Structure: {'🟢 Bullish' if trend == 'bull' else '🔴 Bearish' if trend == 'bear' else '-'}"
    if ev:
        i, side, kind, lvl, _ = ev[-1]
        h += f" (last {kind} {'⬆️' if side == 'bull' else '⬇️'} @ <code>{lvl:.2f}</code>)"
    h += "\n"
    h += f"⚖️ Range position: <b>{pos:.0f}%</b> {'(Premium)' if pos > 55 else '(Discount)' if pos < 45 else '(EQ)'}\n"
    if nb_f:
        h += f"🕳🟢 Nearest bull FVG: <code>{nb_f['lo']:.2f} - {nb_f['hi']:.2f}</code>\n"
    if ns_f:
        h += f"🕳🔴 Nearest bear FVG: <code>{ns_f['lo']:.2f} - {ns_f['hi']:.2f}</code>\n"
    if nb_o:
        h += f"🧱🟢 Demand OB: <code>{nb_o['lo']:.2f} - {nb_o['hi']:.2f}</code>\n"
    if ns_o:
        h += f"🧱🔴 Supply OB: <code>{ns_o['lo']:.2f} - {ns_o['hi']:.2f}</code>\n"
    if eqh:
        h += f"💧 Buy-side liq (EQH): <code>{eqh[-1][1]:.2f}</code>\n"
    if eql:
        h += f"💧 Sell-side liq (EQL): <code>{eql[-1][1]:.2f}</code>\n"
    bull_pts = ("Bull" in lab) + (trend == "bull") + (pos < 45)
    bear_pts = ("Bear" in lab) + (trend == "bear") + (pos > 55)
    if bull_pts >= 2 and bull_pts > bear_pts:
        idea = "🟢 BUY BIAS - WAIT FOR PULLBACK INTO DEMAND OB / BULL FVG, THEN BUY"
    elif bear_pts >= 2 and bear_pts > bull_pts:
        idea = "🔴 SELL BIAS - WAIT FOR PULLBACK INTO SUPPLY OB / BEAR FVG, THEN SELL"
    else:
        idea = "⚪ NO CLEAR BIAS - WAIT, DO NOT TRADE"
    h += f"━━━━━━━━━━━━━━\n▶️ <b>NEXT ACTION: {idea}</b>\nPrice: <code>{p:.2f}</code>"
    boxes = []
    for f, col in ((nb_f, UP), (ns_f, DN)):
        if f and f["i"] >= len(c) - 80:
            boxes.append((f["i"], f["lo"], f["hi"], col, "FVG"))
    for o, col in ((nb_o, "#3d7dff"), (ns_o, "#ff9f1c")):
        if o and o["i"] >= len(c) - 80:
            boxes.append((o["i"], o["lo"], o["hi"], col, "OB"))
    segs = [(si, i, lvl, kind, UP if side == "bull" else DN) for i, side, kind, lvl, si in ev[-4:]]
    hl = [(v, "EQH", "#f6465d") for _, v in eqh[-1:]] + [(v, "EQL", "#0ecb81") for _, v in eql[-1:]]
    return {"html": h + FOOT, "static": True,
            "chart": dict(chart_for(c, f"{sym} · {tf} · SMC", hlines=hl),
                          action=(idea[2:], "#0ecb81" if "BUY" in idea else
                                  "#e5484d" if "SELL" in idea else "#848e9c"))}

def cmd_chart(args):
    sym, tf = parse_args(args)
    c = fetch(sym, tf)
    return {"html": head(sym, tf, "📉 Live Chart") + f"Price: <code>{c[-1]['c']:.2f}</code>" + FOOT,
            "chart": chart_data(c, f"{sym} · {tf}")}

def cmd_session(args):
    return {"html": tg_plain(sessions_text())}

def cmd_status(args):
    lines = [html.escape(status_15m())]
    return {"html": "<b>📊 Bot status</b>\n" + "\n".join(lines) + FOOT}

_STATE_REF = {}

def cmd_trades(args):
    st = _STATE_REF.get("st") or {}
    tr = st.get("trades", [])
    if not tr:
        return {"html": "<b>📂 Open Trades</b>\nNo open trades right now." + FOOT}
    h = "<b>📂 Open trades</b>\n━━━━━━━━━━━━━━\n"
    for t in tr[-10:]:
        h += (f"{'🟢' if t['side'] == 'BUY' else '🔴'} {t['sym']} ({t['tf']}m) {t['side']} "
              f"@ <code>{t['entry']:.2f}</code> | SL <code>{t['sl']:.2f}</code> | "
              f"{'TP' + str(t['hit']) + ' hit' if t['hit'] else ('active' if t['filled'] else 'limit wait')}\n")
    return {"html": h.rstrip() + FOOT}

def cmd_stats(args):
    st = _STATE_REF.get("st") or {}
    return {"html": tg_plain(summary_text(st.get("day", "today"), st.get("stats", {})))}

def cmd_help(args):
    h = ("<b>🤖 Shaushab Bot Commands</b>\n━━━━━━━━━━━━━━\n"
         "<i>Symbol: btc / gold · Timeframe: 1m 5m 15m 1h 4h 1d</i>\n"
         "<i>Example: /fvg gold 1h · /smc btc 4h</i>\n"
         "\n"
         "🧠 /smc — full SMC report + chart\n"
         "🕳 /fvg — Fair Value Gaps\n"
         "🧱 /ob — Order Blocks\n"
         "🏗 /bos — BOS / CHoCH structure\n"
         "💧 /liq — Liquidity (PDH/PDL, EQH/EQL, Asia range)\n"
         "⚖️ /pd — Premium / Discount + OTE\n"
         "📊 /trend — trend from 5m to 1D + bias\n"
         "📈 /ema — EMA cross report (last + next cross time)\n"
         "📉 /chart — live chart\n"
         "💰 /price — BTC + Gold price\n"
         "🕒 /session — Tokyo / London / NY\n"
         "📂 /trades — open trades\n"
         "📅 /stats — today's results\n"
         "🟢 /status — 15m status")
    return {"html": h + FOOT}

COMMANDS = {"smc": cmd_smc, "fvg": cmd_fvg, "ob": cmd_ob, "bos": cmd_bos, "choch": cmd_bos,
            "structure": cmd_bos, "liq": cmd_liq, "liquidity": cmd_liq, "pd": cmd_pd,
            "premium": cmd_pd, "discount": cmd_pd, "trend": cmd_trend, "ema": cmd_ema,
            "chart": cmd_chart, "price": cmd_price, "session": cmd_session,
            "trades": cmd_trades, "stats": cmd_stats, "status": cmd_status,
            "help": cmd_help, "start": cmd_help}

MENU = [("smc", "SMC full report"), ("fvg", "Fair Value Gaps"), ("ob", "Order Blocks"),
        ("bos", "BOS / CHoCH"), ("liq", "Liquidity levels"), ("pd", "Premium / Discount"),
        ("trend", "Multi-timeframe trend"), ("ema", "EMA cross report"), ("chart", "Live chart"),
        ("price", "BTC + Gold price"), ("session", "Sessions"), ("trades", "Open trades"),
        ("stats", "Today's results"), ("status", "Bot status"), ("help", "All commands")]

def _allowed_chats():
    return {x for x in (CHAT, os.environ.get("TELEGRAM_CHAT_15M", "").strip()) if x}

def handle_command(text, chat):
    parts = text.strip().split()
    cmd = parts[0][1:].split("@")[0].lower()
    fn = COMMANDS.get(cmd)
    if not fn:
        return
    try:
        tg = fn(parts[1:])
    except Exception as ex:
        print("command error:", cmd, ex, flush=True)
        tg = {"html": f"⚠️ /{html.escape(cmd)} failed right now, please try again in a minute."}
    tg["chat"] = chat
    _q.put((f"/{cmd}", tg))

def _cmd_worker():
    # Telegram se /commands padhna (long polling)
    try:
        requests.post(f"https://api.telegram.org/bot{TOKEN}/setMyCommands",
                      json={"commands": [{"command": a, "description": b} for a, b in MENU]},
                      timeout=20)
    except Exception as ex:
        print("setMyCommands error:", ex, flush=True)
    offset = None
    allowed = _allowed_chats()
    while True:
        try:
            r = requests.get(f"https://api.telegram.org/bot{TOKEN}/getUpdates",
                             params={"timeout": 25, "offset": offset,
                                     "allowed_updates": json.dumps(["message", "channel_post"])},
                             timeout=40)
            if r.status_code != 200:
                print("getUpdates error:", r.status_code, r.text[:200], flush=True)
                time.sleep(10)
                continue
            for u in r.json().get("result", []):
                offset = u["update_id"] + 1
                m = u.get("message") or u.get("channel_post") or {}
                text = m.get("text") or ""
                chat = str(m.get("chat", {}).get("id", ""))
                if not text.startswith("/") or chat not in allowed:
                    continue  # sirf apne group/chat ke commands
                if time.time() - m.get("date", 0) > 600:
                    continue  # 10 min se purana command chhod do
                handle_command(text, chat)
        except Exception as ex:
            print("command loop error:", ex, flush=True)
            time.sleep(5)

def main():
    start = time.time()
    print("bot started", flush=True)
    st = load_state()
    _STATE_REF["st"] = st
    threading.Thread(target=_cmd_worker, daemon=True).start()  # /commands
    last = st.setdefault("last", {})  # "SYM|tf" -> aakhri check ki hui candle ka time
    send("✅ Crypto bot is ONLINE: BTC + Gold (1m, 5m, 15m)\n\n"
         + status_15m() + "\n\n" + sessions_text()
         + ("\n\nWhatsApp: ON" if WA_PHONE and WA_APIKEY else "\n\nWhatsApp: OFF (secrets not set)"),
         tg={"sticker": "brand"})
    htf = {}  # sym -> 15m trend "BUY"/"SELL"
    last_save = time.time()
    while time.time() - start < RUN_SECONDS:
        try:
            check_day(st)
            session_alerts(st)
        except Exception as ex:
            print("day/session error:", ex, flush=True)
        for sym in SYMBOLS:
            for tf, sec in TFS.items():
                try:
                    c = get_candles(sym, tf, sec)
                    if not c:
                        continue
                    if tf == "1":
                        track(st, sym, c)
                    key = f"{sym}|{tf}"
                    prev = last.get(key, 0)
                    if c[-1]["t"] <= prev:
                        continue
                    # kitni nayi closed candles hain (restart gap me chhuti hui bhi)
                    new_idx = [i for i in range(len(c)) if c[i]["t"] > prev]
                    if prev == 0:
                        new_idx = new_idx[-1:]  # pehli baar: sirf latest
                    new_idx = new_idx[-(CATCHUP[tf] + 1):]
                    for i in new_idx:
                        process(st, htf, sym, tf, sec, c[:i + 1], late=(i != len(c) - 1))
                    last[key] = c[-1]["t"]
                except Exception as ex:
                    print(sym, tf, "error:", ex, flush=True)
        if time.time() - last_save > 60:
            save_state(st)
            last_save = time.time()
        time.sleep(5)
    save_state(st)

main()
end = time.time() + 180
while (_q.unfinished_tasks or _nq.unfinished_tasks or _wq.unfinished_tasks) and time.time() < end:
    time.sleep(1)
