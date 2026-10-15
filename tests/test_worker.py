"""Tests for the persistent hosted worker and its safe first boot."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from se_admiten_humanos.models import Listing, WebsiteDiscovery
from se_admiten_humanos.persistence import RentalStore
from se_admiten_humanos.telegram import DeliveryAttempt
from se_admiten_humanos.telegram_linking import TelegramLinkPollResult
from se_admiten_humanos.worker import (
    WorkerConfigurationError,
    WorkerCycleResult,
    WorkerSettings,
    run_worker,
    run_worker_cycle,
    run_worker_sources_cycle,
)


class MutableSource:
    source_id = "agency"

    def __init__(self, discoveries):
        self.discoveries = list(discoveries)
        self.details_fetched = []
        self.discovery_calls = 0

    def discover(self):
        self.discovery_calls += 1
        return tuple(self.discoveries)

    def fetch_listing(self, discovery):
        self.details_fetched.append(discovery.external_id)
        return Listing(
            source_id=self.source_id,
            external_id=discovery.external_id,
            title="Piso nuevo",
            price_eur=900,
            municipality="Madrid",
            description="Se admiten mascotas.",
            url=discovery.url,
        )


class RecordingNotifier:
    def __init__(self, status="sent"):
        self.deliveries = 0
        self.status = status

    def deliver(self, result):
        attempts = []
        for outcome in result.delivery_candidates:
            self.deliveries += 1
            attempts.append(
                DeliveryAttempt(
                    outcome.match.match_id,
                    outcome.profile.user_id,
                    self.status,
                )
            )
        return tuple(attempts)


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary_directory.name) / "rentals.sqlite3"
        self.settings = WorkerSettings(database_path=str(self.database))
        root = Path(__file__).resolve().parent.parent
        profile_json = (root / "config" / "profiles.local.example.json").read_text(
            encoding="utf-8"
        )
        self.environment = {"PROFILE_BUNDLE_JSON": profile_json}
        self.old = WebsiteDiscovery("old", "https://example.test/homes/old")
        self.source = MutableSource([self.old])

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_first_boot_imports_profiles_and_baselines_without_notifying(self):
        notifier_called = False

        def forbidden_notifier(_store, _environment=None):
            nonlocal notifier_called
            notifier_called = True
            raise AssertionError("baseline must not construct the notifier")

        result = run_worker_cycle(
            self.settings,
            self.environment,
            source=self.source,
            notifier_factory=forbidden_notifier,
        )

        self.assertEqual(result.status, "baseline_created")
        self.assertEqual(result.profiles_imported, 1)
        self.assertEqual(result.discoveries_recorded, 1)
        self.assertFalse(notifier_called)
        self.assertEqual(self.source.details_fetched, [])
        with RentalStore(self.database) as store:
            self.assertEqual(len(store.load_enabled_search_profiles()), 1)
            self.assertTrue(store.website_source_initialized("agency"))

    def test_later_cycles_notify_only_for_new_discoveries(self):
        run_worker_cycle(self.settings, self.environment, source=self.source)
        notifier = RecordingNotifier()

        empty = run_worker_cycle(
            self.settings,
            {},
            source=self.source,
            notifier_factory=lambda _store, _environment=None: notifier,
        )
        self.source.discoveries.append(
            WebsiteDiscovery("new", "https://example.test/homes/new")
        )
        new = run_worker_cycle(
            self.settings,
            {},
            source=self.source,
            notifier_factory=lambda _store, _environment=None: notifier,
        )
        repeated = run_worker_cycle(
            self.settings,
            {},
            source=self.source,
            notifier_factory=lambda _store, _environment=None: notifier,
        )

        self.assertEqual(empty.status, "empty")
        self.assertEqual(new.status, "succeeded")
        self.assertEqual(new.acquisition.notifications_sent, 1)
        self.assertEqual(repeated.status, "empty")
        self.assertEqual(notifier.deliveries, 1)
        self.assertEqual(self.source.details_fetched, ["new"])

    def test_first_live_cycle_migrates_legacy_telegram_mapping(self):
        run_worker_cycle(self.settings, self.environment, source=self.source)
        environment = {
            "TELEGRAM_BOT_TOKEN": "123456:test_token",
            "TELEGRAM_USER_ID": "user-example",
            "TELEGRAM_CHAT_ID": "987654",
        }

        result = run_worker_cycle(self.settings, environment, source=self.source)

        self.assertEqual(result.status, "empty")
        with RentalStore(self.database) as store:
            self.assertEqual(
                store.load_telegram_destinations(),
                {"user-example": "987654"},
            )

    def test_empty_database_requires_private_profile_secret_before_discovery(self):
        with self.assertRaisesRegex(WorkerConfigurationError, "PROFILE_BUNDLE_JSON"):
            run_worker_cycle(self.settings, {}, source=self.source)

        self.assertEqual(self.source.discovery_calls, 0)

    @patch("se_admiten_humanos.worker.process_telegram_link_updates")
    def test_bot_username_enables_account_link_polling(self, mocked_poll):
        mocked_poll.return_value = TelegramLinkPollResult(
            updates_seen=1,
            links_created=1,
        )
        environment = dict(
            self.environment,
            TELEGRAM_BOT_TOKEN="123456:test_token",
            TELEGRAM_BOT_USERNAME="SeAdmitenHumanosBot",
        )

        result = run_worker_cycle(self.settings, environment, source=self.source)

        self.assertEqual(result.telegram_linking.links_created, 1)
        mocked_poll.assert_called_once()

    @patch("se_admiten_humanos.worker.process_telegram_link_updates")
    def test_link_poll_failure_is_visible_without_blocking_property_scan(
        self, mocked_poll
    ):
        mocked_poll.side_effect = OSError("temporary Telegram outage")
        environment = dict(
            self.environment,
            TELEGRAM_BOT_TOKEN="123456:test_token",
            TELEGRAM_BOT_USERNAME="SeAdmitenHumanosBot",
        )

        result = run_worker_cycle(self.settings, environment, source=self.source)

        self.assertEqual(result.status, "baseline_created")
        self.assertIn("OSError", result.telegram_linking_error)
        self.assertEqual(self.source.discovery_calls, 1)

    def test_failed_delivery_makes_worker_cycle_visibly_unhealthy(self):
        run_worker_cycle(self.settings, self.environment, source=self.source)
        self.source.discoveries.append(
            WebsiteDiscovery("new", "https://example.test/homes/new")
        )

        with self.assertRaisesRegex(RuntimeError, "unhealthy status 'failed'"):
            run_worker_cycle(
                self.settings,
                {},
                source=self.source,
                notifier_factory=lambda _store, _environment=None: RecordingNotifier(
                    "failed"
                ),
            )

    def test_invalid_hosted_settings_fail_before_start(self):
        with self.assertRaisesRegex(WorkerConfigurationError, "at least 60"):
            WorkerSettings.from_environment({"POLL_INTERVAL_SECONDS": "30"})

    def test_worker_exits_after_repeated_transient_failures(self):
        calls = 0
        sleeps = []
        settings = WorkerSettings(
            database_path=str(self.database),
            max_consecutive_failures=3,
        )

        def failing_cycle(_settings, _environment):
            nonlocal calls
            calls += 1
            raise OSError("temporary outage")

        exit_code = run_worker(
            settings,
            {},
            max_cycles=5,
            sleep=sleeps.append,
            cycle=failing_cycle,
        )

        self.assertEqual(exit_code, 1)
        self.assertEqual(calls, 3)
        self.assertEqual(len(sleeps), 2)

    def test_multi_source_cycle_baselines_every_source_without_notifying(self):
        second = MutableSource(
            [WebsiteDiscovery("second", "https://example.test/homes/second")]
        )
        second.source_id = "second-agency"

        result = run_worker_sources_cycle(
            self.settings,
            self.environment,
            sources=(self.source, second),
            notifier_factory=lambda *_args: (_ for _ in ()).throw(
                AssertionError("baseline must not construct the notifier")
            ),
        )

        self.assertEqual(result.status, "baseline_created")
        self.assertEqual(
            [source.source_id for source in result.sources],
            ["agency", "second-agency"],
        )
        self.assertEqual([source.discoveries_recorded for source in result.sources], [1, 1])

    def test_multi_source_failure_does_not_starve_healthy_source(self):
        class BrokenSource(MutableSource):
            source_id = "broken"

            def discover(self):
                raise ValueError("parser contract changed")

        healthy = MutableSource(
            [WebsiteDiscovery("healthy", "https://example.test/homes/healthy")]
        )
        healthy.source_id = "healthy"

        result = run_worker_sources_cycle(
            self.settings,
            self.environment,
            sources=(BrokenSource([]), healthy),
        )

        self.assertEqual(result.status, "partial_failure")
        self.assertIn("parser contract changed", result.source_errors[0])
        self.assertEqual(result.sources[0].source_id, "healthy")
        self.assertEqual(healthy.discovery_calls, 1)


if __name__ == "__main__":
    unittest.main()
