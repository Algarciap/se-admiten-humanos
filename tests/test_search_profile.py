"""Tests for Madrid search-profile eligibility."""

import unittest

from se_admiten_humanos.models import Listing, RentalDuration
from se_admiten_humanos.pet_policy import classify
from se_admiten_humanos.search_profile import SearchProfile, evaluate_listing


class SearchProfileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.profile = SearchProfile(
            id="search-madrid",
            user_id="user-1",
            name="Madrid hasta 950",
            max_rent_eur=950,
            municipality="Madrid",
            requires_pet_acceptance=True,
        )

    def test_matching_one_bedroom_is_eligible(self) -> None:
        listing = Listing(price_eur=950, municipality="Madrid", bedrooms=1)

        decision = evaluate_listing(
            listing,
            classify("Se admiten mascotas"),
            self.profile,
        )

        self.assertTrue(decision.eligible)
        self.assertEqual(decision.rejection_reasons, ())

    def test_studios_and_unknown_bedrooms_remain_eligible(self) -> None:
        cases = ((0, "studio"), (None, "bedrooms_not_disclosed"))
        for bedrooms, expected_note in cases:
            with self.subTest(bedrooms=bedrooms):
                listing = Listing(
                    price_eur=900,
                    municipality="Madrid capital",
                    bedrooms=bedrooms,
                )
                decision = evaluate_listing(listing, classify(""), self.profile)
                self.assertTrue(decision.eligible)
                self.assertIn(expected_note, decision.preference_notes)

    def test_furniture_and_unknown_availability_do_not_exclude(self) -> None:
        listing = Listing(
            price_eur=900,
            municipality="Madrid ciudad",
            bedrooms=1,
            is_furnished=False,
        )

        decision = evaluate_listing(listing, classify(""), self.profile)

        self.assertTrue(decision.eligible)
        self.assertIn("availability_not_disclosed", decision.preference_notes)

    def test_hard_filters_reject_listing(self) -> None:
        listing = Listing(price_eur=951, municipality="Getafe", bedrooms=1)

        decision = evaluate_listing(
            listing,
            classify("No se admiten animales"),
            self.profile,
        )

        self.assertFalse(decision.eligible)
        self.assertEqual(
            set(decision.rejection_reasons),
            {
                "rent_above_maximum",
                "outside_selected_municipality",
                "pets_rejected",
            },
        )

    def test_missing_rent_or_municipality_fails_closed(self) -> None:
        decision = evaluate_listing(Listing(), classify(""), self.profile)

        self.assertFalse(decision.eligible)
        self.assertIn("rent_not_disclosed", decision.rejection_reasons)
        self.assertIn("municipality_not_disclosed", decision.rejection_reasons)

    def test_invalid_profile_fails_visibly(self) -> None:
        with self.assertRaisesRegex(ValueError, "Maximum rent"):
            SearchProfile(
                id="invalid",
                user_id="user-1",
                name="Invalid",
                max_rent_eur=0,
                municipality="Madrid",
            )

    def test_different_users_can_apply_different_filters(self) -> None:
        listing = Listing(price_eur=1100, municipality="Madrid", bedrooms=0)
        flexible_profile = SearchProfile(
            id="search-flexible",
            user_id="user-2",
            name="Madrid flexible",
            max_rent_eur=1200,
            municipality="Madrid",
            allow_studios=True,
            requires_pet_acceptance=False,
        )
        pet_ban = classify("No se admiten animales")

        first_decision = evaluate_listing(listing, pet_ban, self.profile)
        second_decision = evaluate_listing(listing, pet_ban, flexible_profile)

        self.assertFalse(first_decision.eligible)
        self.assertTrue(second_decision.eligible)

    def test_selected_districts_are_profile_specific(self) -> None:
        profile = SearchProfile(
            id="search-centre",
            user_id="user-1",
            name="Centro y Chamberí",
            max_rent_eur=1200,
            municipality="Madrid",
            acceptable_districts=("Centro", "Chamberí"),
        )

        accepted = evaluate_listing(
            Listing(
                price_eur=1000,
                municipality="Madrid",
                district="Chamberi",
                bedrooms=1,
            ),
            classify(""),
            profile,
        )
        rejected = evaluate_listing(
            Listing(
                price_eur=1000,
                municipality="Madrid",
                district="Retiro",
                bedrooms=1,
            ),
            classify(""),
            profile,
        )

        self.assertTrue(accepted.eligible)
        self.assertIn("outside_selected_districts", rejected.rejection_reasons)

    def test_rental_duration_is_profile_specific(self) -> None:
        temporary = Listing(
            price_eur=900,
            municipality="Madrid",
            bedrooms=1,
            rental_duration=RentalDuration.TEMPORARY,
        )
        long_term_decision = evaluate_listing(temporary, classify(""), self.profile)
        temporary_profile = SearchProfile(
            id="temporary",
            user_id="user-temporary",
            name="Madrid temporal",
            max_rent_eur=950,
            municipality="Madrid",
            accepted_rental_durations=(RentalDuration.TEMPORARY,),
        )

        temporary_decision = evaluate_listing(
            temporary, classify(""), temporary_profile
        )

        self.assertFalse(long_term_decision.eligible)
        self.assertIn("rental_duration_mismatch", long_term_decision.rejection_reasons)
        self.assertTrue(temporary_decision.eligible)

    def test_unknown_rental_duration_is_not_mislabelled(self) -> None:
        listing = Listing(price_eur=900, municipality="Madrid", bedrooms=1)

        decision = evaluate_listing(listing, classify(""), self.profile)

        self.assertTrue(decision.eligible)
        self.assertIn("rental_duration_not_disclosed", decision.preference_notes)


if __name__ == "__main__":
    unittest.main()
