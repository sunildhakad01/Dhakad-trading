"""Dhakad Pro Trading — FastAPI backend.

Run:  python -m backend.main
Open: http://localhost:8000
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from backend.config import FRONTEND_DIR, settings
from backend.data import backfill
from backend.data import database as db
from backend.data import scrip_master
from backend.data.collector import collector, market_open
from backend.analysis import (backtest, breadth, confluence, indicators,
                              levels, news, reasoning, signals)
from backend.analysis import option_chain as oc
from backend.kotak.client import client

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Pure-DB seed of the common indices: no network, no login. Kept outside the
    # login branch so search works even when credentials are missing/login fails.
    scrip_master.ensure_indices()

    missing = settings.validate()
    if missing:
        log.error("Missing credentials in .env: %s — copy .env.example to .env "
                  "and fill it in.", ", ".join(missing))
    else:
        try:
            client.ensure_login()
            scrip_master.refresh_if_stale()
            collector.start()
            backfill.ensure_backfill_if_stale()  # background thread, non-blocking
        except Exception as e:
            log.error("Startup init failed: %s", e)
    yield
    collector.stop()


app = FastAPI(title="Dhakad Pro Trading", lifespan=lifespan)


# ------------------------------------------------------------------ API
@app.get("/api/status")
def status():
    s = client.session
    return {
        "logged_in": bool(s and s.active),
        "market_open": market_open(),
        "live_trading": settings.live_trading,   # should be false!
        "instruments": db.query("SELECT COUNT(*) c FROM instruments")[0]["c"],
        "daily_bars_backfilled": db.query("SELECT COUNT(*) c FROM daily_candles")[0]["c"],
    }


@app.get("/api/search")
def search(q: str):
    if len(q) < 2:
        return []
    return db.search_instruments(q)


TIMEFRAME_MINUTES = {"1m": 1, "5m": 5, "15m": 15}


@app.get("/api/candles")
def candles(seg: str, token: str, limit: int = 500, timeframe: str = "1m"):
    collector.subscribe(seg, token)          # start collecting for this symbol
    minutes = TIMEFRAME_MINUTES.get(timeframe)
    if minutes is None:
        raise HTTPException(400, f"timeframe must be one of {list(TIMEFRAME_MINUTES)}")
    # pull more raw 1m bars so the aggregated result still has `limit` bars
    raw = db.get_merged_candles(seg, token, limit * minutes)
    return indicators.resample(raw, minutes)[-limit:]


@app.get("/api/quote")
def quote(seg: str, token: str):
    live = collector.latest(seg, token)
    if live:
        return live
    try:
        data = client.quotes([f"{seg}|{token}"], "all")
        return data[0] if data else {}
    except Exception as e:
        raise HTTPException(502, f"Quote fetch failed: {e}")


def _compute_analysis(seg: str, token: str, timeframe: str) -> tuple[dict, list[dict]]:
    minutes = TIMEFRAME_MINUTES.get(timeframe)
    if minutes is None:
        raise HTTPException(400, f"timeframe must be one of {list(TIMEFRAME_MINUTES)}")
    raw = db.get_merged_candles(seg, token, 500 * minutes)
    cndl = indicators.resample(raw, minutes)[-500:]
    prev_day = _prev_day_ohlc(seg, token)
    result = indicators.analyze(cndl, prev_day)
    if "error" in result:
        return result, cndl

    if result["bias"] in ("BULLISH", "BEARISH"):
        direction = "LONG" if result["bias"] == "BULLISH" else "SHORT"
        lvl = result["values"].get("cpr") or {}
        result["trade_plan"] = levels.plan_trade(cndl, direction, lvl)
    else:
        result["trade_plan"] = {"note": "No trade — confluence score too weak "
                                        f"({result['score']}). Wait for alignment."}
    return result, cndl


@app.get("/api/analysis")
def analysis(seg: str, token: str, timeframe: str = "1m"):
    result, _cndl = _compute_analysis(seg, token, timeframe)
    return result


def _confluence_bundle(seg: str, token: str, timeframe: str, use_claude: bool):
    """Shared by /api/confluence and /api/signal: computes indicator analysis,
    option chain, breadth, news → composite. Returns (payload, chain, instrument)."""
    result, cndl = _compute_analysis(seg, token, timeframe)
    if "error" in result:
        return result, None, None

    votes = dict(result["votes"])
    modules: dict = {"indicators": result["votes"]}

    instrument = db.get_instrument(seg, token)
    root = instrument["symbol_root"] if instrument else None
    chain: dict | None = None

    if root:
        try:
            exp = oc.nearest_expiry(root)
            if exp is not None:
                chain = oc.build(root, exp)
                if "error" not in chain and chain.get("oi_field_resolved"):
                    votes["pcr"] = confluence.pcr_vote(chain.get("pcr"))
                    modules["option_chain"] = {"pcr": chain.get("pcr"),
                                               "max_pain": chain.get("max_pain"),
                                               "expiry": chain.get("expiry")}
        except Exception as e:
            log.warning("Confluence: option chain lookup failed for %s: %s", root, e)

        idxs = breadth.indices_containing(root) or (
            [root] if root in breadth.available_indices() else [])
        if idxs:
            b = breadth.compute(idxs[0])
            if "error" not in b:
                votes["breadth"] = confluence.breadth_vote(b.get("bias"))
                modules["breadth"] = {"index": idxs[0], "score": b.get("breadth_score"),
                                      "advances": b.get("advances"), "declines": b.get("declines")}

        try:
            items = news.fetch(root, limit=15, use_claude=use_claude)
            if items:
                nb = news.news_bias(items)
                votes["news"] = nb["vote"]
                modules["news"] = {**nb, "headlines": items[:5]}
        except Exception as e:
            log.warning("Confluence: news lookup failed for %s: %s", root, e)

    composite = confluence.combine(votes)
    payload = {"root": root, "composite": composite, "modules": modules,
               "price_indicators": result}
    return payload, chain, instrument


@app.get("/api/confluence")
def confluence_score(seg: str, token: str, timeframe: str = "1m", use_claude: bool = False):
    """Combines price-indicator votes with option-chain PCR, driver-stock
    breadth, and news sentiment — the modules from Phase 3. Calls out to
    Kotak quotes (option chain, breadth) and RSS feeds, so this is meant to
    be triggered on demand, not polled every few seconds like /api/analysis."""
    payload, _chain, _instr = _confluence_bundle(seg, token, timeframe, use_claude)
    return payload


@app.get("/api/signal")
def signal(seg: str, token: str, timeframe: str = "5m", use_claude: bool = False):
    """Phase 4: full option trade signal. Confluence gate -> strategy by IV
    regime -> delta-translated premium SL/targets -> plain-English rationale."""
    payload, chain, instrument = _confluence_bundle(seg, token, timeframe, use_claude)
    if isinstance(payload, dict) and "error" in payload:
        return payload

    spot_plan = payload["price_indicators"].get("trade_plan") or {}
    lot = 1
    if chain and instrument:
        rows = db.query(
            """SELECT lot_size FROM instruments WHERE symbol_root=? AND
               instrument_type='OPT' LIMIT 1""", (instrument["symbol_root"],))
        if rows:
            lot = rows[0]["lot_size"] or 1
    sig = signals.generate(payload["composite"], spot_plan, chain or {}, lot)
    sig["confluence"] = payload
    sig["reasoning"] = reasoning.explain(
        {k: v for k, v in sig.items() if k != "confluence"} |
        {"modules": payload["modules"]})
    return sig


@app.get("/api/backtest")
def backtest_endpoint(seg: str, token: str, timeframe: str = "1m",
                      mode: str = "walk_forward", threshold: int = 3):
    """Phase 5: honest backtest of the indicator-confluence strategy on this
    symbol's stored candles. mode=walk_forward (default) or single."""
    minutes = TIMEFRAME_MINUTES.get(timeframe)
    if minutes is None:
        raise HTTPException(400, f"timeframe must be one of {list(TIMEFRAME_MINUTES)}")
    raw = db.get_merged_candles(seg, token, 100_000)
    cndl = indicators.resample(raw, minutes) if minutes > 1 else raw
    if mode == "single":
        r = backtest.run(cndl, threshold=threshold)
        r.pop("_all_trades", None)
        return r
    return backtest.walk_forward(cndl)


@app.get("/api/option-chain")
def option_chain(root: str, expiry: int | None = None, seg: str = "nse_fo"):
    return oc.build(root, expiry, seg)


@app.get("/api/option-chain/expiries")
def option_chain_expiries(root: str, seg: str = "nse_fo"):
    return oc.expiries(root, seg)


@app.get("/api/breadth")
def breadth_score(index: str):
    return breadth.compute(index)


@app.get("/api/news")
def news_headlines(symbol: str | None = None, limit: int = 20):
    return news.fetch(symbol, limit)


@app.get("/api/portfolio")
def portfolio():
    try:
        return {
            "positions": client.positions(),
            "holdings": client.holdings(),
        }
    except Exception as e:
        raise HTTPException(502, str(e))


def _prev_day_ohlc(seg: str, token: str) -> dict | None:
    """Previous day OHLC from the quotes API (ohlc.close = prev close)."""
    try:
        data = client.quotes([f"{seg}|{token}"], "ohlc")
        if data and data[0].get("ohlc"):
            o = data[0]["ohlc"]
            return {"high": float(o["high"]), "low": float(o["low"]),
                    "close": float(o["close"])}
    except Exception:
        pass
    return None


# ------------------------------------------------------------------ frontend
app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")


@app.get("/")
def index():
    return FileResponse(FRONTEND_DIR / "index.html")


if __name__ == "__main__":
    uvicorn.run("backend.main:app", host=settings.host, port=settings.port,
                reload=False)
