# Verified accounts

Account mode adds verified email, password login on multiple devices, email-based
password recovery, session revocation, personal-data export/deletion, alert pause,
and Telegram disconnection. It is opt-in while transactional email is being set up.
`ACCOUNTS_ENABLED=false` preserves the existing invited pilot. Do not invite the
public while that mode is active. This change does not enable Idealista or new sources.

## Configure email before rollout

Use Brevo's HTTPS transactional-email API on Railway Free, Trial and Hobby plans,
where outbound SMTP is blocked. Register and verify a sender address in Brevo, then
configure the API key as a Railway sealed variable. SMTP remains available for
Railway Pro or other hosts. Set up domain verification, SPF and DKIM before a public
rollout. Never paste passwords or mail capabilities into a chat or logs.

| Variable | Purpose |
| --- | --- |
| `ACCOUNTS_ENABLED` | `true` enables verified account routes and requires complete mail settings |
| `PUBLIC_BASE_URL` | Canonical website origin, such as `https://se-admiten-humanos-production.up.railway.app`; no path, query, or credentials |
| `BREVO_API_KEY` | Preferred Railway transport; sends through Brevo's HTTPS API and takes precedence over SMTP |
| `SMTP_HOST` | Provider's SMTP hostname |
| `SMTP_PORT` | Defaults to `587` |
| `SMTP_FROM` | Verified sender email address, without a display-name wrapper |
| `SMTP_USERNAME`, `SMTP_PASSWORD` | Authentication pair; both present or both absent for an explicitly supported authenticated-network relay |
| `SMTP_IMPLICIT_TLS` | `false`: mandatory STARTTLS; `true`: TLS from connection start, normally with port `465` |
| `WEB_COOKIE_SECURE` | Keep `true` for production |

Mail never falls back to plaintext transport or printing links. The public origin
comes from configuration, never the request's Host header. Mail failure logs contain
only `account_email_delivery_failed`; the public request response is generic to
avoid disclosing account existence. Monitor this event and the SMTP provider's
delivery/bounce dashboard during rollout. Failed mail can be retried by requesting
another link after the request limit resets. No durable mail queue is included.

Do not enable account mode in production until the following review is complete:

1. Take a volume backup and verify the restoration procedure. Schema 12 adds account
   tables without rewriting existing users or search profiles. Older releases must
   not run against schema 12. A rollback needs a coordinated database/code restore.
2. Verify the email sender and test delivery with a separate local/staging database.
3. Check that reverse-proxy, platform HTTP logs, monitoring and analytics do not
   retain query strings for `/account/verify` and `/account/reset`. Links contain
   short-lived credentials. The application request logger removes query strings,
   responses use `no-store` and `no-referrer`, and token GETs perform no mutation.
4. Keep one replica and `/data/rentals.sqlite3`. Enable account mode only with SMTP
   configured, then verify `/health`, signup, email delivery, verification, login in
   another browser, recovery, profile editing and Telegram delivery.
5. Test pause, disconnect, JSON export and deletion using disposable accounts.
   Resetting a password or verifying a new account revokes all prior sessions;
   login rotates the browser session and CSRF cookie. Check both old browsers.
6. Do not turn account mode off as a routine fallback after rollout: it reopens
   anonymous pilot onboarding. Fix email/configuration while retaining the gate.

## Existing invited users

With the original browser session, open **Mi cuenta → Vincular esta búsqueda**.
Request a verification email, then follow the link and choose a password. The
verified identity remains attached to the same application user, searches, Telegram
destination and notification history. Existing sessions are revoked after verification.

A legacy email address alone is never enough to claim a profile. A person without
their old browser session can create a new account, but automatic recovery of an
unverified legacy profile is deliberately unavailable. An existing account at the
chosen email is never merged with another user's search.

The verified login email cannot be changed through the search form. A dedicated
verified email-change flow is not included. Password changes use the recovery flow.

## Security and data lifecycle

- Passwords use salted PBKDF2-HMAC-SHA256 with 600,000 iterations. New passwords
  must contain 12–128 characters. Passwords and raw session/mail capabilities are
  never stored in SQLite or included in exports.
- Verification and reset links last 30 minutes and are single-use. A newer link
  replaces previous links of the same purpose. Their consumption is transactional.
  Email scanners may open a link safely: consumption requires a CSRF-protected POST
  and a new password. Verification chooses the password only after email possession
  is proven, preventing a pending-signup password from being preselected by an attacker.
- Account POSTs have persistent fixed-window limits: 60 attempts per direct network
  peer and 8 per normalized email across login/signup/recovery, per 15 minutes.
  Forwarded headers are not trusted. If Railway presents a shared proxy peer, the
  peer limit is shared; verify behavior and introduce a trusted proxy policy and
  stronger abuse controls before a broad launch. These limits are suitable for a
  controlled rollout, not a replacement for operational abuse protection.
- State-changing account actions use the existing same-origin CSRF protection.
  Export, deletion and logout-all also require the current password. Deletion
  requires typing `ELIMINAR`. The application export contains the current user's
  data, including their Telegram destination and evaluated listings, and no other
  user's profile or credentials. Store downloaded exports privately.
- Alert pause suppresses Telegram delivery without changing search preferences or
  deduplication state. Resume applies to future discoveries, not a historical replay.
  An already in-flight delivery may finish before a preference change takes effect.
- Disconnect deletes the chat destination and pending Telegram links. Reconnection
  requires a new explicit single-use link. A hashed bootstrap block prevents legacy
  environment variables from reconnecting the destination automatically.
- Deletion cascades through account credentials, tokens, sessions, renter profiles,
  pets, searches, personal matches and notification state. Shared public listing
  inventory remains available to other users. A hashed application-user bootstrap
  block prevents the legacy setup from reimporting the deleted profile. It stores
  neither the email, profile nor chat ID. The initial bootstrap is no longer used
  when account records or these blocks exist.
- Existing delivered Telegram messages are outside application deletion. Backups
  retain older data until their configured expiry; restoring one can restore deleted
  records, so operator restoration must reconcile deletions before restarting alerts.
  Profile fields remain plaintext in SQLite. Account authentication does not provide
  storage encryption or replace the planned privacy and scaling work.

Automated tests use temporary databases and mocked mail; they send no real email
or Telegram messages and never open a private production database.
