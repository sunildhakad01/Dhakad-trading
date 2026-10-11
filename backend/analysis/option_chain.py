"""Option chain builder — strikes from scrip_master + live quotes.

Strikes/expiries come from the `instruments` table (already indexed by
scrip_master from the Kotak scrip master CSVs — no extra API calls needed).
Only OI/IV/LTP come from a live `client.quotes()` call per expiry.

CAUTION: this repo has never seen a live Kotak option-leg quote payload, so
the exact JSON key for open interest / IV isn't confirmed anywhere else in
the codebase. We probe several plausible spellings defensively (same pattern
as scrip_master._pick). If none resolve on your account, the response comes
back with `oi_field_resolved: false` and a `note` explaining it — open a
quote for one option leg, find the real key, and add it to OI_KEYS/IV_KEYS.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from backend.data import database as db
from backend.kotak.client import client

IST = ZoneInfo("Asia/Kolkata")

OI_KEYS = ("oi", "OI", "openInterest", "iOpnInt", "open_interest")
CHG_OI_KEYS = ("oi_chg", "oiChange", "changeOI", "chng_oi", "change_in_oi", "OIChange")
IV_KEYS = ("iv", "impliedVolatility", "iv_percent", "IV")

MAX_QUOTE_BATCH = 40  # matches collector.MAX_SUBSCRIPTIONS — one REST call per chunk


def _pick_num(d: dict, keys: tuple[str, ...]) -> float | None:
    for k in keys:
        v = d.get(k)
        if v not in (None, "", "-1"):
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    return None


def _prev_close(q: dict) -> float | None:
    ohlc = q.get("ohlc") or {}
    v = ohlc.get("close")
    if v in (None, ""):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------ expiries / strikes
def expiries(root: str, seg: str = "nse_fo") -> list[int]:
    rows = db.query(
        """SELECT DISTINCT expiry FROM instruments
           WHERE exchange_segment=? AND symbol_root=? AND instrument_type='OPT'
             AND expiry IS NOT NULL ORDER BY expiry ASC""",
        (seg, root.upper()))
    return [r["expiry"] for r in rows]


def nearest_expiry(root: str, seg: str = "nse_fo") -> int | None:
    today = datetime.now(IST).replace(hour=0, minute=0, second=0, microsecond=0)
    today_epoch = int(today.timestamp())
    for e in expiries(root, seg):
        if e >= today_epoch:
            return e
    return None


def _strikes_for_expiry(root: str, expiry: int, seg: str) -> list[dict]:
    rows = db.query(
        """SELECT token, trading_symbol, strike, option_type FROM instruments
           WHERE exchange_segment=? AND symbol_root=? AND instrument_type='OPT'
             AND expiry=? AND option_type IN ('CE','PE') ORDER BY strike ASC""",
        (seg, root.upper(), expiry))
    return [dict(r) for r in rows]


def _underlying_ltp(root: str) -> float | None:
    """Best-effort spot price for skew/ATM — checks nse_cm then bse_cm."""
    for seg in ("nse_cm", "bse_cm"):
        rows = db.query(
            """SELECT token FROM instruments WHERE exchange_segment=? AND symbol_root=?
               AND instrument_type IN ('EQ','INDEX') LIMIT 1""",
            (seg, root.upper()))
        if not rows:
            continue
        try:
            data = client.quotes([f"{seg}|{rows[0]['token']}"], "ltp")
            if data:
                return float(data[0].get("ltp") or 0) or None
        except Exception:
            return None
    return None


# ------------------------------------------------------------------ buildup classification
def _classify_buildup(price_chg: float | None, oi_chg: float | None) -> str | None:
    if price_chg is None or oi_chg is None:
        return None
    if price_chg > 0 and oi_chg > 0:
        return "Long Buildup"
    if price_chg > 0 and oi_chg < 0:
        return "Short Covering"
    if price_chg < 0 and oi_chg > 0:
        return "Short Buildup"
    if price_chg < 0 and oi_chg < 0:
        return "Long Unwinding"
    return "Neutral"


def _max_pain(rows: list[dict]) -> float | None:
    """Strike minimizing total option-writer payout at expiry."""
    strikes = [r["strike"] for r in rows]
    if not strikes:
        return None
    best_strike, best_pain = None, None
    for s in strikes:
        pain = 0.0
        for r in rows:
            k = r["strike"]
            pain += (r.get("ce_oi") or 0) * max(s - k, 0)
            pain += (r.get("pe_oi") or 0) * max(k - s, 0)
        if best_pain is None or pain < best_pain:
            best_pain, best_strike = pain, s
    return best_strike


# ------------------------------------------------------------------ main entry point
def build(root: str, expiry: int | None = None, seg: str = "nse_fo") -> dict:
    exp = expiry or nearest_expiry(root, seg)
    if exp is None:
        return {"error": f"No option expiries found for {root} on {seg}. "
                         "Has the scrip master loaded F&O instruments yet?"}

    legs = _strikes_for_expiry(root, exp, seg)
    if not legs:
        return {"error": f"No strikes found for {root} expiry {exp}"}

    quotes_by_token: dict[str, dict] = {}
    tokens = [leg["token"] for leg in legs]
    for i in range(0, len(tokens), MAX_QUOTE_BATCH):
        chunk = [f"{seg}|{t}" for t in tokens[i:i + MAX_QUOTE_BATCH]]
        try:
            for item in client.quotes(chunk, "all"):
                quotes_by_token[str(item.get("exchange_token", ""))] = item
        except Exception as e:
            return {"error": f"Quote fetch failed: {e}"}

    strikes: dict[float, dict] = {}
    oi_resolved = False
    for leg in legs:
        q = quotes_by_token.get(leg["token"])
        if not q:
            continue
        strike, side = leg["strike"], leg["option_type"].lower()
        row = strikes.setdefault(strike, {"strike": strike})

        ltp = _pick_num(q, ("ltp",)) or 0.0
        oi = _pick_num(q, OI_KEYS)
        oi_chg = _pick_num(q, CHG_OI_KEYS)
        iv = _pick_num(q, IV_KEYS)
        prev_close = _prev_close(q)
        price_chg = (ltp - prev_close) if (prev_close is not None and ltp) else None
        if oi is not None:
            oi_resolved = True

        row[f"{side}_tsym"] = leg.get("trading_symbol")
        row[f"{side}_token"] = leg["token"]
        row[f"{side}_ltp"] = round(ltp, 2)
        row[f"{side}_oi"] = oi
        row[f"{side}_oi_chg"] = oi_chg
        row[f"{side}_iv"] = iv
        row[f"{side}_chg"] = round(price_chg, 2) if price_chg is not None else None
        row[f"{side}_buildup"] = _classify_buildup(price_chg, oi_chg)

    ordered = [strikes[s] for s in sorted(strikes)]
    total_ce_oi = sum(r.get("ce_oi") or 0 for r in ordered)
    total_pe_oi = sum(r.get("pe_oi") or 0 for r in ordered)
    pcr = round(total_pe_oi / total_ce_oi, 3) if total_ce_oi else None
    max_pain = _max_pain(ordered) if oi_resolved else None

    spot = _underlying_ltp(root)
    iv_skew = None
    if spot:
        atm = min(ordered, key=lambda r: abs(r["strike"] - spot))
        if atm.get("pe_iv") is not None and atm.get("ce_iv") is not None:
            iv_skew = round(atm["pe_iv"] - atm["ce_iv"], 2)

    return {
        "root": root.upper(),
        "expiry": exp,
        "spot": spot,
        "strikes": ordered,
        "pcr": pcr,
        "max_pain": max_pain,
        "iv_skew_atm": iv_skew,
        "total_ce_oi": total_ce_oi or None,
        "total_pe_oi": total_pe_oi or None,
        "oi_field_resolved": oi_resolved,
        "note": None if oi_resolved else (
            "OI field wasn't found in the quote payload using the key names this "
            "build tries (oi/OI/openInterest/iOpnInt/open_interest). PCR, Max Pain "
            "and OI buildup are unavailable until the correct key is confirmed — "
            "see backend/analysis/option_chain.py (OI_KEYS)."),
    }
