"""Tests for SQLite storage, deduplication, and notification claims."""

import tempfile
import unittest
from pathlib import Path

from se_admiten_humanos.models import Listing
from se_admiten_humanos.persistence import ListingStore, canonicalize_url


class PersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_directory.name) / "rentals.sqlite3"
        self.store = ListingStore(self.database_path)

    def tearDown(self) -> None:
        self.store.close()
        self.temp_directory.cleanup()

    def test_repeated_source_id_is_one_listing(self) -> None:
        listing = Listing(source_id="idealista", external_id="123", title="Original")

        first = self.store.upsert(listing)
        second = self.store.upsert(
            Listing(source_id="idealista", external_id="123", title="Updated")
        )

        self.assertTrue(first.is_new)
        self.assertFalse(second.is_new)
        self.assertEqual(first.listing_id, second.listing_id)

    def test_tracking_parameters_do_not_create_duplicates(self) -> None:
        first = self.store.upsert(
            Listing(
                source_id="vivenio",
                url="https://www.vivenio.com/home/42?utm_source=x",
            )
        )
        second = self.store.upsert(
            Listing(source_id="vivenio", url="https://vivenio.com/home/42#details")
        )

        self.assertFalse(second.is_new)
        self.assertEqual(first.listing_id, second.listing_id)

    def test_complete_property_facts_deduplicate_across_sources(self) -> None:
        facts = {
            "address": "Calle de Atocha, 10",
            "municipality": "Madrid",
            "price_eur": 900,
            "bedrooms": 1,
            "area_sqm": 45,
        }
        first = self.store.upsert(
            Listing(source_id="idealista", external_id="a", **facts)
        )
        second = self.store.upsert(
            Listing(source_id="fotocasa", external_id="b", **facts)
        )

        self.assertFalse(second.is_new)
        self.assertEqual(first.listing_id, second.listing_id)

    def test_source_id_remains_stable_when_property_facts_change(self) -> None:
        original = Listing(
            source_id="idealista",
            external_id="a",
            address="Calle de Atocha, 10",
            municipality="Madrid",
            price_eur=900,
            bedrooms=1,
            area_sqm=45,
        )
        updated = Listing(
            source_id="idealista",
            external_id="a",
            address="Calle de Atocha, 10",
            municipality="Madrid",
            price_eur=875,
            bedrooms=1,
            area_sqm=45,
        )

        first = self.store.upsert(original)
        second = self.store.upsert(updated)

        self.assertFalse(second.is_new)
        self.assertEqual(first.listing_id, second.listing_id)

    def test_cross_source_alias_is_reused_without_property_facts(self) -> None:
        facts = {
            "address": "Calle de Atocha, 10",
            "municipality": "Madrid",
            "price_eur": 900,
            "bedrooms": 1,
            "area_sqm": 45,
        }
        first = self.store.upsert(
            Listing(source_id="idealista", external_id="a", **facts)
        )
        cross_post = self.store.upsert(
            Listing(source_id="fotocasa", external_id="b", **facts)
        )
        repeated_without_facts = self.store.upsert(
            Listing(source_id="fotocasa", external_id="b")
        )

        self.assertFalse(cross_post.is_new)
        self.assertFalse(repeated_without_facts.is_new)
        self.assertEqual(first.listing_id, repeated_without_facts.listing_id)

    def test_incomplete_cross_source_facts_are_not_merged(self) -> None:
        facts = {
            "address": "Calle de Atocha, 10",
            "municipality": "Madrid",
            "price_eur": 900,
            "bedrooms": 1,
        }
        first = self.store.upsert(
            Listing(source_id="idealista", external_id="a", **facts)
        )
        second = self.store.upsert(
            Listing(source_id="fotocasa", external_id="b", **facts)
        )

        self.assertTrue(first.is_new)
        self.assertTrue(second.is_new)
        self.assertNotEqual(first.listing_id, second.listing_id)

    def test_notification_is_claimed_once_and_failed_delivery_can_retry(self) -> None:
        stored = self.store.upsert(Listing(source_id="idealista", external_id="123"))

        self.assertTrue(self.store.claim_notification(stored.listing_id, "telegram"))
        self.assertFalse(self.store.claim_notification(stored.listing_id, "telegram"))
        self.store.mark_notification_failed(stored.listing_id, "telegram", "timeout")
        self.assertTrue(self.store.claim_notification(stored.listing_id, "telegram"))
        self.store.mark_notification_sent(stored.listing_id, "telegram")
        self.assertFalse(self.store.claim_notification(stored.listing_id, "telegram"))

    def test_notification_claim_is_visible_to_another_store(self) -> None:
        stored = self.store.upsert(Listing(source_id="idealista", external_id="123"))
        self.assertTrue(self.store.claim_notification(stored.listing_id, "telegram"))

        with ListingStore(self.database_path) as second_store:
            self.assertFalse(
                second_store.claim_notification(stored.listing_id, "telegram")
            )

    def test_conflicting_identities_fail_visibly(self) -> None:
        self.store.upsert(Listing(source_id="idealista", external_id="a"))
        self.store.upsert(
            Listing(source_id="fotocasa", url="https://fotocasa.es/listing/b")
        )

        with self.assertRaisesRegex(RuntimeError, "manual reconciliation"):
            self.store.upsert(
                Listing(
                    source_id="idealista",
                    external_id="a",
                    url="https://fotocasa.es/listing/b",
                )
            )

    def test_invalid_listing_identity_fails_visibly(self) -> None:
        with self.assertRaisesRegex(ValueError, "needs an external_id"):
            self.store.upsert(Listing(source_id="unknown"))

    def test_canonical_url_rejects_non_web_urls(self) -> None:
        self.assertEqual(canonicalize_url("javascript:alert(1)"), "")
        self.assertEqual(canonicalize_url("https://user@example.com/home/42"), "")
        self.assertEqual(canonicalize_url("https://example.com:invalid/home/42"), "")

    def test_canonical_url_preserves_non_tracking_query_parameters(self) -> None:
        first = canonicalize_url("https://example.com/listing?id=1&utm_source=email")
        second = canonicalize_url("https://example.com/listing?id=2&utm_source=email")

        self.assertEqual(first, "https://example.com/listing?id=1")
        self.assertNotEqual(first, second)


if __name__ == "__main__":
    unittest.main()
