"""Operational monitoring transitions must be durable and low-noise."""

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from se_admiten_humanos.monitoring import MonitoringSettings, SourceHealthMonitor
from se_admiten_humanos.persistence import RentalStore


class RecordingMonitoringNotifier:
    def __init__(self):
        self.messages = []

    def notify(self, message):
        self.messages.append(message)


class MonitoringTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary_directory.name) / "rentals.sqlite3"
        self.store = RentalStore(self.database)
        self.notifier = RecordingMonitoringNotifier()
        self.monitor = SourceHealthMonitor(
            self.store,
            MonitoringSettings(failure_threshold=3, stale_after_seconds=1800),
            self.notifier,
        )

    def tearDown(self):
        self.store.close()
        self.temporary_directory.cleanup()

    def test_repeated_failure_alerts_once_and_success_alerts_recovery(self):
        self.assertEqual(self.monitor.failure("redpiso", "timeout one"), ())
        self.assertEqual(self.monitor.failure("redpiso", "timeout two"), ())
        self.assertEqual(self.monitor.failure("redpiso", "timeout three"), ())

        record = self.store.source_health("redpiso")
        self.assertEqual(record.consecutive_failures, 3)
        self.assertEqual(record.alert_state, "failing")
        self.assertEqual(len(self.notifier.messages), 1)
        self.assertIn("fallando repetidamente", self.notifier.messages[0])

        self.monitor.failure("redpiso", "timeout four")
        self.assertEqual(len(self.notifier.messages), 1)

        self.monitor.success("redpiso", "empty", 286)
        recovered = self.store.source_health("redpiso")
        self.assertEqual(recovered.alert_state, "healthy")
        self.assertEqual(recovered.consecutive_failures, 0)
        self.assertEqual(len(self.notifier.messages), 2)
        self.assertIn("recuperada", self.notifier.messages[1])

    def test_zero_inventory_alerts_immediately_but_only_once(self):
        self.monitor.success("tecnocasa", "succeeded", 36)
        self.monitor.failure(
            "tecnocasa",
            "Discovery returned zero total listings",
            unexpected_zero=True,
        )

        record = self.store.source_health("tecnocasa")
        self.assertEqual(record.alert_state, "zero_results")
        self.assertEqual(record.last_listing_count, 0)
        self.assertEqual(len(self.notifier.messages), 1)

        self.monitor.failure(
            "tecnocasa",
            "Discovery still returned zero total listings",
            unexpected_zero=True,
        )
        self.assertEqual(len(self.notifier.messages), 1)

    def test_stale_success_alerts_once(self):
        old = datetime.now(UTC) - timedelta(minutes=31)
        self.store.record_source_success(
            "nestar",
            "succeeded",
            12,
            observed_at=old,
        )

        self.monitor.check_stale(("nestar",))
        self.monitor.check_stale(("nestar",))

        record = self.store.source_health("nestar")
        self.assertEqual(record.alert_state, "stale")
        self.assertEqual(len(self.notifier.messages), 1)
        self.assertIn("sin éxitos recientes", self.notifier.messages[0])


if __name__ == "__main__":
    unittest.main()
