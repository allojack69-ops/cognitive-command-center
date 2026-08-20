# Observer v2.8 — Execution Truth

Critical inconsistency found from the live logs:

The UI showed `ERL1 BLOCK`, but PAPER still emitted `FILLED_PAPER`.
The old PAPER branch required Tradeability + Edge but did not require ERL1 `strict_ready`.

v2.8:
- keeps prediction freezing independent for research
- PAPER execution now requires Tradeability + Edge + ERL1 strict readiness
- an ERL1 block becomes `BLOCKED_ERL1_PAPER` instead of a fake fill
- LIVE keeps its own exchange/risk checks
- UI distinguishes `EH1 BEST` from `prediction H`
