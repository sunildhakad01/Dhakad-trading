# Project rules for Claude (Claude Code / Cowork)

1. NEVER place, modify, or cancel a real order without explicit confirmation from
   Sunil in that same conversation. `LIVE_TRADING` must stay `false` by default.
2. NEVER print, log, or commit credentials (.env contents, tokens, MPIN, TOTP secret).
3. All order-placing code paths must check `settings.live_trading` and refuse when false.
4. Timezone is Asia/Kolkata (IST). Market hours 09:15–15:30, Mon–Fri.
5. Rate limit: max 10 requests/second to Kotak APIs. The client enforces this — keep it.
6. Money values from Kotak APIs arrive as strings — parse carefully, never assume float.
7. Backtests must include costs (slippage, brokerage, STT) — never report gross results.
8. Never claim or imply near-100% accuracy anywhere in UI or docs. Show real tested stats.
9. Read ROADMAP.md before starting work; update it when a phase item is completed.
