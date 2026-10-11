"""Driver-stock breadth score — weighted vote of index constituents.

Answers "is this move broad-based or just 2-3 heavyweight stocks?" — a rally
where breadth is negative (index up, weighted vote down) is weaker than one
where breadth agrees.

Constituent lists + weights live in index_constituents.json, clearly flagged
there as approximate/illustrative — see that file for the refresh path.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from backend.data import database as db
from backend.kotak.client import client

log = logging.getLogger("breadth")

_CONSTITUENTS_PATH = Path(__file__).resolve().parent.parent / "data" / "index_constituents.json"
FLAT_THRESHOLD_PCT = 0.05  # +/- this % counts as "unchanged" for advance/decline


def _load() -> dict:
    with open(_CONSTITUENTS_PATH, encoding="utf-8") as f:
        return json.load(f)


def available_indices() -> list[str]:
    return [k for k in _load() if k not in ("as_of", "source")]


def indices_containing(symbol: str) -> list[str]:
    """Which tracked indices (if any) list `symbol` as a constituent —
    used to auto-pick a breadth context for a stock's confluence score."""
    data = _load()
    sym = symbol.upper()
    return [idx for idx in available_indices()
            if any(c["symbol"] == sym for c in data[idx])]


def compute(index: str, seg: str = "nse_cm") -> dict:
    data = _load()
    constituents = data.get(index.upper())
    if not constituents:
        return {"error": f"Unknown index '{index}'. Available: {available_indices()}"}

    legs = []
    for c in constituents:
        rows = db.query(
            "SELECT token FROM instruments WHERE exchange_segment=? AND trading_symbol=?",
            (seg, f"{c['symbol']}-EQ"))
        if rows:
            legs.append({"symbol": c["symbol"], "weight": c["weight"], "token": rows[0]["token"]})
    if not legs:
        return {"error": "None of this index's constituents were found in the instrument "
                         "index — has the scrip master loaded nse_cm equities yet?"}

    quotes: dict[str, dict] = {}
    tokens = [f"{seg}|{l['token']}" for l in legs]
    for i in range(0, len(tokens), 40):
        try:
            for item in client.quotes(tokens[i:i + 40], "all"):
                quotes[str(item.get("exchange_token", ""))] = item
        except Exception as e:
            return {"error": f"Quote fetch failed: {e}"}

    advances = declines = unchanged = 0
    weighted_sum = 0.0
    total_weight = 0.0
    movers = []
    for leg in legs:
        q = quotes.get(leg["token"])
        if not q:
            continue
        try:
            ltp = float(q.get("ltp") or 0)
        except (TypeError, ValueError):
            continue
        ohlc = q.get("ohlc") or {}
        prev_close_raw = ohlc.get("close")
        try:
            prev_close = float(prev_close_raw) if prev_close_raw not in (None, "") else None
        except (TypeError, ValueError):
            prev_close = None
        if not ltp or not prev_close:
            continue

        pct = (ltp - prev_close) / prev_close * 100
        vote = 1 if pct > FLAT_THRESHOLD_PCT else -1 if pct < -FLAT_THRESHOLD_PCT else 0
        if vote > 0:
            advances += 1
        elif vote < 0:
            declines += 1
        else:
            unchanged += 1
        weighted_sum += vote * leg["weight"]
        total_weight += leg["weight"]
        movers.append({"symbol": leg["symbol"], "pct": round(pct, 2), "weight": leg["weight"]})

    if total_weight == 0:
        return {"error": "No live quotes resolved for this index's constituents."}

    breadth_score = round(weighted_sum / total_weight, 3)  # in [-1, +1]
    movers.sort(key=lambda m: m["pct"], reverse=True)

    return {
        "index": index.upper(),
        "constituents_as_of": data.get("as_of"),
        "constituents_note": data.get("source"),
        "advances": advances,
        "declines": declines,
        "unchanged": unchanged,
        "breadth_score": breadth_score,
        "bias": ("BULLISH" if breadth_score > 0.15 else
                 "BEARISH" if breadth_score < -0.15 else "NEUTRAL"),
        "top_gainers": movers[:5],
        "top_losers": movers[-5:][::-1] if movers else [],
    }
