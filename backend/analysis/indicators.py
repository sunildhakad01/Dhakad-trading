"""Indicator engine — the toolkit professional option traders actually use.

Input: list of candle dicts [{ts, open, high, low, close, volume}, ...] (ascending).
Output: dict of indicator series/values ready for the chart + a per-indicator
bull/bear vote used by the confluence score.

Pure pandas/numpy — no external TA lib needed.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def to_df(candles: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(candles)
    if df.empty:
        return df
    return df.astype({"open": float, "high": float, "low": float,
                      "close": float, "volume": float})


def resample(candles: list[dict], minutes: int) -> list[dict]:
    """Aggregate ascending 1-minute candles into `minutes`-wide bars.

    Bucket boundaries are plain epoch-second floor division. IST is UTC+5:30
    (19800s), which divides evenly into 5/15/30/60-minute buckets, so this
    lines up with real clock-time bars (9:15, 9:20, ... IST) without needing
    a timezone-aware resample. Backfilled daily bars (spaced ~86400s apart)
    each land in their own bucket untouched, so daily history stays daily
    even when the intraday portion gets aggregated.
    """
    if minutes <= 1 or not candles:
        return candles
    df = to_df(candles)
    if df.empty:
        return candles
    bucket_secs = minutes * 60
    bucket = (df["ts"] // bucket_secs) * bucket_secs
    g = df.groupby(bucket, sort=True)
    out = pd.DataFrame({
        "open": g["open"].first(),
        "high": g["high"].max(),
        "low": g["low"].min(),
        "close": g["close"].last(),
        "volume": g["volume"].sum(),
    })
    out.index.name = "ts"
    out = out.reset_index()
    out["ts"] = out["ts"].astype(int)
    return out.to_dict("records")


# ------------------------------------------------------------------ primitives
def ema(s: pd.Series, span: int) -> pd.Series:
    return s.ewm(span=span, adjust=False).mean()


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(50)


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    line = ema(close, fast) - ema(close, slow)
    sig = line.ewm(span=signal, adjust=False).mean()
    return line, sig, line - sig


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    hl = df["high"] - df["low"]
    hc = (df["high"] - df["close"].shift()).abs()
    lc = (df["low"] - df["close"].shift()).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def supertrend(df: pd.DataFrame, period: int = 10, mult: float = 3.0):
    """Returns (line, direction) where direction=1 bullish, -1 bearish."""
    a = atr(df, period)
    mid = (df["high"] + df["low"]) / 2
    upper = mid + mult * a
    lower = mid - mult * a
    close = df["close"].values
    ub, lb = upper.values.copy(), lower.values.copy()
    n = len(df)
    st = np.full(n, np.nan)
    direction = np.ones(n, dtype=int)

    for i in range(1, n):
        ub[i] = min(upper.iloc[i], ub[i - 1]) if close[i - 1] <= ub[i - 1] else upper.iloc[i]
        lb[i] = max(lower.iloc[i], lb[i - 1]) if close[i - 1] >= lb[i - 1] else lower.iloc[i]
        if close[i] > ub[i - 1]:
            direction[i] = 1
        elif close[i] < lb[i - 1]:
            direction[i] = -1
        else:
            direction[i] = direction[i - 1]
        st[i] = lb[i] if direction[i] == 1 else ub[i]

    return pd.Series(st, index=df.index), pd.Series(direction, index=df.index)


def vwap(df: pd.DataFrame) -> pd.Series:
    """Session VWAP — resets each trading day (ts is epoch seconds IST)."""
    day = pd.to_datetime(df["ts"], unit="s", utc=True).dt.tz_convert("Asia/Kolkata").dt.date
    tp = (df["high"] + df["low"] + df["close"]) / 3
    pv = tp * df["volume"].replace(0, 1)  # indices have no volume; fall back to TP avg
    cum_pv = pv.groupby(day).cumsum()
    cum_v = df["volume"].replace(0, 1).groupby(day).cumsum()
    return cum_pv / cum_v


def bollinger(close: pd.Series, period: int = 20, mult: float = 2.0):
    mid = close.rolling(period).mean()
    sd = close.rolling(period).std()
    return mid, mid + mult * sd, mid - mult * sd


def cpr_and_pivots(prev_high: float, prev_low: float, prev_close: float) -> dict:
    """Central Pivot Range + classic floor pivots from previous day OHLC."""
    p = (prev_high + prev_low + prev_close) / 3
    bc = (prev_high + prev_low) / 2
    tc = 2 * p - bc
    return {
        "pivot": round(p, 2),
        "bc": round(min(bc, tc), 2),
        "tc": round(max(bc, tc), 2),
        "r1": round(2 * p - prev_low, 2),
        "s1": round(2 * p - prev_high, 2),
        "r2": round(p + (prev_high - prev_low), 2),
        "s2": round(p - (prev_high - prev_low), 2),
        "prev_high": round(prev_high, 2),
        "prev_low": round(prev_low, 2),
    }


# ------------------------------------------------------------------ full analysis
def analyze(candles: list[dict], prev_day: dict | None = None) -> dict:
    """Compute everything + a bull/bear vote per module.

    Votes: +1 bullish, -1 bearish, 0 neutral. The composite score is the
    weighted sum — displayed with each component so YOU see why.
    """
    df = to_df(candles)
    if len(df) < 30:
        return {"error": f"Need at least 30 candles, have {len(df)}. "
                         "Let the collector run during market hours or backfill history."}

    close = df["close"]
    out: dict = {"votes": {}, "series": {}, "values": {}}

    # EMAs
    for span in (9, 21, 50, 200):
        if len(df) >= span:
            out["series"][f"ema{span}"] = ema(close, span).round(2).tolist()
    e9 = ema(close, 9).iloc[-1]
    e21 = ema(close, 21).iloc[-1]
    e50 = ema(close, 50).iloc[-1] if len(df) >= 50 else None
    ltp = close.iloc[-1]
    out["votes"]["ema_trend"] = 1 if e9 > e21 else -1 if e9 < e21 else 0
    out["values"]["ema9"], out["values"]["ema21"] = round(e9, 2), round(e21, 2)
    if e50:
        out["values"]["ema50"] = round(e50, 2)

    # RSI
    r = rsi(close)
    rv = r.iloc[-1]
    out["series"]["rsi"] = r.round(2).tolist()
    out["values"]["rsi"] = round(rv, 2)
    out["votes"]["rsi"] = 1 if rv > 60 else -1 if rv < 40 else 0

    # MACD
    m_line, m_sig, m_hist = macd(close)
    out["values"]["macd_hist"] = round(m_hist.iloc[-1], 3)
    rising = len(m_hist) > 1 and m_hist.iloc[-1] > m_hist.iloc[-2]
    out["votes"]["macd"] = 1 if (m_line.iloc[-1] > m_sig.iloc[-1] and rising) else \
                           -1 if (m_line.iloc[-1] < m_sig.iloc[-1] and not rising) else 0

    # Supertrend
    st_line, st_dir = supertrend(df)
    out["series"]["supertrend"] = [None if np.isnan(x) else round(x, 2) for x in st_line]
    out["values"]["supertrend_dir"] = int(st_dir.iloc[-1])
    out["votes"]["supertrend"] = int(st_dir.iloc[-1])

    # VWAP
    vw = vwap(df)
    out["series"]["vwap"] = vw.round(2).tolist()
    out["values"]["vwap"] = round(vw.iloc[-1], 2)
    out["votes"]["vwap"] = 1 if ltp > vw.iloc[-1] else -1

    # Bollinger
    mid, ub, lb = bollinger(close)
    if not np.isnan(mid.iloc[-1]):
        out["values"]["bb"] = {"mid": round(mid.iloc[-1], 2),
                               "upper": round(ub.iloc[-1], 2),
                               "lower": round(lb.iloc[-1], 2)}

    # ATR (used by SL engine)
    a = atr(df)
    out["values"]["atr"] = round(a.iloc[-1], 2)

    # CPR / pivots from previous day
    if prev_day:
        out["values"]["cpr"] = cpr_and_pivots(prev_day["high"], prev_day["low"],
                                              prev_day["close"])

    # Composite confluence score (equal weights v1; tune after backtesting)
    votes = out["votes"]
    score = sum(votes.values())
    out["score"] = score
    out["bias"] = ("BULLISH" if score >= 3 else
                   "BEARISH" if score <= -3 else "NEUTRAL")
    out["ltp"] = round(ltp, 2)
    return out
