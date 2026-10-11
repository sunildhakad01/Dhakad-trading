"""Confluence scoring engine — combines every module's vote into one signal.

Each analysis module (price indicators, option-chain PCR, driver-stock
breadth, news sentiment) casts a vote: +1 bullish, -1 bearish, 0 neutral.
The composite reads BULLISH/BEARISH only when >=60% of the modules that
actually produced a vote agree; otherwise NEUTRAL.

Equal weights and the 60% cutoff are a v1 default, same status as the
auto SL/target engine in levels.py: a sensible starting heuristic, not a
backtested rule. Tune both once Phase 5 backtesting shows which modules
actually carry predictive value. Per CLAUDE.md rule 8, this is never framed
as more accurate than that.
"""
from __future__ import annotations

import math


def combine(votes: dict[str, int]) -> dict:
    """votes: {module_name: -1|0|1}. A module that couldn't be computed
    (no option chain for this symbol, no news matched, etc.) should be left
    out entirely by the caller — passing 0 would misrepresent "not computed"
    as "genuinely neutral"."""
    if not votes:
        return {"score": 0, "bias": "NEUTRAL", "votes": {}, "agreement": 0.0,
               "modules_voting": 0}

    score = sum(votes.values())
    n = len(votes)
    needed = math.ceil(0.6 * n)
    bias = "BULLISH" if score >= needed else "BEARISH" if score <= -needed else "NEUTRAL"
    return {
        "score": score,
        "bias": bias,
        "votes": votes,
        "agreement": round(abs(score) / n, 2),
        "modules_voting": n,
    }


def pcr_vote(pcr: float | None) -> int:
    """PCR > 1.3: heavy put writing underneath reads bullish (support).
    PCR < 0.7: heavy call writing overhead reads bearish (resistance).
    Common desk heuristic, not backtested — see module docstring."""
    if pcr is None:
        return 0
    if pcr > 1.3:
        return 1
    if pcr < 0.7:
        return -1
    return 0


def breadth_vote(bias: str | None) -> int:
    return {"BULLISH": 1, "BEARISH": -1}.get(bias or "", 0)
