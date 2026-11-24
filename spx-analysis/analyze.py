#!/usr/bin/env python3
"""SPX multi-school technical analysis from the live TradingView chart.

Reads OHLCV for 5m / 15m / 1H / Daily through the tradingview-mcp CLI
(Chrome DevTools Protocol on :9222), scores 10 technical-analysis schools on
each intraday timeframe, derives CALL / PUT triggers, targets and stops, and
writes report.json + report.html (Arabic, RTL) + chart screenshots to OUT_DIR.

Usage:  python3 analyze.py [OUT_DIR]
"""
import datetime as dt
import html
import json
import os
import shutil
import subprocess
import sys
import time
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
HOME = os.path.expanduser("~")
TV_DIR = os.path.join(HOME, "tradingview-mcp")
NODE = shutil.which("node") or "/opt/node22/bin/node"
OUT = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else "spx-report")
TFS = [("5", "5 دقائق", 5), ("15", "15 دقيقة", 4), ("60", "ساعة", 3), ("240", "4 ساعات", 3)]


def market_open(now=None):
    """US cash session (09:30-16:00 New York, Mon-Fri); holidays not handled."""
    n = now or dt.datetime.now(NY)
    return n.weekday() < 5 and dt.time(9, 30) <= n.time() < dt.time(16, 0)


# SPX (cash index) only moves during the US session; outside it, analyse the
# E-mini S&P 500 futures, which trade ~23h/day and track SPX.
SYMBOL = os.environ.get("SYMBOL") or ("SP:SPX" if market_open() else "CME_MINI:ES1!")
IS_FUT = "ES1!" in SYMBOL
NAME = "ES (عقود S&P 500 المستقبلية)" if IS_FUT else "SPX"


# ---------------------------------------------------------------- data access
def tv(*args, timeout=90):
    r = subprocess.run([NODE, os.path.join(TV_DIR, "src/cli/index.js"), *args],
                       capture_output=True, text=True, timeout=timeout)
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        raise RuntimeError(f"tv {' '.join(args)} failed: {r.stdout[-300:]} {r.stderr[-300:]}")


SPACING = {"5": 300, "15": 900, "60": 3600, "240": 14400, "D": 86400}
DISMISS_JS = """(()=>{let n=0;document.querySelectorAll('button').forEach(b=>{const t=(b.innerText||'').trim();
if(/^Got it/.test(t)){b.click();n++}});document.querySelectorAll('[class*=dialog-]').forEach(d=>{
if(/Join for free|Black Friday|Explore offers|Sign up/i.test(d.innerText||'')){d.remove();n++}});return n})()"""


def dismiss():
    try:
        tv("ui", "keyboard", "Escape")
        tv("ui", "eval", DISMISS_JS)
    except Exception:
        pass


def tf_ok(bars, tf):
    if len(bars) < 60:
        return False
    g = min(b["time"] - a["time"] for a, b in zip(bars[-30:], bars[-29:]))
    return g >= 72000 if tf == "D" else g == SPACING[tf]


def fetch(tf, shot=None):
    """Switch the chart to tf and return its bars; reload the page if TradingView
    ignores the switch (a sign-up modal can freeze the chart)."""
    bars = []
    for attempt in range(4):
        if attempt >= 2:
            tv("ui", "eval", f"location.href='https://www.tradingview.com/chart/?symbol={SYMBOL.replace(':', '%3A')}&interval={tf}'")
            time.sleep(15)
        dismiss()
        tv("timeframe", tf)
        time.sleep(5)
        bars = tv("ohlcv", "-n", "500").get("bars") or []
        if tf_ok(bars, tf):
            break
    else:
        raise RuntimeError(f"chart did not switch to timeframe {tf}")
    dismiss()
    if shot:
        s = tv("screenshot", "-r", "chart", "-o", shot)
        if s.get("success"):
            shutil.copy(s["file_path"], os.path.join(OUT, shot + ".png"))
    return bars


# ---------------------------------------------------------------- indicators
def ema(v, n):
    k, e, out = 2 / (n + 1), v[0], []
    for x in v:
        e = x * k + e * (1 - k)
        out.append(e)
    return out


def sma(v, n):
    return sum(v[-n:]) / min(n, len(v))


def rsi(c, n=14):
    g = l = 0.0
    for i in range(1, n + 1):
        d = c[i] - c[i - 1]
        g += max(d, 0); l += max(-d, 0)
    g /= n; l /= n
    for i in range(n + 1, len(c)):
        d = c[i] - c[i - 1]
        g = (g * (n - 1) + max(d, 0)) / n
        l = (l * (n - 1) + max(-d, 0)) / n
    return 100 - 100 / (1 + g / l) if l else 100.0


def atr(b, n=14):
    tr = [max(x["high"] - x["low"], abs(x["high"] - p["close"]), abs(x["low"] - p["close"]))
          for p, x in zip(b, b[1:])]
    a = sum(tr[:n]) / n
    for t in tr[n:]:
        a = (a * (n - 1) + t) / n
    return a


def macd(c):
    m = [a - b for a, b in zip(ema(c, 12), ema(c, 26))]
    s = ema(m, 9)
    h = [a - b for a, b in zip(m, s)]
    return m[-1], s[-1], h[-1], h[-2]


def stoch(b, n=14, k=3):
    ks = []
    for i in range(len(b) - n - k, len(b)):
        w = b[i - n + 1:i + 1]
        hh, ll = max(x["high"] for x in w), min(x["low"] for x in w)
        ks.append(100 * (b[i]["close"] - ll) / (hh - ll) if hh > ll else 50)
    kk = [sum(ks[i - k + 1:i + 1]) / k for i in range(k - 1, len(ks))]
    return kk[-1], sum(kk[-k:]) / k


def bollinger(c, n=20, m=2):
    mid = sma(c, n)
    sd = (sum((x - mid) ** 2 for x in c[-n:]) / n) ** 0.5
    widths = []
    for i in range(len(c) - 100, len(c)):
        w = c[i - n + 1:i + 1]
        mu = sum(w) / n
        widths.append(2 * m * (sum((x - mu) ** 2 for x in w) / n) ** 0.5 / mu)
    return mid + m * sd, mid, mid - m * sd, widths[-1] <= min(widths) * 1.15


def ichimoku(b):
    def mid(w):
        return (max(x["high"] for x in w) + min(x["low"] for x in w)) / 2
    tenkan, kijun = mid(b[-9:]), mid(b[-26:])
    p = b[:-26]  # cloud shown now was projected 26 bars ago
    span_a = (mid(p[-9:]) + mid(p[-26:])) / 2
    span_b = mid(p[-52:])
    return tenkan, kijun, max(span_a, span_b), min(span_a, span_b)


def swings(b, k):
    hs, ls = [], []
    for i in range(k, len(b) - k):
        w = b[i - k:i + k + 1]
        if b[i]["high"] == max(x["high"] for x in w):
            hs.append((i, b[i]["high"]))
        if b[i]["low"] == min(x["low"] for x in w):
            ls.append((i, b[i]["low"]))
    return hs, ls


def vwap(bars):
    v = sum(x["volume"] for x in bars)
    if not v:
        return None
    return sum((x["high"] + x["low"] + x["close"]) / 3 * x["volume"] for x in bars) / v


def poc(bars, step):
    hist = {}
    for x in bars:
        lo, hi = int(x["low"] // step), int(x["high"] // step)
        for k in range(lo, hi + 1):
            hist[k] = hist.get(k, 0) + x["volume"] / (hi - lo + 1)
    return (max(hist, key=hist.get) + 0.5) * step if hist else None


def fvgs(b, a):
    out = []
    for i in range(len(b) - 60, len(b)):
        if b[i]["low"] > b[i - 2]["high"] and b[i]["low"] - b[i - 2]["high"] > 0.15 * a:
            top, bot = b[i]["low"], b[i - 2]["high"]
            if min(x["low"] for x in b[i + 1:] or [b[i]]) > bot:
                out.append(("bull", bot, top))
        if b[i]["high"] < b[i - 2]["low"] and b[i - 2]["low"] - b[i]["high"] > 0.15 * a:
            top, bot = b[i - 2]["low"], b[i]["high"]
            if max(x["high"] for x in b[i + 1:] or [b[i]]) < top:
                out.append(("bear", bot, top))
    return out


def candle(b):
    p, x = b[-2], b[-1]
    body, rng = abs(x["close"] - x["open"]), x["high"] - x["low"] or 1e-9
    up, lo = x["high"] - max(x["open"], x["close"]), min(x["open"], x["close"]) - x["low"]
    if x["close"] > x["open"] and p["close"] < p["open"] and x["close"] >= p["open"] and x["open"] <= p["close"]:
        return "ابتلاع شرائي", 1
    if x["close"] < x["open"] and p["close"] > p["open"] and x["close"] <= p["open"] and x["open"] >= p["close"]:
        return "ابتلاع بيعي", -1
    if lo > 2 * body and up < body:
        return "مطرقة / رفض للأسفل", 1
    if up > 2 * body and lo < body:
        return "شهاب / رفض للأعلى", -1
    if body < 0.1 * rng:
        return "دوجي (تردد)", 0
    if x["high"] < p["high"] and x["low"] > p["low"]:
        return "شمعة داخلية (انضغاط)", 0
    return ("شمعة صاعدة" if x["close"] > x["open"] else "شمعة هابطة"), (1 if x["close"] > x["open"] else -1) * 0.5


# ---------------------------------------------------------------- analysis
def analyze_tf(b, k, common, today_bars):
    c = [x["close"] for x in b]
    px, a = c[-1], atr(b)
    sch, levels = [], []

    def add(name, score, note):
        sch.append({"school": name, "score": score, "note": note})

    # 1 Dow theory / market structure
    hs, ls = swings(b, k)
    (h1, h2), (l1, l2) = [y for _, y in hs[-2:]], [y for _, y in ls[-2:]]
    if h2 > h1 and l2 > l1:
        add("نظرية داو (هيكل السوق)", 1, f"قمم وقيعان صاعدة ({h1:.1f}→{h2:.1f} / {l1:.1f}→{l2:.1f})")
    elif h2 < h1 and l2 < l1:
        add("نظرية داو (هيكل السوق)", -1, f"قمم وقيعان هابطة ({h1:.1f}→{h2:.1f} / {l1:.1f}→{l2:.1f})")
    else:
        add("نظرية داو (هيكل السوق)", 0, f"هيكل متذبذب: قمم {h1:.1f}→{h2:.1f}، قيعان {l1:.1f}→{l2:.1f}")
    levels += [y for _, y in hs[-10:]] + [y for _, y in ls[-10:]]

    # 2 SMC / ICT: BOS, liquidity pools, FVG
    last_h, last_l = hs[-1][1], ls[-1][1]
    gaps = fvgs(b, a)
    eqh = [y for _, y in hs[-8:] if y > px and any(abs(y - z) < 0.12 * a and y != z for _, z in hs[-8:])]
    eql = [y for _, y in ls[-8:] if y < px and any(abs(y - z) < 0.12 * a and y != z for _, z in ls[-8:])]
    if px > last_h:
        s, n = 1, f"كسر هيكل صاعد (BOS) فوق {last_h:.1f}"
    elif px < last_l:
        s, n = -1, f"كسر هيكل هابط (BOS) تحت {last_l:.1f}"
    else:
        s, n = 0, f"داخل النطاق بين سيولة {last_l:.1f} و{last_h:.1f}"
    if eqh:
        n += f"؛ قمم متساوية (سيولة شراء) عند {min(eqh):.1f}"
    if eql:
        n += f"؛ قيعان متساوية (سيولة بيع) عند {max(eql):.1f}"
    gaps = sorted(gaps, key=lambda g: min(abs(px - g[1]), abs(px - g[2])))[:3]
    for kind, lo_, hi_ in gaps:
        n += f"؛ FVG {'شرائية' if kind == 'bull' else 'بيعية'} {lo_:.1f}–{hi_:.1f}"
        levels += [lo_, hi_]
    add("SMC / ICT (السيولة والهيكل)", s, n)

    # 3 price action / candlesticks
    name, s = candle(b[:-1])  # last CLOSED bar
    add("حركة السعر والشموع اليابانية", s, f"آخر شمعة مغلقة: {name}")

    # 4 moving averages
    e9, e21, e50, e200 = (ema(c, n)[-1] for n in (9, 21, 50, 200))
    above = sum(px > e for e in (e21, e50, e200))
    s = 1 if above == 3 and e9 > e21 else -1 if above == 0 and e9 < e21 else 0
    add("المتوسطات المتحركة", s,
        f"EMA9 {e9:.1f} / EMA21 {e21:.1f} / EMA50 {e50:.1f} / EMA200 {e200:.1f} — السعر فوق {above} من 3")
    levels += [e50, e200]

    # 5 momentum oscillators
    r = rsi(c)
    m, sig, hist, hist_prev = macd(c)
    sk, sd = stoch(b)
    s = (1 if r > 55 else -1 if r < 45 else 0) + (1 if hist > 0 and hist > hist_prev else -1 if hist < 0 and hist < hist_prev else 0)
    s = max(-1, min(1, s))
    tag = " (تشبّع شرائي)" if r > 70 or sk > 85 else " (تشبّع بيعي)" if r < 30 or sk < 15 else ""
    add("مؤشرات الزخم (RSI / MACD / Stoch)", s,
        f"RSI {r:.1f}، MACD هيستوغرام {hist:+.2f} ({'يتزايد' if hist > hist_prev else 'يتناقص'})، Stoch {sk:.0f}/{sd:.0f}{tag}")

    # 6 volume / VWAP / volume profile
    vw = common["vwap"]
    pc = poc(b[-120:], max(a / 3, 0.25))
    vol_ratio = b[-2]["volume"] / (sma([x["volume"] for x in b[:-2]], 20) or 1)
    s = (1 if vw and px > vw else -1 if vw else 0)
    add("الحجم و VWAP و Volume Profile", s,
        f"VWAP الجلسة {vw:.1f} (السعر {'فوقه' if px > vw else 'تحته'})، POC {pc:.1f}، حجم آخر شمعة مغلقة ×{vol_ratio:.1f} من المتوسط")
    levels += [vw, pc]

    # 7 Fibonacci on the latest major leg (last 120 bars)
    w = b[-120:]
    hi_i = max(range(len(w)), key=lambda i: w[i]["high"])
    lo_i = min(range(len(w)), key=lambda i: w[i]["low"])
    H, L = w[hi_i]["high"], w[lo_i]["low"]
    up_leg = lo_i < hi_i
    fib = {}
    for f in (0.236, 0.382, 0.5, 0.618, 0.786):
        fib[f] = H - (H - L) * f if up_leg else L + (H - L) * f
    for f in (1.272, 1.618):
        fib[f] = L + (H - L) * f if up_leg else H - (H - L) * f
    pos = (px - L) / (H - L) if H > L else 0.5
    s = 1 if pos > 0.618 else -1 if pos < 0.382 else 0
    add("فيبوناتشي", s,
        f"موجة {'صاعدة' if up_leg else 'هابطة'} {(L if up_leg else H):.1f}→{(H if up_leg else L):.1f}؛ 38.2% {fib[0.382]:.1f}، 50% {fib[0.5]:.1f}، 61.8% {fib[0.618]:.1f}؛ امتداد 127.2% {fib[1.272]:.1f}")
    levels += list(fib.values())

    # 8 Ichimoku
    tk, kj, ctop, cbot = ichimoku(b)
    s = 1 if px > ctop and tk > kj else -1 if px < cbot and tk < kj else 0
    where = "فوق السحابة" if px > ctop else "تحت السحابة" if px < cbot else "داخل السحابة"
    add("إيشيموكو", s, f"{where} ({cbot:.1f}–{ctop:.1f})، تنكان {tk:.1f} {'>' if tk > kj else '<'} كيجن {kj:.1f}")
    levels += [ctop, cbot, kj]

    # 9 Bollinger / volatility
    bu, bm, bl, squeeze = bollinger(c)
    pb = (px - bl) / (bu - bl) if bu > bl else 0.5
    s = 1 if pb > 0.6 else -1 if pb < 0.4 else 0
    add("بولينجر والتقلب (ATR)", s,
        f"%B {pb:.2f}، النطاق {bl:.1f}–{bu:.1f}{'، انضغاط ← انفجار قريب' if squeeze else ''}، ATR {a:.1f}")
    levels += [bu, bl]

    # 10 classic floor pivots (previous session)
    pv = common["pivots"]
    s = 1 if px > pv["P"] and px > common["pdc"] else -1 if px < pv["P"] else 0
    add("نقاط الارتكاز (Pivot Points)", s,
        f"P {pv['P']:.1f} | R1 {pv['R1']:.1f} R2 {pv['R2']:.1f} | S1 {pv['S1']:.1f} S2 {pv['S2']:.1f}")

    levels += list(pv.values()) + common["extra"]
    total = sum(x["score"] for x in sch)
    bias = "صاعد" if total >= 3 else "هابط" if total <= -3 else "محايد"
    return {"price": px, "atr": a, "schools": sch, "score": round(total, 1), "bias": bias,
            "plan": plan(px, a, levels)}


def plan(px, a, levels):
    lv = sorted({round(x, 2) for x in levels if x})
    merged = []
    for x in lv:  # merge levels closer than 0.2 ATR
        if merged and x - merged[-1] < 0.2 * a:
            continue
        merged.append(x)
    above = [x for x in merged if x > px + 0.1 * a]
    below = [x for x in merged if x < px - 0.1 * a][::-1]

    def pick(seq, start, sign):
        out, last = [], start
        for x in seq:
            if sign * (x - last) >= 0.5 * a:
                out.append(round(x, 1)); last = x
            if len(out) == 3:
                break
        return out

    call = put = None
    if above:
        trig = above[0]
        stop = (below[0] if below else px - a) - 0.1 * a
        call = {"trigger": round(trig, 1), "targets": pick(above[1:], trig, 1), "stop": round(stop, 1)}
    if below:
        trig = below[0]
        stop = (above[0] if above else px + a) + 0.1 * a
        put = {"trigger": round(trig, 1), "targets": pick(below[1:], trig, -1), "stop": round(stop, 1)}
    return {"call": call, "put": put}


def main():
    os.makedirs(OUT, exist_ok=True)
    tv("symbol", SYMBOL)
    time.sleep(4)
    data = {tf: fetch(tf, f"spx_{tf}") for tf, _, _ in TFS}
    daily = fetch("D")
    tv("timeframe", "15")
    q = tv("quote")

    b5 = data["5"]
    last_t = dt.datetime.fromtimestamp(b5[-1]["time"], NY)
    today = last_t.date()
    # session date: futures sessions start 18:00 NY on the previous day
    shift = dt.timedelta(hours=6 if IS_FUT else 0)
    day = lambda x: (dt.datetime.fromtimestamp(x["time"], NY) + shift).date()
    today = day(b5[-1])
    today_bars = [x for x in b5 if day(x) == today]
    prev = [x for x in daily if day(x) < today][-1]
    H, L, C = prev["high"], prev["low"], prev["close"]
    P = (H + L + C) / 3
    pivots = {"P": P, "R1": 2 * P - L, "S1": 2 * P - H, "R2": P + (H - L), "S2": P - (H - L),
              "R3": H + 2 * (P - L), "S3": L - 2 * (H - P)}
    th, tl = max(x["high"] for x in today_bars), min(x["low"] for x in today_bars)
    common = {"vwap": vwap(today_bars), "pivots": pivots, "pdc": C,
              "extra": [H, L, C, th, tl]}

    dc = [x["close"] for x in daily]
    d_e20, d_e50, d_e200 = (ema(dc, n)[-1] for n in (20, 50, 200))
    d_trend = "صاعد" if dc[-1] > d_e50 > d_e200 else "هابط" if dc[-1] < d_e50 and dc[-1] < d_e20 else "متذبذب"

    report = {
        "symbol": q.get("symbol", SYMBOL),
        "name": NAME,
        "market_open": market_open(),
        "as_of_ny": last_t.strftime("%Y-%m-%d %H:%M"),
        "generated_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M"),
        "last": q.get("last", b5[-1]["close"]),
        "day": {"open": today_bars[0]["open"], "high": th, "low": tl,
                "prev_high": H, "prev_low": L, "prev_close": C,
                "change_pct": round((b5[-1]["close"] / C - 1) * 100, 2),
                "vwap": round(common["vwap"], 2)},
        "daily_context": {"trend": d_trend, "ema20": round(d_e20, 1), "ema50": round(d_e50, 1),
                          "ema200": round(d_e200, 1), "rsi": round(rsi(dc), 1), "atr": round(atr(daily), 1)},
        "pivots": {k: round(v, 1) for k, v in pivots.items()},
        "timeframes": {},
    }
    for tf, label, k in TFS:
        r = analyze_tf(data[tf], k, common, today_bars)
        r["label"] = label
        report["timeframes"][tf] = r
    with open(os.path.join(OUT, "report.json"), "w") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    with open(os.path.join(OUT, "report.html"), "w") as f:
        f.write(render(report))
    print(json.dumps({"out": OUT, "last": report["last"], "as_of_ny": report["as_of_ny"],
                      "bias": {tf: (r["bias"], r["score"]) for tf, r in report["timeframes"].items()}},
                     ensure_ascii=False))


# ---------------------------------------------------------------- HTML email
def render(r):
    e = html.escape
    col = lambda s: "#16a34a" if s > 0 else "#dc2626" if s < 0 else "#6b7280"
    sym = lambda s: "▲" if s > 0 else "▼" if s < 0 else "■"
    d = r["day"]
    out = [f"""<div dir="rtl" style="font-family:Tahoma,Arial,sans-serif;color:#111;max-width:760px;margin:auto">
<h2 style="margin:0 0 4px">تحليل {e(r['name'])} الساعي — {e(r['as_of_ny'])} (نيويورك)</h2>
{'' if r['market_open'] else '<p style="margin:0 0 8px;padding:6px 10px;background:#fff7ed;border:1px solid #fed7aa;border-radius:6px;font-size:13px">السوق النقدي لـ SPX مغلق الآن، لذلك التحليل على عقود ES المستقبلية التي تتداول قرابة 23 ساعة وتتبع SPX. فرق السعر بينهما (الأساس) عادةً بضع نقاط إلى عشرات النقاط، فطبّق المستويات بالنسبة نفسها.</p>'}
<p style="margin:0 0 12px;color:#555">آخر سعر <b>{r['last']:.2f}</b> ({d['change_pct']:+.2f}%) · افتتاح {d['open']:.1f} · أعلى {d['high']:.1f} · أدنى {d['low']:.1f} · VWAP {d['vwap']:.1f}<br>
إغلاق الجلسة السابقة {d['prev_close']:.1f} (أعلى {d['prev_high']:.1f} / أدنى {d['prev_low']:.1f}) · الاتجاه اليومي: <b>{e(r['daily_context']['trend'])}</b> (EMA20 {r['daily_context']['ema20']} / EMA50 {r['daily_context']['ema50']} / EMA200 {r['daily_context']['ema200']}، RSI {r['daily_context']['rsi']})</p>
<!--SUMMARY-->"""]
    for tf, t in r["timeframes"].items():
        p = t["plan"]
        out.append(f"""<h3 style="margin:18px 0 6px;border-bottom:2px solid #eee">فريم {e(t['label'])} — الانحياز: <span style="color:{col(t['score'])}">{e(t['bias'])} ({t['score']:+g}/10)</span></h3>
<table style="border-collapse:collapse;width:100%;font-size:14px;margin-bottom:8px">
<tr style="background:#f3f4f6"><th style="padding:6px;text-align:right">الخطة</th><th style="text-align:right">الدخول (إغلاق شمعة)</th><th style="text-align:right">الأهداف</th><th style="text-align:right">الوقف</th></tr>""")
        for key, name, c in (("call", "🟢 كول", "#16a34a"), ("put", "🔴 بوت", "#dc2626")):
            x = p.get(key)
            if x:
                arrow = " ← ".join(f"{v:.1f}" for v in x["targets"]) or "—"
                cmp_ = "فوق" if key == "call" else "تحت"
                out.append(f"<tr><td style='padding:6px;color:{c};font-weight:bold'>{name}</td><td>{cmp_} {x['trigger']:.1f}</td><td>{arrow}</td><td>{x['stop']:.1f}</td></tr>")
        out.append("</table><table style='border-collapse:collapse;width:100%;font-size:13px'>")
        for s in t["schools"]:
            out.append(f"<tr style='border-top:1px solid #eee'><td style='padding:4px;white-space:nowrap;color:{col(s['score'])}'>{sym(s['score'])} {e(s['school'])}</td><td style='padding:4px;color:#333'>{e(s['note'])}</td></tr>")
        out.append("</table>")
    pv = r["pivots"]
    out.append(f"""<p style="font-size:13px;color:#555;margin-top:14px">Pivots: R3 {pv['R3']} · R2 {pv['R2']} · R1 {pv['R1']} · <b>P {pv['P']}</b> · S1 {pv['S1']} · S2 {pv['S2']} · S3 {pv['S3']}</p>
<!--CHARTS-->
<p style="font-size:12px;color:#888;margin-top:16px">البيانات من TradingView (قد تتأخر 10 دقائق دون تسجيل دخول). تحليل فني آلي للمعلومة فقط وليس توصية مالية. الدخول بعد إغلاق شمعة على الفريم نفسه خلف مستوى الدخول.</p></div>""")
    return "\n".join(out)


if __name__ == "__main__":
    main()
