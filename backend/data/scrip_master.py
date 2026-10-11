"""Download the daily Kotak scrip master CSVs and index them into SQLite.

Column names in the CSVs (per Kotak docs): pSymbol (token), pExchSeg, pTrdSymbol,
lLotSize, lExpiryDate, plus descriptive columns whose exact names vary by segment.
We read defensively: unknown/renamed columns won't crash the import.
"""
from __future__ import annotations

import csv
import io
import logging
from datetime import date

import httpx

from backend.data import database as db
from backend.kotak.client import client

log = logging.getLogger("scrip_master")

# v1 cash-market files are preferred (transformed-v1); keep fo files as-is
SEGMENTS_WANTED = ("nse_cm", "bse_cm", "nse_fo", "bse_fo", "cde_fo", "mcx_fo")


def _pick(row: dict, *names: str) -> str:
    for n in names:
        v = row.get(n)
        if v not in (None, "", "-1", "0.000000"):
            return str(v).strip()
    return ""


def refresh_if_stale() -> None:
    today = date.today().isoformat()
    if db.get_meta("scrip_master_date") == today:
        log.info("Scrip master already loaded for %s", today)
        return
    refresh()
    db.set_meta("scrip_master_date", today)


def refresh() -> None:
    paths = client.scrip_master_paths()
    total = 0
    for url in paths:
        seg = _segment_from_url(url)
        if seg not in SEGMENTS_WANTED:
            continue
        try:
            total += _load_csv(url, seg)
        except Exception as e:
            log.warning("Failed loading %s: %s", url.rsplit('/', 1)[-1], e)
    log.info("Scrip master refresh done: %d instruments", total)


def _segment_from_url(url: str) -> str:
    fname = url.rsplit("/", 1)[-1].lower()
    for seg in SEGMENTS_WANTED:
        if fname.startswith(seg):
            return seg
    return ""


def _load_csv(url: str, seg: str) -> int:
    log.info("Downloading %s ...", url.rsplit('/', 1)[-1])
    with httpx.Client(timeout=120) as http:
        resp = http.get(url)
        resp.raise_for_status()
        text = resp.text

    reader = csv.DictReader(io.StringIO(text))
    rows: list[tuple] = []
    for row in reader:
        token = _pick(row, "pSymbol", "pSymbolName" if False else "pSymbol")
        tsym = _pick(row, "pTrdSymbol")
        if not token or not tsym:
            continue
        name = _pick(row, "pDesc", "pCompanyName", "pSymbolName", "pInstName")
        itype = _pick(row, "pInstType", "pOptionType") or _infer_type(seg, row)
        root = _pick(row, "pSymbolName", "pGroup") or tsym.split("-")[0]
        lot = _pick(row, "lLotSize", "iLotSize") or "1"
        tick = _pick(row, "dTickSize", "lTickSize")
        expiry = _pick(row, "lExpiryDate")
        strike = _pick(row, "dStrikePrice;", "dStrikePrice", "lStrikePrice")
        opt = _pick(row, "pOptionType")
        try:
            lot_i = int(float(lot))
        except ValueError:
            lot_i = 1
        rows.append((
            seg, token, tsym, name, itype, root.upper(), lot_i,
            _f(tick), _i(expiry), _f(strike), opt if opt in ("CE", "PE") else None,
        ))

    db.executemany(
        """INSERT INTO instruments
           (exchange_segment, token, trading_symbol, name, instrument_type,
            symbol_root, lot_size, tick_size, expiry, strike, option_type)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(exchange_segment, token) DO UPDATE SET
             trading_symbol=excluded.trading_symbol,
             name=excluded.name,
             instrument_type=excluded.instrument_type,
             symbol_root=excluded.symbol_root,
             lot_size=excluded.lot_size""",
        rows,
    )
    log.info("Loaded %d instruments for %s", len(rows), seg)
    return len(rows)


def _infer_type(seg: str, row: dict) -> str:
    if seg.endswith("_cm"):
        return "EQ"
    if _pick(row, "pOptionType") in ("CE", "PE"):
        return "OPT"
    return "FUT"


def _f(v: str) -> float | None:
    try:
        return float(v) if v else None
    except ValueError:
        return None


def _i(v: str) -> int | None:
    try:
        return int(float(v)) if v else None
    except ValueError:
        return None


# Common indices (quotes API needs the exact case-sensitive index name)
INDICES = [
    {"exchange_segment": "nse_cm", "token": "Nifty 50", "trading_symbol": "NIFTY 50",
     "name": "Nifty 50 Index", "instrument_type": "INDEX", "symbol_root": "NIFTY",
     "lot_size": 1},
    {"exchange_segment": "nse_cm", "token": "Nifty Bank", "trading_symbol": "NIFTY BANK",
     "name": "Bank Nifty Index", "instrument_type": "INDEX", "symbol_root": "BANKNIFTY",
     "lot_size": 1},
    {"exchange_segment": "nse_cm", "token": "Nifty Fin Service", "trading_symbol": "FINNIFTY",
     "name": "Nifty Financial Services", "instrument_type": "INDEX", "symbol_root": "FINNIFTY",
     "lot_size": 1},
    {"exchange_segment": "nse_cm", "token": "India VIX", "trading_symbol": "INDIA VIX",
     "name": "India VIX", "instrument_type": "INDEX", "symbol_root": "INDIAVIX",
     "lot_size": 1},
    {"exchange_segment": "bse_cm", "token": "SENSEX", "trading_symbol": "SENSEX",
     "name": "BSE Sensex", "instrument_type": "INDEX", "symbol_root": "SENSEX",
     "lot_size": 1},
]


def ensure_indices() -> None:
    rows = [(
        d["exchange_segment"], d["token"], d["trading_symbol"], d["name"],
        d["instrument_type"], d["symbol_root"], d["lot_size"], None, None, None, None,
    ) for d in INDICES]
    db.executemany(
        """INSERT OR IGNORE INTO instruments
           (exchange_segment, token, trading_symbol, name, instrument_type,
            symbol_root, lot_size, tick_size, expiry, strike, option_type)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        rows,
    )
