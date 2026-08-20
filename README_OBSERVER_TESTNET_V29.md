# Observer v2.9 — Binance Spot Testnet

Adds a private Binance Spot Testnet operator layer directly to `/observer/control`.

- real Binance Spot Testnet REST/WebSocket endpoints
- virtual funds only
- separate `BINANCE_TESTNET_API_KEY` / `BINANCE_TESTNET_API_SECRET`
- strict ERL1 execution gates
- session max order / max fills / max runtime
- CLOSE TEST POSITION
- STOP & FLATTEN
- main-market Observer checkpoint is protected before switching modes
- Testnet runtime is stored in separate DB checkpoint events
- rolling 24 full Testnet runtime checkpoints
- Testnet fills are archived as database events
- Testnet SELL is limited to BTC accumulated above the session baseline

Real-money LIVE credentials are not required or armed.
