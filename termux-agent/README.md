# Universe Lab · Termux Observer Agent v1

The website is the control plane. Android/Termux is the Binance Spot Testnet execution node.

- Testnet only.
- No leverage.
- No real-money API path.
- Binance Testnet API key/secret stay only on the phone in `~/.universe-observer-agent.json` (mode 600).
- START defaults: max 10 USDT per order, 20 fills, 120 minutes.
- CLOSE sells only BTC accumulated above the session baseline.
- STOP & FLATTEN closes the bot-opened Testnet position and stops the MOR runtime.
- Full runtime checkpoints are compressed and sent back to the main database on state changes.
- Temporary website/internet interruption does not deliberately kill the local MOR process; the agent retries the control link.

