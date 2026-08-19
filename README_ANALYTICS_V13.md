# Universe Lab Private Analytics v1.3

Private first-party traffic analytics for the existing Universe Lab.

## What is tracked

- total pageviews
- total anonymous unique browsers
- total 30-minute visits
- daily unique browsers / visits / pageviews
- daily runs started / completed
- top pages
- external referrer domains
- `utm_source`, `utm_medium`, `utm_campaign`

## Privacy design

This module does **not** store IP addresses or full user-agent strings.

It uses:
- `ul_vid`: anonymous first-party browser ID, 2-year max age
- `ul_visit`: rolling 30-minute visit ID

Admin, static, API and detected bot traffic are excluded.

## Access

The analytics UI and JSON are protected by the existing admin session:

- `/admin/analytics`
- `/admin/analytics.json`

Log in through `/admin/login` using the existing `ADMIN_KEY`.

## Important

Traffic starts accumulating only after this version is deployed.
It cannot reconstruct historical page traffic that was never logged.

No database schema migration is needed; the existing `events` table is reused.
