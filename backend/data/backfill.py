"""Historical backfill so indicators have real lookback on day 1 instead of
waiting ~200 minutes for the live collector to accumulate bars.

- Indices: yfinance daily history (NSE doesn't bhavcopy indices in the equity file).
- Equities: NSE's daily "full bhavdata" CSV — one file covers EVERY NSE equity for
  that session, so N days of history costs N HTTP requests total, not N-per-symbol.

Both write into the `daily_candles` table (see database.py), kept separate from
the 1-minute `candles` table the live collector fills. `database.get_merged_candles`
stitches backfilled daily history + today's live minutes into one series for the
analysis engine.

Runs once per calendar day (gated via the `meta` table), on a background thread,
so app startup isn't blocked by dozens of HTTP round-trips.
"""
from __future__ import annotations

import csv
import io
import logging
import threading
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from backend.data import database as db
from backend.data.scrip_master import INDICES

log = logging.getLogger("backfill")
IST = ZoneInfo("Asia/Kolkata")

# yfinance tickers for the indices we track (see scrip_master.INDICES).
# Not every index has a reliable Yahoo symbol — missing ones are skipped.
INDEX_YF_TICKERS = {
    "NIFTY": "^NSEI",
    "BANKNIFTY": "^NSEBANK",
    "INDIAVIX": "^INDIAVIX",
    "SENSEX": "^BSESN",
}

BHAVCOPY_LOOKBACK_DAYS = 60   # trading days of equity daily history to backfill
INDEX_LOOKBACK_DAYS = 400     # calendar days of index daily history (covers EMA200)

_NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "text/csv,application/json,text/plain,*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/all-reports",
}


def _session_start_ts(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, 9, 15, tzinfo=IST).timestamp())


# ------------------------------------------------------------------ indices
def backfill_indices() -> int:
    try:
        import yfinance as yf
    except ImportError:
        log.warning("yfinance not installed — skipping index backfill "
                    "(pip install -r requirements.txt)")
        return 0

    total = 0
    for entry in INDICES:
        root = entry["symbol_root"]
        ticker = INDEX_YF_TICKERS.get(root)
        if not ticker:
            continue
        try:
            hist = yf.Ticker(ticker).history(
                period=f"{INDEX_LOOKBACK_DAYS}d", interval="1d", auto_adjust=False)
        except Exception as e:
            log.warning("yfinance fetch failed for %s (%s): %s", root, ticker, e)
            continue
        if hist is None or hist.empty:
            continue

        rows = 0
        for idx, row in hist.iterrows():
            d = idx.date() if hasattr(idx, "date") else idx
            try:
                o, h, l, c = float(row["Open"]), float(row["High"]), float(row["Low"]), float(row["Close"])
            except (TypeError, ValueError, KeyError):
                continue
            if o <= 0 or h <= 0 or l <= 0 or c <= 0:
                continue
            vol = float(row.get("Volume") or 0)
            db.upsert_daily_candle(entry["exchange_segment"], entry["token"],
                                   _session_start_ts(d), o, h, l, c, vol)
            rows += 1
        log.info("Backfilled %d daily bars for index %s (%s)", rows, root, ticker)
        total += rows
    return total


# ------------------------------------------------------------------ equities
def _bhavcopy_url(d: date) -> str:
    return (f"https://nsearchives.nseindia.com/products/content/"
            f"sec_bhavdata_full_{d.strftime('%d%m%Y')}.csv")


def _fetch_bhavcopy(http: httpx.Client, d: date) -> str | None:
    try:
        resp = http.get(_bhavcopy_url(d), headers=_NSE_HEADERS, timeout=30)
    except httpx.HTTPError as e:
        log.debug("Bhavcopy fetch error %s: %s", d, e)
        return None
    if resp.status_code != 200 or not resp.text.strip():
        return None  # holiday / weekend / no file that day
    return resp.text


def _instrument_token_map() -> dict[str, str]:
    rows = db.query(
        "SELECT trading_symbol, token FROM instruments "
        "WHERE exchange_segment='nse_cm' AND instrument_type='EQ'")
    return {r["trading_symbol"]: r["token"] for r in rows}


def backfill_equities(lookback_days: int = BHAVCOPY_LOOKBACK_DAYS) -> int:
    tok_by_symbol = _instrument_token_map()
    if not tok_by_symbol:
        log.warning("No equity instruments loaded yet — skipping bhavcopy backfill")
        return 0

    total_rows = 0
    fetched_days = 0
    d = date.today() - timedelta(days=1)  # NSE publishes after market close
    cutoff = date.today() - timedelta(days=lookback_days * 2)  # generous calendar bound

    with httpx.Client() as http:
        # warm up cookies — NSE's edge sometimes 403s a cold request
        try:
            http.get("https://www.nseindia.com/all-reports", headers=_NSE_HEADERS, timeout=15)
        except httpx.HTTPError:
            pass

        while fetched_days < lookback_days and d >= cutoff:
            if d.weekday() < 5:  # skip weekends outright, save a request
                text = _fetch_bhavcopy(http, d)
                if text:
                    total_rows += _load_bhavcopy_csv(text, tok_by_symbol, d)
                    fetched_days += 1
                time.sleep(0.3)  # be polite to NSE's servers
            d -= timedelta(days=1)

    log.info("Equity backfill: %d sessions, %d bars", fetched_days, total_rows)
    return total_rows


def _load_bhavcopy_csv(text: str, tok_by_symbol: dict[str, str], d: date) -> int:
    ts = _session_start_ts(d)
    reader = csv.DictReader(io.StringIO(text))
    rows = 0
    for row in reader:
        symbol = (row.get("SYMBOL") or "").strip()
        series = (row.get("SERIES") or "").strip()
        if series != "EQ" or not symbol:
            continue
        token = tok_by_symbol.get(f"{symbol}-EQ")
        if not token:
            continue
        try:
            o = float(row["OPEN_PRICE"])
            h = float(row["HIGH_PRICE"])
            l = float(row["LOW_PRICE"])
            c = float(row["CLOSE_PRICE"])
            v = float(row.get("TTL_TRD_QNTY") or 0)
        except (KeyError, ValueError):
            continue
        if o <= 0 or h <= 0 or l <= 0 or c <= 0:
            continue
        db.upsert_daily_candle("nse_cm", token, ts, o, h, l, c, v)
        rows += 1
    return rows


# ------------------------------------------------------------------ orchestration
def run_backfill() -> None:
    try:
        n_idx = backfill_indices()
    except Exception as e:
        log.warning("Index backfill failed: %s", e)
        n_idx = 0
    try:
        n_eq = backfill_equities()
    except Exception as e:
        log.warning("Equity backfill failed: %s", e)
        n_eq = 0
    log.info("Historical backfill complete: %d index bars, %d equity bars", n_idx, n_eq)


def ensure_backfill_if_stale() -> None:
    today = date.today().isoformat()
    if db.get_meta("backfill_date") == today:
        log.info("Historical backfill already done for %s", today)
        return

    def _worker():
        run_backfill()
        db.set_meta("backfill_date", today)

    threading.Thread(target=_worker, daemon=True, name="backfill").start()
