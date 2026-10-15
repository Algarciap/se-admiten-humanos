# Railway deployment with persistent SQLite

This is the initial hosted topology for `Se admiten humanos`:

```text
Railway combined web + worker service (one replica)
    ├── polls Alquiler Seguro, Tecnocasa, Redpiso, and Nestar every 180 seconds
    ├── polls the official Idealista API every 900 seconds when credentials exist
    ├── accepts user-owned search profiles over HTTPS
    ├── sends eligible new listings to Telegram
    ├── records and alerts on per-source health transitions
    └── reads/writes /data/rentals.sqlite3
                      │
                Railway volume
```

It deliberately uses a continuously running worker instead of Railway cron. The source needs checks
faster than daily alerts, and the worker's internal interval is predictable while the process is
healthy. One worker owns one SQLite database; do not add replicas. Move to PostgreSQL before adding
multiple concurrent workers or a public multi-user website.

## 1. Create the service and volume

1. In Railway, create a project from the GitHub repository.
2. Select the `main` branch and allow deployments from it.
3. Add one persistent volume to the worker service.
4. Mount the volume at exactly `/data`.
5. Keep the service at one replica.

The committed `Dockerfile` installs the package and starts `se-admiten-humanos run-service`.
`railway.toml` uses the `Always` restart policy and checks `/health`. The same single process serves
the public onboarding site while a background thread owns the polling loop.

## 2. Add sealed variables

Add these variables to the Railway service. Treat every value other than the numeric settings as
private.

| Variable | Value / purpose |
| --- | --- |
| `DATABASE_PATH` | `/data/rentals.sqlite3` |
| `POLL_INTERVAL_SECONDS` | `180` |
| `MAX_LISTINGS_PER_RUN` | `50` |
| `MAX_CONSECUTIVE_FAILURES` | `3` |
| `SOURCE_FAILURE_ALERT_THRESHOLD` | Consecutive source failures before an operator alert; default `3` |
| `SOURCE_STALE_AFTER_SECONDS` | Seconds without a successful source check before alerting; default `1800` |
| `TELEGRAM_BOT_TOKEN` | BotFather token |
| `TELEGRAM_BOT_USERNAME` | Bot username without `@`; enables secure `/start` link polling |
| `PORT` | Injected automatically by Railway; local default is `8080` |
| `WEB_COOKIE_SECURE` | Keep `true` in production |
| `TELEGRAM_CHAT_ID` | Legacy bootstrap: first user's numeric private chat id |
| `MONITORING_TELEGRAM_CHAT_ID` | Operator's numeric private chat id; falls back to legacy `TELEGRAM_CHAT_ID` |
| `TELEGRAM_USER_ID` | Legacy bootstrap: first user's application id |
| `PROFILE_BUNDLE_JSON` | Entire private profile file as one JSON value |
| `STATUS_PAGE_TOKEN` | Strong password enabling the protected `/status` operations page |
| `IDEALISTA_API_KEY` | Approved Idealista Search API key; omit until access is granted |
| `IDEALISTA_API_SECRET` | Matching Idealista secret; must be configured with the key |
| `IDEALISTA_POLL_INTERVAL_SECONDS` | Idealista-only cadence; default `900` to conserve quota |
| `IDEALISTA_MAX_PAGES` | Pages per requested category; keep `1` until quota is confirmed |

For `PROFILE_BUNDLE_JSON`, copy the contents of the ignored `config/profiles.local.json`. Minifying
it first makes pasting less error-prone, but whitespace and line breaks are valid JSON. Do not add
quotes around the whole value and do not paste it into a build argument, command, repository file,
or deployment log.

The profile variable is read only when the database has no enabled search profiles. It is safe to
leave it configured so a deliberate fresh-volume recovery can initialize itself, but changing it
does not overwrite profiles already stored in SQLite.

The first live notification cycle stores the legacy `TELEGRAM_USER_ID`/`TELEGRAM_CHAT_ID` pair in
SQLite. Later cycles route from the database and will not overwrite that record, even if the Railway
variables remain configured. Future website onboarding can therefore add, change, disable, or remove
per-user destinations without sharing one global chat setting. The bot token remains a single sealed
service secret. When `TELEGRAM_BOT_USERNAME` is set, every worker cycle also consumes private-chat,
single-use account links and persists the Bot API cursor. A temporary polling error is included in the
cycle's `telegram_linking_error` field without preventing time-sensitive property acquisition.

## 3. Understand the first deployment

The first successful worker cycle performs these operations in order:

1. Creates the SQLite schema on the persistent volume.
2. Imports the private user, renter, pet, and search profile.
3. Polls Telegram account-link commands when `TELEGRAM_BOT_USERNAME` is configured.
4. Discovers current Alquiler Seguro, Tecnocasa, Redpiso, and Nestar inventory from their published sitemaps and, when credentials are configured, through the official Idealista API.
5. Records every current listing id as a separate no-alert baseline for each source.
6. Writes a `baseline_created` event to the Railway logs and sends zero property alerts.

## Public networking

The container now runs `se-admiten-humanos run-service`. Railway routes HTTPS traffic to its injected
`PORT`, and `/health` must return `{"status": "ok"}` before a deployment becomes healthy. Generate one
Railway domain under the service's Networking settings and share that HTTPS URL only after testing a
complete form submission and Telegram connection. Do not set `WEB_COOKIE_SECURE=false` in Railway.

When `STATUS_PAGE_TOKEN` is configured, `/status` uses HTTP Basic authentication with username
`admin` and that token as the password. The page contains source names, health, inventory totals,
failure counts, and sanitized technical errors only. It deliberately excludes users, Telegram chat
ids, and search criteria. Without the variable, the route returns `404` rather than exposing an
unprotected dashboard.

The onboarding session is currently tied to one browser. Email verification, cross-device sign-in,
account recovery, abuse controls, and administrative deletion tools are required before broad public
promotion; this release is suitable for a small invited pilot.

On the next cycle, the worker constructs the Telegram notifier and performs a normal live check.
That cycle also migrates the first user's legacy environment mapping into the database.
Only listing ids that appeared after the baseline are detail-fetched and evaluated. An empty result
is logged with status `empty`; it is a healthy outcome.

## 4. Verify before handing over

Keep the existing local scheduler active during deployment so there is no monitoring gap. In the
Railway deployment logs, verify all of the following:

- one `baseline_created` result per enabled source with a non-zero `discoveries_recorded` value;
- a later `empty` or `succeeded` acquisition event;
- no `worker_cycle_failed` or configuration error;
- the same volume remains attached after one manual redeploy;
- the post-redeploy cycle does **not** create a second baseline.

Once those checks pass, stop the local scheduled run. Running both indefinitely is undesirable:
each has an independent SQLite database, so both can send the same newly discovered listing.

No test listing should be injected into the public source. Telegram delivery is already covered by
the local integration test and automated tests; the first real new eligible listing is the safest
end-to-end production confirmation.

## Failure behaviour and recovery

- Missing/invalid profile or Telegram settings stop the worker immediately with a sanitized error.
- Website, parsing, SQLite, and delivery failures are logged. A failed source does not starve the
  remaining sources in that cycle. Three consecutive partially or fully failed cycles make the
  process exit non-zero, after which Railway's `Always` policy restarts it.
- A failed Telegram delivery remains retryable and the discovery is not acknowledged as complete.
- A Telegram account-link polling error is reported separately and does not delay property checks.
- Parser contract changes fail visibly instead of turning a changed page into a false empty result.
- Source health survives restarts in SQLite. Repeated failures alert once, zero-total inventory
  alerts immediately, stale success alerts once, and recovery sends one follow-up.
- Removing or replacing the volume creates a fresh database and therefore a fresh baseline. Treat
  that as a recovery operation, not routine deployment.

Use Railway volume backups before risky schema or source changes. SQLite database files and backups
contain private profile information in plaintext in this MVP and must be handled as personal data.
