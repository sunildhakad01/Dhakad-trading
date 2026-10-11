# Dhakad Pro Trading

Personal options-trading analysis platform built on the Kotak Securities NEO Trade API.
Search any index or stock → live chart → indicators, option-chain analysis, auto SL/target.

> **Safety:** `LIVE_TRADING=false` by default. The platform can NEVER place a real order
> unless you deliberately set `LIVE_TRADING=true` in `.env` AND confirm each order.

## Setup (Windows)

1. Install Python 3.11+ from https://python.org (tick "Add to PATH").
2. Open a terminal in this folder and run:
   ```
   pip install -r requirements.txt
   ```
3. Copy `.env.example` to `.env` and fill in your Kotak NEO credentials:
   - `KOTAK_ACCESS_TOKEN` — from NEO app → Invest → Trade API → your application
   - `KOTAK_MOBILE` — registered mobile with +91
   - `KOTAK_UCC` — your client code
   - `KOTAK_MPIN` — your 6-digit MPIN
   - `KOTAK_TOTP_SECRET` — (optional) the secret behind your TOTP QR code.
     If set, login is fully automatic. If not set, you type the 6-digit TOTP at startup.
   - `ANTHROPIC_API_KEY` — (optional) enables Claude-scored news sentiment. Without it,
     the built-in keyword sentiment scorer is used instead.

## Run

```
python -m backend.main
```

Then open http://localhost:8000 in your browser.

On first run it downloads the scrip master (~all NSE/BSE instruments) into `data/dhakad.db`.
During market hours (9:15–15:30) the quote poller builds live 1-minute candles for
whatever symbol you have open.

## Project layout

```
backend/
  main.py               FastAPI app + API endpoints (serves the frontend)
  config.py             Settings loaded from .env
  kotak/auth.py         TOTP + MPIN login, session management
  kotak/client.py       REST wrapper: quotes, orders, positions, holdings, limits
  data/database.py      SQLite storage (instruments, candles)
  data/scrip_master.py  Downloads + indexes the daily scrip master CSVs
  data/collector.py     Quote poller -> 1-minute candle builder
  data/backfill.py      Historical daily backfill (yfinance indices, NSE bhavcopy equities)
  data/index_constituents.json  Illustrative NIFTY50/BANKNIFTY weights for the breadth score
  analysis/indicators.py EMA, RSI, MACD, Supertrend, VWAP, ATR, Bollinger, CPR, timeframe resample
  analysis/levels.py    Swing highs/lows, auto SL/target engine
  analysis/option_chain.py Strikes from scrip master + live quotes -> OI, PCR, Max Pain
  analysis/breadth.py    Driver-stock breadth score (index constituents weighted vote)
  analysis/news.py       RSS headlines + keyword/Claude sentiment scoring
  analysis/confluence.py Combines every module's vote into one composite signal
frontend/
  index.html            Search + candlestick chart (TradingView Lightweight Charts)
data/                   SQLite DB + downloaded scrip CSVs (created at runtime)
```

## Roadmap status

See `ROADMAP.md`. Phases 1–3 are implemented: auth, scrip master, candle collection,
historical backfill, multi-timeframe charting, the indicator engine, auto SL/target,
option-chain analytics (PCR/Max Pain/OI buildup), driver-stock breadth, news sentiment,
and a confluence engine that combines all of the above into one vote. What's still
outstanding: live-market verification of the pieces above, the Kotak HSM websocket,
and everything in Phases 4–6 (signal generation, option strategy selection, backtesting,
and position sizing/alerts) — none of that has been built or tested yet.

## Disclaimer

Educational tool. Options trading carries real risk of loss. No indicator or signal is
guaranteed. Never trade money you cannot afford to lose.
