"""Tests for Madrid search-profile eligibility."""

import unittest

from se_admiten_humanos.models import Listing
from se_admiten_humanos.pet_policy import classify
from se_admiten_humanos.search_profile import SearchProfile, evaluate_listing


class SearchProfileTests(unittest.TestCase):
    def test_matching_one_bedroom_is_eligible(self) -> None:
        listing = Listing(price_eur=950, municipality="Madrid", bedrooms=1)

        decision = evaluate_listing(listing, classify("Se admiten mascotas"))

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
                decision = evaluate_listing(listing, classify(""))
                self.assertTrue(decision.eligible)
                self.assertIn(expected_note, decision.preference_notes)

    def test_furniture_and_unknown_availability_do_not_exclude(self) -> None:
        listing = Listing(
            price_eur=900,
            municipality="Madrid ciudad",
            bedrooms=1,
            is_furnished=False,
        )

        decision = evaluate_listing(listing, classify(""))

        self.assertTrue(decision.eligible)
        self.assertIn("availability_not_disclosed", decision.preference_notes)

    def test_hard_filters_reject_listing(self) -> None:
        listing = Listing(price_eur=951, municipality="Getafe", bedrooms=1)

        decision = evaluate_listing(listing, classify("No se admiten animales"))

        self.assertFalse(decision.eligible)
        self.assertEqual(
            set(decision.rejection_reasons),
            {"rent_above_maximum", "outside_madrid_city", "pets_rejected"},
        )

    def test_missing_rent_or_municipality_fails_closed(self) -> None:
        decision = evaluate_listing(Listing(), classify(""))

        self.assertFalse(decision.eligible)
        self.assertIn("rent_not_disclosed", decision.rejection_reasons)
        self.assertIn("municipality_not_disclosed", decision.rejection_reasons)

    def test_invalid_profile_fails_visibly(self) -> None:
        with self.assertRaisesRegex(ValueError, "Maximum rent"):
            SearchProfile(max_rent_eur=0)


if __name__ == "__main__":
    unittest.main()
