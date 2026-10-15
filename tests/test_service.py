"""Configuration tests for the combined Railway service."""

import unittest

from se_admiten_humanos.service import web_settings_from_environment


class ServiceTests(unittest.TestCase):
    def test_hosted_web_settings_use_secure_cookies(self):
        settings, port = web_settings_from_environment(
            {
                "DATABASE_PATH": "/data/rentals.sqlite3",
                "TELEGRAM_BOT_USERNAME": "SeAdmitenHumanosBot",
                "PORT": "9000",
            }
        )

        self.assertEqual(settings.database_path, "/data/rentals.sqlite3")
        self.assertTrue(settings.secure_cookies)
        self.assertEqual(port, 9000)

    def test_local_preview_can_explicitly_disable_secure_cookie_flag(self):
        settings, _ = web_settings_from_environment(
            {
                "TELEGRAM_BOT_USERNAME": "SeAdmitenHumanosBot",
                "WEB_COOKIE_SECURE": "false",
            }
        )

        self.assertFalse(settings.secure_cookies)

    def test_web_onboarding_requires_bot_username(self):
        with self.assertRaisesRegex(ValueError, "TELEGRAM_BOT_USERNAME"):
            web_settings_from_environment({})


if __name__ == "__main__":
    unittest.main()
