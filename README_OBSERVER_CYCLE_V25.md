# Observer Closed-Candle Cycle v2.5

Adds a hybrid live adaptive-loop visualization to `/observer/control`.

Compact: 8 live phases.
Expanded: 21 steps in four research groups.
Updates with the existing 3-second Observer status refresh.
Also changes RES1 `n=0` from misleading 100% reliability to `WARMUP`.

No DB migration. No new route. No Observer restart required after Render deploy.
