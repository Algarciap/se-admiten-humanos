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
        self.assertIn("🟢 Dentro de tu presupuesto", alert)

    def test_urgent_budget_suggestion_is_clearly_labelled(self) -> None:
        self.store.save_search_profile(
            SearchProfile(
                id="search-urgent",
                user_id="user-1",
                name="Madrid urgente",
                max_rent_eur=850,
                municipality="Madrid",
                urgent=True,
                stretch_max_rent_eur=925,
            )
        )
        result = ListingPipeline(self.store).process(self.listing)
        urgent_outcome = next(
            outcome
            for outcome in result.delivery_candidates
            if outcome.profile.id == "search-urgent"
        )

        alert = format_alert(self.listing, result.pet_decision, urgent_outcome)

        self.assertIn("🟡 Sugerencia urgente", alert)
        self.assertIn("50 € sobre tu presupuesto preferido", alert)

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

    @patch("se_admiten_humanos.telegram.urlopen")
    def test_client_accepts_reusable_action_button_copy(self, mocked_urlopen: Mock) -> None:
        mocked_urlopen.return_value = _Response(
            json.dumps({"ok": True, "result": {"message_id": 42}}).encode()
        )

        TelegramBotClient("123456:test_token").send_message(
            "987654", "Welcome", "https://humanos.example/account", "Ir a mi cuenta"
        )

        payload = json.loads(mocked_urlopen.call_args.args[0].data)
        button = payload["reply_markup"]["inline_keyboard"][0][0]
        self.assertEqual(button, {"text": "Ir a mi cuenta", "url": "https://humanos.example/account"})

    @patch("se_admiten_humanos.telegram.urlopen")
    def test_client_fetches_message_updates_from_requested_offset(
        self, mocked_urlopen: Mock
    ) -> None:
        mocked_urlopen.return_value = _Response(
            json.dumps({"ok": True, "result": [{"update_id": 42}]}).encode()
        )

        updates = TelegramBotClient("123456:test_token").get_updates(40)

        self.assertEqual(updates, ({"update_id": 42},))
        request = mocked_urlopen.call_args.args[0]
        payload = json.loads(request.data)
        self.assertEqual(payload["offset"], 40)
        self.assertEqual(payload["timeout"], 0)
        self.assertEqual(payload["allowed_updates"], ["message"])

    def test_notifier_marks_success_and_avoids_duplicates(self) -> None:
        client = Mock()
        self.store.save_telegram_destination("user-1", "987654")
        notifier = TelegramNotifier(self.store, client)

        first = notifier.deliver(self.result)
        repeated = notifier.deliver(self.result)

        self.assertEqual(first[0].status, "sent")
        self.assertEqual(repeated[0].status, "skipped")
        self.assertEqual(repeated[0].detail, "already claimed")
        self.assertEqual(client.send_message.call_count, 1)

    def test_failed_delivery_is_released_for_retry(self) -> None:
        client = Mock()
        client.send_message.side_effect = RuntimeError("network down")
        self.store.save_telegram_destination("user-1", "987654")
        notifier = TelegramNotifier(self.store, client)

        attempt = notifier.deliver(self.result)[0]

        self.assertEqual(attempt.status, "failed")
        self.assertEqual(
            self.store.notification_status(attempt.match_id, "telegram"), "failed"
        )

    def test_missing_user_destination_does_not_claim(self) -> None:
        attempt = TelegramNotifier(self.store, Mock()).deliver(self.result)[0]

        self.assertEqual(attempt.status, "skipped")
        self.assertIsNone(self.store.notification_status(attempt.match_id, "telegram"))

    def test_legacy_environment_mapping_is_persisted_for_the_user(self) -> None:
        notifier = notifier_from_environment(
            self.store,
            {
                "TELEGRAM_BOT_TOKEN": "123456:test_token",
                "TELEGRAM_USER_ID": "user-1",
                "TELEGRAM_CHAT_ID": "987654",
            },
        )

        self.assertIsInstance(notifier, TelegramNotifier)
        self.assertEqual(
            self.store.load_telegram_destinations(), {"user-1": "987654"}
        )

    def test_existing_database_mapping_needs_only_the_bot_token(self) -> None:
        self.store.save_telegram_destination("user-1", "987654")

        notifier = notifier_from_environment(
            self.store,
            {"TELEGRAM_BOT_TOKEN": "123456:test_token"},
        )

        self.assertIsInstance(notifier, TelegramNotifier)

    def test_legacy_environment_does_not_overwrite_database_mapping(self) -> None:
        self.store.save_telegram_destination("user-1", "111111")

        notifier_from_environment(
            self.store,
            {
                "TELEGRAM_BOT_TOKEN": "123456:test_token",
                "TELEGRAM_USER_ID": "user-1",
                "TELEGRAM_CHAT_ID": "999999",
            },
        )

        self.assertEqual(self.store.telegram_chat_id("user-1"), "111111")

    def test_users_receive_only_matches_for_their_own_budget(self) -> None:
        self.store.save_telegram_destination("user-1", "111111")
        self.store.save_user(User(id="user-2", display_name="Lower budget"))
        self.store.save_search_profile(
            SearchProfile(
                id="search-2",
                user_id="user-2",
                name="Madrid up to 700",
                max_rent_eur=700,
                municipality="Madrid",
            )
        )
        self.store.save_telegram_destination("user-2", "222222")
        client = Mock()
        notifier = TelegramNotifier(self.store, client)

        expensive_result = ListingPipeline(self.store).process(
            Listing(
                source_id="idealista",
                external_id="costs-900",
                title="Piso de 900 euros",
                price_eur=900,
                municipality="Madrid",
                url="https://example.com/listing/costs-900",
            )
        )
        affordable_result = ListingPipeline(self.store).process(
            Listing(
                source_id="idealista",
                external_id="costs-650",
                title="Piso de 650 euros",
                price_eur=650,
                municipality="Madrid",
                url="https://example.com/listing/costs-650",
            )
        )

        expensive_attempts = notifier.deliver(expensive_result)
        affordable_attempts = notifier.deliver(affordable_result)

        self.assertEqual([attempt.user_id for attempt in expensive_attempts], ["user-1"])
        self.assertEqual(
            [attempt.user_id for attempt in affordable_attempts],
            ["user-1", "user-2"],
        )
        delivered_chat_ids = [call.args[0] for call in client.send_message.call_args_list]
        self.assertEqual(delivered_chat_ids, ["111111", "111111", "222222"])

    def test_environment_configuration_fails_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "TELEGRAM_CHAT_ID"):
            notifier_from_environment(
                self.store,
                {
                    "TELEGRAM_BOT_TOKEN": "123456:test_token",
                    "TELEGRAM_USER_ID": "user-1",
                },
            )

    def test_bot_token_without_a_database_destination_fails_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "enabled Telegram destination"):
            notifier_from_environment(
                self.store,
                {"TELEGRAM_BOT_TOKEN": "123456:test_token"},
            )


if __name__ == "__main__":
    unittest.main()
