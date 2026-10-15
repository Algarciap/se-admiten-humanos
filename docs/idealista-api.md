# Idealista Search API setup

The Idealista adapter uses the official Search API. It does not parse or scrape Idealista web
pages. Idealista currently asks developers to [request access](https://developers.idealista.com/)
and describe their project before it issues an API key and secret.

Suggested access-request description:

> Se admiten humanos is a free, open-source, non-commercial tool that helps renters find suitable
> long-term homes. We want to query new rental listings in Madrid, filter them using user-defined
> budgets and location preferences, classify only the pet-policy text supplied by the API, and send
> private Telegram alerts. We do not resell listing data, bypass access controls, or automate portal
> contact. We would like confirmation of the permitted request rate, daily quota, retention rules,
> and attribution requirements for the Search API.

Once access is approved, keep the credentials only in the ignored `.env` file:

```dotenv
IDEALISTA_API_KEY=
IDEALISTA_API_SECRET=
```

On Railway, add the same two values as sealed service variables. When both are present, the hosted
worker enables Idealista automatically. A partial credential pair stops startup visibly instead of
silently disabling the source.

Import at least one enabled search profile and run:

```powershell
python -m se_admiten_humanos run-idealista --database rentals.sqlite3
```

The adapter derives the API maximum price from the highest enabled search budget, then applies each
user's exact profile independently in the shared processing pipeline. It queries the official
`homes` category when any enabled search requests a full home and `bedrooms` when any search requests
a room. Garages and commercial units are never requested. Studios and listings whose bedroom count
is absent are not removed at acquisition time.

The hosted default runs Idealista at most once every 15 minutes and requests one page of 50 results
per requested category from the recent-rental window. The first response creates a no-alert baseline;
later responses process only newly seen property codes. Keep `IDEALISTA_MAX_PAGES=1` and adjust
`IDEALISTA_POLL_INTERVAL_SECONDS` only after Idealista confirms the account's quota and rate limits.
API errors, zero-result snapshots, and response-shape changes fail visibly rather than being treated
as an empty search.
