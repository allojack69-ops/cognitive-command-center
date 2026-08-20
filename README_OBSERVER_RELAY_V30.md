# Observer v3.0 — Frankfurt Execution Relay

Architecture:

Universe Lab / Observer Control
        |
        | HMAC-signed HTTPS
        v
Frankfurt Execution Relay
        |
        | Binance Spot Testnet REST + WebSocket
        v
Binance Spot Testnet

Why:
The current Render service receives HTTP 451 from Binance Spot Testnet.
Render regions are immutable, so the Testnet execution engine is split into a dedicated
Frankfurt service. The main site remains unchanged and owns the durable research database.

Security:
- Relay exposes Testnet only.
- Binance Testnet credentials exist only on the relay service.
- Cross-region control traffic is HMAC-SHA256 signed with timestamp anti-replay.
- No real Binance key is needed.
- No LIVE endpoint exists in the relay.

Persistence:
- Relay sends each new runtime checkpoint back to the main Universe Lab.
- Main site stores it using the existing observer_testnet_checkpoint_v29 event stream.
- Relay sends Testnet fills back to the main DB with idempotency keys.
- On a later START, the main site sends the last saved Testnet checkpoint back to the relay.

Render:
Use `render-relay.yaml` as a separate Blueprint.
It deliberately uses `plan: starter`, because free Render web services can spin down after
15 minutes without inbound traffic. This relay is intended for unattended multi-hour tests.

Main service environment:
- OBSERVER_TESTNET_RELAY_URL=https://<relay-host>.onrender.com
- OBSERVER_TESTNET_RELAY_SECRET=<same secret as RELAY_SHARED_SECRET>

Relay service environment:
- RELAY_SHARED_SECRET=<same secret>
- BINANCE_TESTNET_API_KEY=<Spot Testnet key>
- BINANCE_TESTNET_API_SECRET=<Spot Testnet secret>
- CONTROL_CALLBACK_URL is already defined in render-relay.yaml
