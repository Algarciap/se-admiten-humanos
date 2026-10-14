"""Regression tests for Spanish outreach messages."""

import unittest

from se_admiten_humanos.models import Listing, Pet, RenterProfile
from se_admiten_humanos.outreach import build_message, subject
from se_admiten_humanos.pet_policy import classify


class OutreachTests(unittest.TestCase):
    def setUp(self) -> None:
        self.profile = RenterProfile(
            id="renter-1",
            user_id="user-1",
            label="Persona interesada",
            sender_name="Persona remitente",
            renter_name="Persona interesada",
            monthly_income_eur=2500,
            job="profesional de producción",
            contract_type="contrato indefinido",
            pets=(Pet(id="cats", species="gato", count=2),),
        )
        self.listing = Listing(
            title="Piso en Chamberí",
            price="1.250 €",
            location="Madrid",
        )

    def test_unknown_policy_asks_about_configured_pets(self) -> None:
        message = build_message(self.listing, classify(""), self.profile)
        self.assertIn("confirmarnos si se admiten dos gatos", message)
        self.assertIn("Persona remitente", message)

    def test_accepted_policy_asks_for_a_visit(self) -> None:
        message = build_message(
            self.listing,
            classify("Se admiten mascotas"),
            self.profile,
        )
        self.assertIn("Hemos visto que la vivienda admite mascotas", message)
        self.assertIn("concertar una visita", message)

    def test_subject_contains_listing_reference(self) -> None:
        self.assertEqual(
            subject(self.listing),
            "Interés en alquiler: Piso en Chamberí · 1.250 € · Madrid",
        )

    def test_pet_free_profile_does_not_ask_about_pets(self) -> None:
        profile = RenterProfile(
            id="renter-2",
            user_id="user-2",
            label="No pets",
            sender_name="Sender",
            renter_name="Renter",
        )

        message = build_message(self.listing, classify(""), profile)

        self.assertIn("No viviría con mascotas", message)
        self.assertNotIn("si se admiten", message)


if __name__ == "__main__":
    unittest.main()
