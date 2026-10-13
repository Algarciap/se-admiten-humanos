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
- Safe direct-email gate: draft by default, allow-listed agency email only for automatic sending.
- Python regression tests plus source-registry validation.
- Email ingestion, Telegram delivery, persistence, and live source adapters are the next implementation
  milestones; none are deployed yet.

## Run locally

Python 3.11 or newer is required.

```powershell
python -m unittest discover -s tests -v
"No se mencionan mascotas" | python -m se_admiten_humanos classify
python -m se_admiten_humanos validate-sources
```

The Python package is the single source of truth. The former Google Apps Script prototype and its
duplicated classifier, outreach, registry, and tests were removed before deployment. Email ingestion
will be implemented as a Python adapter so all inputs follow the same tested pipeline.

## Source policy

Each source in [`data/sources.json`](data/sources.json) declares its approved acquisition route.
Unknown or unsupported routes must fail closed: they may produce a browser bookmark or manual task,
but never background crawling.

See [`docs/source-strategy.md`](docs/source-strategy.md) for the prioritized Madrid source list.
