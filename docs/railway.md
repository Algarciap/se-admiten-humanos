# Railway deployment with persistent SQLite

This is the initial hosted topology for `Se admiten humanos`:

```text
Railway background worker (one replica)
    ├── polls Alquiler Seguro every 180 seconds
    ├── sends eligible new listings to Telegram
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

The committed `Dockerfile` installs the package and starts `se-admiten-humanos run-worker`.
`railway.toml` uses the `Always` restart policy. The service is a background worker and therefore
does not expose a public HTTP port or health-check endpoint.

## 2. Add sealed variables

Add these variables to the Railway service. Treat every value other than the numeric settings as
private.

| Variable | Value / purpose |
| --- | --- |
| `DATABASE_PATH` | `/data/rentals.sqlite3` |
| `POLL_INTERVAL_SECONDS` | `180` |
| `MAX_LISTINGS_PER_RUN` | `50` |
| `MAX_CONSECUTIVE_FAILURES` | `3` |
| `TELEGRAM_BOT_TOKEN` | BotFather token |
| `TELEGRAM_CHAT_ID` | Numeric private chat id |
| `TELEGRAM_USER_ID` | Application user id used in the private profile JSON |
| `PROFILE_BUNDLE_JSON` | Entire private profile file as one JSON value |

For `PROFILE_BUNDLE_JSON`, copy the contents of the ignored `config/profiles.local.json`. Minifying
it first makes pasting less error-prone, but whitespace and line breaks are valid JSON. Do not add
quotes around the whole value and do not paste it into a build argument, command, repository file,
or deployment log.

The profile variable is read only when the database has no enabled search profiles. It is safe to
leave it configured so a deliberate fresh-volume recovery can initialize itself, but changing it
does not overwrite profiles already stored in SQLite.

## 3. Understand the first deployment

The first successful worker cycle performs these operations in order:

1. Creates the SQLite schema on the persistent volume.
2. Imports the private user, renter, pet, and search profile.
3. Discovers the current Alquiler Seguro inventory.
4. Records every current listing id as the no-alert baseline.
5. Writes a `baseline_created` event to the Railway logs and sends zero Telegram messages.

On the next cycle, the worker constructs the Telegram notifier and performs a normal live check.
Only listing ids that appeared after the baseline are detail-fetched and evaluated. An empty result
is logged with status `empty`; it is a healthy outcome.

## 4. Verify before handing over

Keep the existing local scheduler active during deployment so there is no monitoring gap. In the
Railway deployment logs, verify all of the following:

- one `baseline_created` event with a non-zero `discoveries_recorded` value;
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
- Website, parsing, SQLite, and delivery failures are logged. Three consecutive failed cycles make
  the process exit non-zero, after which Railway's `Always` policy restarts it.
- A failed Telegram delivery remains retryable and the discovery is not acknowledged as complete.
- Parser contract changes fail visibly instead of turning a changed page into a false empty result.
- Removing or replacing the volume creates a fresh database and therefore a fresh baseline. Treat
  that as a recovery operation, not routine deployment.

Use Railway volume backups before risky schema or source changes. SQLite database files and backups
contain private profile information in plaintext in this MVP and must be handled as personal data.
