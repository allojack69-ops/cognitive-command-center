# Universe Lab — Observer Runtime v2.2

This package attaches the uploaded MOR Trader v1.23 engine to the existing
private `/observer/control` page.

## Result

- START launches the real `observer_runtime/app.py`
- STOP sends SIGINT so MOR saves `runtime_state.json`
- RESTART performs the same graceful stop and starts again
- UI reads `observer_status.json` every 3 seconds
- the uploaded learned runtime state is preserved as the seed

## First server phase: PAPER LOCK

The web-launched process is forced to `MOR_EXECUTION_MODE=PAPER`.

The controller strips exchange API key/secret and disables live/testnet
override gates before spawning the child process.

The Observer can consume the public BTCUSDT Binance market stream and use the
existing paper simulator, but this package cannot send exchange orders.

## Persistence

Within one Render deployment, START/STOP preserves the updated runtime state.
Without a persistent Render disk, a service redeploy/restart can roll the
runtime back to the seed committed in GitHub.

The next infrastructure step should be persistent Observer storage or a
separate persistent worker.
