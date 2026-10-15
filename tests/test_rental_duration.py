"""Tests for conservative rental-duration classification."""

import unittest

from se_admiten_humanos.models import RentalDuration
from se_admiten_humanos.rental_duration import classify_rental_duration


class RentalDurationTests(unittest.TestCase):
    def test_explicit_temporary_phrase_wins_over_source_default(self) -> None:
        duration, evidence = classify_rental_duration(
            "Contrato de temporada por 11 meses",
            category_default=RentalDuration.LONG_TERM,
        )

        self.assertIs(duration, RentalDuration.TEMPORARY)
        self.assertEqual(evidence, "contrato de temporada")

    def test_explicit_long_term_phrase_is_preserved(self) -> None:
        duration, evidence = classify_rental_duration("Solo larga duración")

        self.assertIs(duration, RentalDuration.LONG_TERM)
        self.assertEqual(evidence, "larga duracion")

    def test_silence_remains_not_specified_without_category_evidence(self) -> None:
        duration, evidence = classify_rental_duration("Bonito piso en Madrid")

        self.assertIs(duration, RentalDuration.NOT_SPECIFIED)
        self.assertEqual(evidence, "")


if __name__ == "__main__":
    unittest.main()
