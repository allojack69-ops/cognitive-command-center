# Observer market feed hotfix v2.4

The Render-hosted Observer reached Binance but the standard Spot REST and
WebSocket endpoints returned HTTP 451.

This patch changes only the public market-data path:

REST:
`https://data-api.binance.vision/api/v3/klines`

WebSocket:
`wss://data-stream.binance.vision/ws/btcusdt@kline_1m`

These are Binance-documented market-data-only endpoints.

The execution bridge is not changed. The server process remains hard-locked
to PAPER by the existing Observer controller.

Environment overrides are supported:

- MOR_MARKET_REST_URL
- MOR_MARKET_WS_URL
