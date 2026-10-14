# Alquiler Seguro website watcher

Last reviewed on 2026-10-14.

The Alquiler Seguro daily email is too slow for the initial Madrid use case. This adapter instead
uses the agency's published [`dynamics.xml`](https://www.alquilerseguro.es/dynamics.xml) sitemap as
a lightweight inventory index. It requests a public `/viviendas/{id}` detail page only when an ID
has not been processed before.

The current [`robots.txt`](https://www.alquilerseguro.es/robots.txt) disallows `/api/`, filtered
query URLs, `/inmuebles/`, and `/viviendas/referencia/`. The adapter never uses those routes. It does
not authenticate, solve CAPTCHAs, call private endpoints, rotate identities, or hide its user agent.
The site's [`aviso legal`](https://www.alquilerseguro.es/aviso-legal) should be reviewed again if the
route or project usage changes.

## First-time initialization

Run this exactly once for a database:

```powershell
python -m se_admiten_humanos baseline-alquiler-seguro --database rentals.sqlite3
```

Initialization stores all IDs already present in the sitemap as processed. It deliberately fetches
no listing detail pages and sends no Telegram messages. A second initialization attempt fails.

## Live check

After Telegram and the user profile are configured:

```powershell
python -m se_admiten_humanos run-alquiler-seguro --database rentals.sqlite3
```

Each run performs one sitemap request, diffs its IDs against SQLite, and fetches only unseen detail
pages. Detail requests are spaced by at least 1.5 seconds. The parser normalizes title, price,
municipality, address, bedrooms when stated, area, furnishing when stated, description, and URL.
The shared pipeline then applies the user's rent and location filters, classifies the pet wording,
deduplicates the property, and sends eligible alerts.

An empty sitemap, malformed XML, missing core detail fields, unexpected URL, oversized response,
or delivery failure is recorded as a visible failed run. Failed delivery remains retryable. A
listing that simply says nothing about pets is labelled `not_specified`, never `accepted`.

For a local loop, schedule the live command every two to five minutes with only one concurrent
instance. The application's per-source run lock prevents overlap, but the scheduler should also log
non-zero exits so parser changes are noticed quickly.
