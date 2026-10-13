"""Tests for source-registry loading and URL validation."""

import unittest

from se_admiten_humanos.source_registry import (
    REQUIRED_FIELDS,
    SourceRegistry,
    default_registry,
)


def valid_source(**overrides: object) -> dict[str, object]:
    """Build one complete registry entry for focused validation tests."""
    source: dict[str, object] = {
        "id": "example",
        "name": "Example",
        "kind": "agency",
        "priority": 1,
        "base_url": "https://example.com/",
        "search_url": "https://example.com/rentals",
        "coverage": "Madrid city",
        "acquisition": "manual",
        "automation_status": "planned",
        "pet_signal": "description_only",
        "direct_contact": "email",
        "evidence_url": "https://example.com/policy",
        "notes": "Example source.",
        "verified_on": "2026-10-13",
    }
    source.update(overrides)
    return source


class SourceRegistryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.registry = default_registry()

    def test_all_sources_load(self) -> None:
        self.assertEqual(self.registry.count, 24)

    def test_recognizes_supported_source_domains(self) -> None:
        self.assertEqual(
            self.registry.source_id_from_text(
                "alerta@idealista.com https://www.idealista.com/inmueble/1"
            ),
            "idealista",
        )
        self.assertEqual(
            self.registry.source_id_from_text("https://www.vivenio.com/edificio/example"),
            "vivenio",
        )

    def test_allows_only_registered_listing_domains(self) -> None:
        self.assertTrue(
            self.registry.allowed_listing_url(
                "https://www.testahomes.com/viviendas/example"
            )
        )
        self.assertFalse(
            self.registry.allowed_listing_url("https://evil.example/idealista.com")
        )
        self.assertFalse(self.registry.allowed_listing_url("not a url"))

    def test_rejects_urls_with_embedded_credentials(self) -> None:
        self.assertFalse(
            self.registry.allowed_listing_url("https://evil.test@idealista.com/inmueble/1")
        )

    def test_required_field_fixture_stays_complete(self) -> None:
        self.assertEqual(set(valid_source()), set(REQUIRED_FIELDS))

    def test_rejects_empty_required_values(self) -> None:
        with self.assertRaisesRegex(ValueError, "empty values: name"):
            SourceRegistry([valid_source(name="  ")])

    def test_rejects_invalid_search_and_evidence_urls(self) -> None:
        with self.assertRaisesRegex(ValueError, "Invalid source URL"):
            SourceRegistry([valid_source(search_url="not a URL")])
        with self.assertRaisesRegex(ValueError, "Invalid source URL"):
            SourceRegistry([valid_source(evidence_url="ftp://example.com/policy")])


if __name__ == "__main__":
    unittest.main()
