# Universe Lab — Observer Control Center v2.1

Private admin UI for controlling and inspecting the MOR Observer.

## UI

`/observer/control`

Features:

- START / STOP / RESTART
- process state, PID, uptime
- MOR state / price / regime / action / horizon
- recent-state price trajectory
- decision pipeline: Strategy → Tradeability → Edge → Geometry → Execution
- horizon tradeability surface
- runtime counters
- derived-module matrix
- RES1 reliability/error
- engine log tail
- automatic refresh every 3 seconds

## Security

All routes are admin-only. Control POSTs are protected by a session CSRF token. The process command cannot be supplied from the browser.

## Attaching the real engine

The current Universe Lab repository contains Observer/MOR snapshots but not the actual MOR runtime Python entrypoint.

Deploy the runtime under:

`observer_runtime/`

with one of:

- `run_observer.py`
- `main.py`
- `observer.py`
- `trader.py`

or set Render environment variables:

`OBSERVER_RUNTIME_DIR=/path/to/runtime`

`OBSERVER_COMMAND_JSON=["python","your_entrypoint.py"]`

Once the engine is attached, START/STOP/RESTART become active without rebuilding the UI.

Important: if Observer is launched as a subprocess of the Render web service, it stops whenever the web service restarts or sleeps. For durable 24/7 operation, move Observer to a persistent worker/service and keep this dashboard as the controller.
