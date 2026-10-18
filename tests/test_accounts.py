"""Account lifecycle regressions with in-memory mail transport and temporary data."""

import io
import json
import tempfile
import unittest
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from http.cookies import SimpleCookie
from pathlib import Path
from unittest.mock import patch, MagicMock
from urllib.parse import urlencode, urlsplit

from se_admiten_humanos.accounts import Accounts, AccountMailError, MailSettings, digest, normalize_email, password_hash, password_matches, send_account_email
from se_admiten_humanos.models import Listing, PropertyType, RentalDuration, User
from se_admiten_humanos.persistence import RentalStore
from se_admiten_humanos.pipeline import ListingPipeline
from se_admiten_humanos.search_profile import SearchProfile
from se_admiten_humanos.service import web_settings_from_environment, QuietRequestHandler
from se_admiten_humanos.telegram import TelegramNotifier, notifier_from_environment
from se_admiten_humanos.web import WebApplication, WebSettings
from se_admiten_humanos.worker import _ensure_profiles
import test_web


PASSWORD = "a long test password"
MAIL = MailSettings("https://humanos.example", "smtp.example", "accounts@example.com")


class AccountTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = str(Path(self.temp.name) / "test.sqlite3")
        self.app = WebApplication(WebSettings(self.database, "ExampleBot", secure_cookies=False, accounts_enabled=True, mail=MAIL))
        self.cookies = {}
        self.messages = []
        self.mail_patch = patch("se_admiten_humanos.account_web.send_account_email", side_effect=lambda *args: self.messages.append(args))
        self.mail_patch.start()
        self.request("GET", "/account/register")

    def tearDown(self):
        self.mail_patch.stop()
        self.temp.cleanup()

    def request(self, method, path, form=None, jar=None, csrf=True):
        jar = self.cookies if jar is None else jar
        form = dict(form or {})
        if csrf and method == "POST":
            form.setdefault("csrf_token", jar.get("humanos_csrf", ""))
        data = urlencode(form).encode()
        target = urlsplit(path)
        response = {}
        def start(status, headers):
            response.update(status=status, headers=headers)
        body = b"".join(self.app({
            "REQUEST_METHOD": method, "PATH_INFO": target.path, "QUERY_STRING": target.query,
            "HTTP_COOKIE": "; ".join(f"{k}={v}" for k, v in jar.items()),
            "CONTENT_TYPE": "application/x-www-form-urlencoded", "CONTENT_LENGTH": str(len(data)),
            "wsgi.input": io.BytesIO(data), "REMOTE_ADDR": "127.0.0.1",
        }, start)).decode()
        for name, value in response["headers"]:
            if name == "Set-Cookie":
                parsed = SimpleCookie(value)
                for key, morsel in parsed.items():
                    if morsel["max-age"] == "0":
                        jar.pop(key, None)
                    else:
                        jar[key] = morsel.value
        return response["status"], response["headers"], body

    def register(self, email="ana@example.com"):
        self.request("POST", "/account/register", {"email": email})
        token = self.messages[-1][3]
        status, _, _ = self.request("POST", "/account/verify", {"token": token, "password": PASSWORD})
        self.assertEqual(status, "303 See Other")
        with RentalStore(self.database) as store:
            return store.web_session_user_id(digest(self.cookies["humanos_session"]))

    def test_login_explains_project_before_account_actions(self):
        status, _, body = self.request("GET", "/account/login")

        self.assertEqual(status, "200 OK")
        intro = body.index('class="account-project-intro"')
        form = body.index('<form method="post" action="/account/login"')
        create_account = body.index('href="/account/register"')
        self.assertLess(intro, form)
        self.assertLess(form, create_account)
        self.assertIn("revisa anuncios nuevos, elimina duplicados y aplica tus criterios", body)
        self.assertIn("no hay suscripciones, comisiones, anuncios pagados ni venta de datos personales", body)

    def test_signed_in_account_uses_wide_landing_page_rhythm(self):
        self.register()
        status, _, body = self.request("GET", "/account")

        self.assertEqual(status, "200 OK")
        self.assertIn('account-page account-dashboard', body)
        self.assertIn(".account-dashboard .success{width:100%;max-width:none;text-align:left", body)
        self.assertIn(".account-dashboard .success>form{margin:24px 0 0}", body)

    def test_verification_required_get_does_not_consume_and_session_is_hashed(self):
        self.assertEqual(self.request("GET", "/")[0], "303 See Other")
        self.request("POST", "/account/register", {"email": " ANA@example.com "})
        token = self.messages[-1][3]
        self.assertEqual(self.request("POST", "/account/login", {"email": "ana@example.com", "password": PASSWORD})[0], "401 Unauthorized")
        self.assertEqual(self.request("GET", "/account/verify?token=" + token)[0], "200 OK")
        with RentalStore(self.database) as store:
            stored = store.connection.execute("select token_hash from account_tokens").fetchone()[0]
            self.assertEqual(stored, digest(token))
            self.assertNotEqual(stored, token)
        self.assertEqual(self.request("POST", "/account/verify", {"token": token, "password": PASSWORD})[0], "303 See Other")
        self.assertEqual(self.request("POST", "/account/verify", {"token": token, "password": PASSWORD})[0], "400 Bad Request")
        with RentalStore(self.database) as store:
            row = store.connection.execute("select token_hash from web_sessions").fetchone()
            self.assertEqual(row[0], digest(self.cookies["humanos_session"]))
        self.assertEqual(self.request("GET", "/")[0], "200 OK")

    def test_cross_device_login_reset_revokes_all_sessions_and_old_password(self):
        self.register()
        first_jar = dict(self.cookies)
        second = {}
        self.request("GET", "/account/login", jar=second)
        self.assertEqual(self.request("POST", "/account/login", {"email": "ana@example.com", "password": PASSWORD}, jar=second)[0], "303 See Other")
        self.request("POST", "/account/recover", {"email": "ana@example.com"})
        token = self.messages[-1][3]
        new_password = "another long password"
        self.assertEqual(self.request("POST", "/account/reset", {"token": token, "password": new_password})[0], "303 See Other")
        self.assertEqual(self.request("GET", "/", jar=first_jar)[0], "303 See Other")
        self.assertEqual(self.request("GET", "/", jar=second)[0], "303 See Other")
        self.assertEqual(self.request("POST", "/account/login", {"email": "ana@example.com", "password": PASSWORD})[0], "401 Unauthorized")
        self.assertEqual(self.request("POST", "/account/login", {"email": "ana@example.com", "password": new_password})[0], "303 See Other")

    def test_existing_email_does_not_claim_legacy_profile(self):
        with RentalStore(self.database) as store:
            store.save_user(User("legacy", "Legacy", "ana@example.com"))
        new_id = self.register()
        self.assertNotEqual(new_id, "legacy")
        with RentalStore(self.database) as store:
            self.assertIsNone(Accounts(store).account("legacy"))

    def test_legacy_browser_claim_preserves_user_and_revokes_legacy_sessions(self):
        raw = "legacy-browser-secret"
        with RentalStore(self.database) as store:
            store.save_user(User("legacy", "Legacy", "old@example.com"))
            store.save_web_session(digest(raw), "legacy", datetime.now(UTC) + timedelta(days=1))
        self.cookies["humanos_session"] = raw
        user_id = self.register()
        self.assertEqual(user_id, "legacy")
        with RentalStore(self.database) as store:
            self.assertIsNone(store.web_session_user_id(digest(raw)))
            self.assertEqual(store.load_user("legacy").email, "ana@example.com")

    def test_legacy_session_can_correct_pending_email_without_leaving_old_link_valid(self):
        with RentalStore(self.database) as store:
            store.save_user(User("legacy", "Legacy"))
            accounts = Accounts(store)
            old = accounts.issue("typo@example.com", "verify", "legacy")
            corrected = accounts.issue("ana@example.com", "verify", "legacy")
            self.assertIsNone(accounts.consume(old, "verify", PASSWORD))
            self.assertEqual(accounts.consume(corrected, "verify", PASSWORD), "legacy")
            self.assertIsNone(accounts.issue("another@example.com", "verify", "legacy"))

    def test_unknown_recovery_and_duplicate_registration_have_generic_response(self):
        self.register()
        known = self.request("POST", "/account/recover", {"email": "ana@example.com"})
        count = len(self.messages)
        unknown = self.request("POST", "/account/recover", {"email": "missing@example.com"})
        self.assertEqual(known, unknown)
        self.assertEqual(len(self.messages), count)
        self.request("POST", "/account/register", {"email": "ana@example.com"})
        self.assertEqual(len(self.messages), count)

    def test_mail_failure_keeps_retry_and_recovery_available_without_leaking_account(self):
        with patch("se_admiten_humanos.account_web.send_account_email", side_effect=AccountMailError("private")):
            with self.assertLogs("se_admiten_humanos.account_web", level="ERROR") as logs:
                status, _, body = self.request("POST", "/account/register", {"email": "ana@example.com"})
        self.assertEqual(status, "200 OK")
        self.assertIn("Volver a solicitar el enlace", body)
        self.assertIn("Corregir correo", body)
        self.assertNotIn("private", body)
        self.assertNotIn("ana@example.com", str(logs.output))

    def test_telegram_status_requires_session_and_uses_enabled_destination(self):
        self.assertEqual(self.request("GET", "/telegram-status")[0], "303 See Other")
        user_id = self.register()
        self.assertIn("Conexión pendiente", self.request("GET", "/telegram-status")[2])
        with RentalStore(self.database) as store:
            store.save_telegram_destination(user_id, "123456")
        self.assertIn("Telegram conectado", self.request("GET", "/telegram-status")[2])
        self.assertNotIn("Conectar Telegram", self.request("GET", "/account")[2])
        self.request("POST", "/account/disconnect")
        self.assertIn("Conexión pendiente", self.request("GET", "/telegram-status")[2])

    def test_csrf_and_rate_limit_survive_new_application_instance(self):
        self.assertEqual(self.request("POST", "/account/register", {"email": "ana@example.com"}, csrf=False)[0], "403 Forbidden")
        for _ in range(8):
            self.assertEqual(self.request("POST", "/account/recover", {"email": "missing@example.com"})[0], "200 OK")
        self.app = WebApplication(self.app.settings)
        self.assertEqual(self.request("POST", "/account/recover", {"email": "missing@example.com"})[0], "429 Too Many Requests")

    def test_expired_links_and_previous_links_fail(self):
        with RentalStore(self.database) as store:
            accounts = Accounts(store)
            old = accounts.issue("ana@example.com", "verify")
            current = accounts.issue("ana@example.com", "verify")
            self.assertIsNone(accounts.consume(old, "verify", PASSWORD))
            with store.connection:
                store.connection.execute("update account_tokens set expires_at = ?", ((datetime.now(UTC) - timedelta(minutes=1)).isoformat(),))
            self.assertIsNone(accounts.consume(current, "verify", PASSWORD))

    def test_one_capability_cannot_be_consumed_by_two_requests(self):
        with RentalStore(self.database) as store:
            token = Accounts(store).issue("ana@example.com", "verify")
        def consume(_):
            with RentalStore(self.database) as store:
                return Accounts(store).consume(token, "verify", PASSWORD)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(consume, range(2)))
        self.assertEqual(sum(value is not None for value in results), 1)

    def test_populated_schema_11_upgrade_preserves_legacy_sessions_and_profiles(self):
        raw = "legacy-session"
        with RentalStore(self.database) as store:
            store.save_user(User("legacy", "Legacy"))
            store.save_search_profile(SearchProfile("legacy-search", "legacy", "Madrid", 1100, "Madrid"))
            store.save_web_session(digest(raw), "legacy", datetime.now(UTC) + timedelta(days=1))
            store.connection.executescript("""
                drop table account_tokens;
                drop table accounts;
                drop table auth_limits;
                drop table bootstrap_blocks;
                pragma user_version = 11;
            """)
        with RentalStore(self.database) as store:
            self.assertEqual(store.connection.execute("pragma user_version").fetchone()[0], 13)
            self.assertEqual(store.web_session_user_id(digest(raw)), "legacy")
            self.assertEqual(store.load_primary_search_profile("legacy").max_rent_eur, 1100)
            self.assertIsNone(Accounts(store).account("legacy"))

    def test_deleting_last_account_does_not_reimport_environment_profile(self):
        user_id = self.register()
        with RentalStore(self.database) as store:
            Accounts(store).delete(user_id)
            self.assertEqual(_ensure_profiles(store, {"PROFILE_BUNDLE_JSON": "must not be imported"}), 0)
            self.assertEqual(store.connection.execute("select count(*) from users").fetchone()[0], 0)

    def test_profile_edit_cannot_change_verified_identity_or_unpause_alerts(self):
        user_id = self.register()
        self.request("POST", "/account/pause", {"paused": "1"})
        form = test_web.WebTests().valid_form(self.cookies["humanos_csrf"])
        form["email"] = "attacker@example.com"
        self.assertEqual(self.request("POST", "/profile", form)[0], "200 OK")
        with RentalStore(self.database) as store:
            self.assertEqual(store.load_user(user_id).email, "ana@example.com")
            self.assertTrue(Accounts(store).account(user_id)[4])

    def test_pause_disconnect_do_not_allow_legacy_reconnect(self):
        user_id = self.register()
        with RentalStore(self.database) as store:
            store.save_telegram_destination(user_id, "111111")
        self.request("POST", "/account/pause", {"paused": "1"})
        with RentalStore(self.database) as store:
            self.assertIsNone(store.telegram_chat_id(user_id))
        self.request("POST", "/account/pause", {"paused": "0"})
        with RentalStore(self.database) as store:
            self.assertEqual(store.telegram_chat_id(user_id), "111111")
            from se_admiten_humanos.telegram_linking import create_telegram_link
            link = create_telegram_link(store, user_id, "ExampleBot")
        self.request("POST", "/account/disconnect")
        with RentalStore(self.database) as store:
            notifier_from_environment(store, {"TELEGRAM_BOT_TOKEN": "123:test", "TELEGRAM_USER_ID": user_id, "TELEGRAM_CHAT_ID": "111111"})
            self.assertIsNone(store.telegram_chat_id(user_id))
            self.assertEqual(store.connection.execute("select count(*) from telegram_link_tokens").fetchone()[0], 0)

    def test_paused_user_is_skipped_while_another_user_receives_the_same_listing(self):
        user_id = self.register()
        self.request("POST", "/profile", test_web.WebTests().valid_form(self.cookies["humanos_csrf"]))
        self.request("POST", "/account/pause", {"paused": "1"})
        with RentalStore(self.database) as store:
            store.save_user(User("other", "Other"))
            store.save_search_profile(SearchProfile("other-search", "other", "Madrid", 1100, "Madrid"))
            store.save_telegram_destination(user_id, "111111")
            store.save_telegram_destination("other", "222222")
            listing = Listing(source_id="fixture", external_id="1", municipality="Madrid", district="Centro", price_eur=900, bedrooms=1, property_type=PropertyType.HOME, rental_duration=RentalDuration.LONG_TERM)
            result = ListingPipeline(store).process(listing)
            client = MagicMock()
            attempts = TelegramNotifier(store, client).deliver(result)
            self.assertEqual({a.user_id: a.status for a in attempts}, {user_id: "skipped", "other": "sent"})
            self.assertEqual(client.send_message.call_count, 1)
            self.assertEqual(client.send_message.call_args.args[0], "222222")

    def test_export_is_scoped_and_deletion_cascades_without_bootstrap_resurrection(self):
        user_id = self.register()
        self.request("POST", "/profile", test_web.WebTests().valid_form(self.cookies["humanos_csrf"]))
        with RentalStore(self.database) as store:
            store.save_user(User("other", "Other person's name", "other@example.com"))
            store.save_telegram_destination(user_id, "111111")
            store.save_search_profile(SearchProfile("other-search", "other", "Madrid", 1100, "Madrid"))
            listing = Listing(source_id="fixture", external_id="1", municipality="Madrid", district="Centro", price_eur=900, bedrooms=1, property_type=PropertyType.HOME, rental_duration=RentalDuration.LONG_TERM)
            result = ListingPipeline(store).process(listing)
            self.assertEqual(len(result.delivery_candidates), 2)
            for outcome in result.delivery_candidates:
                store.claim_notification(outcome.match.match_id, "telegram")
                store.mark_notification_sent(outcome.match.match_id, "telegram")
        self.assertEqual(self.request("POST", "/account/export", {"password": "wrong"})[0], "401 Unauthorized")
        status, headers, body = self.request("POST", "/account/export", {"password": PASSWORD})
        self.assertEqual(status, "200 OK")
        data = json.loads(body)
        self.assertEqual(data["user"][0]["id"], user_id)
        self.assertEqual(len(data["searches"]), 1)
        self.assertEqual(len(data["notifications"]), 1)
        self.assertNotIn("other@example.com", body)
        self.assertNotIn("password_hash", body)
        self.assertNotIn("token_hash", body)
        self.assertIn(("Cache-Control", "no-store"), headers)
        self.assertEqual(self.request("POST", "/account/delete", {"password": PASSWORD})[0], "422 Unprocessable Entity")
        self.assertEqual(self.request("POST", "/account/delete", {"password": PASSWORD, "confirmation": "ELIMINAR"})[0], "303 See Other")
        with RentalStore(self.database) as store:
            self.assertIsNone(Accounts(store).account(user_id))
            self.assertEqual(store.load_user("other").email, "other@example.com")
            for table in ("renter_profiles", "pets", "telegram_destinations", "telegram_link_tokens", "web_sessions", "account_tokens"):
                self.assertEqual(store.connection.execute(f"select count(*) from {table}").fetchone()[0], 0, table)
            for table in ("search_profiles", "listing_matches", "notifications", "listings"):
                self.assertEqual(store.connection.execute(f"select count(*) from {table}").fetchone()[0], 1, table)
            self.assertEqual(_ensure_profiles(store, {"PROFILE_BUNDLE_JSON": "must not be imported"}), 0)
            notifier_from_environment(store, {"TELEGRAM_BOT_TOKEN": "123:test", "TELEGRAM_USER_ID": user_id, "TELEGRAM_CHAT_ID": "111111"})
            self.assertIsNone(store.telegram_chat_id(user_id))


class MailAndSettingsTests(unittest.TestCase):
    def test_email_validation_rejects_multiple_recipients_and_header_syntax(self):
        for value in ("one@example.com,two", "one,two@example.com", "Name <one@example.com>", "one@example.com\r\nBcc:x@example.com", "a..b@example.com"):
            with self.subTest(email=value), self.assertRaises(ValueError):
                normalize_email(value)
        self.assertEqual(normalize_email(" Ana+tag@Example.COM "), "ana+tag@example.com")

    def test_passwords_are_salted_and_wrong_values_fail(self):
        one, two = password_hash(PASSWORD), password_hash(PASSWORD)
        self.assertNotEqual(one, two)
        self.assertTrue(password_matches(PASSWORD, one))
        self.assertFalse(password_matches("wrong", one))
        with self.assertRaises(ValueError):
            password_hash("short")

    def test_smtp_starttls_precedes_login_and_no_plaintext_fallback(self):
        with patch("se_admiten_humanos.accounts.smtplib.SMTP") as factory:
            # SMTP's context manager returns itself.
            factory.return_value.__enter__.return_value = factory.return_value
            send_account_email(MailSettings("https://humanos.example", "smtp.example", "accounts@example.com", username="user", password="secret"), "ana@example.com", "verify", "token")
            names = [call[0] for call in factory.return_value.mock_calls]
            self.assertLess(names.index("starttls"), names.index("login"))
            self.assertLess(names.index("login"), names.index("send_message"))
            factory.return_value.starttls.side_effect = OSError("sensitive server detail")
            with self.assertRaisesRegex(AccountMailError, "No se pudo enviar") as caught:
                send_account_email(MAIL, "ana@example.com", "verify", "secret-token")
            self.assertNotIn("sensitive", str(caught.exception))

    def test_brevo_https_api_takes_precedence_without_exposing_key(self):
        response = MagicMock(status=201)
        response.__enter__.return_value = response
        settings = MailSettings(
            "https://humanos.example", "", "accounts@example.com",
            brevo_api_key="private-api-key",
        )
        with patch("se_admiten_humanos.accounts.urllib.request.urlopen", return_value=response) as urlopen:
            send_account_email(settings, "ana@example.com", "verify", "secret-token")
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.brevo.com/v3/smtp/email")
        self.assertEqual(request.get_header("Api-key"), "private-api-key")
        body = json.loads(request.data)
        self.assertEqual(body["to"], [{"email": "ana@example.com"}])
        self.assertIn("secret-token", body["textContent"])
        self.assertIn("Tu casa empieza por aquí", body["htmlContent"])
        self.assertIn("secret-token", body["htmlContent"])
        self.assertIn("background:#f5f1e8", body["htmlContent"])

        with patch("se_admiten_humanos.accounts.urllib.request.urlopen", side_effect=urllib.error.URLError("sensitive")):
            with self.assertRaisesRegex(AccountMailError, "No se pudo enviar") as caught:
                send_account_email(settings, "ana@example.com", "verify", "secret-token")
        self.assertNotIn("sensitive", str(caught.exception))

    def test_enabled_accounts_fail_closed_on_missing_or_insecure_mail_settings(self):
        with self.assertRaises(ValueError):
            web_settings_from_environment({"TELEGRAM_BOT_USERNAME": "ExampleBot", "ACCOUNTS_ENABLED": "true"})
        with self.assertRaises(ValueError):
            MailSettings("http://humanos.example", "smtp.example", "accounts@example.com").validate()
        settings, _ = web_settings_from_environment({"TELEGRAM_BOT_USERNAME": "ExampleBot", "ACCOUNTS_ENABLED": "true", "PUBLIC_BASE_URL": "https://humanos.example", "SMTP_HOST": "smtp.example", "SMTP_FROM": "accounts@example.com"})
        self.assertTrue(settings.accounts_enabled)
        self.assertEqual(settings.mail.port, 587)
        settings, _ = web_settings_from_environment({"TELEGRAM_BOT_USERNAME": "ExampleBot", "ACCOUNTS_ENABLED": "true", "PUBLIC_BASE_URL": "https://humanos.example", "SMTP_FROM": "accounts@example.com", "BREVO_API_KEY": "secret"})
        self.assertEqual(settings.mail.brevo_api_key, "secret")

    def test_request_logging_omits_capabilities_in_query_strings(self):
        handler = object.__new__(QuietRequestHandler)
        handler.path = "/account/reset?token=never-log-this"
        handler.command = "GET"
        handler.log_message = MagicMock()
        handler.log_request(200, 100)
        self.assertNotIn("never-log-this", str(handler.log_message.call_args))


if __name__ == "__main__":
    unittest.main()
