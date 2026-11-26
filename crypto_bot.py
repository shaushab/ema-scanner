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
    buf = io.BytesIO()
    _frame(d, len(d["c"]), True, dpi=110).save(buf, format="PNG")
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
    # 512x512 WEBP sticker: "rocket" ya "brand", naam SHAUSHAB ke saath
    if kind in _stickers:
        return _stickers[kind]
    fig = Figure(figsize=(5.12, 5.12), dpi=100)
    fig.patch.set_alpha(0)
    FigureCanvasAgg(fig)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, 10)
    ax.set_ylim(0, 10)
    ax.axis("off")
    if kind == "rocket":
        ax.add_patch(Circle((5, 6.0), 3.7, color="#1a1f3a"))
        for sx, sy in ((2.6, 8.0), (7.5, 7.8), (2.9, 4.6), (7.6, 4.4), (7.0, 8.4)):
            ax.add_patch(Circle((sx, sy), 0.11, color="white"))
        ax.add_patch(Polygon([[4.35, 4.6], [5.65, 4.6], [5.0, 3.1]], color="#ff9f1c"))  # flame
        ax.add_patch(Polygon([[4.6, 4.6], [5.4, 4.6], [5.0, 3.7]], color="#ffd400"))
        ax.add_patch(Rectangle((4.35, 4.6), 1.3, 2.8, color="#e8ecf1"))          # body
        ax.add_patch(Polygon([[4.35, 7.4], [5.65, 7.4], [5.0, 8.6]], color="#f6465d"))  # nose
        ax.add_patch(Polygon([[4.35, 4.6], [3.6, 4.4], [4.35, 5.8]], color="#f6465d"))  # fins
        ax.add_patch(Polygon([[5.65, 4.6], [6.4, 4.4], [5.65, 5.8]], color="#f6465d"))
        ax.add_patch(Circle((5, 6.6), 0.38, color="#3d7dff"))                  # window
        ax.text(5, 9.0, "TO THE MOON", ha="center", va="center", fontsize=13,
                color="#ffd400", fontweight="bold")
        ax.add_patch(Rectangle((0.5, 0.6), 9.0, 1.7, color="#0ecb81"))
        ax.text(5, 1.45, BRAND, ha="center", va="center", fontsize=34, color="white",
                fontweight="bold")
    else:
        ax.add_patch(Circle((5, 5), 4.7, color="#0ecb81"))
        ax.add_patch(Circle((5, 5), 4.2, color="#0b0e11"))
        ax.text(5, 5.5, BRAND, ha="center", va="center", fontsize=34, color="white",
                fontweight="bold")
        ax.text(5, 4.0, "CRYPTO BOT", ha="center", va="center", fontsize=16,
                color="#0ecb81", fontweight="bold")
        ax.text(5, 7.1, "BTC  •  GOLD", ha="center", va="center", fontsize=12, color="#ffd400")
    buf = io.BytesIO()
    fig.savefig(buf, format="png", transparent=True)
    img = Image.open(io.BytesIO(buf.getvalue())).convert("RGBA").resize((512, 512))
    out = io.BytesIO()
    img.save(out, format="WEBP")
    _stickers[kind] = out.getvalue()
    return _stickers[kind]

_anims = {}

def _rocket_frame(i, nf):
    fig = Figure(figsize=(4, 4), dpi=100, facecolor="#0b1026")
    FigureCanvasAgg(fig)
    ax = fig.add_axes([0, 0, 1, 1], facecolor="#0b1026")
    ax.set_xlim(0, 10)
    ax.set_ylim(0, 10)
    ax.axis("off")
    rnd = random.Random(7)
    for _ in range(28):  # stars neeche ki taraf chalte hain = rocket upar ja raha
        sx, sy, sp = rnd.uniform(0, 10), rnd.uniform(0, 10), rnd.uniform(0.15, 0.45)
        y = (sy - i * sp) % 10
        ax.add_patch(Circle((sx, y), rnd.uniform(0.04, 0.1), color="white",
                            alpha=rnd.uniform(0.4, 1)))
    t = i / nf
    rise = min(1, t * 1.6)
    cx = 5 + 0.15 * math.sin(i * 0.9)          # halka wobble
    by = 1.2 + rise * 2.6                       # rocket neeche se upar aata hai
    fl = 1.0 + 0.45 * abs(math.sin(i * 1.7))    # flame flicker
    ax.add_patch(Polygon([[cx - 0.6, by], [cx + 0.6, by], [cx, by - 1.3 * fl]], color="#ff6b1a"))
    ax.add_patch(Polygon([[cx - 0.35, by], [cx + 0.35, by], [cx, by - 0.8 * fl]], color="#ffd400"))
    ax.add_patch(Rectangle((cx - 0.6, by), 1.2, 2.5, color="#e8ecf1"))
    ax.add_patch(Polygon([[cx - 0.6, by + 2.5], [cx + 0.6, by + 2.5], [cx, by + 3.6]], color="#f6465d"))
    ax.add_patch(Polygon([[cx - 0.6, by], [cx - 1.3, by - 0.2], [cx - 0.6, by + 1.1]], color="#f6465d"))
    ax.add_patch(Polygon([[cx + 0.6, by], [cx + 1.3, by - 0.2], [cx + 0.6, by + 1.1]], color="#f6465d"))
    ax.add_patch(Circle((cx, by + 1.7), 0.33, color="#3d7dff"))
    ax.add_patch(Rectangle((0.4, 0.25), 9.2, 1.45, color="#0ecb81"))
    k = min(len(BRAND), int(t * 2.2 * len(BRAND)) + 1)  # naam letter by letter
    ax.text(5, 0.97, BRAND[:k], ha="center", va="center", fontsize=30, color="white",
            fontweight="bold")
    if t > 0.45:
        ax.text(5, 9.1, "TO THE MOON", ha="center", va="center", fontsize=20,
                color="#ffd400", fontweight="bold", alpha=min(1, (t - 0.45) * 4))
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor="#0b1026")
    return Image.open(io.BytesIO(buf.getvalue())).convert("RGB")

def _brand_frame(i, nf):
    fig = Figure(figsize=(4, 4), dpi=100, facecolor=BG)
    FigureCanvasAgg(fig)
    ax = fig.add_axes([0, 0, 1, 1], facecolor=BG)
    ax.set_xlim(0, 10)
    ax.set_ylim(0, 10)
    ax.axis("off")
    from matplotlib.patches import Wedge
    a0 = -i * (360 / nf)  # ghoomta hua green ring
    ax.add_patch(Circle((5, 5), 4.6, color="#1e2329"))
    ax.add_patch(Wedge((5, 5), 4.6, a0, a0 + 250, width=0.45, color="#0ecb81"))
    ax.add_patch(Wedge((5, 5), 4.6, a0 + 270, a0 + 340, width=0.45, color="#ffd400"))
    sc = 1 + 0.08 * math.sin(i / nf * 2 * math.pi * 2)  # naam pulse karta hai
    ax.text(5, 5.4, BRAND, ha="center", va="center", fontsize=32 * sc, color="white",
            fontweight="bold")
    ax.text(5, 3.9, "CRYPTO BOT", ha="center", va="center", fontsize=15, color="#0ecb81",
            fontweight="bold")
    ax.text(5, 7.0, "BTC  •  GOLD", ha="center", va="center", fontsize=12, color="#ffd400")
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=BG)
    return Image.open(io.BytesIO(buf.getvalue())).convert("RGB")

def make_sticker_anim(kind):
    # animated sticker (GIF): rocket upar udta hai / SHAUSHAB badge ghoomta hai
    if kind in _anims:
        return _anims[kind]
    nf = 32
    fn = _rocket_frame if kind == "rocket" else _brand_frame
    frames = [fn(i, nf).quantize(colors=96, method=Image.Quantize.FASTOCTREE) for i in range(nf)]
    durs = [70] * nf
    durs[-1] = 900
    buf = io.BytesIO()
    frames[0].save(buf, format="GIF", save_all=True, append_images=frames[1:],
                   duration=durs, loop=0, optimize=True)
    _anims[kind] = buf.getvalue()
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
    anim_ok = ANIMATION and not tg.get("static")
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
                sent = _tg_call("sendPhoto", {"chat_id": chat, "caption": cap[:1024],
                                              "parse_mode": "HTML", **rm},
                                files={"photo": ("chart.png", png, "image/png")})
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

def chart_data(c, title, levels=(), zone=None, n=70):
    cl = [x["c"] for x in c]
    e20, e50, e200 = ema(cl, 20), ema(cl, 50), ema(cl, 200)
    return {"c": c[-n:], "e20": e20[-n:], "e50": e50[-n:],
            "e200": e200[-n:] if len(c) >= 200 else None,
            "levels": list(levels), "zone": zone, "title": title}

def signal_html(sym, label, kind, side, e, sl, tp, extra, trend_ok, late_t):
    risk = abs(e - sl)
    pct = risk / e * 100 if e else 0
    h = (f"{'🟢' if side == 'BUY' else '🔴'} <b>{side} · {sym}</b>  <i>({label})</i>\n"
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
        h += f"⏰ <i>Late alert: {late_t} wali candle</i>\n"
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
        send(summary_text(st["day"], st["stats"]), wa=True, tg={"sticker": "brand"})
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
        lv = [(t["entry"], "ENTRY", "#5e6673"), (t["sl"], "SL", "#f6465d")] + \
             [(p, f"TP{i + 1}", "#0a9e66") for i, p in enumerate(t["tps"])]

        def tgm(icon, title, extra_line, sticker=None):
            return {"html": (f"{icon} <b>{title}</b>\n━━━━━━━━━━━━━━\n"
                             f"🪙 <b>{sym}</b>  <i>({t['tf']}m)</i>  ·  {t['side']}\n"
                             f"🎯 Entry: <code>{t['entry']:.2f}</code>\n{extra_line}" + FOOT),
                    "chart": chart_data(c1, f"{sym} · 1m · {title}", lv, n=60),
                    "sticker": sticker}
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
                    send(f"📥 Limit fill: {head}",
                         tg=tgm("📥", "LIMIT ORDER FILL", "Trade ab active hai 💪"))
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
                    send(f"🛑 SL hit: {head}", wa=True,
                         tg=tgm("🛑", "STOP LOSS HIT", f"❌ SL: <code>{t['sl']:.2f}</code>\n<i>Koi baat nahi, next trade 💪</i>"))
                else:
                    send(f"↩️ TP{t['hit']} ke baad SL hit: {head}", wa=True,
                         tg=tgm("↩️", f"TP{t['hit']} KE BAAD SL HIT", f"✅ TP{t['hit']} pehle hi mil chuka tha"))
                alive = False
                break
            for n in range(t["hit"] + 1, 4):
                tp = t["tps"][n - 1]
                if (x["h"] >= tp) if buy else (x["l"] <= tp):
                    t["hit"] = n
                    bump(st, key, f"tp{n}")
                    send(f"✅ TP{n} hit: {head} → {tp:.2f}", wa=True,
                         tg=tgm("🚀" if n == 3 else "✅", f"TP{n} HIT" + (" · FULL TARGET" if n == 3 else ""),
                                f"💰 TP{n}: <code>{tp:.2f}</code>  <i>(+{n}R)</i>",
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

CATCHUP = {"15": 8, "5": 6, "1": 0}  # restart ke gap me chhuti candles (15m: 2 ghante tak)

def process(st, htf, sym, tf, sec, c, late=False):
    # ek closed candle (c[-1]) pe saare signals check karo
    label = tf + "m"
    tag = ""
    t = ""
    if late:
        t = datetime.fromtimestamp(c[-1]["t"] + sec, DXB).strftime("%I:%M %p").lstrip("0")
        tag = f"\n⏰ Late alert: {t} wali candle (bot restart ke gap me close hui)"
    if tf == "15":
        br = ema200_break(c)
        if br:
            send(f"⚡ {sym} (15m): price EMA 200 ke {br[0]} close hua\n"
                 f"Close: {c[-1]['c']:.2f} | EMA 200: {br[1]:.2f}\n"
                 f"{'Trend bullish ho sakta hai' if br[0] == 'UPAR' else 'Trend bearish ho sakta hai'}"
                 + tag, wa=True,
                 tg={"html": (f"⚡ <b>EMA 200 BREAK · {sym}</b>  <i>(15m)</i>\n━━━━━━━━━━━━━━\n"
                              f"Price EMA 200 ke <b>{br[0]}</b> close hua {'⬆️' if br[0] == 'UPAR' else '⬇️'}\n"
                              f"💰 Close: <code>{c[-1]['c']:.2f}</code>\n"
                              f"🟡 EMA 200: <code>{br[1]:.2f}</code>\n"
                              f"{'📈 Trend bullish ho sakta hai' if br[0] == 'UPAR' else '📉 Trend bearish ho sakta hai'}"
                              + (f"\n⏰ <i>Late alert: {t} wali candle</i>" if late else "") + FOOT),
                     "chart": chart_data(c, f"{sym} · 15m · EMA 200 break",
                                         [(br[1], "EMA200", "#b8a000")])})
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
        msg = (f"{'🟢' if side == 'BUY' else '🔴'} {side} {sym} ({label})\n"
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
                                tf in ("1", "5") and HTF_FILTER, t),
            "chart": chart_data(c, f"{sym} · {label} · {side} · {kind}", lv, zone),
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
    # unmitigated OB: impulse se pehle ki ulti candle, price wapas nahi aaya
    out = []
    for i in range(1, len(c) - 1):
        o, nx = c[i], c[i + 1]
        body = abs(nx["c"] - nx["o"])
        if body < 1.2 * atr[i + 1]:
            continue
        after = c[i + 2:]
        if o["c"] < o["o"] and nx["c"] > o["h"]:
            if not any(x["l"] <= o["h"] for x in after):
                out.append({"i": i, "side": "bull", "lo": o["l"], "hi": o["h"]})
        elif o["c"] > o["o"] and nx["c"] < o["l"]:
            if not any(x["h"] >= o["l"] for x in after):
                out.append({"i": i, "side": "bear", "lo": o["l"], "hi": o["h"]})
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
        lines.append(f"{'🟢' if ch >= 0 else '🔴'} <b>{sym}</b>: <code>{p:.2f}</code>  ({ch:+.2f}% aaj)")
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
    bias = "🟢 BUY side" if score >= 3 else "🔴 SELL side" if score <= -3 else "⚪ Mixed / wait"
    h += f"━━━━━━━━━━━━━━\n🧭 <b>Overall bias:</b> {bias}  <i>(score {score:+d})</i>"
    return {"html": h + FOOT}

def cmd_ema(args):
    sym, tf = parse_args(args)
    c = fetch(sym, tf)
    lab, e20, e50, e200 = ema_trend(c)
    p = c[-1]["c"]
    h = head(sym, tf, "📈 EMA Status")
    for n_, v in (("EMA 20", e20), ("EMA 50", e50), ("EMA 200", e200)):
        h += f"{n_}: <code>{v:.2f}</code>  ({(p - v) / v * 100:+.2f}%)\n"
    h += f"Price: <code>{p:.2f}</code>\nTrend: {lab}"
    return {"html": h + FOOT, "chart": chart_data(c, f"{sym} · {tf} · EMA"), "static": True}

def cmd_fvg(args):
    sym, tf = parse_args(args)
    c, atr, *_ = smc_pack(sym, tf)
    p = c[-1]["c"]
    fv = fvgs(c, atr)
    bull = sorted([f for f in fv if f["side"] == "bull"], key=lambda f: p - f["hi"])[:3]
    bear = sorted([f for f in fv if f["side"] == "bear"], key=lambda f: f["lo"] - p)[:3]
    h = head(sym, tf, "🕳 Fair Value Gaps (unfilled)")
    h += "🟢 <b>Bullish FVG</b> (support, neeche):\n"
    h += "".join(f"  • <code>{f['lo']:.2f} - {f['hi']:.2f}</code>  ({f['fill'] * 100:.0f}% filled)\n"
                 for f in bull) or "  koi nahi\n"
    h += "🔴 <b>Bearish FVG</b> (resistance, upar):\n"
    h += "".join(f"  • <code>{f['lo']:.2f} - {f['hi']:.2f}</code>  ({f['fill'] * 100:.0f}% filled)\n"
                 for f in bear) or "  koi nahi\n"
    h += f"Price: <code>{p:.2f}</code>"
    boxes = [(f["i"], f["lo"], f["hi"], UP if f["side"] == "bull" else DN, "FVG")
             for f in bull + bear if f["i"] >= len(c) - 80]
    return {"html": h + FOOT, "static": True,
            "chart": chart_for(c, f"{sym} · {tf} · FVG", boxes=boxes)}

def cmd_ob(args):
    sym, tf = parse_args(args)
    c, atr, *_ = smc_pack(sym, tf)
    p = c[-1]["c"]
    obs = order_blocks(c, atr)
    bull = sorted([o for o in obs if o["side"] == "bull"], key=lambda o: p - o["hi"])[:3]
    bear = sorted([o for o in obs if o["side"] == "bear"], key=lambda o: o["lo"] - p)[:3]
    h = head(sym, tf, "🧱 Order Blocks (unmitigated)")
    h += "🟢 <b>Bullish OB</b> (demand):\n"
    h += "".join(f"  • <code>{o['lo']:.2f} - {o['hi']:.2f}</code>\n" for o in bull) or "  koi nahi\n"
    h += "🔴 <b>Bearish OB</b> (supply):\n"
    h += "".join(f"  • <code>{o['lo']:.2f} - {o['hi']:.2f}</code>\n" for o in bear) or "  koi nahi\n"
    h += f"Price: <code>{p:.2f}</code>"
    boxes = [(o["i"], o["lo"], o["hi"], "#3d7dff" if o["side"] == "bull" else "#ff9f1c", "OB")
             for o in bull + bear if o["i"] >= len(c) - 80]
    return {"html": h + FOOT, "static": True,
            "chart": chart_for(c, f"{sym} · {tf} · Order Blocks", boxes=boxes)}

def cmd_bos(args):
    sym, tf = parse_args(args)
    c, atr, trend, ev, hs, ls = smc_pack(sym, tf)
    h = head(sym, tf, "🏗 Market Structure (BOS / CHoCH)")
    h += f"Trend: <b>{'🟢 Bullish' if trend == 'bull' else '🔴 Bearish' if trend == 'bear' else '⚪ -'}</b>\n"
    for i, side, kind, lvl, _ in ev[-5:][::-1]:
        ago = (c[-1]["t"] - c[i]["t"]) / 3600
        h += f"{'⬆️' if side == 'bull' else '⬇️'} <b>{kind}</b> @ <code>{lvl:.2f}</code>  <i>{ago:.1f} ghante pehle</i>\n"
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
    h += f"📅 PDH (kal ka high): <code>{pdh:.2f}</code> {'✅ aaj sweep hua' if max(x['h'] for x in today) > pdh else ''}\n"
    h += f"📅 PDL (kal ka low): <code>{pdl:.2f}</code> {'✅ aaj sweep hua' if min(x['l'] for x in today) < pdl else ''}\n"
    h += f"🕛 Aaj ka open: <code>{dop:.2f}</code>\n"
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
    zone = "🔴 PREMIUM (mehnga, SELL zone)" if pos > 55 else "🟢 DISCOUNT (sasta, BUY zone)" if pos < 45 else "⚪ Equilibrium (beech)"
    h = head(sym, tf, "⚖️ Premium / Discount")
    h += (f"Range high: <code>{hi:.2f}</code>\nEquilibrium (50%): <code>{eq:.2f}</code>\n"
          f"Range low: <code>{lo:.2f}</code>\n"
          f"OTE (62–79%): <code>{hi - (hi - lo) * 0.62:.2f}</code> – <code>{hi - (hi - lo) * 0.79:.2f}</code> (buy) | "
          f"<code>{lo + (hi - lo) * 0.62:.2f}</code> – <code>{lo + (hi - lo) * 0.79:.2f}</code> (sell)\n"
          f"Price: <code>{p:.2f}</code> → range ka <b>{pos:.0f}%</b>\n{zone}")
    hl = [(hi, "Range H", "#848e9c"), (eq, "EQ 50%", "#ffd400"), (lo, "Range L", "#848e9c")]
    boxes = [(len(c) - 100, eq, hi, DN, "PREMIUM"), (len(c) - 100, lo, eq, UP, "DISCOUNT")]
    return {"html": h + FOOT, "static": True,
            "chart": chart_for(c, f"{sym} · {tf} · Premium/Discount", boxes=boxes, hlines=hl)}

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
        idea = "🟢 Bias BUY — discount zone ke demand OB / bull FVG pe pullback ka wait karo"
    elif bear_pts >= 2 and bear_pts > bull_pts:
        idea = "🔴 Bias SELL — premium zone ke supply OB / bear FVG pe pullback ka wait karo"
    else:
        idea = "⚪ Clear bias nahi — wait karo ya chhota size"
    h += f"━━━━━━━━━━━━━━\n🧭 {idea}\nPrice: <code>{p:.2f}</code>"
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
            "chart": chart_for(c, f"{sym} · {tf} · SMC", boxes=boxes, segs=segs, hlines=hl)}

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
        return {"html": "<b>📂 Open trades</b>\nAbhi koi open trade nahi." + FOOT}
    h = "<b>📂 Open trades</b>\n━━━━━━━━━━━━━━\n"
    for t in tr[-10:]:
        h += (f"{'🟢' if t['side'] == 'BUY' else '🔴'} {t['sym']} ({t['tf']}m) {t['side']} "
              f"@ <code>{t['entry']:.2f}</code> | SL <code>{t['sl']:.2f}</code> | "
              f"{'TP' + str(t['hit']) + ' hit' if t['hit'] else ('active' if t['filled'] else 'limit wait')}\n")
    return {"html": h.rstrip() + FOOT}

def cmd_stats(args):
    st = _STATE_REF.get("st") or {}
    return {"html": tg_plain(summary_text(st.get("day", "aaj"), st.get("stats", {})))}

def cmd_help(args):
    h = ("<b>🤖 Shaushab Bot Commands</b>\n━━━━━━━━━━━━━━\n"
         "<i>Symbol: btc / gold · Timeframe: 1m 5m 15m 1h 4h 1d</i>\n"
         "<i>Example: /fvg gold 1h · /smc btc 4h</i>\n\n"
         "🧠 /smc — poora SMC report + chart\n"
         "🕳 /fvg — Fair Value Gaps\n"
         "🧱 /ob — Order Blocks\n"
         "🏗 /bos — BOS / CHoCH structure\n"
         "💧 /liq — Liquidity (PDH/PDL, EQH/EQL, Asia range)\n"
         "⚖️ /pd — Premium / Discount + OTE\n"
         "📊 /trend — 5m se 1D tak trend + bias\n"
         "📈 /ema — EMA 20/50/200\n"
         "📉 /chart — live moving chart\n"
         "💰 /price — BTC + Gold price\n"
         "🕒 /session — Tokyo / London / NY\n"
         "📂 /trades — open trades\n"
         "📅 /stats — aaj ka hisaab\n"
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
        ("trend", "Multi-timeframe trend"), ("ema", "EMA 20/50/200"), ("chart", "Live chart"),
        ("price", "BTC + Gold price"), ("session", "Sessions"), ("trades", "Open trades"),
        ("stats", "Aaj ka hisaab"), ("status", "Bot status"), ("help", "Saare commands")]

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
        tg = {"html": f"⚠️ /{html.escape(cmd)} abhi nahi chala, thodi der baad try karo."}
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
    send("✅ Crypto bot chalu hai: BTC + Gold (1m, 5m, 15m)\n\n"
         + status_15m() + "\n\n" + sessions_text()
         + ("\n\nWhatsApp: ON" if WA_PHONE and WA_APIKEY else "\n\nWhatsApp: OFF (secrets nahi mile)"),
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
