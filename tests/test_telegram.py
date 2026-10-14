"""Tests for Telegram formatting, transport, and delivery state."""

import json
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import Mock, patch

from se_admiten_humanos.models import Listing, User
from se_admiten_humanos.persistence import RentalStore
from se_admiten_humanos.pipeline import ListingPipeline
from se_admiten_humanos.search_profile import SearchProfile
from se_admiten_humanos.telegram import (
    TelegramBotClient,
    TelegramNotifier,
    format_alert,
    notifier_from_environment,
)


class _Response(BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


class TelegramTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = RentalStore(Path(self.temp.name) / "rentals.sqlite3")
        self.store.save_user(User(id="user-1", display_name="User"))
        self.store.save_search_profile(
            SearchProfile(
                id="search-1",
                user_id="user-1",
                name="Madrid",
                max_rent_eur=950,
                municipality="Madrid",
            )
        )
        self.listing = Listing(
            source_id="idealista",
            external_id="123",
            title="Piso luminoso",
            price_eur=900,
            municipality="Madrid",
            description="Disponible ahora.",
            url="https://example.com/listing/123",
        )
        self.result = ListingPipeline(self.store).process(self.listing)

    def tearDown(self) -> None:
        self.store.close()
        self.temp.cleanup()

    def test_alert_never_turns_silence_into_acceptance(self) -> None:
        alert = format_alert(
            self.listing,
            self.result.pet_decision,
            self.result.delivery_candidates[0],
        )
        self.assertIn("Mascotas: no especificado", alert)
        self.assertIn("el anuncio no menciona mascotas", alert)

    @patch("se_admiten_humanos.telegram.urlopen")
    def test_client_posts_actionable_message(self, mocked_urlopen: Mock) -> None:
        mocked_urlopen.return_value = _Response(
            json.dumps({"ok": True, "result": {"message_id": 42}}).encode()
        )
        client = TelegramBotClient("123456:test_token")

        message_id = client.send_message("987654", "Hello", self.listing.url)

        self.assertEqual(message_id, 42)
        request = mocked_urlopen.call_args.args[0]
        payload = json.loads(request.data)
        self.assertEqual(payload["chat_id"], "987654")
        self.assertEqual(payload["reply_markup"]["inline_keyboard"][0][0]["url"], self.listing.url)

    def test_notifier_marks_success_and_avoids_duplicates(self) -> None:
        client = Mock()
        notifier = TelegramNotifier(self.store, client, {"user-1": "987654"})

        first = notifier.deliver(self.result)
        repeated = notifier.deliver(self.result)

        self.assertEqual(first[0].status, "sent")
        self.assertEqual(repeated[0].status, "skipped")
        self.assertEqual(repeated[0].detail, "already claimed")
        self.assertEqual(client.send_message.call_count, 1)

    def test_failed_delivery_is_released_for_retry(self) -> None:
        client = Mock()
        client.send_message.side_effect = RuntimeError("network down")
        notifier = TelegramNotifier(self.store, client, {"user-1": "987654"})

        attempt = notifier.deliver(self.result)[0]

        self.assertEqual(attempt.status, "failed")
        self.assertEqual(
            self.store.notification_status(attempt.match_id, "telegram"), "failed"
        )

    def test_missing_user_destination_does_not_claim(self) -> None:
        attempt = TelegramNotifier(self.store, Mock(), {}).deliver(self.result)[0]

        self.assertEqual(attempt.status, "skipped")
        self.assertIsNone(self.store.notification_status(attempt.match_id, "telegram"))

    def test_environment_configuration_is_scoped_to_one_user(self) -> None:
        notifier = notifier_from_environment(
            self.store,
            {
                "TELEGRAM_BOT_TOKEN": "123456:test_token",
                "TELEGRAM_USER_ID": "user-1",
                "TELEGRAM_CHAT_ID": "987654",
            },
        )

        self.assertEqual(notifier.user_chat_ids, {"user-1": "987654"})

    def test_environment_configuration_fails_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "TELEGRAM_CHAT_ID"):
            notifier_from_environment(
                self.store,
                {
                    "TELEGRAM_BOT_TOKEN": "123456:test_token",
                    "TELEGRAM_USER_ID": "user-1",
                },
            )


if __name__ == "__main__":
    unittest.main()
