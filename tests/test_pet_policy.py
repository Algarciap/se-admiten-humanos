"""Regression tests for pet-policy classification."""

import unittest

from se_admiten_humanos.models import PetStatus
from se_admiten_humanos.pet_policy import classify


class PetPolicyTests(unittest.TestCase):
    def test_text_cases(self) -> None:
        cases = (
            ("No se admiten animales.", PetStatus.REJECTED),
            ("No aceptamos mascotas", PetStatus.REJECTED),
            ("Se alquila sin mascotas", PetStatus.REJECTED),
            ("Mascotas no", PetStatus.REJECTED),
            ("No pets allowed", PetStatus.REJECTED),
            ("Se aceptan mascotas", PetStatus.ACCEPTED),
            ("Pet friendly", PetStatus.ACCEPTED),
            ("No hay problema con las mascotas", PetStatus.ACCEPTED),
            ("Gatos permitidos", PetStatus.ACCEPTED),
            ("Mascotas a consultar", PetStatus.CONDITIONAL),
            ("Solo se admiten animales pequeños", PetStatus.CONDITIONAL),
            ("No se admiten mascotas de gran tamaño", PetStatus.CONDITIONAL),
            ("No se admiten animales excepto gatos", PetStatus.ACCEPTED),
            ("No perros, gatos sí", PetStatus.ACCEPTED),
            ("No perros", PetStatus.NOT_SPECIFIED),
            ("No gatos", PetStatus.REJECTED),
            ("Piso luminoso junto al metro", PetStatus.NOT_SPECIFIED),
        )

        for text, expected in cases:
            with self.subTest(text=text):
                self.assertIs(classify(text).status, expected)

    def test_structured_field_takes_precedence(self) -> None:
        self.assertIs(classify("", True).status, PetStatus.ACCEPTED)
        self.assertIs(classify("Se aceptan mascotas", False).status, PetStatus.REJECTED)

    def test_rejected_decision_suppresses_notification_and_contact(self) -> None:
        decision = classify("No se admiten mascotas")
        self.assertFalse(decision.should_notify)
        self.assertFalse(decision.should_contact)

    def test_evidence_preserves_the_matching_phrase(self) -> None:
        decision = classify("Piso exterior. Mascotas a consultar con la propiedad.")
        self.assertEqual(decision.evidence, "Mascotas a consultar")


if __name__ == "__main__":
    unittest.main()
