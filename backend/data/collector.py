"""Live data collector: polls the Quotes API for subscribed instruments and
builds 1-minute candles into SQLite.

v1 uses REST polling (~1 poll every 3s for all subscribed symbols in ONE call,
well inside the 10 req/s limit). The Kotak HSM websocket can replace this later
for tick-level data; the candle storage stays the same.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

from backend.data import database as db
from backend.kotak.client import client

log = logging.getLogger("collector")
IST = ZoneInfo("Asia/Kolkata")

MARKET_OPEN = dtime(9, 15)
MARKET_CLOSE = dtime(15, 30)
POLL_SECONDS = 3.0
MAX_SUBSCRIPTIONS = 40  # one REST call handles all of these comma-separated


def market_open(now: datetime | None = None) -> bool:
    now = now or datetime.now(IST)
    return now.weekday() < 5 and MARKET_OPEN <= now.time() <= MARKET_CLOSE


class Collector:
    """Background thread. subscribe('nse_cm','11536') -> candles appear in DB."""

    def __init__(self) -> None:
        self._subs: dict[str, tuple[str, str]] = {}   # "seg|token" -> (seg, token)
        self._live: dict[str, dict] = {}              # latest quote per key
        self._working: dict[str, dict] = {}           # in-progress minute candle per key
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # ---------------------------------------------------------- public
    def subscribe(self, seg: str, token: str) -> None:
        key = f"{seg}|{token}"
        with self._lock:
            if key not in self._subs and len(self._subs) >= MAX_SUBSCRIPTIONS:
                # drop the oldest subscription
                oldest = next(iter(self._subs))
                self._subs.pop(oldest, None)
            self._subs[key] = (seg, token)

    def latest(self, seg: str, token: str) -> dict | None:
        return self._live.get(f"{seg}|{token}")

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="collector")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    # ---------------------------------------------------------- loop
    def _run(self) -> None:
        log.info("Collector started")
        while not self._stop.is_set():
            try:
                with self._lock:
                    queries = list(self._subs)
                if queries and market_open():
                    self._poll(queries)
                elif queries:
                    # outside market hours poll slowly just to keep LTP fresh
                    self._poll(queries)
                    self._stop.wait(30)
            except Exception as e:
                log.warning("Collector error: %s", e)
                self._stop.wait(5)
            self._stop.wait(POLL_SECONDS)
        log.info("Collector stopped")

    def _poll(self, queries: list[str]) -> None:
        results = client.quotes(queries, "all")
        now = datetime.now(IST)
        minute_ts = int(now.replace(second=0, microsecond=0).timestamp())
        for item in results:
            seg = item.get("exchange", "")
            token = str(item.get("exchange_token", ""))
            key = f"{seg}|{token}"
            try:
                ltp = float(item.get("ltp") or 0)
            except (TypeError, ValueError):
                continue
            if ltp <= 0:
                continue
            try:
                vol = float(item.get("last_volume") or 0)
            except (TypeError, ValueError):
                vol = 0.0

            self._live[key] = {
                "ltp": ltp,
                "change": item.get("change"),
                "per_change": item.get("per_change"),
                "ohlc": item.get("ohlc"),
                "depth": item.get("depth"),
                "ts": int(now.timestamp()),
            }

            if not market_open(now):
                continue  # don't write candles outside market hours

            wc = self._working.get(key)
            if wc is None or wc["ts"] != minute_ts:
                if wc is not None:
                    db.upsert_candle(seg, token, wc["ts"], wc["o"], wc["h"],
                                     wc["l"], wc["c"], wc["v"])
                self._working[key] = {"ts": minute_ts, "o": ltp, "h": ltp,
                                      "l": ltp, "c": ltp, "v": vol}
            else:
                wc["h"] = max(wc["h"], ltp)
                wc["l"] = min(wc["l"], ltp)
                wc["c"] = ltp
                wc["v"] += vol
            # write-through so the chart updates live
            wc = self._working[key]
            db.upsert_candle(seg, token, wc["ts"], wc["o"], wc["h"], wc["l"],
                             wc["c"], wc["v"])


collector = Collector()
