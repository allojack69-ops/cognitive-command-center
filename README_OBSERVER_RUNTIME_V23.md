# Observer Runtime hotfix v2.3

Fixes the X100 startup stall.

The MOR engine runs Session Gap Lab replay before opening the live Binance
WebSocket. Its default maximum replay gap is 10080 minutes (7 days). A gap of
several hours can therefore execute hundreds of full geometry states
synchronously and keep the operator UI on the seeded X-state.

v2.3 changes the web-launched process policy:

- Python runs unbuffered, so ENGINE LOG shows startup progress immediately.
- synchronous gap replay is capped at 120 minutes.
- gaps longer than 120 minutes no longer block live observation.
- the original MOR logic and learned state are preserved.

Long-gap forensic replay should later move to a separate research worker so
historical reconstruction cannot block the live observer.
