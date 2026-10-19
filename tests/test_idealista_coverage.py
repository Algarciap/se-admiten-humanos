"""Tests for profile-driven Idealista source coverage."""

import unittest

from se_admiten_humanos.idealista_coverage import plan_idealista_source_searches
from se_admiten_humanos.models import PropertyType
from se_admiten_humanos.search_profile import SearchProfile


class IdealistaCoverageTests(unittest.TestCase):
    def test_overlapping_profiles_share_broad_alert_but_keep_personal_filters(self):
        profiles = (
            SearchProfile(
                id="cats-two-bedrooms", user_id="one", name="Madrid",
                max_rent_eur=1200, municipality="Madrid",
                preferred_min_bedrooms=2, requires_pet_acceptance=True,
            ),
            SearchProfile(
                id="no-pets-one-bedroom", user_id="two", name="Madrid",
                max_rent_eur=1500, municipality="madrid",
                preferred_min_bedrooms=1,
            ),
        )

        plan = plan_idealista_source_searches(profiles)

        self.assertEqual(len(plan), 1)
        self.assertEqual(plan[0].max_price_eur, 1500)
        self.assertEqual(plan[0].profile_count, 2)
        self.assertNotIn("bedrooms", plan[0].to_dict()["source_filters"])
        self.assertIn("bedrooms", plan[0].to_dict()["matched_after_ingestion"])
        self.assertIn("pets", plan[0].to_dict()["matched_after_ingestion"])

    def test_property_types_are_separate_idealista_alerts(self):
        profile = SearchProfile(
            id="both", user_id="one", name="Madrid", max_rent_eur=900,
            municipality="Madrid",
            accepted_property_types=(PropertyType.HOME, PropertyType.ROOM),
        )
        plan = plan_idealista_source_searches((profile,))
        self.assertEqual(
            {item.property_type for item in plan},
            {PropertyType.HOME, PropertyType.ROOM},
        )

    def test_urgent_stretch_budget_sets_source_ceiling(self):
        profile = SearchProfile(
            id="urgent", user_id="one", name="Madrid", max_rent_eur=1000,
            municipality="Madrid", urgent=True, stretch_max_rent_eur=1150,
        )
        self.assertEqual(plan_idealista_source_searches((profile,))[0].max_price_eur, 1150)


if __name__ == "__main__":
    unittest.main()
