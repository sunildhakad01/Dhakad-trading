"""Kotak NEO REST client: quotes, scrip master, orders, positions, holdings, limits.

- Enforces the 10 req/sec rate limit.
- Auto re-login on expired session (401 / 1003).
- Order placement is HARD-GATED behind settings.live_trading.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from typing import Any

import httpx

from backend.config import settings
from backend.kotak.auth import AuthError, Session, login

log = logging.getLogger("kotak.client")


class LiveTradingDisabled(Exception):
    """Raised when an order API is called while LIVE_TRADING=false."""


class RateLimiter:
    """Simple sliding-window limiter: max `rate` calls per second."""

    def __init__(self, rate: int = 9):  # 9 to stay safely under Kotak's 10/s
        self.rate = rate
        self._calls: deque[float] = deque()
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            while self._calls and now - self._calls[0] > 1.0:
                self._calls.popleft()
            if len(self._calls) >= self.rate:
                sleep_for = 1.0 - (now - self._calls[0]) + 0.01
                time.sleep(max(sleep_for, 0))
            self._calls.append(time.monotonic())


class KotakClient:
    def __init__(self) -> None:
        self.session: Session | None = None
        self._http = httpx.Client(timeout=30)
        self._limiter = RateLimiter()
        self._login_lock = threading.Lock()

    # ------------------------------------------------------------- auth
    def ensure_login(self) -> Session:
        with self._login_lock:
            if self.session is None or not self.session.active:
                self.session = login()
            return self.session

    def _relogin(self) -> None:
        with self._login_lock:
            self.session = login()

    # ------------------------------------------------------------- core request helpers
    def _trade_headers(self) -> dict[str, str]:
        s = self.ensure_login()
        return {
            "accept": "application/json",
            "Auth": s.token,
            "Sid": s.sid,
            "neo-fin-key": "neotradeapi",
        }

    def _request(self, method: str, path: str, *, jdata: dict | None = None,
                 quotes_auth: bool = False, retry: bool = True) -> Any:
        """path starts with '/'. jdata -> form field 'jData' (urlencoded JSON)."""
        s = self.ensure_login()
        url = s.base_url + path
        self._limiter.wait()

        if quotes_auth:  # Quotes & scrip master: only access token
            headers = {"Authorization": settings.access_token}
            resp = self._http.request(method, url, headers=headers)
        else:
            headers = self._trade_headers()
            if jdata is not None:
                headers["Content-Type"] = "application/x-www-form-urlencoded"
                resp = self._http.request(method, url, headers=headers,
                                          data={"jData": json.dumps(jdata)})
            else:
                resp = self._http.request(method, url, headers=headers)

        # session expiry -> single re-login retry
        if resp.status_code == 401 and retry:
            log.info("Session expired, re-logging in")
            self._relogin()
            return self._request(method, path, jdata=jdata, quotes_auth=quotes_auth, retry=False)

        try:
            body = resp.json()
        except Exception:
            raise RuntimeError(f"{path}: non-JSON response HTTP {resp.status_code}")

        if isinstance(body, dict) and body.get("stat") == "Not_Ok":
            code = body.get("stCode")
            if code in (1003,) and retry:  # invalid session
                self._relogin()
                return self._request(method, path, jdata=jdata, quotes_auth=quotes_auth, retry=False)
            raise RuntimeError(f"{path}: {body.get('emsg', 'unknown error')} (code {code})")
        return body

    # ------------------------------------------------------------- market data
    def scrip_master_paths(self) -> list[str]:
        body = self._request("GET", "/script-details/1.0/masterscrip/file-paths",
                             quotes_auth=True)
        return body["data"]["filesPaths"]

    def quotes(self, queries: list[str], flt: str = "all") -> list[dict]:
        """queries like 'nse_cm|11536' or 'nse_cm|Nifty 50' (indices by exact name)."""
        q = ",".join(queries)
        body = self._request("GET", f"/script-details/1.0/quotes/neosymbol/{q}/{flt}",
                             quotes_auth=True)
        return body if isinstance(body, list) else body.get("data", [])

    def ltp(self, queries: list[str]) -> dict[str, float]:
        """Map 'exchange|token' -> last traded price."""
        out: dict[str, float] = {}
        for item in self.quotes(queries, "ltp"):
            key = f"{item.get('exchange')}|{item.get('exchange_token')}"
            try:
                out[key] = float(item.get("ltp") or 0)
            except (TypeError, ValueError):
                out[key] = 0.0
        return out

    # ------------------------------------------------------------- portfolio / funds
    def orders(self) -> list[dict]:
        return self._request("GET", "/quick/user/orders").get("data", []) or []

    def trades(self) -> list[dict]:
        return self._request("GET", "/quick/user/trades").get("data", []) or []

    def positions(self) -> list[dict]:
        return self._request("GET", "/quick/user/positions").get("data", []) or []

    def holdings(self) -> list[dict]:
        return self._request("GET", "/portfolio/v1/holdings").get("data", []) or []

    def limits(self) -> dict:
        return self._request("POST", "/quick/user/limits",
                             jdata={"seg": "ALL", "exch": "ALL", "prod": "ALL"})

    def check_margin(self, jdata: dict) -> dict:
        jdata.setdefault("brkName", "KOTAK")
        jdata.setdefault("brnchId", "ONLINE")
        return self._request("POST", "/quick/user/check-margin", jdata=jdata)

    def order_history(self, order_no: str) -> list[dict]:
        return self._request("POST", "/quick/order/history",
                             jdata={"nOrdNo": order_no}).get("data", []) or []

    # ------------------------------------------------------------- orders (GATED)
    def _gate(self) -> None:
        if not settings.live_trading:
            raise LiveTradingDisabled(
                "LIVE_TRADING=false — refusing to touch real orders. "
                "This is a deliberate safety switch."
            )

    def place_order(self, jdata: dict) -> dict:
        self._gate()
        return self._request("POST", "/quick/order/rule/ms/place", jdata=jdata)

    def modify_order(self, jdata: dict) -> dict:
        self._gate()
        return self._request("POST", "/quick/order/vr/modify", jdata=jdata)

    def cancel_order(self, order_no: str, amo: bool = False, trading_symbol: str | None = None) -> dict:
        self._gate()
        jdata: dict = {"on": order_no, "am": "YES" if amo else "NO"}
        if amo and trading_symbol:
            jdata["ts"] = trading_symbol
        return self._request("POST", "/quick/order/cancel", jdata=jdata)


# module-level singleton
client = KotakClient()
