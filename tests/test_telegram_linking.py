"""Tests for secure, one-time Telegram account linking."""

import hashlib
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from se_admiten_humanos.models import User
from se_admiten_humanos.persistence import RentalStore
from se_admiten_humanos.telegram_linking import (
    create_telegram_link,
    process_telegram_link_updates,
)


class FakeTelegramClient:
    def __init__(self, updates=()):
        self.updates = tuple(updates)
        self.offsets = []
        self.messages = []

    def get_updates(self, offset, *, limit=100):
        self.offsets.append((offset, limit))
        return self.updates

    def send_message(self, chat_id, text, action_url="", action_text="Abrir anuncio"):
        self.messages.append((chat_id, text, action_url, action_text))
        return len(self.messages)


class TelegramLinkingTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary_directory.name) / "rentals.sqlite3"
        self.store = RentalStore(self.database)
        self.store.save_user(User(id="user-1", display_name="First user"))

    def tearDown(self):
        self.store.close()
        self.temporary_directory.cleanup()

    def test_link_is_single_use_and_only_its_hash_is_stored(self):
        link = create_telegram_link(
            self.store,
            "user-1",
            "SeAdmitenHumanosBot",
        )

        row = self.store.connection.execute(
            "select token_hash from telegram_link_tokens"
        ).fetchone()
        self.assertNotIn(link.token, str(row[0]))
        self.assertEqual(
            row[0], hashlib.sha256(link.token.encode("ascii")).hexdigest()
        )
        token_hash = str(row[0])
        self.assertEqual(
            self.store.consume_telegram_link_token(token_hash, "123456"),
            "user-1",
        )
        self.assertEqual(self.store.telegram_chat_id("user-1"), "123456")
        with self.assertRaisesRegex(ValueError, "Invalid or expired"):
            self.store.consume_telegram_link_token(token_hash, "123456")

    def test_expired_link_is_rejected(self):
        token_hash = "a" * 64
        future = datetime.now(UTC) + timedelta(minutes=1)
        self.store.save_telegram_link_token("user-1", token_hash, future)

        with self.assertRaisesRegex(ValueError, "Invalid or expired"):
            self.store.consume_telegram_link_token(
                token_hash,
                "123456",
                now=future + timedelta(seconds=1),
            )

    def test_chat_collision_does_not_consume_the_second_users_token(self):
        self.store.save_user(User(id="user-2", display_name="Second user"))
        first = create_telegram_link(self.store, "user-1", "SeAdmitenHumanosBot")
        second = create_telegram_link(self.store, "user-2", "SeAdmitenHumanosBot")
        first_hash = hashlib.sha256(first.token.encode("ascii")).hexdigest()
        second_hash = hashlib.sha256(second.token.encode("ascii")).hexdigest()
        self.store.consume_telegram_link_token(first_hash, "123456")

        with self.assertRaisesRegex(ValueError, "different user"):
            self.store.consume_telegram_link_token(second_hash, "123456")

        self.assertEqual(
            self.store.consume_telegram_link_token(second_hash, "654321"),
            "user-2",
        )

    def test_private_start_command_links_and_advances_cursor(self):
        link = create_telegram_link(
            self.store, "user-1", "@SeAdmitenHumanosBot"
        )
        client = FakeTelegramClient(
            (
                {
                    "update_id": 42,
                    "message": {
                        "text": f"/start {link.token}",
                        "chat": {"id": 123456, "type": "private"},
                    },
                },
            )
        )

        result = process_telegram_link_updates(self.store, client)

        self.assertEqual(result.links_created, 1)
        self.assertEqual(self.store.telegram_chat_id("user-1"), "123456")
        self.assertEqual(self.store.telegram_next_update_id(), 43)
        self.assertEqual(client.messages[0][0], "123456")

        repeated = process_telegram_link_updates(self.store, client)
        self.assertEqual(repeated.updates_seen, 0)
        self.assertEqual(client.offsets, [(0, 100), (43, 100)])

    def test_plain_start_prompts_account_creation(self):
        client = FakeTelegramClient(({
            "update_id": 1,
            "message": {"text": "/start", "chat": {"id": 123456, "type": "private"}},
        },))

        result = process_telegram_link_updates(
            self.store, client, "https://humanos.example"
        )

        self.assertEqual(result.updates_seen, 1)
        self.assertEqual(result.links_created, 0)
        self.assertEqual(client.messages[0][2], "https://humanos.example/account/register")
        self.assertEqual(client.messages[0][3], "Crear mi cuenta")

    def test_rejected_link_explains_how_to_try_again(self):
        client = FakeTelegramClient(({
            "update_id": 1,
            "message": {
                "text": "/start " + "x" * 24,
                "chat": {"id": 123456, "type": "private"},
            },
        },))

        result = process_telegram_link_updates(
            self.store, client, "https://humanos.example"
        )

        self.assertEqual(result.rejected_links, 1)
        self.assertIn("no es válido", client.messages[0][1])
        self.assertEqual(client.messages[0][2], "https://humanos.example/account")

    def test_malformed_start_is_rejected_instead_of_silently_ignored(self):
        client = FakeTelegramClient(({
            "update_id": 1,
            "message": {
                "text": "/start not-a-valid-token",
                "chat": {"id": 123456, "type": "private"},
            },
        },))

        result = process_telegram_link_updates(
            self.store, client, "https://humanos.example"
        )

        self.assertEqual(result.rejected_links, 1)
        self.assertEqual(len(client.messages), 1)

    def test_unknown_private_message_returns_account_help(self):
        client = FakeTelegramClient(({
            "update_id": 1,
            "message": {"text": "hello", "chat": {"id": 123456, "type": "private"}},
        },))

        result = process_telegram_link_updates(
            self.store, client, "https://humanos.example"
        )

        self.assertEqual(result.ignored_updates, 1)
        self.assertEqual(client.messages[0][3], "Ir a mi cuenta")

    def test_group_and_unrelated_messages_are_ignored_but_checkpointed(self):
        link = create_telegram_link(self.store, "user-1", "SeAdmitenHumanosBot")
        client = FakeTelegramClient(
            (
                {
                    "update_id": 1,
                    "message": {
                        "text": f"/start {link.token}",
                        "chat": {"id": -1001, "type": "group"},
                    },
                },
                {
                    "update_id": 2,
                    "message": {
                        "text": "hello",
                        "chat": {"id": 123456, "type": "private"},
                    },
                },
            )
        )

        result = process_telegram_link_updates(self.store, client)

        self.assertEqual(result.ignored_updates, 2)
        self.assertEqual(result.links_created, 0)
        self.assertIsNone(self.store.telegram_chat_id("user-1"))
        self.assertEqual(self.store.telegram_next_update_id(), 3)
        self.assertEqual(len(client.messages), 1)


if __name__ == "__main__":
    unittest.main()
