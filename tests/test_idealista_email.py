"""Tests for privacy-preserving Idealista email-alert ingestion."""

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from se_admiten_humanos.idealista_email import parse_brevo_idealista_payload
from se_admiten_humanos.models import Pet, RenterProfile, User
from se_admiten_humanos.persistence import RentalStore
from se_admiten_humanos.search_profile import SearchProfile
from se_admiten_humanos.web import WebApplication, WebSettings


def payload(*, authenticated=True):
    authentication = (
        "mx.example; dkim=pass header.d=idealista.com header.from=idealista.com"
        if authenticated
        else "mx.example; dkim=fail header.from=idealista.com"
    )
    return {
        "items": [
            {
                "MessageId": "<alert-123@idealista.com>",
                "From": {"Name": "idealista", "Address": "alertas@idealista.com"},
                "Headers": {"Authentication-Results": authentication},
                "Subject": "Nuevos pisos para ti",
                "RawHtmlBody": """
                    <html><body>
                    <a href="https://www.idealista.com/inmueble/104567890/?utm_source=email">
                      Piso luminoso en Vallecas
                    </a>
                    <p>900 €/mes · Madrid · 2 habitaciones · 63 m²</p>
                    <p>Disponible ahora. Se aceptan mascotas.</p>
                    </body></html>
                """,
                "RawTextBody": "",
                "Attachments": [],
            }
        ]
    }


class IdealistaEmailParserTests(unittest.TestCase):
    def test_authenticated_alert_is_normalized_without_fetching_the_page(self):
        listings = parse_brevo_idealista_payload(payload())

        self.assertEqual(len(listings), 1)
        listing = listings[0]
        self.assertEqual(listing.source_id, "idealista_email")
        self.assertEqual(listing.external_id, "104567890")
        self.assertEqual(listing.price_eur, 900)
        self.assertEqual(listing.bedrooms, 2)
        self.assertEqual(listing.area_sqm, 63)
        self.assertEqual(
            listing.url, "https://www.idealista.com/inmueble/104567890/"
        )
        self.assertIn("Se aceptan mascotas", listing.description)

    def test_spoofed_sender_without_passing_dkim_is_ignored(self):
        self.assertEqual(parse_brevo_idealista_payload(payload(authenticated=False)), ())

    def test_non_idealista_links_are_never_emitted(self):
        value = payload()
        value["items"][0]["RawHtmlBody"] = value["items"][0]["RawHtmlBody"].replace(
            "www.idealista.com", "idealista.example"
        )

        self.assertEqual(parse_brevo_idealista_payload(value), ())


class IdealistaEmailWebhookTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary_directory.name) / "rentals.sqlite3"
        self.token = "a" * 40
        with RentalStore(self.database) as store:
            store.save_user(User(id="user-1", display_name="Ana"))
            store.save_renter_profile(
                RenterProfile(
                    id="renter-1",
                    user_id="user-1",
                    label="Principal",
                    sender_name="Ana",
                    renter_name="Ana",
                    monthly_income_eur=2500,
                    contract_type="contrato indefinido",
                    pets=(Pet(id="cats", species="gato", count=2),),
                )
            )
            store.save_search_profile(
                SearchProfile(
                    id="search-1",
                    user_id="user-1",
                    renter_profile_id="renter-1",
                    name="Madrid",
                    max_rent_eur=950,
                    municipality="Madrid",
                )
            )
            store.save_telegram_destination("user-1", "123456")
        self.app = WebApplication(
            WebSettings(
                database_path=str(self.database),
                bot_username="SeAdmitenHumanosBot",
                secure_cookies=False,
                telegram_bot_token="123456:test_token",
                idealista_email_webhook_token=self.token,
            )
        )

    def tearDown(self):
        self.temporary_directory.cleanup()

    def request(self, authorization):
        body = json.dumps(payload()).encode()
        captured = {}

        def start_response(status, headers):
            captured["status"] = status

        environ = {
            "REQUEST_METHOD": "POST",
            "PATH_INFO": "/webhooks/brevo/idealista",
            "CONTENT_TYPE": "application/json",
            "CONTENT_LENGTH": str(len(body)),
            "HTTP_AUTHORIZATION": authorization,
            "wsgi.input": io.BytesIO(body),
        }
        response = b"".join(self.app(environ, start_response)).decode()
        return captured["status"], json.loads(response)

    def test_webhook_requires_bearer_authentication(self):
        status, response = self.request("Bearer wrong")

        self.assertEqual(status, "401 Unauthorized")
        self.assertEqual(response["status"], "unauthorized")

    @patch("se_admiten_humanos.telegram.TelegramBotClient._request")
    def test_webhook_matches_and_delivers_without_mailbox_access(self, request):
        request.return_value = {"message_id": 42}

        status, response = self.request(f"Bearer {self.token}")

        self.assertEqual(status, "200 OK")
        self.assertEqual(response["listings_seen"], 1)
        self.assertEqual(response["notifications_sent"], 1)
        method, telegram_payload = request.call_args.args
        self.assertEqual(method, "sendMessage")
        self.assertEqual(telegram_payload["chat_id"], "123456")
        self.assertIn("Copiar mensaje", str(telegram_payload["reply_markup"]))

        status, replay = self.request(f"Bearer {self.token}")
        self.assertEqual(status, "200 OK")
        self.assertEqual(replay["notifications_sent"], 0)
        self.assertEqual(request.call_count, 1)


if __name__ == "__main__":
    unittest.main()
