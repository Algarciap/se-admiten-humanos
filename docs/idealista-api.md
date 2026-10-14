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

Import at least one enabled search profile and run:

```powershell
python -m se_admiten_humanos run-idealista --database rentals.sqlite3
```

The adapter derives the API maximum price from the highest enabled search budget, then applies each
user's exact profile independently in the shared processing pipeline. Studios and listings whose
bedroom count is absent are not removed at acquisition time.

The default run requests at most one page of 50 homes published in the API's recent-rental window.
Do not schedule recurring runs or raise `--max-pages` until Idealista confirms the account's quota
and rate limits. API errors and response-shape changes are stored as failed source runs rather than
being treated as an empty search.
