# Se admiten humanos

A free and open-source project for helping people find long-term rentals where every member of the
household—including pets—is welcome.

## Why this exists

Finding somewhere to live can become an exhausting race. Affordable homes receive attention almost
immediately, the same property appears on several portals, listings disappear quickly, alerts can
arrive too late, and pet policies are often missing or buried in free text.

This project began while I was helping someone close to me whose living situation had suddenly
become untenable. She needed to find a home in Madrid quickly, depended on public transport, and
would be moving with two cats. At a moment that was already emotionally difficult, repeatedly
refreshing property portals, comparing duplicated adverts, and asking the same question about pets
again and again became another full-time burden.

`Se admiten humanos` cannot create more housing or make rents affordable. Its purpose is narrower
and practical: reduce repetitive searching, detect promising listings sooner, explain what each
advert actually says about pets, and help someone respond quickly—without ever pretending that
silence about pets means acceptance.

## Free and open source

I built this to help people, not to make money from a difficult housing situation. The project is
intended to be free and open source so that other people can inspect it, run it themselves, improve
it, and adapt it to their own housing search.

There are no subscriptions, referral commissions, affiliate links, paid listings, lead sales, or
personal-data resale. No agency or landlord can pay to appear first. The goal is simply to make a
stressful and unequal search a little more manageable.

## How it works

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
- End-to-end listing pipeline that classifies once and evaluates every enabled search independently.
- Machine-readable registry of portals, agencies, institutional landlords, and public housing sources.
- Safe direct-email gate: draft by default, allow-listed agency email only for automatic sending.
- Python regression tests plus source-registry validation.
- Email ingestion, Telegram delivery, and live source adapters remain undeployed.

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

Source adapters should pass normalized listings to `ListingPipeline.process()`. The pipeline returns
profile-specific delivery candidates but does not claim or send them; a notification adapter must
atomically call `claim_notification()` immediately before delivery.

## Source policy

Each source in [`data/sources.json`](data/sources.json) declares its approved acquisition route.
Unknown or unsupported routes must fail closed: they may produce a browser bookmark or manual task,
but never background crawling.

See [`docs/source-strategy.md`](docs/source-strategy.md) for the prioritized Madrid source list.
