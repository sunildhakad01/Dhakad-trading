"""Claude trade-reasoning panel — turns the raw analysis snapshot into a
plain-English trade rationale.

Uses the Anthropic API when ANTHROPIC_API_KEY is set in .env (same pattern
as news.py). Without a key, returns a deterministic template summary built
from the same data, so the panel always renders.

Claude here EXPLAINS the analysis; it never overrides the signal engine's
numbers, and it is instructed to surface risks, not cheerlead.
"""
from __future__ import annotations

import json
import logging

from backend.config import settings

log = logging.getLogger("reasoning")

SYSTEM = """You are a risk-first options trading analyst assisting a retail trader
in Indian markets (NSE). You receive a JSON snapshot: price indicators with votes,
option-chain stats, driver-stock breadth, news sentiment, the generated signal and
its SL/targets. Write a compact trade rationale in plain English:

1. THE SETUP (2-3 sentences): what the confluence is saying and why.
2. WHAT DISAGREES (1-2 sentences): every module that voted against or is missing —
   never hide contrary evidence.
3. THE PLAN (2-3 sentences): restate the strategy, entry/SL/targets and what
   invalidates the trade.
4. RISKS (2-3 bullets): event risk, IV/theta risk, liquidity, anything relevant.

Rules: never invent numbers not in the snapshot; never suggest increasing size;
never imply certainty — these are probabilistic setups. If signal is NO_TRADE,
explain what would need to change for a valid setup. Keep it under 220 words."""


def explain(snapshot: dict) -> dict:
    """snapshot: the /api/signal response (or any analysis bundle)."""
    if settings.anthropic_api_key:
        try:
            return {"source": "claude", "text": _ask_claude(snapshot)}
        except Exception as e:
            log.warning("Claude reasoning failed, using template: %s", e)
    return {"source": "template", "text": _template(snapshot)}


def _ask_claude(snapshot: dict) -> str:
    import anthropic
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    msg = client.messages.create(
        model="claude-sonnet-5",
        max_tokens=600,
        system=SYSTEM,
        messages=[{"role": "user", "content": json.dumps(snapshot, default=str)}],
    )
    return "".join(b.text for b in msg.content if b.type == "text")


def _template(s: dict) -> str:
    """No-API fallback: honest, terse summary from the same fields."""
    if s.get("signal") == "NO_TRADE":
        return f"NO TRADE. {s.get('reason', 'Confluence too weak.')}"

    comp = s.get("composite", {})
    plan = s.get("spot_plan", {})
    prem = s.get("premium_plan") or {}
    legs = ", ".join(f"{l['action']} {l['strike']} {l['type']}"
                     for l in s.get("legs", []))
    lines = [
        f"{s.get('signal')} setup — {comp.get('bias')} with "
        f"{comp.get('agreement', 0):.0%} module agreement (score {comp.get('score')}).",
        f"Strategy: {s.get('strategy')} ({s.get('iv_regime')} IV"
        + (f", ATM IV {s.get('atm_iv')}%" if s.get("atm_iv") else "") + f"). Legs: {legs}.",
        f"Spot plan: entry {plan.get('entry')}, SL {plan.get('sl')}, "
        f"T1 {plan.get('t1')} (RR {plan.get('rr_t1')}), T2 {plan.get('t2')} "
        f"(RR {plan.get('rr_t2')}).",
    ]
    if prem:
        lines.append(f"Option premium plan: entry ~{prem.get('entry')}, "
                     f"SL {prem.get('sl')}, T1 {prem.get('t1')}, T2 {prem.get('t2')} "
                     f"(delta {prem.get('delta_used')}).")
    lines.append("Risks: heuristic v1 signal, not backtested; theta/gamma ignored in "
                 "premium translation; check event calendar before entry. "
                 "Set ANTHROPIC_API_KEY in .env for a fuller Claude-written rationale.")
    return "\n".join(lines)
