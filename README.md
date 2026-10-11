# Dhakad Pro Trading

A personal options-trading analysis platform for Indian markets (NSE/BSE), built on the
**Kotak Securities NEO Trade API**.

Search any index or stock and get a live candlestick chart with indicators, option-chain
analytics, driver-stock breadth, news sentiment, and an auto-drawn entry / stop-loss /
target plan. Everything is combined into one confluence signal and translated into a
concrete option strategy. A walk-forward backtester reports the strategy's real,
cost-adjusted, out-of-sample statistics.

> **Safety first:** order placement is hard-gated behind `LIVE_TRADING=false` (the default).
> While it is false, the client refuses to place, modify, or cancel any order.

---

## What it does

| Feature | Details |
|---|---|
| **Instrument search** | Every NSE/BSE instrument from Kotak's daily scrip master, plus NIFTY 50, BANK NIFTY, FINNIFTY, INDIA VIX and SENSEX |
| **Live charting** | Candlestick chart (TradingView Lightweight Charts) at 1m / 5m / 15m, with entry, SL and target lines drawn on the chart |
| **Indicator engine** | EMA 9/21/50/200, RSI(14), MACD, Supertrend(10,3), VWAP, ATR(14), Bollinger(20,2), CPR + pivots, previous-day high/low |
| **Auto SL / target** | Swing-structure stop-loss with an ATR pad, and R-multiple targets snapped to nearby key levels |
| **Option chain** | OI, change in OI, PCR, Max Pain, IV skew and OI-buildup classification for any expiry |
| **Breadth score** | Weighted advance/decline vote of index constituents. It shows whether a move is broad-based or driven by a few heavyweights |
| **News sentiment** | Headlines from Economic Times and Moneycontrol RSS feeds, filtered by symbol and scored bullish or bearish |
| **Confluence signal** | Every module votes, and a signal fires only when enough of them agree |
| **Option strategy** | Picks a long option, a debit spread or a credit spread based on the IV regime, with premium-level SL and targets |
| **Trade reasoning** | A plain-English explanation of the signal that puts risks first (Claude API, with a template fallback) |
| **Backtesting** | Walk-forward backtest with slippage, brokerage and STT included. It reports only out-of-sample stats |
| **Portfolio** | Read-only view of positions and holdings |

---

## Strategy

### 1. Price-indicator votes
Each indicator casts a vote of **+1 (bullish)**, **−1 (bearish)** or **0 (neutral)**:

- **Trend:** EMA 9 vs EMA 21 crossover, plus Supertrend(10, 3) direction
- **Momentum:** RSI(14) above 60 is bullish, below 40 is bearish, anything between is neutral
- **MACD:** MACD line above or below its signal line

### 2. Confluence engine
The price votes are combined with three independent market-context modules:

| Module | Bullish | Bearish |
|---|---|---|
| Price indicators | majority of indicator votes up | majority down |
| Option-chain PCR | PCR > 1.3 (heavy put writing = support) | PCR < 0.7 (heavy call writing = resistance) |
| Driver-stock breadth | weighted constituents advancing | weighted constituents declining |
| News sentiment | net bullish headlines | net bearish headlines |

A module that can't be computed, for example a stock with no options or with no matching
headlines, is **left out**. It doesn't count as neutral. The composite reads BULLISH or
BEARISH only when **at least 60% of the voting modules agree**. Otherwise the result is
`NO_TRADE`.

### 3. Trade plan (spot)
- **Entry:** the current price
- **Stop-loss:** the last confirmed swing low (for longs) or swing high (for shorts), found with a 3-bar fractal and padded by
  0.25 × ATR. It is capped at 1.5 × ATR from entry so the risk stays bounded.
- **Targets:** T1 = 1.5R, T2 = 2.5R, where R is the distance from entry to the SL. A target snaps to a nearby
  CPR, pivot or previous-day level when one is within 0.3R, so it doesn't aim through a wall. T1 never drops below
  1.2R.

### 4. Option strategy by IV regime
The regime comes from ATM implied volatility, taken from the live chain:

| ATM IV | Strategy | Why |
|---|---|---|
| < 13% (low) | Buy ATM CE/PE | Premium is cheap, so it's worth taking long gamma |
| 13–18% (normal) | Debit spread (buy ATM, sell 2 strikes OTM) | Cuts theta decay |
| > 18% (high) | Credit spread on the opposite side (sell 2 OTM, buy 4 OTM) | Sells expensive premium with defined risk |

The spot SL and targets are translated to option-premium levels with **Black-Scholes delta**,
computed from the chain's own IV. If IV is missing, a moneyness heuristic is used instead. The plan
also shows risk per lot.

### 5. Backtesting & verification
- Simulates the same indicator-confluence logic on stored candles. Entries fill at the
  **next bar's open**, so there is no look-ahead.
- SL = 1.5 × ATR, target = 2R. A trade also exits on an opposite signal.
- **Costs are deducted on every round trip:** 0.05% slippage, ₹40 brokerage, and an STT approximation.
- **Walk-forward validation:** the threshold is optimised on each in-sample fold and then tested
  untouched on the next fold. Only the out-of-sample numbers are reported.
- A strategy gets a **"verified" badge only if** its out-of-sample results show at least 30 trades, a win rate of at least 50%,
  a profit factor of at least 1.3, and a max drawdown under 30%.

> All thresholds above (60% agreement, PCR bands, IV cutoffs, 1.5R/2.5R) are v1 starting
> heuristics. They have **not yet been validated** on enough real market history. No accuracy
> claim is made. Use the backtester's out-of-sample stats, and paper-trade first.

---

## Data sources

| Data | Source | Used for |
|---|---|---|
| Instrument master | Kotak NEO daily scrip master CSVs (NSE/BSE cash + F&O) | Search, lot sizes, option strikes and expiries |
| Live quotes | Kotak NEO Quotes API, polled about every 3 s and built into 1-minute candles | Charts, indicators, option-chain OI/IV/LTP |
| Index history | Yahoo Finance via `yfinance` (`^NSEI`, `^NSEBANK`, `^INDIAVIX`, `^BSESN`), 400 days of daily bars | Lookback for EMA 200 and other indicators on day 1 |
| Equity history | NSE daily "full bhavdata" CSV, last 60 trading days | Daily history for stocks (one file covers every NSE equity) |
| Index constituents | `backend/data/index_constituents.json`: NIFTY 50 and BANK NIFTY with approximate weights | Breadth score. Refresh it from [niftyindices.com](https://www.niftyindices.com) factsheets |
| News | Economic Times Markets and Moneycontrol RSS feeds | Sentiment vote |
| Sentiment / reasoning (optional) | Anthropic Claude API | Headline scoring and the trade-reasoning panel. Falls back to a keyword scorer and a template |

All data is stored locally in SQLite (`data/dhakad.db`). Backfilled daily history and live
1-minute candles are kept in separate tables and merged into one series for analysis.

---

## Tech stack

- **Backend:** Python 3.11+, FastAPI, Uvicorn, httpx, pandas, numpy. All indicators are
  hand-written in pandas, with no TA library.
- **Frontend:** a single-page HTML/JS app with TradingView Lightweight Charts
- **Storage:** SQLite
- **Auth:** Kotak NEO TOTP → MPIN → session token, with automatic re-login (`pyotp`)
- **Rate limiting:** the client caps requests at 10 per second to Kotak APIs

## Project structure

```
backend/
  main.py                  FastAPI app, REST endpoints, serves the frontend
  config.py                Settings loaded from .env
  kotak/
    auth.py                TOTP + MPIN login, session management
    client.py              Quotes, orders (gated), positions, holdings, limits
  data/
    database.py            SQLite schema + candle storage/merging
    scrip_master.py        Downloads and indexes the daily scrip master
    collector.py           Quote poller → 1-minute candle builder
    backfill.py            Historical daily backfill (yfinance + NSE bhavcopy)
    index_constituents.json
  analysis/
    indicators.py          EMA, RSI, MACD, Supertrend, VWAP, ATR, Bollinger, CPR, resampling
    levels.py              Swing points + auto SL/target engine
    option_chain.py        OI, PCR, Max Pain, IV skew, OI buildup
    breadth.py             Driver-stock weighted breadth score
    news.py                RSS headlines + sentiment scoring
    confluence.py          Combines module votes into one composite
    signals.py             IV-regime strategy selection + delta translation
    reasoning.py           Plain-English trade rationale
    backtest.py            Cost-aware walk-forward backtester
frontend/
  index.html               Search, chart, analysis side panel
```

## API endpoints

| Endpoint | Purpose |
|---|---|
| `GET /api/search` | Instrument search |
| `GET /api/candles` | Candles for a symbol / timeframe |
| `GET /api/quote` | Live quote |
| `GET /api/analysis` | Indicators + auto trade plan |
| `GET /api/confluence` | All module votes + composite bias |
| `GET /api/signal` | Full option trade signal with reasoning |
| `GET /api/backtest` | Walk-forward (or single-pass) backtest |
| `GET /api/option-chain`, `/api/option-chain/expiries` | Option chain analytics |
| `GET /api/breadth` | Driver-stock breadth score |
| `GET /api/news` | Symbol-filtered headlines + sentiment |
| `GET /api/portfolio` | Positions and holdings (read-only) |
| `GET /api/status` | Login, market-open, live-trading flag, data counts |

---

## Setup (Windows)

1. Install Python 3.11 or newer and tick "Add to PATH".
2. Install the dependencies:
   ```
   pip install -r requirements.txt
   ```
3. Copy `.env.example` to `.env` and fill in your Kotak NEO credentials:
   - `KOTAK_ACCESS_TOKEN`: from the NEO app → Invest → Trade API → your application
   - `KOTAK_MOBILE`: your registered mobile number, with +91
   - `KOTAK_UCC`: your client code
   - `KOTAK_MPIN`: your 6-digit MPIN
   - `KOTAK_TOTP_SECRET`: optional. If you set it, login is fully automatic. If not, you type the TOTP at startup.
   - `ANTHROPIC_API_KEY`: optional. It enables Claude-scored sentiment and reasoning.

   `.env` is git-ignored. Never commit it.

4. Run:
   ```
   python -m backend.main
   ```
5. Open http://localhost:8000.

On first run the app downloads the scrip master and backfills history. During market hours
(09:15–15:30 IST, Mon–Fri) the collector builds live 1-minute candles for the symbol you
have open.

## Status

**Built:** authentication, the data pipeline and backfill, multi-timeframe charting, the
indicator engine, auto SL/target, option-chain analytics, breadth, news sentiment, the
confluence engine, option strategy selection, trade reasoning, and the cost-aware
walk-forward backtester.

**Next:**
- Validate the strategy on real backfilled history and tune weights and thresholds from the walk-forward results
- Replace polling with the Kotak HSM websocket
- Position sizing (1–2% risk), a daily loss lock, Telegram alerts, and a trade journal
- At least four weeks of paper trading before any live use

## Disclaimer

This is an educational and personal research tool, not investment advice. Options trading
carries a substantial risk of loss. No indicator or signal is guaranteed. Never trade money you
cannot afford to lose.
