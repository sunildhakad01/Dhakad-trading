# Dhakad Pro Trading — Roadmap

Goal: search an index/stock → live chart → full analysis (indicators, option chain,
news, driver stocks) → auto-drawn SL/target for option trades → every strategy
backtested and its REAL win-rate shown next to the signal.

Honest constraint: no system is ~100% accurate. Target = verified positive expectancy
(55–65% win rate, RR >= 1:2, profit factor > 1.5 on walk-forward tests).

## Phase 1 — Data foundation  [DONE - needs live testing]
- [x] Kotak NEO auth (TOTP -> MPIN -> session token), auto re-login
- [x] Scrip master download -> SQLite instrument index (powers search + option chain)
- [x] Live quote poller -> builds 1m candles into DB
- [ ] Test login + live data during market hours
- [x] Historical backfill (NSE bhavcopy / yfinance for indices) — needed for indicators on day 1
- [ ] Kotak websocket (HSM) integration to replace polling (200 scrips, richer depth)

## Phase 2 — Charting UI  [DONE - v1]
- [x] Web app: search box -> instrument -> candlestick chart (Lightweight Charts)
- [x] Overlay layer: entry/SL/target lines drawn on chart
- [x] Multiple timeframes (5m/15m aggregation from 1m)

## Phase 3 — Analysis engine  [DONE - needs live verification]
- [x] Indicators: EMA 9/21/50/200, RSI(14), MACD, Supertrend(10,3), VWAP, ATR(14),
      Bollinger(20,2), CPR + pivots, prev day high/low
- [x] Auto SL/target: swing structure + ATR-based SL, RR-based + level-based targets
- [x] Option chain builder: strikes from scrip master + quotes -> OI, change in OI,
      PCR, Max Pain, IV skew, OI buildup classification
- [x] Driver-stock breadth score (index constituents weighted vote)
- [x] News feed (RSS Moneycontrol/ET) + Claude API sentiment scoring
- [x] Confluence scoring engine (each module votes -> composite signal)

## Phase 4 — Signal + trade plan  [DONE - needs live verification]
- [x] Signal generation only when confluence >= threshold (signals.py, /api/signal)
- [x] Option strategy selector by IV regime (buy CE/PE / debit spread / credit spread)
- [x] Spot SL/target -> option premium translation via BS delta (chain IV, fallback heuristic)
- [x] Claude API: plain-English trade reasoning panel (reasoning.py; template fallback
      without ANTHROPIC_API_KEY) — UI buttons "Option signal" / "Backtest" in side panel

## Phase 5 — Backtesting & verification  [ENGINE DONE - needs real data]
- [x] Backtest engine (pure pandas, backtest.py) with slippage + brokerage + STT
- [x] Walk-forward validation (k folds, optimize in-sample, report out-of-sample only)
- [x] Verified badge shown only if OOS: n>=30, WR>=50%, PF>=1.3, DD<=30% (/api/backtest)
- [ ] Run on real backfilled history once enough candles collected (needs market days)
- [ ] Tune confluence weights/thresholds from walk-forward results
- [ ] 4+ weeks paper trading before any real money

## Phase 6 — Polish
- [ ] Position sizing (1–2% risk), daily loss lock, Telegram alerts, trade journal
- [ ] One-click order placement (behind LIVE_TRADING switch + manual confirm)
