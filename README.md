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
- Multi-user renter and search profiles with structured pets and independent preferences.
- SQLite schema migrations, shared listing history, per-profile matches, and notification claims.
- Machine-readable registry of portals, agencies, institutional landlords, and public housing sources.
- Safe direct-email gate: draft by default, allow-listed agency email only for automatic sending.
- Python regression tests plus source-registry validation.
- Email ingestion, Telegram delivery, and live source adapters are the next implementation milestones;
  none are deployed yet.

## Run locally

Python 3.11 or newer is required.

```powershell
python -m unittest discover -s tests -v
"No se mencionan mascotas" | python -m se_admiten_humanos classify
python -m se_admiten_humanos validate-sources
python -m se_admiten_humanos import-profiles `
  --file config/profiles.local.json `
  --database rentals.sqlite3
```

The Python package is the single source of truth. The former Google Apps Script prototype and its
duplicated classifier, outreach, registry, and tests were removed before deployment. Email ingestion
will be implemented as a Python adapter so all inputs follow the same tested pipeline.

Search values are no longer application defaults. Each user can own multiple renter profiles and
multiple searches with different cities, districts, budgets, bedroom rules, furnishing, dates, and
pet requirements. Listings are stored once globally; matching and notification state are isolated
per search profile so one person's alert never suppresses another person's.

Copy `config/profiles.local.example.json` to `config/profiles.local.json` for private local setup.
The local file is ignored by Git because it may contain names, income, employment, and pet details.
The SQLite database is also ignored and should be treated as private because this development version
stores those fields in plaintext. The future website will replace the JSON import with authenticated
forms and encrypted production storage.

## Source policy

Each source in [`data/sources.json`](data/sources.json) declares its approved acquisition route.
Unknown or unsupported routes must fail closed: they may produce a browser bookmark or manual task,
but never background crawling.

See [`docs/source-strategy.md`](docs/source-strategy.md) for the prioritized Madrid source list.
