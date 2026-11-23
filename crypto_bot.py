import os, time, requests

TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT = os.environ["TELEGRAM_CHAT_ID"]
SYMBOLS = ["BTCUSDT", "XAUUSDT"]
TFS = {"1": 60, "15": 900}  # interval -> seconds
RUN_SECONDS = 5 * 3600 + 45 * 60
LOOKBACK = 60
IMPULSE = 1.2   # impulse body >= 1.2 x ATR
ADX_MIN = 20    # 1m pe isse neeche = sideways, alert band

def get_candles(sym, tf, sec):
    r = requests.get(
        "https://api.bybit.com/v5/market/kline",
        params={"category": "linear", "symbol": sym,
                "interval": tf, "limit": 300},
        timeout=15)
    r.raise_for_status()
    rows = r.json()["result"]["list"][::-1]
    c = [{"t": int(x[0]) // 1000, "o": float(x[1]), "h": float(x[2]),
          "l": float(x[3]), "c": float(x[4])} for x in rows]
    now = time.time()
    return [x for x in c if x["t"] + sec <= now]  # sirf closed candles

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

def find_ob(c):
    n = len(c)
    if n < 210:
        return None
    closes = [x["c"] for x in c]
    e20, e50, e200 = ema(closes, 20), ema(closes, 50), ema(closes, 200)
    atr = atr_list(c)
    last = c[-1]
    bull = e20[-1] > e50[-1] > e200[-1] and last["c"] > e50[-1]
    bear = e20[-1] < e50[-1] < e200[-1] and last["c"] < e50[-1]
    if not (bull or bear):
        return None
    for i in range(n - 3, n - LOOKBACK, -1):
        o = c[i]
        nx = c[i + 1]
        body = abs(nx["c"] - nx["o"])
        strong = body >= IMPULSE * atr[i]
        mid = c[i + 2: n - 1]
        if bull and o["c"] < o["o"] and nx["c"] > o["h"] and strong:
            lo, hi = o["l"], o["h"]
            if any(x["l"] <= hi for x in mid):
                continue
            if last["l"] <= hi and last["c"] > hi:
                entry = last["c"]
                sl = lo - 0.1 * atr[-1]
                risk = entry - sl
                if risk <= 0:
                    return None
                return ("OB Retest", "BUY", entry, sl,
                        [entry + risk * m for m in (1, 2, 3)],
                        f"OB zone: {lo:.2f} - {hi:.2f}")
        if bear and o["c"] > o["o"] and nx["c"] < o["l"] and strong:
            lo, hi = o["l"], o["h"]
            if any(x["h"] >= lo for x in mid):
                continue
            if last["h"] >= lo and last["c"] < lo:
                entry = last["c"]
                sl = hi + 0.1 * atr[-1]
                risk = sl - entry
                if risk <= 0:
                    return None
                return ("OB Retest", "SELL", entry, sl,
                        [entry - risk * m for m in (1, 2, 3)],
                        f"OB zone: {lo:.2f} - {hi:.2f}")
    return None

def find_cross(c):
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

def send(text):
    requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        data={"chat_id": CHAT, "text": text}, timeout=15)

def main():
    start = time.time()
    seen = {}
    while time.time() - start < RUN_SECONDS:
        for sym in SYMBOLS:
            for tf, sec in TFS.items():
                try:
                    c = get_candles(sym, tf, sec)
                    key = (sym, tf)
                    if not c or seen.get(key) == c[-1]["t"]:
                        continue
                    seen[key] = c[-1]["t"]
                    sigs = []
                    ob = find_ob(c)
                    if ob:
                        sigs.append(ob)
                    sigs += find_cross(c)
                    # 1m pe sideways market me alert nahi
                    if sigs and tf == "1" and adx_last(c) < ADX_MIN:
                        sigs = []
                    label = "1m" if tf == "1" else "15m"
                    for kind, side, e, sl, tp, extra in sigs:
                        msg = (f"{'🟢' if side == 'BUY' else '🔴'} {side} {sym} ({label})\n"
                               f"Type: {kind}\n"
                               f"Entry: {e:.2f}\nSL: {sl:.2f}\n"
                               f"TP1: {tp[0]:.2f}\nTP2: {tp[1]:.2f}\n"
                               f"TP3: {tp[2]:.2f}")
                        if extra:
                            msg += "\n" + extra
                        send(msg)
                except Exception as ex:
                    print(sym, tf, "error:", ex, flush=True)
        time.sleep(10)

main()
