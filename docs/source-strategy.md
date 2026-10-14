# Madrid source strategy

Last reviewed on 2026-10-14. This is an operational registry, not an endorsement of any company.

## First wave

Configure these immediately because they offer native alerts, strong pet signals, or meaningful
first-party Madrid inventory:

| Source | Why it matters | Safe acquisition route | Pet signal |
|---|---|---|---|
| Idealista | Largest general portal | Official API when access is approved; immediate alert as fallback | Description; unknown is not acceptance |
| Fotocasa | Major general portal | Native alert plus pet-filtered search | Explicit pet filter |
| Pisos.com | Major general portal | Native alert if available | Description |
| Yaencontre | Major general portal | Personalized email alert | Description |
| Alquiler Seguro | Rental specialist | Daily email alert | Description or written agent confirmation |
| Vivenio | First-party landlord | Direct inventory/contact | Company says it is pet-friendly |
| Testa Homes | First-party landlord | Pet filter plus direct contact | Explicit `Acepta mascotas` field |
| Agencia Negociadora del Alquiler | Madrid rental specialist | Agency site plus its portal profile | Confirm per property |
| Tecnocasa | Large local-office network | Search plus selected district offices | Confirm per property |
| Redpiso | Large Madrid network | Search plus listing-level contact | Confirm per property |
| Gilmar | Dedicated Madrid rental office | Saved search/direct email | Confirm per property |
| Engel & Völkers | Hundreds of Madrid rentals | Native saved-search alert | Description |

## Second wave

- Nestar, Fidere/Encaja tu alquiler, and Avalon/Plan Vive for direct or affordable portfolios.
- RE/MAX, aProperties, and Vivienda2 for agency-owned inventory.
- Habitaclia and Indomio for incremental portal coverage.
- Plan Alquila as a parallel public route.

## Conditional sources

- Promora and similar premium agencies should only be enabled when the configured maximum rent overlaps
  their inventory.
- Milanuncios should remain manual until fraud and contact safeguards are implemented.
- Room and temporary-stay platforms should be a separate search profile; mixing them with long-term
  homes makes alerts noisy and can hide extra fees or duration restrictions.

## Acquisition rules

1. Prefer an official API, RSS feed, native saved-search alert, or written permission.
2. Public agency and direct-owner inventory without a timely API or alert may receive a dedicated,
   rate-limited parser only after its terms, robots policy, and technical behavior have been reviewed.
3. Never bypass CAPTCHAs, anti-bot controls, authentication boundaries, or robots restrictions.
4. Never infer that a property accepts cats merely because its description is silent.
5. Automatic email is limited to allow-listed agency domains and is disabled by default.
6. Portal chat and personal WhatsApp remain confirmation-based unless an official integration permits
   automation.
7. Keep one contact record per real property, even if it is cross-posted on several sites.

## Direct-source observations

- Alquiler Seguro exposes configurable daily emails for matching homes.
- Testa Homes exposes an `Acepta mascotas` characteristic in its first-party inventory.
- Vivenio states in its FAQ that it accepts pets subject to community rules and focuses on long-term
  rental.
- Engel & Völkers exposes a saved-search alert from its Madrid results.
- Gilmar has a dedicated Madrid rental office and a save action in its search.
- Redpiso exposes office-level phone, email, and sometimes WhatsApp contact on listings.

These signals should be rechecked periodically because websites and policies change.

The credential-gated Idealista Search API adapter is implemented. Until access and quotas are
confirmed, it must not be scheduled; its native immediate alert remains the operational fallback.
See [`idealista-api.md`](idealista-api.md) for the access request and conservative run settings.

[Vivenio's current terms](https://www.vivenio.com/terminos-condiciones) restrict monitoring and
scraping in commercial or competitive contexts and require permission for deep linking. It remains
manual until written permission is obtained; the project does not infer that public visibility alone
authorizes a background parser.

[Testa Homes' current conditions](https://www.testahomes.com/condiciones-de-uso/) do not provide a
clear grant for recurring automated extraction. Its pet filter remains valuable for manual searches,
but a background adapter also waits for written permission.
