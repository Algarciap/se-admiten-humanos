# Se admiten humanos

Open-source tooling for finding long-term rentals where every member of the family is welcome,
including pets.

The project deliberately separates discovery, classification, notification, and outreach:

1. Portal or agency native alerts arrive by email.
2. The email is classified as `accepted`, `conditional`, `not_specified`, or `rejected`.
3. Matching listings are deduplicated and sent to a private Telegram chat.
4. An approved outreach message is drafted. Automatic delivery is enabled only for explicitly
   allow-listed direct agency email channels.

The project does **not** crawl portals, bypass CAPTCHAs, automate personal WhatsApp accounts, or send
messages through unsupported portal APIs.

## Current state

- Dependency-free Python core for classification, source recognition, contact safety, and outreach.
- Explainable Spanish/English pet-policy classifier.
- Configurable Spanish outreach-message generator.
- Machine-readable registry of portals, agencies, institutional landlords, and public housing sources.
- Existing Google Apps Script prototype retained as an optional Gmail bridge during the Python migration.
- Safe direct-email gate: draft by default, allow-listed agency email only for automatic sending.
- Python and JavaScript regression tests plus source-registry validation.

## Run locally

Python 3.11 or newer is required for the core. Node.js 22 or newer is needed only for the retained
Apps Script regression suite.

```powershell
python -m unittest discover -s tests -v
"No se mencionan mascotas" | python -m se_admiten_humanos classify

# Optional regression checks for the Apps Script bridge
npm test
npm run validate:sources
```

The Python package is the source of truth for new backend work. The JavaScript implementation remains
temporarily so the Gmail bridge can continue to be tested while source adapters move to Python.

## Gmail labels

Create these labels in the dedicated Gmail account:

- `rent-alerts`
- `rent-alerts/processed`
- `rent-alerts/error`

Portal and agency alert messages should be filtered into `rent-alerts`. The script adds the processed
or error label without changing the user's read/unread state.

## Apps Script properties

Set these in Apps Script project properties; never commit them:

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `OUTREACH_MODE`: `draft` or `auto_email`
- `OUTREACH_FROM_NAME`
- `RENTER_NAME`
- `RENTER_MONTHLY_INCOME`
- `RENTER_JOB`
- `RENTER_PETS`
- `AUTO_EMAIL_DOMAIN_ALLOWLIST`: comma-separated agency domains

Run `installRentAlertTrigger()` once after reviewing the requested Gmail permissions. It installs a
one-minute trigger. `OUTREACH_MODE=draft` is the safe default. Outreach is prepared only when the
recipient's domain is explicitly included in `AUTO_EMAIL_DOMAIN_ALLOWLIST`; unknown addresses fail
closed.

## Source policy

Each source in [`data/sources.json`](data/sources.json) declares its approved acquisition route.
Unknown or unsupported routes must fail closed: they may produce a browser bookmark or manual task,
but never background crawling.

See [`docs/source-strategy.md`](docs/source-strategy.md) for the prioritized Madrid source list.
