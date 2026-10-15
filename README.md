# Se admiten humanos

A free and open-source project for helping people find long-term or temporary rentals where every member of the
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

The code is licensed under the [GNU Affero General Public License v3 or later](LICENSE). That means
people may use, study, share, and modify it. If someone modifies the software and makes that version
available to users over a network, those users must also be offered access to its corresponding
source code. Like other open-source licenses, the AGPL permits commercial use; the commitments above
describe how this project itself is intended to operate.

## How it works

The project deliberately separates discovery, classification, notification, and outreach:

1. Source adapters look for new listings through the Idealista official API, permitted parsing of
   public agency websites, and native email alerts where they provide useful additional coverage.
2. Listings from every source are converted into the same normalized format.
3. Each listing's pet policy is classified as `accepted`, `conditional`, `not_specified`, or
   `rejected`, with the relevant phrase retained as evidence.
4. Listings are filtered against each user's search profile and deduplicated across sources.
5. Matching listings are sent to the correct private Telegram chat.

## Web onboarding

The Railway service now hosts a Spanish onboarding page as well as the background watcher. A person
can enter their own rent ceiling, Madrid districts, bedroom flexibility, furnishing preference,
rental duration (long-term, temporary, or both),
move-in needs, employment details, and animals. The form writes a user-owned renter and search
profile directly to the same SQLite database used by matching, then creates a ten-minute Telegram
connection link.

The first MVP uses an opaque, browser-bound session cookie: only its SHA-256 digest is stored, it is
`HttpOnly`, `Secure`, and `SameSite=Lax`, and state-changing forms require a separate CSRF token.
Returning with the same browser allows the person to edit their profile. There is deliberately no
pretence of a finished account system yet: email is not verified, and account recovery across devices
is the next identity milestone.

For a local HTTP preview only:

```powershell
$env:TELEGRAM_BOT_USERNAME='SeAdmitenHumanosBot'
$env:WEB_COOKIE_SECURE='false'
python -m se_admiten_humanos run-web
```

Production uses `run-service`, which serves HTTP on Railway's `PORT` while the single background
thread continues three-minute property checks. `/health` is the deployment health endpoint.
6. An approved outreach message is drafted. Automatic delivery is enabled only for explicitly
   allow-listed direct agency email channels.

Idealista API access and its request limits still need to be confirmed before that adapter can go
live. Website parsing is enabled source by source only where it is permitted and technically safe.
The project does **not** bypass CAPTCHAs or access controls, disguise abusive traffic, automate
personal WhatsApp accounts, or send messages through unsupported portal APIs.

## Current state

- Dependency-free Python core for classification, source recognition, contact safety, and outreach.
- Explainable Spanish/English pet-policy classifier.
- Configurable Spanish outreach-message generator.
- Multi-user renter and search profiles with structured pets and independent preferences.
- SQLite schema migrations, shared listing history, per-profile matches, and notification claims.
- End-to-end listing pipeline that classifies once and evaluates every enabled search independently.
- Auditable acquisition runner with strict normalized-JSON input, per-source execution locks, hard
  run limits, and explicit `succeeded`, `empty`, and `failed` outcomes.
- Credential-gated Idealista Search API adapter with OAuth, conservative pagination, strict response
  validation, and automatic maximum-price selection from enabled user searches.
- Baseline-safe, multi-source website watcher with live Alquiler Seguro, Tecnocasa, Redpiso, and Nestar
  adapters. Each uses a published sitemap for discovery and fetches detail pages only for unseen
  listing IDs; one broken parser cannot prevent the other sources from running.
- Machine-readable registry of portals, agencies, institutional landlords, and public housing sources.
- Safe direct-email gate: draft by default, allow-listed agency email only for automatic sending.
- Python regression tests plus source-registry validation.
- Tested Telegram delivery adapter with database-backed per-user destinations, atomic claims,
  retries, and an actionable listing button; different budgets route to different private chats.
- Railway-ready background worker with safe first-boot profile import, no-alert website baseline,
  three-minute polling, persistent SQLite state, and visible repeated-failure exits.
- Idealista API access and quota confirmation remain pending. Wolo, Testa Homes, and Vivenio remain
  in the registry but their background parsers are disabled until written permission is obtained. Fotocasa is deliberately
  excluded from the initial deployment.

## Run locally

Python 3.11 or newer is required.

```powershell
python -m unittest discover -s tests -v
"No se mencionan mascotas" | python -m se_admiten_humanos classify
python -m se_admiten_humanos validate-sources
python -m se_admiten_humanos import-profiles `
  --file config/profiles.local.json `
  --database rentals.sqlite3
python -m se-admiten-humanos run-json-source `
  --file examples/fixtures/madrid-telegram-test.json `
  --source-id manual-fixture `
  --database rentals.sqlite3
# After Idealista approves API access and credentials are in .env:
python -m se-admiten-humanos run-idealista --database rentals.sqlite3
# Once only: record current Alquiler Seguro stock without sending alerts:
python -m se_admiten_humanos baseline-alquiler-seguro --database rentals.sqlite3
# On later checks, process and alert only on unseen listing IDs:
python -m se_admiten_humanos run-alquiler-seguro --database rentals.sqlite3
```

The Python package is the single source of truth. The former Google Apps Script prototype and its
duplicated classifier, outreach, registry, and tests were removed before deployment. API, website,
and email source adapters all feed the same tested processing pipeline.

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

The `run-json-source` command exercises the complete path used by future live adapters: acquire,
normalize, classify, match, deduplicate, notify, and record the run. It loads `.env` when present and
has a default hard limit of 500 listings per execution. The committed fixture is unmistakably marked
as a test, but running it with live Telegram credentials sends a real message. Repeating it against
the same database does not send a duplicate.

The Idealista command uses only the official Search API and refuses to start without approved
credentials. It defaults to one page per run until the account-specific rate and daily limits are
known. See [`docs/idealista-api.md`](docs/idealista-api.md) before enabling it; the project does not
fall back to scraping Idealista pages.

The hosted worker uses published sitemaps for lightweight discovery on Alquiler Seguro, Tecnocasa,
Redpiso, and Nestar, then requests public detail pages only for new IDs. Each source receives its own
no-alert baseline, preventing a first-run flood. Empty or structurally changed pages fail visibly,
while the other sources still complete their checks. The separate Alquiler Seguro CLI remains useful
for local diagnostics. See
[`docs/alquiler-seguro.md`](docs/alquiler-seguro.md) before scheduling it.

## Run continuously on Railway

The initial hosted deployment uses one Railway service and one persistent volume. Its background
worker performs a check every three minutes while the same process serves web onboarding; it does not rely on Railway cron
and it does not require a personal computer to remain on. SQLite is appropriate for this single
worker, but the service must remain at one replica and the database must live on the mounted volume.

On a brand-new database, the worker imports the private profile from a sealed environment variable
and creates independent Alquiler Seguro, Tecnocasa, Redpiso, and Nestar baselines. That first cycle sends
zero messages. Later cycles process only newly discovered listing IDs through the normal classifier,
matching, deduplication, and Telegram pipeline. Configuration errors stop immediately; transient
failures are logged and the process exits after three consecutive failures so Railway can restart it
visibly.

See [`docs/railway.md`](docs/railway.md) for the exact volume, variables, deployment, verification,
and local-scheduler handover procedure. Never commit the Telegram token, chat id, private profile,
or SQLite database.

## Telegram delivery

Telegram is the first alert channel. Create a bot with BotFather, send it `/start`, and keep its
token outside Git. Telegram destinations are stored per application user in SQLite; the notifier
looks up the owner of each eligible search match and sends only to that user's enabled chat. A chat
cannot be linked to two application users, and one user's notification claim never suppresses a
different user's match.

`TELEGRAM_USER_ID` and `TELEGRAM_CHAT_ID` remain as a backward-compatible bootstrap for the first
deployed user. The worker imports that pair into the database only when that user has no destination;
it never overwrites a database-managed mapping. New users link their own private chat through a
single-use `/start` URL. The capability expires, is stored only as a SHA-256 digest, cannot be replayed,
and is accepted only in a private Telegram chat. The worker checkpoints Telegram update IDs in SQLite
so deploys and restarts do not process the same command twice.

After an authenticated application user exists, generate a ten-minute link with:

```powershell
python -m se_admiten_humanos create-telegram-link --user-id USER_ID
```

The command reads `TELEGRAM_BOT_USERNAME` (or accepts `--bot-username`) and prints the deep link once.
This is the service layer the future website form will call; the public form and authentication UI are
the next milestone, not part of this background worker.

Every alert labels the pet policy, shows the matching phrase (or clearly says it was not mentioned),
and includes an **Abrir anuncio** button. A delivery is claimed immediately before the API call,
marked `sent` only after Telegram confirms it, and marked `failed` on errors so a later run can retry.

Once `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are present in the process environment, verify the
connection without processing a listing:

```powershell
python -m se_admiten_humanos test-telegram
```

## Source policy

Each source in [`data/sources.json`](data/sources.json) declares its approved acquisition route.
Unknown or unsupported routes must fail closed: they may produce a browser bookmark or manual task,
but never background crawling.

See [`docs/source-strategy.md`](docs/source-strategy.md) for the prioritized Madrid source list.
