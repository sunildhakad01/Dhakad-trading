"""Kotak NEO login: TOTP -> view token -> MPIN validate -> session (trade) token.

Docs: https://mis.kotaksecurities.com/login/1.0/tradeApiLogin  (step 1)
      https://mis.kotaksecurities.com/login/1.0/tradeApiValidate (step 2)

Never log or print tokens, MPIN, or the TOTP secret.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field

import httpx

from backend.config import settings

log = logging.getLogger("kotak.auth")


class AuthError(Exception):
    pass


@dataclass
class Session:
    """Holds everything post-login APIs need."""
    token: str = ""          # session (trade) token -> 'Auth' header
    sid: str = ""            # session sid -> 'Sid' header
    base_url: str = ""       # base for all post-login APIs
    data_center: str = ""    # for websocket (e.g. E43)
    logged_in_at: float = 0.0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def active(self) -> bool:
        return bool(self.token and self.sid and self.base_url)


def _totp_code() -> str:
    """Generate TOTP from secret if configured, else ask on the terminal."""
    if settings.totp_secret:
        import pyotp
        return pyotp.TOTP(settings.totp_secret).now()
    return input("Enter the 6-digit TOTP from your authenticator app: ").strip()


def login(max_retries: int = 2) -> Session:
    """Full login flow. Returns an active Session or raises AuthError."""
    missing = settings.validate()
    if missing:
        raise AuthError(f"Missing credentials in .env: {', '.join(missing)}")

    headers = {
        "Authorization": settings.access_token,  # plain token, NO 'Bearer'
        "neo-fin-key": "neotradeapi",
        "Content-Type": "application/json",
    }

    last_err: Exception | None = None
    for attempt in range(1, max_retries + 1):
        try:
            with httpx.Client(timeout=30) as client:
                # ---- Step 1: TOTP login -> view token + view sid
                r1 = client.post(
                    f"{settings.login_base}/tradeApiLogin",
                    headers=headers,
                    json={
                        "mobileNumber": settings.mobile,
                        "ucc": settings.ucc,
                        "totp": _totp_code(),
                    },
                )
                d1 = _extract(r1, "TOTP login")

                # ---- Step 2: MPIN validate -> trade token + sid + baseUrl
                r2 = client.post(
                    f"{settings.login_base}/tradeApiValidate",
                    headers={**headers, "sid": d1["sid"], "Auth": d1["token"]},
                    json={"mpin": settings.mpin},
                )
                d2 = _extract(r2, "MPIN validate")

                sess = Session(
                    token=d2["token"],
                    sid=d2["sid"],
                    base_url=(d2.get("baseUrl") or "").rstrip("/"),
                    data_center=d2.get("dataCenter", ""),
                    logged_in_at=time.time(),
                )
                if not sess.active:
                    raise AuthError("Login response missing token/sid/baseUrl")
                log.info("Kotak login OK (kType=%s, dc=%s)", d2.get("kType"), sess.data_center)
                return sess
        except AuthError:
            raise
        except Exception as e:  # network / transient (e.g. 424 dependency)
            last_err = e
            log.warning("Login attempt %d failed: %s", attempt, type(e).__name__)
            time.sleep(2 * attempt)

    raise AuthError(f"Login failed after {max_retries} attempts: {last_err}")


def _extract(resp: httpx.Response, step: str) -> dict:
    try:
        body = resp.json()
    except Exception:
        raise AuthError(f"{step}: non-JSON response (HTTP {resp.status_code})")
    data = body.get("data") if isinstance(body, dict) else None
    if resp.status_code != 200 or not data or data.get("status") != "success":
        msg = (body.get("message") or body.get("emsg") or str(body))[:200] if isinstance(body, dict) else ""
        raise AuthError(f"{step} failed (HTTP {resp.status_code}): {msg}")
    return data
