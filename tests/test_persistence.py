"""Tests for SQLite storage, deduplication, and notification claims."""

import sqlite3
import tempfile
import unittest
from pathlib import Path

from se_admiten_humanos.models import Listing, Pet, RentalDuration, RenterProfile, User
from se_admiten_humanos.persistence import RentalStore, canonicalize_url
from se_admiten_humanos.pet_policy import classify
from se_admiten_humanos.search_profile import SearchProfile, evaluate_listing


class PersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_directory.name) / "rentals.sqlite3"
        self.store = RentalStore(self.database_path)
        self.user = User(id="user-1", display_name="First user")
        self.renter_profile = RenterProfile(
            id="renter-1",
            user_id=self.user.id,
            label="Primary renter",
            sender_name="Sender",
            renter_name="Renter",
            monthly_income_eur=2500,
            job="production professional",
            contract_type="contrato indefinido",
            pets=(Pet(id="pet-1", species="gato", count=2),),
        )
        self.search_profile = SearchProfile(
            id="search-1",
            user_id=self.user.id,
            name="Madrid",
            max_rent_eur=950,
            municipality="Madrid",
            renter_profile_id=self.renter_profile.id,
            requires_pet_acceptance=True,
        )
        self.store.save_user(self.user)
        self.store.save_renter_profile(self.renter_profile)
        self.store.save_search_profile(self.search_profile)

    def tearDown(self) -> None:
        self.store.close()
        self.temp_directory.cleanup()

    def _record_eligible_match(
        self,
        external_id: str = "123",
        profile: SearchProfile | None = None,
    ) -> int:
        selected_profile = profile or self.search_profile
        listing = Listing(
            source_id="idealista",
            external_id=external_id,
            price_eur=900,
            municipality="Madrid",
            bedrooms=1,
        )
        stored = self.store.upsert(listing)
        pet_decision = classify("")
        search_decision = evaluate_listing(
            listing,
            pet_decision,
            selected_profile,
        )
        match = self.store.record_match(
            stored.listing_id,
            selected_profile.id,
            search_decision,
            pet_decision,
        )
        return match.match_id

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
        match_id = self._record_eligible_match()

        self.assertTrue(self.store.claim_notification(match_id, "telegram"))
        self.assertFalse(self.store.claim_notification(match_id, "telegram"))
        self.store.mark_notification_failed(match_id, "telegram", "timeout")
        self.assertTrue(self.store.claim_notification(match_id, "telegram"))
        self.store.mark_notification_sent(match_id, "telegram")
        self.assertFalse(self.store.claim_notification(match_id, "telegram"))

    def test_notification_claim_is_visible_to_another_store(self) -> None:
        match_id = self._record_eligible_match()
        self.assertTrue(self.store.claim_notification(match_id, "telegram"))

        with RentalStore(self.database_path) as second_store:
            self.assertFalse(second_store.claim_notification(match_id, "telegram"))

    def test_same_listing_notifies_two_profiles_independently(self) -> None:
        second_user = User(id="user-2", display_name="Second user")
        second_profile = SearchProfile(
            id="search-2",
            user_id=second_user.id,
            name="Madrid flexible",
            max_rent_eur=1200,
            municipality="Madrid",
        )
        self.store.save_user(second_user)
        self.store.save_search_profile(second_profile)

        first_match_id = self._record_eligible_match()
        second_match_id = self._record_eligible_match(profile=second_profile)

        self.assertNotEqual(first_match_id, second_match_id)
        self.assertTrue(self.store.claim_notification(first_match_id, "telegram"))
        self.assertTrue(self.store.claim_notification(second_match_id, "telegram"))
        self.assertFalse(self.store.claim_notification(first_match_id, "telegram"))

    def test_telegram_destinations_are_isolated_per_user(self) -> None:
        second_user = User(id="user-2", display_name="Second user")
        self.store.save_user(second_user)

        self.store.save_telegram_destination(self.user.id, "111111")
        self.store.save_telegram_destination(second_user.id, "222222")

        self.assertEqual(self.store.telegram_chat_id(self.user.id), "111111")
        self.assertEqual(self.store.telegram_chat_id(second_user.id), "222222")
        self.assertEqual(
            self.store.load_telegram_destinations(),
            {"user-1": "111111", "user-2": "222222"},
        )

    def test_telegram_chat_cannot_be_linked_to_two_users(self) -> None:
        second_user = User(id="user-2", display_name="Second user")
        self.store.save_user(second_user)
        self.store.save_telegram_destination(self.user.id, "111111")

        with self.assertRaisesRegex(ValueError, "different user"):
            self.store.save_telegram_destination(second_user.id, "111111")

    def test_profile_round_trip_preserves_user_preferences(self) -> None:
        profile = SearchProfile(
            id="search-districts",
            user_id=self.user.id,
            name="Selected districts",
            max_rent_eur=1400,
            municipality="Madrid",
            urgent=True,
            stretch_max_rent_eur=1500,
            acceptable_districts=("Centro", "Chamberí"),
            preferred_min_bedrooms=2,
            allow_studios=False,
            allow_unknown_bedrooms=False,
            furnished=True,
            move_in_asap=False,
            accepted_rental_durations=(
                RentalDuration.LONG_TERM,
                RentalDuration.TEMPORARY,
            ),
            requires_pet_acceptance=True,
        )

        self.store.save_search_profile(profile)

        self.assertEqual(self.store.load_search_profile(profile.id), profile)

    def test_renter_profile_cannot_be_shared_across_users(self) -> None:
        second_user = User(id="user-2", display_name="Second user")
        self.store.save_user(second_user)
        invalid_profile = SearchProfile(
            id="invalid-search",
            user_id=second_user.id,
            name="Invalid",
            max_rent_eur=1000,
            municipality="Madrid",
            renter_profile_id=self.renter_profile.id,
        )

        with self.assertRaisesRegex(ValueError, "different user"):
            self.store.save_search_profile(invalid_profile)

    def test_existing_profile_cannot_be_moved_to_another_user(self) -> None:
        second_user = User(id="user-2", display_name="Second user")
        self.store.save_user(second_user)
        moved_profile = SearchProfile(
            id=self.search_profile.id,
            user_id=second_user.id,
            name="Moved search",
            max_rent_eur=1000,
            municipality="Madrid",
        )

        with self.assertRaisesRegex(ValueError, "ownership"):
            self.store.save_search_profile(moved_profile)

    def test_ineligible_match_cannot_be_notified(self) -> None:
        listing = Listing(
            source_id="idealista",
            external_id="over-budget",
            price_eur=1200,
            municipality="Madrid",
        )
        stored = self.store.upsert(listing)
        pet_decision = classify("")
        search_decision = evaluate_listing(
            listing,
            pet_decision,
            self.search_profile,
        )
        match = self.store.record_match(
            stored.listing_id,
            self.search_profile.id,
            search_decision,
            pet_decision,
        )

        with self.assertRaisesRegex(ValueError, "ineligible"):
            self.store.claim_notification(match.match_id, "telegram")

    def test_v1_database_migrates_without_losing_legacy_records(self) -> None:
        legacy_path = Path(self.temp_directory.name) / "legacy.sqlite3"
        connection = sqlite3.connect(legacy_path)
        connection.executescript(
            """
            create table listings (
                id integer primary key,
                fingerprint text not null unique,
                source_id text not null,
                external_id text not null,
                canonical_url text not null,
                payload_json text not null,
                first_seen_at text not null,
                last_seen_at text not null
            );
            create table listing_identities (
                identity text primary key,
                listing_id integer not null references listings(id) on delete cascade,
                identity_kind text not null
            );
            create table notifications (
                listing_id integer not null references listings(id) on delete cascade,
                channel text not null,
                status text not null,
                attempts integer not null default 1,
                last_error text not null default '',
                updated_at text not null,
                primary key (listing_id, channel)
            );
            insert into listings values (
                1, 'fingerprint', 'idealista', '123', '', '{}',
                '2026-10-13T00:00:00+00:00', '2026-10-13T00:00:00+00:00'
            );
            insert into notifications values (
                1, 'telegram', 'sent', 1, '', '2026-10-13T00:00:00+00:00'
            );
            pragma user_version = 1;
            """
        )
        connection.close()

        with RentalStore(legacy_path) as migrated:
            version = migrated.connection.execute("pragma user_version").fetchone()[0]
            listing_count = migrated.connection.execute(
                "select count(*) from listings"
            ).fetchone()[0]
            legacy_count = migrated.connection.execute(
                "select count(*) from legacy_notifications_v1"
            ).fetchone()[0]

            self.assertEqual(version, 10)
        self.assertEqual(listing_count, 1)
        self.assertEqual(legacy_count, 1)

    def test_source_run_claim_prevents_overlap_across_connections(self) -> None:
        first = self.store.begin_source_run("agency-example")
        self.assertIsNotNone(first)

        with RentalStore(self.database_path) as second_store:
            self.assertIsNone(second_store.begin_source_run("agency-example"))

        assert first is not None
        completed = self.store.finish_source_run(first.run_id, "empty")
        self.assertEqual(completed.status, "empty")

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
