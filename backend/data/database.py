"""SQLite storage: instruments (from scrip master) + 1-minute candles.

SQLite is fine for one user. check_same_thread=False + a lock keeps it safe
across FastAPI worker threads.
"""
from __future__ import annotations

import sqlite3
import threading
from backend.config import DATA_DIR

DB_PATH = DATA_DIR / "dhakad.db"

_conn = sqlite3.connect(DB_PATH, check_same_thread=False)
_conn.row_factory = sqlite3.Row
try:
    _conn.execute("PRAGMA journal_mode=WAL")
except sqlite3.OperationalError:
    pass  # WAL unsupported on some filesystems; default journal is fine
_lock = threading.Lock()

_conn.executescript(
    """
    CREATE TABLE IF NOT EXISTS instruments (
        exchange_segment TEXT NOT NULL,      -- pExchSeg e.g. nse_cm
        token TEXT NOT NULL,                 -- pSymbol (instrument token)
        trading_symbol TEXT NOT NULL,        -- pTrdSymbol e.g. RELIANCE-EQ
        name TEXT,                           -- description / company name
        instrument_type TEXT,                -- EQ / INDEX / FUT / OPT ...
        symbol_root TEXT,                    -- e.g. RELIANCE, NIFTY
        lot_size INTEGER DEFAULT 1,
        tick_size REAL,
        expiry INTEGER,                      -- epoch (F&O) else NULL
        strike REAL,                         -- option strike else NULL
        option_type TEXT,                    -- CE / PE else NULL
        PRIMARY KEY (exchange_segment, token)
    );
    CREATE INDEX IF NOT EXISTS idx_instr_tsym ON instruments (trading_symbol);
    CREATE INDEX IF NOT EXISTS idx_instr_root ON instruments (symbol_root);

    CREATE TABLE IF NOT EXISTS candles (
        exchange_segment TEXT NOT NULL,
        token TEXT NOT NULL,
        ts INTEGER NOT NULL,                 -- minute-start epoch seconds
        open REAL, high REAL, low REAL, close REAL,
        volume REAL DEFAULT 0,
        PRIMARY KEY (exchange_segment, token, ts)
    );

    CREATE TABLE IF NOT EXISTS daily_candles (
        exchange_segment TEXT NOT NULL,
        token TEXT NOT NULL,
        ts INTEGER NOT NULL,                 -- session-start epoch seconds (that day, IST)
        open REAL, high REAL, low REAL, close REAL,
        volume REAL DEFAULT 0,
        PRIMARY KEY (exchange_segment, token, ts)
    );

    CREATE TABLE IF NOT EXISTS meta (
        key TEXT PRIMARY KEY,
        value TEXT
    );
    """
)
_conn.commit()


def execute(sql: str, params: tuple = ()) -> None:
    with _lock:
        _conn.execute(sql, params)
        _conn.commit()


def executemany(sql: str, rows: list[tuple]) -> None:
    with _lock:
        _conn.executemany(sql, rows)
        _conn.commit()


def query(sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    with _lock:
        return _conn.execute(sql, params).fetchall()


def get_meta(key: str) -> str | None:
    rows = query("SELECT value FROM meta WHERE key=?", (key,))
    return rows[0]["value"] if rows else None


def set_meta(key: str, value: str) -> None:
    execute("INSERT INTO meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def upsert_candle(seg: str, token: str, ts: int, o: float, h: float, l: float,
                  c: float, v: float = 0) -> None:
    execute(
        """INSERT INTO candles (exchange_segment, token, ts, open, high, low, close, volume)
           VALUES (?,?,?,?,?,?,?,?)
           ON CONFLICT(exchange_segment, token, ts) DO UPDATE SET
             high=MAX(high, excluded.high),
             low=MIN(low, excluded.low),
             close=excluded.close,
             volume=excluded.volume""",
        (seg, token, ts, o, h, l, c, v),
    )


def get_candles(seg: str, token: str, limit: int = 500) -> list[dict]:
    rows = query(
        """SELECT ts, open, high, low, close, volume FROM candles
           WHERE exchange_segment=? AND token=?
           ORDER BY ts DESC LIMIT ?""",
        (seg, token, limit),
    )
    return [dict(r) for r in reversed(rows)]


def upsert_daily_candle(seg: str, token: str, ts: int, o: float, h: float, l: float,
                        c: float, v: float = 0) -> None:
    execute(
        """INSERT INTO daily_candles (exchange_segment, token, ts, open, high, low, close, volume)
           VALUES (?,?,?,?,?,?,?,?)
           ON CONFLICT(exchange_segment, token, ts) DO UPDATE SET
             open=excluded.open, high=excluded.high, low=excluded.low,
             close=excluded.close, volume=excluded.volume""",
        (seg, token, ts, o, h, l, c, v),
    )


def get_daily_candles(seg: str, token: str, limit: int = 300,
                      before_ts: int | None = None) -> list[dict]:
    if before_ts is not None:
        rows = query(
            """SELECT ts, open, high, low, close, volume FROM daily_candles
               WHERE exchange_segment=? AND token=? AND ts < ?
               ORDER BY ts DESC LIMIT ?""",
            (seg, token, before_ts, limit),
        )
    else:
        rows = query(
            """SELECT ts, open, high, low, close, volume FROM daily_candles
               WHERE exchange_segment=? AND token=?
               ORDER BY ts DESC LIMIT ?""",
            (seg, token, limit),
        )
    return [dict(r) for r in reversed(rows)]


def get_merged_candles(seg: str, token: str, limit: int = 500) -> list[dict]:
    """Backfilled daily history (all prior sessions) + today's 1-minute candles.

    Lets the analysis engine run with a full indicator lookback (EMA200 etc.)
    from the moment a symbol is first opened, instead of waiting ~200 minutes
    for the live collector to build up enough 1-minute bars.
    """
    today_start = _today_start_ts()
    minute_rows = query(
        """SELECT ts, open, high, low, close, volume FROM candles
           WHERE exchange_segment=? AND token=? AND ts >= ?
           ORDER BY ts ASC""",
        (seg, token, today_start),
    )
    n_daily = max(limit - len(minute_rows), 0)
    daily_rows = get_daily_candles(seg, token, n_daily, before_ts=today_start) if n_daily else []
    merged = daily_rows + [dict(r) for r in minute_rows]
    return merged[-limit:]


def _today_start_ts() -> int:
    from datetime import datetime
    from zoneinfo import ZoneInfo
    now = datetime.now(ZoneInfo("Asia/Kolkata"))
    return int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())


def search_instruments(text: str, limit: int = 25) -> list[dict]:
    """Search by trading symbol / name / root. Equity & index first."""
    like = f"%{text.upper()}%"
    rows = query(
        """SELECT * FROM instruments
           WHERE trading_symbol LIKE ? OR name LIKE ? OR symbol_root LIKE ?
           ORDER BY
             CASE WHEN instrument_type IN ('EQ','INDEX') THEN 0 ELSE 1 END,
             LENGTH(trading_symbol)
           LIMIT ?""",
        (like, like, like, limit),
    )
    return [dict(r) for r in rows]


def get_instrument(seg: str, token: str) -> dict | None:
    rows = query("SELECT * FROM instruments WHERE exchange_segment=? AND token=?",
                 (seg, token))
    return dict(rows[0]) if rows else None
