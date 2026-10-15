"""Tests for private, user-specific profile configuration."""

import json
import tempfile
import unittest
from pathlib import Path

from se_admiten_humanos.cli import main
from se_admiten_humanos.persistence import RentalStore
from se_admiten_humanos.profile_config import (
    load_profile_bundle,
    load_profile_bundle_json,
)


class ProfileConfigTests(unittest.TestCase):
    def test_example_bundle_loads_without_application_defaults(self) -> None:
        root = Path(__file__).resolve().parent.parent

        bundle = load_profile_bundle(root / "config" / "profiles.local.example.json")

        self.assertEqual(bundle.user.id, "user-example")
        self.assertEqual(bundle.search_profiles[0].max_rent_eur, 1000)
        self.assertEqual(bundle.renter_profiles[0].pets[0].species, "gato")

    def test_unknown_renter_reference_fails_visibly(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profiles.json"
            payload = {
                "user": {"id": "user-1", "display_name": "User"},
                "renter_profiles": [],
                "search_profiles": [
                    {
                        "id": "search-1",
                        "user_id": "user-1",
                        "renter_profile_id": "missing",
                        "name": "Search",
                        "max_rent_eur": 1000,
                        "municipality": "Madrid",
                    }
                ],
            }
            path.write_text(json.dumps(payload), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "unknown renter"):
                load_profile_bundle(path)

    def test_cli_imports_profiles_into_sqlite(self) -> None:
        root = Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "rentals.sqlite3"

            result = main(
                (
                    "import-profiles",
                    "--file",
                    str(root / "config" / "profiles.local.example.json"),
                    "--database",
                    str(database),
                )
            )

            with RentalStore(database) as store:
                profile = store.load_search_profile("search-example")
            self.assertEqual(result, 0)
            self.assertEqual(profile.user_id, "user-example")

    def test_boolean_strings_are_rejected_instead_of_coerced(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profiles.json"
            payload = {
                "user": {"id": "user-1", "display_name": "User"},
                "renter_profiles": [],
                "search_profiles": [
                    {
                        "id": "search-1",
                        "user_id": "user-1",
                        "name": "Search",
                        "max_rent_eur": 1000,
                        "municipality": "Madrid",
                        "allow_studios": "false",
                    }
                ],
            }
            path.write_text(json.dumps(payload), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "JSON boolean"):
                load_profile_bundle(path)

    def test_bundle_can_load_from_hosted_environment_json(self) -> None:
        root = Path(__file__).resolve().parent.parent
        raw = (root / "config" / "profiles.local.example.json").read_text(
            encoding="utf-8"
        )

        bundle = load_profile_bundle_json(raw)

        self.assertEqual(bundle.user.id, "user-example")

    def test_invalid_environment_json_does_not_echo_private_value(self) -> None:
        private_value = '{"renter_name":"Private Name"'

        with self.assertRaises(ValueError) as raised:
            load_profile_bundle_json(private_value)

        self.assertNotIn("Private Name", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
