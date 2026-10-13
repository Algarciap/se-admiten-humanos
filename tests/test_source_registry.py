"""Tests for source-registry loading and URL validation."""

import unittest

from se_admiten_humanos.source_registry import default_registry


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


if __name__ == "__main__":
    unittest.main()
