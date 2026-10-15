# Madrid source strategy

Last reviewed on 2026-10-15. This is an operational registry, not an endorsement of any company.

## First wave

Configure these immediately because they offer native alerts, strong pet signals, or meaningful
first-party Madrid inventory:

| Source | Why it matters | Safe acquisition route | Pet signal |
|---|---|---|---|
| Idealista | Largest general portal | Official API when access is approved; immediate alert as fallback | Description; unknown is not acceptance |
| Pisos.com | Major general portal | Native alert if available | Description |
| Yaencontre | Major general portal | Personalized email alert | Description |
| Alquiler Seguro | Rental specialist | Public sitemap watcher; daily email only as fallback | Description or written agent confirmation |
| Wolo | First-party rental agency | Manual pet-filter watch and direct visit booking until written parser permission | Explicit pet filter and listing text |
| Vivenio | First-party landlord | Direct inventory/contact | Company says it is pet-friendly |
| Testa Homes | First-party landlord | Pet filter plus direct contact | Explicit `Acepta mascotas` field |
| Agencia Negociadora del Alquiler | Madrid rental specialist | Agency site plus its portal profile | Confirm per property |
| Tecnocasa | Large local-office network | Published compressed property sitemaps plus unseen public details | Confirm per property |
| Redpiso | Large Madrid network | Published property sitemap plus unseen public details | Confirm per property |
| Nestar | First-party landlord | Published property sitemap plus unseen public details | Confirm per property |
| Gilmar | Dedicated Madrid rental office | Saved search/direct email | Confirm per property |
| Engel & Völkers | Hundreds of Madrid rentals | Native saved-search alert | Description |

## Second wave

- Fidere/Encaja tu alquiler and Avalon/Plan Vive for direct or affordable portfolios.
- RE/MAX, aProperties, and Vivienda2 for agency-owned inventory.
- Habitaclia and Indomio for incremental portal coverage.
- Plan Alquila as a parallel public route.

Fotocasa is excluded from the initial deployment by product choice. It remains documented for future
users, but no Fotocasa alert or parser is enabled.

## Conditional sources

- Promora and similar premium agencies should only be enabled when the configured maximum rent overlaps
  their inventory.
- Milanuncios should remain manual until fraud and contact safeguards are implemented.
- Room platforms remain a separate product category. Temporary and long-term homes are normalized
  explicitly and selected per search profile; adverts that do not state a duration stay labelled
  “not specified.”

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

- Alquiler Seguro publishes a dynamic sitemap of public listing URLs. The implemented watcher uses
  it for discovery, fetches detail pages only for unseen IDs, and treats the daily email as fallback.
- Tecnocasa publishes compressed property sitemaps. The adapter keeps only residential rentals in
  Madrid city and reads its server-rendered listing object without calling private API routes.
- Redpiso publishes a property sitemap and schema.org listing data. The adapter keeps residential
  long-term and temporary Madrid-region rentals; each user's duration, municipality, and budget
  filters are applied later.
- Nestar publishes property details in a first-party sitemap and server-rendered Next.js data. The
  adapter distinguishes rentals from sales, fetches only unseen details, and applies user filters
  downstream.
- Wolo exposes an `Acepta mascotas` filter and states accepted, rejected, or pending pet status in
  individual listings. It also supports direct visit booking, making it a high-priority manual source.
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

The Alquiler Seguro adapter is implemented with an explicit first-run baseline, a 2 MB response
limit, strict first-party URL checks, and a delay between new detail-page requests. It does not use
the site's disallowed `/api/` paths or filtered query URLs. See
[`alquiler-seguro.md`](alquiler-seguro.md).

[Vivenio's current terms](https://www.vivenio.com/terminos-condiciones) restrict monitoring and
scraping in commercial or competitive contexts and require permission for deep linking. It remains
manual until written permission is obtained; the project does not infer that public visibility alone
authorizes a background parser.

[Testa Homes' current conditions](https://www.testahomes.com/condiciones-de-uso/) do not provide a
clear grant for recurring automated extraction. Its pet filter remains valuable for manual searches,
but a background adapter also waits for written permission.

[Wolo's current legal notice](https://www.wolorent.com/aviso-legal) requires prior written
authorization for reproduction or reuse of its site content. Its first-party search and pet filter
remain valuable for manual use, but a recurring parser must wait for written permission.

## Permission requests for restricted sources

The project is free, open source, non-commercial, rate-limited, and sends users back to the original
advert. Before enabling Testa Homes, Vivenio, or Wolo, request written permission to:

- poll public rental inventory no more often than every three minutes;
- retain only listing identity and normalized matching fields;
- show a short evidence phrase and deep link in a private Telegram alert;
- avoid images, authentication, private APIs, CAPTCHAs, and contact automation;
- identify the bot with the public repository URL and stop promptly on request.

Suggested Spanish request:

> Estamos desarrollando “Se admiten humanos”, un proyecto gratuito, abierto y sin ánimo de lucro que
> ayuda a personas a encontrar vivienda compatible con sus animales. Solicitamos autorización escrita
> para consultar de forma limitada el inventario público, como máximo cada tres minutos, y enviar a
> cada usuario únicamente coincidencias con un enlace al anuncio original. No copiaríamos imágenes,
> no accederíamos a zonas privadas ni automatizaríamos contactos o reservas. ¿Podrían autorizarnos e
> indicarnos el método técnico y los límites que prefieren?

Agencia Negociadora del Alquiler does not currently publish individual inventory on its own public
site. Its portal stock should be covered by the official Idealista API when credentials are approved;
Fotocasa parsing remains excluded by product choice.
