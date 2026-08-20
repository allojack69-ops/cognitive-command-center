# Observer v2.7 — Persistence + Opportunity + LIVE Canary

## Persistence
- every new closed-candle Observer state is archived to the existing SQL database
- full `runtime_state.json` is gzip-compressed into a rolling checkpoint window
- START restores the newest DB runtime checkpoint before launching the engine
- a background watchdog persists state even when the Observer page is not open
- STOP takes a final forced checkpoint

A non-SQLite `DATABASE_URL` is required for persistence across Render restart/redeploy.

## Opportunity gauge
The UI parses the most recent GEO3 support/resistance and ATR lines from the Observer log.
BUY uses support; SELL uses resistance. If a GEO3 level is unavailable, it falls back to a recent
micro-range proxy. The UI reports IN ZONE / APPROACHING / NEAR / FAR / structural-break states.

This is model-relative geometry, not a profit guarantee.

## LIVE Canary
LIVE Canary is deliberately NOT auto-armed.
The private admin page requires:
- max one-order USDT
- max filled trades
- max runtime minutes
- explicit `LIVE` confirmation

The child runtime remains Spot-only, uses existing strict ERL1/readiness/exchange risk gates,
keeps research bypasses disabled, runs `/api/v3/order/test` before an actual Binance order,
and is auto-stopped by the controller on the time/fill limit.

`BINANCE_API_KEY` and `BINANCE_API_SECRET` must be configured in Render environment variables.
Do not paste the secret into chat or the page.
