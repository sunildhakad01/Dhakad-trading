"""Auto SL / target engine.

Method (what professional discretionary traders do, encoded):
  - Entry  = current price (assumes signal fired now)
  - SL     = structure-based: last confirmed swing low (long) / swing high (short),
             padded by 0.25*ATR; capped at 1.5*ATR from entry so risk stays sane
  - T1     = 1.5R,  T2 = 2.5R  (R = entry-to-SL distance)
             then snapped to the nearest meaningful level (CPR/pivots/prev day H-L)
             if one sits close (within 0.3R) — targets into a wall are cut short.

Everything returned is drawn on the chart by the frontend.
This logic gets tuned by backtesting in Phase 5 — treat v1 as a sensible default.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from backend.analysis.indicators import atr, to_df


def swing_points(df: pd.DataFrame, lookback: int = 3) -> tuple[list[int], list[int]]:
    """Indices of confirmed swing highs / lows (fractal method, `lookback` bars each side)."""
    highs, lows = [], []
    h, l = df["high"].values, df["low"].values
    n = len(df)
    for i in range(lookback, n - lookback):
        if h[i] == max(h[i - lookback:i + lookback + 1]):
            highs.append(i)
        if l[i] == min(l[i - lookback:i + lookback + 1]):
            lows.append(i)
    return highs, lows


def plan_trade(candles: list[dict], direction: str, levels: dict | None = None) -> dict:
    """direction: 'LONG' or 'SHORT'. Returns entry/sl/t1/t2/rr + reasoning."""
    df = to_df(candles)
    if len(df) < 30:
        return {"error": "Not enough candles to plan a trade."}

    entry = float(df["close"].iloc[-1])
    a = float(atr(df).iloc[-1])
    highs, lows = swing_points(df)
    pad = 0.25 * a
    max_risk = 1.5 * a

    if direction == "LONG":
        swing = float(df["low"].iloc[lows[-1]]) if lows else entry - a
        sl = swing - pad
        if entry - sl > max_risk:
            sl = entry - max_risk
        risk = entry - sl
        t1, t2 = entry + 1.5 * risk, entry + 2.5 * risk
        basis = "below last swing low"
    elif direction == "SHORT":
        swing = float(df["high"].iloc[highs[-1]]) if highs else entry + a
        sl = swing + pad
        if sl - entry > max_risk:
            sl = entry + max_risk
        risk = sl - entry
        t1, t2 = entry - 1.5 * risk, entry - 2.5 * risk
        basis = "above last swing high"
    else:
        return {"error": "direction must be LONG or SHORT"}

    if risk <= 0:
        return {"error": "Invalid structure — SL would be on the wrong side of entry."}

    # Snap targets to nearby key levels (don't aim through a wall)
    snap_notes = []
    if levels:
        candidates = [v for v in levels.values() if isinstance(v, (int, float))]
        for name, tgt in (("t1", t1), ("t2", t2)):
            near = [lv for lv in candidates if abs(lv - tgt) <= 0.3 * risk]
            if near:
                snapped = min(near, key=lambda lv: abs(lv - tgt))
                # only snap if it doesn't reduce RR below 1.2 for t1
                if name == "t1" and abs(snapped - entry) / risk < 1.2:
                    continue
                if name == "t1":
                    t1 = snapped
                else:
                    t2 = snapped
                snap_notes.append(f"{name.upper()} snapped to level {snapped:.2f}")

    return {
        "direction": direction,
        "entry": round(entry, 2),
        "sl": round(sl, 2),
        "t1": round(t1, 2),
        "t2": round(t2, 2),
        "risk_points": round(risk, 2),
        "rr_t1": round(abs(t1 - entry) / risk, 2),
        "rr_t2": round(abs(t2 - entry) / risk, 2),
        "atr": round(a, 2),
        "reasoning": f"SL {basis} + 0.25*ATR pad (ATR={a:.2f}); "
                     f"targets at 1.5R/2.5R. " + "; ".join(snap_notes),
        "warning": "v1 heuristic — not yet backtested. Do not trade real money on this.",
    }
