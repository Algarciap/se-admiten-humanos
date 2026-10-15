"""Tests for conservative property-type filtering."""

import unittest

from se_admiten_humanos.models import PropertyType
from se_admiten_humanos.property_type import classify_property_type


class PropertyTypeTests(unittest.TestCase):
    def test_explicit_garage_is_not_a_home(self) -> None:
        self.assertEqual(
            classify_property_type("Plaza de garaje en alquiler"),
            PropertyType.GARAGE,
        )

    def test_room_category_is_detected_without_confusing_bedroom_counts(self) -> None:
        self.assertEqual(
            classify_property_type("Alquiler de habitación en Lavapiés"),
            PropertyType.ROOM,
        )
        self.assertEqual(
            classify_property_type("Piso de 3 habitaciones"),
            PropertyType.HOME,
        )


if __name__ == "__main__":
    unittest.main()
