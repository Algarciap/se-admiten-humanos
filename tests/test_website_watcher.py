"""Tests for baseline-safe public website monitoring."""

import tempfile
import unittest
from pathlib import Path

from se_admiten_humanos.acquisition import AcquisitionRunner
from se_admiten_humanos.models import Listing, User, WebsiteDiscovery
from se_admiten_humanos.persistence import RentalStore
from se_admiten_humanos.search_profile import SearchProfile
from se_admiten_humanos.telegram import DeliveryAttempt
from se_admiten_humanos.website_watcher import WebsiteWatcherAdapter


class MutableWebsiteSource:
    source_id = "agency"

    def __init__(self, discoveries: list[WebsiteDiscovery]) -> None:
        self.discoveries = discoveries
        self.details_fetched: list[str] = []

    def discover(self):
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


class PartlyBrokenWebsiteSource(MutableWebsiteSource):
    def fetch_listing(self, discovery):
        self.details_fetched.append(discovery.external_id)
        if discovery.external_id == "broken":
            raise TimeoutError("detail timed out")
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
    def __init__(self, status: str = "sent", detail: str = "") -> None:
        self.status = status
        self.detail = detail

    def deliver(self, result):
        return tuple(
            DeliveryAttempt(
                outcome.match.match_id,
                outcome.profile.user_id,
                self.status,
                self.detail,
            )
            for outcome in result.delivery_candidates
        )


class WebsiteWatcherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.store = RentalStore(
            Path(self.temporary_directory.name) / "rentals.sqlite3"
        )
        self.store.save_user(User(id="user-1", display_name="Recipient"))
        self.store.save_search_profile(
            SearchProfile(
                id="search-1",
                user_id="user-1",
                name="Madrid",
                max_rent_eur=950,
                municipality="Madrid",
            )
        )
        self.old = WebsiteDiscovery("old", "https://example.test/homes/old")
        self.source = MutableWebsiteSource([self.old])
        self.adapter = WebsiteWatcherAdapter(self.store, self.source)

    def tearDown(self) -> None:
        self.store.close()
        self.temporary_directory.cleanup()

    def test_baseline_records_existing_stock_without_fetching_details(self) -> None:
        result = self.adapter.initialize_baseline()

        self.assertEqual(result.discoveries_recorded, 1)
        self.assertEqual(self.source.details_fetched, [])

    def test_live_run_requires_explicit_baseline(self) -> None:
        with self.assertRaisesRegex(ValueError, "needs a baseline"):
            tuple(self.adapter.fetch())

    def test_only_new_identity_is_fetched_and_sent(self) -> None:
        self.adapter.initialize_baseline()
        self.source.discoveries.append(
            WebsiteDiscovery("new", "https://example.test/homes/new")
        )

        result = AcquisitionRunner(self.store, RecordingNotifier()).run(self.adapter)

        self.assertEqual(result.status, "succeeded")
        self.assertEqual(result.listings_seen, 1)
        self.assertEqual(result.notifications_sent, 1)
        self.assertEqual(self.source.details_fetched, ["new"])
        repeated = AcquisitionRunner(self.store, RecordingNotifier()).run(self.adapter)
        self.assertEqual(repeated.status, "empty")

    def test_delivery_failure_leaves_discovery_pending_for_retry(self) -> None:
        self.adapter.initialize_baseline()
        self.source.discoveries.append(
            WebsiteDiscovery("new", "https://example.test/homes/new")
        )

        failed = AcquisitionRunner(self.store, RecordingNotifier("failed")).run(
            self.adapter
        )
        retried = AcquisitionRunner(self.store, RecordingNotifier()).run(self.adapter)

        self.assertEqual(failed.status, "failed")
        self.assertEqual(retried.notifications_sent, 1)
        self.assertEqual(self.source.details_fetched, ["new", "new"])

    def test_zero_discoveries_fail_visibly(self) -> None:
        self.adapter.initialize_baseline()
        self.source.discoveries.clear()

        with self.assertRaisesRegex(ValueError, "zero listings"):
            tuple(self.adapter.fetch())

    def test_one_broken_detail_does_not_starve_later_discoveries(self) -> None:
        self.adapter.initialize_baseline()
        source = PartlyBrokenWebsiteSource(
            [
                self.old,
                WebsiteDiscovery("broken", "https://example.test/homes/broken"),
                WebsiteDiscovery("healthy", "https://example.test/homes/healthy"),
            ]
        )
        adapter = WebsiteWatcherAdapter(self.store, source)

        with self.assertRaisesRegex(Exception, "successful details continued"):
            AcquisitionRunner(self.store, RecordingNotifier()).run(adapter)

        self.assertEqual(source.details_fetched, ["broken", "healthy"])
        pending = self.store.sync_website_discoveries(
            source.source_id, tuple(source.discoveries)
        )
        self.assertEqual([item.external_id for item in pending], ["broken"])


if __name__ == "__main__":
    unittest.main()
