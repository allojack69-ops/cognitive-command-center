# Universe Lab v1.8 — DOM Background Fix

This fixes the background at the rendering level.

Previous versions used `body::before` with a negative z-index. Some mobile
browser/compositing combinations can place that pseudo-element behind the
root canvas, making the image effectively invisible.

v1.8 uses a real fixed DOM element:

- `#universe-bg-layer`
- `z-index: 0`
- veil at `z-index: 1`
- header/main/footer at `z-index: 2`

It also:

- copies `universe_bg_acrylic.png` into `static/universe/` again
- adds `?v=18` cache busting
- auto-refreshes `/admin/analytics` every 30 seconds while the tab is visible
- changes no routes or database schema
