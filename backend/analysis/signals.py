"""Phase 4 signal engine.

Turns the confluence composite + spot trade plan + option chain into an
actionable *option* trade plan:

  1. Gate: signal fires only when the composite bias is non-neutral AND
     agreement >= MIN_AGREEMENT (i.e. enough independent modules concur).
  2. Strategy selection by IV regime (desk heuristic, documented, tunable):
       low IV      -> buy ATM CE/PE (cheap premium, long gamma)
       normal IV   -> debit spread (buy ATM, sell 2 steps OTM — cuts theta)
       high IV     -> credit spread (sell 2 steps OTM, buy 4 steps OTM —
                      sell expensive premium, defined risk)
  3. Delta translation: spot entry/SL/targets from levels.plan_trade are
     converted to option-premium levels via Black-Scholes delta computed
     from the chain's own IV (fallback: moneyness heuristic).

All thresholds here are v1 defaults awaiting Phase 5 backtesting — per
CLAUDE.md rule 8 they are shown as heuristics, never as verified accuracy.
"""
from __future__ import annotations

import math
from datetime import datetime
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

MIN_AGREEMENT = 0.6          # fraction of voting modules that must agree
RISK_FREE_RATE = 0.066       # ~India 10y; only mildly affects delta

# IV regime cutoffs (annualised %, ATM). Absolute heuristics until we have
# enough stored IV history to compute a proper IV percentile.
IV_LOW = 13.0
IV_HIGH = 18.0


# ------------------------------------------------------------------ BS delta
def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_delta(spot: float, strike: float, iv_pct: float, days_to_expiry: float,
             option_type: str, r: float = RISK_FREE_RATE) -> float:
    """Black-Scholes delta. iv_pct is annualised IV in percent (e.g. 14.5)."""
    t = max(days_to_expiry, 0.25) / 365.0
    sigma = max(iv_pct, 1.0) / 100.0
    d1 = (math.log(spot / strike) + (r + sigma * sigma / 2) * t) / (sigma * math.sqrt(t))
    delta = _norm_cdf(d1)
    return delta if option_type == "CE" else delta - 1.0


def _fallback_delta(spot: float, strike: float, option_type: str) -> float:
    """No IV available: crude moneyness ramp (ATM≈0.5, deep ITM→1, deep OTM→0)."""
    m = (spot - strike) / spot if option_type == "CE" else (strike - spot) / spot
    delta = min(max(0.5 + m * 8, 0.05), 0.95)
    return delta if option_type == "CE" else -delta


# ------------------------------------------------------------------ helpers
def _iv_regime(chain: dict) -> tuple[str, float | None]:
    """Regime from ATM IV (mean of CE/PE IV at the ATM strike)."""
    spot = chain.get("spot")
    rows = chain.get("strikes") or []
    if not spot or not rows:
        return "unknown", None
    atm = min(rows, key=lambda r: abs(r["strike"] - spot))
    ivs = [v for v in (atm.get("ce_iv"), atm.get("pe_iv")) if v]
    if not ivs:
        return "unknown", None
    iv = sum(ivs) / len(ivs)
    if iv < IV_LOW:
        return "low", round(iv, 2)
    if iv > IV_HIGH:
        return "high", round(iv, 2)
    return "normal", round(iv, 2)


def _strike_ladder(chain: dict, direction: str) -> list[dict]:
    """Rows sorted so index 0 = ATM, walking OTM in the trade direction."""
    spot = chain.get("spot")
    rows = sorted(chain.get("strikes") or [], key=lambda r: r["strike"])
    if not spot or not rows:
        return []
    atm_i = min(range(len(rows)), key=lambda i: abs(rows[i]["strike"] - spot))
    return rows[atm_i:] if direction == "LONG" else rows[atm_i::-1]


def _days_to_expiry(expiry_epoch: int | None) -> float:
    if not expiry_epoch:
        return 3.0
    now = datetime.now(IST).timestamp()
    return max((expiry_epoch - now) / 86400.0, 0.25)


def _leg(row: dict, opt: str, action: str) -> dict:
    return {
        "action": action,                       # BUY / SELL
        "strike": row["strike"],
        "type": opt,                            # CE / PE
        "trading_symbol": row.get(f"{opt.lower()}_tsym"),
        "ltp": row.get(f"{opt.lower()}_ltp"),
        "iv": row.get(f"{opt.lower()}_iv"),
        "oi": row.get(f"{opt.lower()}_oi"),
    }


# ------------------------------------------------------------------ main
def generate(composite: dict, spot_plan: dict, chain: dict,
             lot_size: int = 1) -> dict:
    """composite: confluence.combine() output; spot_plan: levels.plan_trade()
    output; chain: option_chain.build() output."""
    bias = composite.get("bias", "NEUTRAL")
    agreement = composite.get("agreement", 0)

    if bias == "NEUTRAL" or agreement < MIN_AGREEMENT:
        return {"signal": "NO_TRADE",
                "reason": f"Confluence too weak (bias={bias}, agreement={agreement}). "
                          f"Need >= {MIN_AGREEMENT:.0%} of modules agreeing."}
    if not spot_plan or "entry" not in spot_plan:
        return {"signal": "NO_TRADE", "reason": "No valid spot trade plan."}

    direction = "LONG" if bias == "BULLISH" else "SHORT"
    opt = "CE" if direction == "LONG" else "PE"
    regime, atm_iv = _iv_regime(chain)
    ladder = _strike_ladder(chain, direction)
    if len(ladder) < 5:
        return {"signal": "NO_TRADE",
                "reason": "Option chain too thin to build a strategy "
                          "(need >= 5 strikes from ATM)."}

    # ---- strategy by IV regime
    if regime in ("low", "unknown"):
        strategy, legs = f"BUY_ATM_{opt}", [_leg(ladder[0], opt, "BUY")]
        note = "Low/unknown IV: premium is cheap — plain long option."
    elif regime == "normal":
        strategy = f"DEBIT_SPREAD_{opt}"
        legs = [_leg(ladder[0], opt, "BUY"), _leg(ladder[2], opt, "SELL")]
        note = "Normal IV: debit spread cuts theta decay for a small cap on profit."
    else:
        anti = "PE" if opt == "CE" else "CE"  # credit spread on the OTHER side
        strategy = f"CREDIT_SPREAD_SELL_{anti}"
        legs = [_leg(ladder[2], anti, "SELL"), _leg(ladder[4], anti, "BUY")]
        note = ("High IV: sell expensive premium against the move "
                f"(sell {anti} spread) instead of buying dear options.")

    # ---- delta translation for the primary (bought/sold ATM-most) leg
    spot = chain.get("spot") or spot_plan["entry"]
    dte = _days_to_expiry(chain.get("expiry"))
    main = legs[0]
    if main.get("iv"):
        delta = bs_delta(spot, main["strike"], main["iv"], dte, main["type"])
    else:
        delta = _fallback_delta(spot, main["strike"], main["type"])
    adelta = abs(delta)

    premium = main.get("ltp")
    prem_levels = None
    if premium:
        risk_pts = abs(spot_plan["entry"] - spot_plan["sl"])
        t1_pts = abs(spot_plan["t1"] - spot_plan["entry"])
        t2_pts = abs(spot_plan["t2"] - spot_plan["entry"])
        sign = 1 if main["action"] == "BUY" else -1
        prem_levels = {
            "entry": round(premium, 2),
            "sl": round(max(premium - sign * adelta * risk_pts, 0.05), 2),
            "t1": round(premium + sign * adelta * t1_pts, 2),
            "t2": round(premium + sign * adelta * t2_pts, 2),
            "delta_used": round(delta, 3),
            "note": "Premium levels ≈ delta × spot move (gamma/theta ignored — "
                    "recheck near expiry).",
        }
        risk_per_lot = round(abs(premium - prem_levels["sl"]) * lot_size, 2)
    else:
        risk_per_lot = None

    return {
        "signal": direction,
        "strategy": strategy,
        "strategy_note": note,
        "iv_regime": regime,
        "atm_iv": atm_iv,
        "days_to_expiry": round(dte, 1),
        "legs": legs,
        "spot_plan": spot_plan,
        "premium_plan": prem_levels,
        "lot_size": lot_size,
        "risk_per_lot": risk_per_lot,
        "composite": {"bias": bias, "agreement": agreement,
                      "score": composite.get("score")},
        "warning": "Heuristic v1 — not yet validated by Phase 5 backtesting. "
                   "Paper-trade only.",
    }
