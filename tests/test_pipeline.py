"""Integration tests for the normalized-listing processing pipeline."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from se_admiten_humanos.models import Listing, Pet, RenterProfile, User
from se_admiten_humanos.pet_policy import classify
from se_admiten_humanos.pipeline import ListingPipeline
from se_admiten_humanos.persistence import RentalStore
from se_admiten_humanos.search_profile import SearchProfile


class PipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_directory = tempfile.TemporaryDirectory()
        database = Path(self.temp_directory.name) / "rentals.sqlite3"
        self.store = RentalStore(database)

        pet_user = User(id="pet-user", display_name="Pet household")
        pet_renter = RenterProfile(
            id="pet-renter",
            user_id=pet_user.id,
            label="Pet renter",
            sender_name="Sender",
            renter_name="Renter",
            pets=(Pet(id="cats", species="gato", count=2),),
        )
        self.pet_profile = SearchProfile(
            id="pet-search",
            user_id=pet_user.id,
            name="Madrid with cats",
            max_rent_eur=950,
            municipality="Madrid",
            renter_profile_id=pet_renter.id,
            requires_pet_acceptance=True,
        )
        flexible_user = User(id="flex-user", display_name="Flexible household")
        self.flexible_profile = SearchProfile(
            id="flex-search",
            user_id=flexible_user.id,
            name="Madrid flexible",
            max_rent_eur=1200,
            municipality="Madrid",
        )
        disabled_profile = SearchProfile(
            id="disabled-search",
            user_id=flexible_user.id,
            name="Disabled",
            max_rent_eur=2000,
            municipality="Madrid",
            enabled=False,
        )

        self.store.save_user(pet_user)
        self.store.save_renter_profile(pet_renter)
        self.store.save_search_profile(self.pet_profile)
        self.store.save_user(flexible_user)
        self.store.save_search_profile(self.flexible_profile)
        self.store.save_search_profile(disabled_profile)
        self.pipeline = ListingPipeline(self.store)

    def tearDown(self) -> None:
        self.store.close()
        self.temp_directory.cleanup()

    @staticmethod
    def listing(**overrides: object) -> Listing:
        values: dict[str, object] = {
            "source_id": "idealista",
            "external_id": "listing-1",
            "title": "Piso luminoso",
            "description": "Disponible inmediatamente.",
            "price_eur": 900,
            "municipality": "Madrid",
            "bedrooms": 1,
        }
        values.update(overrides)
        return Listing(**values)

    def test_classifies_once_and_matches_every_enabled_profile(self) -> None:
        listing = self.listing(description="No se admiten animales.")

        with patch(
            "se_admiten_humanos.pipeline.classify",
            wraps=classify,
        ) as classifier:
            result = self.pipeline.process(listing)

        self.assertEqual(classifier.call_count, 1)
        self.assertEqual(len(result.profile_outcomes), 2)
        outcomes = {outcome.profile.id: outcome for outcome in result.profile_outcomes}
        self.assertFalse(outcomes[self.pet_profile.id].decision.eligible)
        self.assertTrue(outcomes[self.flexible_profile.id].decision.eligible)
        self.assertEqual(
            [candidate.profile.id for candidate in result.delivery_candidates],
            [self.flexible_profile.id],
        )
        self.assertNotIn("disabled-search", outcomes)
        stored_policy = self.store.connection.execute(
            """
            select pet_status, pet_evidence
            from listing_matches
            where id = ?
            """,
            (outcomes[self.pet_profile.id].match.match_id,),
        ).fetchone()
        self.assertEqual(stored_policy, ("rejected", "No se admiten animales"))

    def test_sent_notification_is_not_offered_again(self) -> None:
        first = self.pipeline.process(self.listing())
        candidate = next(
            item
            for item in first.delivery_candidates
            if item.profile.id == self.pet_profile.id
        )
        self.assertTrue(
            self.store.claim_notification(candidate.match.match_id, "telegram")
        )
        self.store.mark_notification_sent(candidate.match.match_id, "telegram")

        second = self.pipeline.process(self.listing(title="Updated title"))

        repeated = next(
            item
            for item in second.profile_outcomes
            if item.profile.id == self.pet_profile.id
        )
        self.assertFalse(second.stored_listing.is_new)
        self.assertEqual(repeated.notification_status, "sent")
        self.assertFalse(repeated.should_deliver)

    def test_pending_delivery_is_suppressed_and_failed_delivery_can_retry(self) -> None:
        first = self.pipeline.process(self.listing())
        candidate = first.delivery_candidates[0]
        match_id = candidate.match.match_id
        self.assertTrue(self.store.claim_notification(match_id, "telegram"))

        pending = self.pipeline.process(self.listing())
        pending_outcome = next(
            item for item in pending.profile_outcomes if item.match.match_id == match_id
        )
        self.assertEqual(pending_outcome.notification_status, "pending")
        self.assertFalse(pending_outcome.should_deliver)

        self.store.mark_notification_failed(match_id, "telegram", "timeout")
        retry = self.pipeline.process(self.listing())
        retry_outcome = next(
            item for item in retry.profile_outcomes if item.match.match_id == match_id
        )
        self.assertEqual(retry_outcome.notification_status, "failed")
        self.assertTrue(retry_outcome.should_deliver)

    def test_structured_pet_field_overrides_conflicting_text(self) -> None:
        listing = self.listing(description="Se aceptan mascotas")

        result = self.pipeline.process(listing, structured_pet_friendly=False)

        pet_outcome = next(
            item
            for item in result.profile_outcomes
            if item.profile.id == self.pet_profile.id
        )
        self.assertEqual(result.pet_decision.source, "structured_field")
        self.assertEqual(result.pet_decision.evidence, "pet-friendly field: no")
        self.assertFalse(pet_outcome.decision.eligible)

    def test_policy_change_can_create_a_new_delivery_candidate(self) -> None:
        rejected = self.pipeline.process(
            self.listing(description="No se admiten animales")
        )
        rejected_pet_match = next(
            item
            for item in rejected.profile_outcomes
            if item.profile.id == self.pet_profile.id
        )
        self.assertFalse(rejected_pet_match.should_deliver)

        accepted = self.pipeline.process(
            self.listing(description="Se aceptan mascotas")
        )
        accepted_pet_match = next(
            item
            for item in accepted.profile_outcomes
            if item.profile.id == self.pet_profile.id
        )

        self.assertEqual(
            rejected_pet_match.match.match_id,
            accepted_pet_match.match.match_id,
        )
        self.assertTrue(accepted_pet_match.should_deliver)

    def test_title_is_included_in_pet_policy_classification(self) -> None:
        listing = self.listing(
            title="Alquiler sin mascotas",
            description="Piso luminoso.",
        )

        result = self.pipeline.process(listing)

        self.assertEqual(result.pet_decision.evidence, "sin mascotas")

    def test_invalid_listing_identity_fails_without_creating_matches(self) -> None:
        listing = self.listing(source_id="unknown", external_id="")

        with self.assertRaisesRegex(ValueError, "needs an external_id"):
            self.pipeline.process(listing)

        match_count = self.store.connection.execute(
            "select count(*) from listing_matches"
        ).fetchone()[0]
        self.assertEqual(match_count, 0)


if __name__ == "__main__":
    unittest.main()
