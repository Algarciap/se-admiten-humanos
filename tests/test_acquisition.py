"""Tests for bounded source acquisition and auditable execution state."""

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from se_admiten_humanos.acquisition import (
    AcquisitionRunError,
    AcquisitionRunner,
    JsonFileAdapter,
)
from se_admiten_humanos.models import Listing, User
from se_admiten_humanos.persistence import RentalStore
from se_admiten_humanos.search_profile import SearchProfile
from se_admiten_humanos.telegram import DeliveryAttempt


class StaticAdapter:
    def __init__(self, source_id: str, listings: list[Listing]) -> None:
        self.source_id = source_id
        self.listings = listings

    def fetch(self):
        yield from self.listings


class FailingAdapter:
    source_id = "broken-source"

    def fetch(self):
        yield Listing(
            source_id=self.source_id,
            external_id="before-failure",
            price_eur=900,
            municipality="Madrid",
        )
        raise ValueError("parser contract changed")


class RecordingNotifier:
    def __init__(self, status: str = "sent") -> None:
        self.status = status

    def deliver(self, result):
        return tuple(
            DeliveryAttempt(
                match_id=outcome.match.match_id,
                user_id=outcome.profile.user_id,
                status=self.status,
            )
            for outcome in result.delivery_candidates
        )


class AcquisitionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_directory.name) / "rentals.sqlite3"
        self.store = RentalStore(self.database_path)
        user = User(id="user-1", display_name="Alert recipient")
        self.store.save_user(user)
        self.store.save_search_profile(
            SearchProfile(
                id="search-1",
                user_id=user.id,
                name="Madrid",
                max_rent_eur=950,
                municipality="Madrid",
            )
        )

    def tearDown(self) -> None:
        self.store.close()
        self.temp_directory.cleanup()

    @staticmethod
    def listing(external_id: str = "listing-1") -> Listing:
        return Listing(
            source_id="fixture",
            external_id=external_id,
            title="Piso de prueba",
            price_eur=900,
            municipality="Madrid",
            description="Se admiten mascotas.",
        )

    def test_successful_run_processes_and_delivers(self) -> None:
        result = AcquisitionRunner(self.store, RecordingNotifier()).run(
            StaticAdapter("fixture", [self.listing()])
        )

        self.assertEqual(result.status, "succeeded")
        self.assertEqual(result.listings_seen, 1)
        self.assertEqual(result.eligible_matches, 1)
        self.assertEqual(result.notifications_sent, 1)
        self.assertIsNotNone(result.duration_ms)
        self.assertEqual(self.store.source_run(result.run_id).status, "succeeded")

    def test_empty_source_is_distinct_from_success(self) -> None:
        result = AcquisitionRunner(self.store, RecordingNotifier()).run(
            StaticAdapter("fixture", [])
        )

        self.assertEqual(result.status, "empty")
        self.assertEqual(result.listings_seen, 0)

    def test_parser_failure_records_partial_progress_and_raises(self) -> None:
        with self.assertRaises(AcquisitionRunError) as caught:
            AcquisitionRunner(self.store, RecordingNotifier()).run(FailingAdapter())

        record = caught.exception.record
        self.assertEqual(record.status, "failed")
        self.assertEqual(record.listings_seen, 1)
        self.assertIn("parser contract changed", record.error)

    def test_active_run_is_not_started_twice(self) -> None:
        claim = self.store.begin_source_run("fixture")
        self.assertIsNotNone(claim)

        result = AcquisitionRunner(self.store, RecordingNotifier()).run(
            StaticAdapter("fixture", [self.listing()])
        )

        self.assertEqual(result.status, "busy")
        self.assertIn("active source-run lease", result.error)
        self.assertIsNone(result.run_id)

    def test_source_mismatch_fails_visibly(self) -> None:
        adapter = StaticAdapter("another-source", [self.listing()])

        with self.assertRaises(AcquisitionRunError) as caught:
            AcquisitionRunner(self.store, RecordingNotifier()).run(adapter)

        self.assertIn("emitted listing", caught.exception.record.error)

    def test_per_run_limit_is_enforced(self) -> None:
        listings = [self.listing("one"), self.listing("two")]

        with self.assertRaises(AcquisitionRunError) as caught:
            AcquisitionRunner(
                self.store,
                RecordingNotifier(),
                max_listings_per_run=1,
            ).run(StaticAdapter("fixture", listings))

        self.assertEqual(caught.exception.record.listings_seen, 1)
        self.assertIn("run limit", caught.exception.record.error)

    def test_notification_failure_marks_run_failed(self) -> None:
        result = AcquisitionRunner(self.store, RecordingNotifier("failed")).run(
            StaticAdapter("fixture", [self.listing()])
        )

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.notifications_failed, 1)

    def test_json_adapter_parses_iso_date(self) -> None:
        path = Path(self.temp_directory.name) / "listings.json"
        path.write_text(
            json.dumps(
                {
                    "listings": [
                        {
                            "source_id": "fixture",
                            "external_id": "dated",
                            "available_from": "2026-10-15",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

        listings = tuple(JsonFileAdapter(path, "fixture").fetch())

        self.assertEqual(listings[0].available_from, date(2026, 10, 15))

    def test_json_adapter_rejects_unknown_fields(self) -> None:
        path = Path(self.temp_directory.name) / "invalid.json"
        path.write_text(
            json.dumps({"listings": [{"source_id": "fixture", "typo": 1}]}),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ValueError, "unknown field"):
            tuple(JsonFileAdapter(path, "fixture").fetch())


if __name__ == "__main__":
    unittest.main()
