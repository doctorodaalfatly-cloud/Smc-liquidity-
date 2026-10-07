"""
Liquidity Scanner — SMC liquidity pools + volume liquidity
Data: Yahoo Finance (yfinance)
Run:  streamlit run app.py
"""
import numpy as np
import pandas as pd

# ───────────────────────── DATA ─────────────────────────
INTERVALS = {
    "5m": ["5d", "1mo"],
    "15m": ["5d", "1mo", "60d"],
    "1h": ["1mo", "3mo", "6mo", "1y", "2y"],
    "1d": ["6mo", "1y", "2y", "5y"],
}


def load(ticker: str, period: str, interval: str) -> pd.DataFrame:
    import yfinance as yf
    df = yf.download(ticker, period=period, interval=interval,
                     auto_adjust=False, progress=False)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df[["Open", "High", "Low", "Close", "Volume"]].dropna(subset=["Close"])
    return df


# ───────────────────────── SMC LIQUIDITY ─────────────────────────
def pivots(df: pd.DataFrame, n: int):
    """Swing highs/lows: (position, price) lists."""
    w = 2 * n + 1
    hi_max = df["High"].rolling(w, center=True).max()
    lo_min = df["Low"].rolling(w, center=True).min()
    highs = [(i, h) for i, (h, m) in enumerate(zip(df["High"], hi_max))
             if not np.isnan(m) and h == m]
    lows = [(i, l) for i, (l, m) in enumerate(zip(df["Low"], lo_min))
            if not np.isnan(m) and l == m]
    return highs, lows


def cluster(levels, tol_pct: float):
    """Group swing levels within tol_pct of each other -> equal highs/lows."""
    levels = sorted(levels, key=lambda x: x[1])
    groups, cur = [], []
    for pos, price in levels:
        if cur and abs(price - cur[0][1]) / cur[0][1] * 100 <= tol_pct:
            cur.append((pos, price))
        else:
            if cur:
                groups.append(cur)
            cur = [(pos, price)]
    if cur:
        groups.append(cur)
    return groups


def status_of(df: pd.DataFrame, last_pos: int, level: float, side: str) -> str:
    """side='high' (BSL) or 'low' (SSL). Active / Swept (rejected) / Broken."""
    after = df.iloc[last_pos + 1:]
    if after.empty:
        return "Active"
    if side == "high":
        if (after["Close"] > level).any():
            return "Broken"
        if (after["High"] > level).any():
            return "Swept"
    else:
        if (after["Close"] < level).any():
            return "Broken"
        if (after["Low"] < level).any():
            return "Swept"
    return "Active"


def build_pools(df: pd.DataFrame, swing_n: int, tol_pct: float) -> pd.DataFrame:
    highs, lows = pivots(df, swing_n)
    rows = []
    for side, swings in (("high", highs), ("low", lows)):
        used = set()
        for g in cluster(swings, tol_pct):
            if len(g) >= 2:
                lvl = float(np.mean([p for _, p in g]))
                last = max(i for i, _ in g)
                used.update(i for i, _ in g)
                rows.append(dict(
                    Type="EQH (BSL)" if side == "high" else "EQL (SSL)",
                    Level=lvl, Touches=len(g), Pos=last,
                    Status=status_of(df, last, lvl, side)))
        # single swing points not part of an equal cluster
        for i, p in swings:
            if i not in used:
                st = status_of(df, i, p, side)
                rows.append(dict(
                    Type="Swing High (BSL)" if side == "high" else "Swing Low (SSL)",
                    Level=float(p), Touches=1, Pos=i, Status=st))
    return pd.DataFrame(rows, columns=["Type", "Level", "Touches", "Pos", "Status"])


def key_levels(daily: pd.DataFrame, intraday_last_date=None) -> pd.DataFrame:
    rows = []
    if len(daily) >= 2:
        d = daily
        # drop the still-forming day if it's the same date as the last bar
        prev = d.iloc[-2] if (intraday_last_date is None or
                              d.index[-1].date() == intraday_last_date) else d.iloc[-1]
        rows += [dict(Type="PDH (BSL)", Level=float(prev["High"])),
                 dict(Type="PDL (SSL)", Level=float(prev["Low"]))]
        wk = d.resample("W-FRI").agg({"High": "max", "Low": "min"}).dropna()
        if len(wk) >= 2:
            pw = wk.iloc[-2]
            rows += [dict(Type="PWH (BSL)", Level=float(pw["High"])),
                     dict(Type="PWL (SSL)", Level=float(pw["Low"]))]
    out = pd.DataFrame(rows, columns=["Type", "Level"])
    out["Touches"], out["Pos"], out["Status"] = 1, None, "Active"
    return out


def enrich(pools: pd.DataFrame, price: float, atr: float) -> pd.DataFrame:
    if pools.empty:
        return pools
    p = pools.copy()
    p["Dist %"] = (p["Level"] - price) / price * 100
    p["Dist ATR"] = (p["Level"] - price) / atr if atr else np.nan
    p["Side"] = np.where(p["Level"] > price, "Above", "Below")
    return p


# ───────────────────────── VOLUME LIQUIDITY ─────────────────────────
def atr(df: pd.DataFrame, n: int = 14) -> float:
    tr = pd.concat([df["High"] - df["Low"],
                    (df["High"] - df["Close"].shift()).abs(),
                    (df["Low"] - df["Close"].shift()).abs()], axis=1).max(axis=1)
    return float(tr.rolling(n).mean().iloc[-1])


def volume_liquidity(daily: pd.DataFrame) -> dict:
    d = daily.tail(60)
    vol20 = d["Volume"].tail(20).mean()
    dollar_vol = float((d["Close"] * d["Volume"]).tail(20).mean())
    rvol = float(d["Volume"].iloc[-1] / vol20) if vol20 else np.nan
    range_pct = float(((d["High"] - d["Low"]) / d["Close"]).tail(20).mean() * 100)
    ret = d["Close"].pct_change().abs()
    dv = d["Close"] * d["Volume"]
    amihud = float((ret / dv.replace(0, np.nan)).tail(20).mean() * 1e9)  # x1e9 for readability

    if vol20 == 0 or np.isnan(vol20):
        return dict(available=False)

    s_dv = np.clip((np.log10(max(dollar_vol, 1)) - 6) / 3 * 60, 0, 60)   # $1M→0 , $1B→60
    s_rng = np.clip(40 * (1 - range_pct / 5), 0, 40)                      # tight range = liquid
    score = float(s_dv + s_rng)
    label = ("عالية جداً" if score >= 80 else "عالية" if score >= 60
             else "متوسطة" if score >= 40 else "ضعيفة")
    return dict(available=True, avg_vol=float(vol20), dollar_vol=dollar_vol,
                rvol=rvol, range_pct=range_pct, amihud=amihud,
                score=score, label=label)


# ───────────────────────── UI ─────────────────────────
def fmt_money(x):
    for unit, v in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(x) >= v:
            return f"${x / v:,.2f}{unit}"
    return f"${x:,.0f}"


def main():
    import streamlit as st
    import plotly.graph_objects as go

    st.set_page_config(page_title="Liquidity Scanner", layout="wide")
    st.title("💧 Liquidity Scanner — SMC + Volume")

    with st.sidebar:
        ticker = st.text_input("Ticker (Yahoo)", "SPY").strip().upper()
        st.caption("أمثلة: NVDA · TSLA · MU · ^GSPC · ES=F · BTC-USD")
        interval = st.selectbox("Interval", list(INTERVALS), index=1)
        period = st.selectbox("Period", INTERVALS[interval])
        swing_n = st.slider("Swing length", 2, 20, 5)
        tol = st.slider("Equal-level tolerance %", 0.01, 0.50, 0.08, 0.01)
        max_levels = st.slider("Max levels on chart", 4, 30, 12)

    try:
        df = load(ticker, period, interval)
        daily = df if interval == "1d" else load(ticker, "1y", "1d")
    except Exception as e:  # noqa
        st.error(f"فشل تحميل البيانات: {e}")
        return
    if df.empty or len(df) < swing_n * 4:
        st.warning("لا توجد بيانات كافية لهذا الرمز/الفترة.")
        return

    price = float(df["Close"].iloc[-1])
    a = atr(df)

    pools = build_pools(df, swing_n, tol)
    kl = key_levels(daily, df.index[-1].date() if interval != "1d" else None)
    allp = enrich(pd.concat([pools, kl], ignore_index=True), price, a)

    # ── Volume liquidity
    vl = volume_liquidity(daily)
    st.subheader("📊 سيولة التداول")
    if vl["available"]:
        c = st.columns(5)
        c[0].metric("Liquidity Score", f"{vl['score']:.0f}/100", vl["label"])
        c[1].metric("Avg Volume (20d)", f"{vl['avg_vol']:,.0f}")
        c[2].metric("Avg $ Volume (20d)", fmt_money(vl["dollar_vol"]))
        c[3].metric("RVOL (today)", f"{vl['rvol']:.2f}x")
        c[4].metric("Avg Daily Range", f"{vl['range_pct']:.2f}%")
    else:
        st.info("الحجم غير متوفر لهذا الرمز (المؤشرات غالباً بدون Volume). "
                "استخدم ETF مثل SPY أو عقد ES=F لقياس سيولة التداول.")

    # ── SMC pools
    st.subheader("🎯 مناطق السيولة (SMC)")
    active = allp[allp["Status"] == "Active"].copy() if not allp.empty else allp
    if not active.empty:
        above = active[active["Side"] == "Above"].sort_values("Dist %").head(1)
        below = active[active["Side"] == "Below"].sort_values("Dist %", ascending=False).head(1)
        c = st.columns(3)
        c[0].metric("السعر الحالي", f"{price:,.2f}", f"ATR {a:,.2f}")
        if not above.empty:
            r = above.iloc[0]
            c[1].metric(f"أقرب BSL فوق — {r['Type']}", f"{r['Level']:,.2f}",
                        f"{r['Dist %']:+.2f}% | {r['Dist ATR']:+.1f} ATR")
        if not below.empty:
            r = below.iloc[0]
            c[2].metric(f"أقرب SSL تحت — {r['Type']}", f"{r['Level']:,.2f}",
                        f"{r['Dist %']:+.2f}% | {r['Dist ATR']:+.1f} ATR")

    # ── Chart
    show = active.copy()
    if not show.empty:
        show["absd"] = show["Dist %"].abs()
        show = show.sort_values("absd").head(max_levels)
    fig = go.Figure(go.Candlestick(
        x=df.index, open=df["Open"], high=df["High"],
        low=df["Low"], close=df["Close"], name=ticker))
    for _, r in show.iterrows():
        color = "#ffeb3b" if r["Side"] == "Above" else "#ff9800"
        fig.add_hline(y=r["Level"], line_dash="dot", line_color=color,
                      annotation_text=f"{r['Type']} {r['Level']:,.2f}",
                      annotation_position="right")
    fig.update_layout(height=640, xaxis_rangeslider_visible=False,
                      template="plotly_dark", margin=dict(l=10, r=10, t=30, b=10))
    if interval != "1d" and "-USD" not in ticker:
        fig.update_xaxes(rangebreaks=[dict(bounds=["sat", "mon"])])
    st.plotly_chart(fig, use_container_width=True)

    # ── Table
    if not allp.empty:
        t = allp.drop(columns=["Pos"]).sort_values("Level", ascending=False)
        st.dataframe(
            t.style.format({"Level": "{:,.2f}", "Dist %": "{:+.2f}",
                            "Dist ATR": "{:+.1f}"}),
            use_container_width=True, hide_index=True)
    st.caption("Active = لم تُلمس بعد · Swept = سُحبت بذيل ثم رجع السعر · "
               "Broken = أُغلق السعر خلفها. للتعليم والتحليل فقط وليس توصية.")


if __name__ == "__main__":
    main()
