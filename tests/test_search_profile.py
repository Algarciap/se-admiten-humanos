"""Tests for Madrid search-profile eligibility."""

import unittest

from se_admiten_humanos.models import Listing, PropertyType, RentalDuration
from se_admiten_humanos.pet_policy import classify
from se_admiten_humanos.search_profile import MatchTier, SearchProfile, evaluate_listing


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

    def test_listing_below_minimum_bedrooms_is_rejected(self) -> None:
        profile = SearchProfile(
            id="three-bedroom-search",
            user_id="user-1",
            name="Madrid con 3 dormitorios",
            max_rent_eur=1_500,
            municipality="Madrid",
            preferred_min_bedrooms=3,
        )

        decision = evaluate_listing(
            Listing(price_eur=995, municipality="Madrid", bedrooms=1),
            classify(""),
            profile,
        )

        self.assertFalse(decision.eligible)
        self.assertIn("below_preferred_bedrooms", decision.rejection_reasons)

    def test_garages_are_rejected_from_default_home_search(self) -> None:
        listing = Listing(
            price_eur=100,
            municipality="Madrid",
            property_type=PropertyType.GARAGE,
        )

        decision = evaluate_listing(listing, classify(""), self.profile)

        self.assertFalse(decision.eligible)
        self.assertIn("property_type_mismatch", decision.rejection_reasons)

    def test_room_search_is_independent_from_full_home_search(self) -> None:
        room_profile = SearchProfile(
            id="room-search",
            user_id="user-1",
            name="Habitación",
            max_rent_eur=700,
            municipality="Madrid",
            accepted_property_types=(PropertyType.ROOM,),
        )
        room = Listing(
            price_eur=650,
            municipality="Madrid",
            property_type=PropertyType.ROOM,
        )

        self.assertTrue(evaluate_listing(room, classify(""), room_profile).eligible)
        self.assertFalse(evaluate_listing(room, classify(""), self.profile).eligible)

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

    def test_urgent_search_labels_a_listing_inside_the_exceptional_ceiling(self) -> None:
        profile = SearchProfile(
            id="urgent-search",
            user_id="user-1",
            name="Madrid urgente",
            max_rent_eur=500,
            municipality="Madrid",
            urgent=True,
            stretch_max_rent_eur=550,
        )

        decision = evaluate_listing(
            Listing(price_eur=520, municipality="Madrid", bedrooms=1),
            classify(""),
            profile,
        )

        self.assertTrue(decision.eligible)
        self.assertIs(decision.match_tier, MatchTier.BUDGET_STRETCH)
        self.assertIn("budget_stretch", decision.preference_notes)

    def test_exceptional_ceiling_is_absolute(self) -> None:
        profile = SearchProfile(
            id="urgent-search",
            user_id="user-1",
            name="Madrid urgente",
            max_rent_eur=500,
            municipality="Madrid",
            urgent=True,
            stretch_max_rent_eur=550,
        )

        decision = evaluate_listing(
            Listing(price_eur=551, municipality="Madrid", bedrooms=1),
            classify(""),
            profile,
        )

        self.assertFalse(decision.eligible)
        self.assertIn("rent_above_maximum", decision.rejection_reasons)

    def test_non_urgent_search_never_stretches_the_budget(self) -> None:
        decision = evaluate_listing(
            Listing(price_eur=520, municipality="Madrid", bedrooms=1),
            classify(""),
            SearchProfile(
                id="strict-search",
                user_id="user-1",
                name="Madrid estricta",
                max_rent_eur=500,
                municipality="Madrid",
            ),
        )

        self.assertFalse(decision.eligible)
        self.assertIs(decision.match_tier, MatchTier.EXACT)

    def test_stretch_budget_requires_urgent_mode_and_a_higher_ceiling(self) -> None:
        with self.assertRaisesRegex(ValueError, "requires urgent"):
            SearchProfile(
                id="invalid-stretch",
                user_id="user-1",
                name="Invalid",
                max_rent_eur=500,
                municipality="Madrid",
                stretch_max_rent_eur=550,
            )
        with self.assertRaisesRegex(ValueError, "greater than the preferred"):
            SearchProfile(
                id="invalid-ceiling",
                user_id="user-1",
                name="Invalid",
                max_rent_eur=500,
                municipality="Madrid",
                urgent=True,
                stretch_max_rent_eur=500,
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
