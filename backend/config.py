"""Settings loaded from .env — credentials never leave this machine."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
FRONTEND_DIR = PROJECT_ROOT / "frontend"

load_dotenv(PROJECT_ROOT / ".env")


def _bool(v: str | None) -> bool:
    return (v or "").strip().lower() in ("1", "true", "yes")


@dataclass
class Settings:
    access_token: str = field(default_factory=lambda: os.getenv("KOTAK_ACCESS_TOKEN", ""))
    mobile: str = field(default_factory=lambda: os.getenv("KOTAK_MOBILE", ""))
    ucc: str = field(default_factory=lambda: os.getenv("KOTAK_UCC", ""))
    mpin: str = field(default_factory=lambda: os.getenv("KOTAK_MPIN", ""))
    totp_secret: str = field(default_factory=lambda: os.getenv("KOTAK_TOTP_SECRET", ""))
    anthropic_api_key: str = field(default_factory=lambda: os.getenv("ANTHROPIC_API_KEY", ""))
    live_trading: bool = field(default_factory=lambda: _bool(os.getenv("LIVE_TRADING")))
    host: str = field(default_factory=lambda: os.getenv("HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: int(os.getenv("PORT", "8000")))

    login_base = "https://mis.kotaksecurities.com/login/1.0"

    def validate(self) -> list[str]:
        missing = []
        for name, val in (
            ("KOTAK_ACCESS_TOKEN", self.access_token),
            ("KOTAK_MOBILE", self.mobile),
            ("KOTAK_UCC", self.ucc),
            ("KOTAK_MPIN", self.mpin),
        ):
            if not val:
                missing.append(name)
        return missing


settings = Settings()
DATA_DIR.mkdir(exist_ok=True)
