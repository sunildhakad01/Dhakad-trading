"""Backtest engine — tests the confluence indicator strategy on
stored candles and reports HONEST statistics (costs included).

What it simulates (same logic the live signal engine uses on price data):
  - votes: EMA9>EMA21, Supertrend direction, RSI>60/<40, MACD line vs signal
  - enter LONG when score >= +threshold, SHORT when <= -threshold,
    at the NEXT bar's open (no look-ahead)
  - SL = 1.5*ATR from entry, target = rr * risk, exit also on opposite signal
  - costs deducted per round trip: slippage + brokerage + STT approximation

Walk-forward: split the series into k folds; pick the best threshold on the
training fold, evaluate it untouched on the next fold. Reported stats come
ONLY from out-of-sample folds — this is the honest number.

A strategy is marked verified=True only if out-of-sample results clear:
  n_trades >= 30, win_rate >= 0.50, profit_factor >= 1.3, max_drawdown < 30%.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from backend.analysis.indicators import atr, ema, macd, rsi, supertrend, to_df

# --- cost model (per ROUND TRIP, as a fraction of traded value) -------------
SLIPPAGE_PCT = 0.0005      # 0.05% total slippage (both sides)
BROKERAGE_RS = 40.0        # ~Rs 20 per executed order x2 (flat plans)
STT_PCT = 0.00025          # sell-side STT on delivery-ish approximation

VERIFY_MIN_TRADES = 30
VERIFY_MIN_WR = 0.50
VERIFY_MIN_PF = 1.3
VERIFY_MAX_DD = 0.30


# ------------------------------------------------------------------ signals
def score_series(df: pd.DataFrame) -> pd.Series:
    close = df["close"]
    s = pd.Series(0, index=df.index, dtype=int)
    s += np.where(ema(close, 9) > ema(close, 21), 1, -1)
    _, st_dir = supertrend(df)
    s += st_dir.astype(int)
    r = rsi(close)
    s += np.where(r > 60, 1, np.where(r < 40, -1, 0))
    m_line, m_sig, _ = macd(close)
    s += np.where(m_line > m_sig, 1, -1)
    return pd.Series(s, index=df.index)


# ------------------------------------------------------------------ core sim
def run(candles: list[dict], threshold: int = 3, sl_atr: float = 1.5,
        rr: float = 2.0, notional_rs: float = 100_000.0) -> dict:
    """Simulate on one candle list. Returns stats + trade list."""
    df = to_df(candles)
    if len(df) < 60:
        return {"error": f"Need >= 60 candles to backtest, have {len(df)}."}

    score = score_series(df).values
    a = atr(df).values
    o, h, l, c = (df[k].values for k in ("open", "high", "low", "close"))
    n = len(df)

    trades: list[dict] = []
    pos = 0            # 0 flat, +1 long, -1 short
    entry = sl = tgt = 0.0
    entry_i = 0

    def close_trade(i: int, exit_px: float, why: str) -> None:
        nonlocal pos
        pts = (exit_px - entry) * pos
        qty = notional_rs / entry
        gross = pts * qty
        costs = notional_rs * (SLIPPAGE_PCT + STT_PCT) + BROKERAGE_RS
        trades.append({"entry_i": entry_i, "exit_i": i, "dir": pos,
                       "entry": round(entry, 2), "exit": round(exit_px, 2),
                       "pts": round(pts, 2), "net_rs": round(gross - costs, 2),
                       "why": why})
        pos = 0

    for i in range(1, n - 1):
        if pos == 0:
            if abs(score[i]) >= threshold and not np.isnan(a[i]) and a[i] > 0:
                pos = 1 if score[i] > 0 else -1
                entry = o[i + 1]                      # next bar open — no look-ahead
                entry_i = i + 1
                risk = sl_atr * a[i]
                sl = entry - pos * risk
                tgt = entry + pos * rr * risk
        else:
            j = i  # manage on the current bar
            if pos == 1:
                if l[j] <= sl:
                    close_trade(j, sl, "SL")
                elif h[j] >= tgt:
                    close_trade(j, tgt, "TARGET")
                elif score[j] <= -threshold:
                    close_trade(j, c[j], "FLIP")
            else:
                if h[j] >= sl:
                    close_trade(j, sl, "SL")
                elif l[j] <= tgt:
                    close_trade(j, tgt, "TARGET")
                elif score[j] >= threshold:
                    close_trade(j, c[j], "FLIP")
    if pos != 0:
        close_trade(n - 1, c[-1], "EOD")

    return _stats(trades, notional_rs) | {
        "params": {"threshold": threshold, "sl_atr": sl_atr, "rr": rr},
        "_all_trades": trades,
    }


def _stats(trades: list[dict], notional: float) -> dict:
    if not trades:
        return {"n_trades": 0, "note": "No trades triggered."}
    net = np.array([t["net_rs"] for t in trades])
    wins, losses = net[net > 0], net[net <= 0]
    equity = net.cumsum()
    peak = np.maximum.accumulate(np.concatenate([[0.0], equity]))[1:]
    dd = (peak - equity).max() / notional if len(equity) else 0.0
    pf = float(wins.sum() / abs(losses.sum())) if losses.sum() != 0 else float("inf")
    return {
        "n_trades": int(len(trades)),
        "win_rate": round(float(len(wins) / len(trades)), 3),
        "profit_factor": round(pf, 2) if np.isfinite(pf) else None,
        "net_pnl_rs": round(float(net.sum()), 2),
        "avg_trade_rs": round(float(net.mean()), 2),
        "max_drawdown_pct": round(float(dd) * 100, 2),
        "costs_included": True,
        "trades_sample": trades[-5:],
    }


# ------------------------------------------------------------------ walk-forward
def walk_forward(candles: list[dict], folds: int = 4,
                 thresholds: tuple[int, ...] = (2, 3, 4)) -> dict:
    """Optimize threshold in-sample, evaluate out-of-sample, repeat. The
    aggregate out-of-sample stats are the ONLY numbers shown as 'verified'."""
    n = len(candles)
    if n < 240:
        return {"error": f"Need >= 240 candles for walk-forward, have {n}. "
                         "Let the backfill/collector gather more history."}

    fold_len = n // (folds + 1)
    oos_trades: list[dict] = []
    fold_reports = []

    for f in range(folds):
        train = candles[f * fold_len:(f + 1) * fold_len]
        test = candles[(f + 1) * fold_len:(f + 2) * fold_len]
        best_th, best_pnl = None, -np.inf
        for th in thresholds:
            r = run(train, threshold=th)
            pnl = r.get("net_pnl_rs", -np.inf) if "error" not in r else -np.inf
            if r.get("n_trades", 0) >= 5 and pnl > best_pnl:
                best_th, best_pnl = th, pnl
        if best_th is None:
            fold_reports.append({"fold": f + 1, "note": "no viable threshold in-sample"})
            continue
        r_test = run(test, threshold=best_th)
        fold_reports.append({"fold": f + 1, "threshold": best_th,
                             "oos": {k: r_test.get(k) for k in
                                     ("n_trades", "win_rate", "profit_factor",
                                      "net_pnl_rs", "max_drawdown_pct")}})
        oos_trades.extend(r_test.get("_all_trades", []))

    agg = _stats(oos_trades, 100_000.0) if oos_trades else {"n_trades": 0}
    verified = bool(
        agg.get("n_trades", 0) >= VERIFY_MIN_TRADES
        and (agg.get("win_rate") or 0) >= VERIFY_MIN_WR
        and (agg.get("profit_factor") or 0) >= VERIFY_MIN_PF
        and (agg.get("max_drawdown_pct") or 100) / 100 <= VERIFY_MAX_DD
    )
    agg.pop("trades_sample", None)
    return {
        "method": "walk-forward",
        "folds": fold_reports,
        "out_of_sample": agg,
        "verified": verified,
        "verify_criteria": {
            "min_trades": VERIFY_MIN_TRADES, "min_win_rate": VERIFY_MIN_WR,
            "min_profit_factor": VERIFY_MIN_PF, "max_drawdown": VERIFY_MAX_DD,
        },
        "honesty_note": "Out-of-sample stats only; costs included. If verified "
                        "is false, DO NOT trade this setup — that is the system "
                        "working as intended, not a bug.",
    }


