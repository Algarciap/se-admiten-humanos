"""Tests for the first public website adapter."""

import unittest

from se_admiten_humanos.alquiler_seguro import (
    SITEMAP_URL,
    AlquilerSeguroSource,
    parse_listing,
    parse_sitemap,
)
from se_admiten_humanos.models import WebsiteDiscovery


SITEMAP = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url>
    <loc>https://www.alquilerseguro.es/viviendas/11986</loc>
    <lastmod>2026-10-14</lastmod>
  </url>
  <url><loc>https://www.alquilerseguro.es/contacto</loc></url>
</urlset>
"""

LISTING = """<!doctype html><html><head>
<meta name="description" content="Piso luminoso. No se aceptan animales.">
<meta property="og:title" content="Calle Arganda - MADRID - Alquiler Seguro">
</head><body>
<h1 class="titulo-inmueble">piso en Calle Arganda, MADRID MADRID</h1>
<p class="precio mt-3">1.100 &#x20AC;</p>
<div class="margen-caracteristicas">
  <span><i title="Habitaciones"></i>1 Habitaciones</span>
  <span><i title="Superficie"></i>68 m<sup>2</sup></span>
  <span>Ref: 2818-038402</span>
</div>
<p class="card-precio-inmueble">9.999 €/mes</p>
</body></html>
"""


class AlquilerSeguroTests(unittest.TestCase):
    def test_sitemap_accepts_only_exact_public_listing_urls(self) -> None:
        discoveries = parse_sitemap(SITEMAP)

        self.assertEqual(len(discoveries), 1)
        self.assertEqual(discoveries[0].external_id, "11986")
        self.assertEqual(discoveries[0].source_last_modified, "2026-10-14")

    def test_sitemap_contract_failure_is_visible(self) -> None:
        with self.assertRaisesRegex(ValueError, "no valid listing"):
            parse_sitemap("<urlset></urlset>")

    def test_listing_parser_uses_main_property_not_recommendation_cards(self) -> None:
        discovery = WebsiteDiscovery(
            "11986",
            "https://www.alquilerseguro.es/viviendas/11986",
        )

        listing = parse_listing(LISTING, discovery)

        self.assertEqual(listing.source_id, "alquiler_seguro")
        self.assertEqual(listing.price_eur, 1100)
        self.assertEqual(listing.bedrooms, 1)
        self.assertEqual(listing.area_sqm, 68)
        self.assertEqual(listing.municipality, "Madrid")
        self.assertEqual(listing.address, "Calle Arganda")
        self.assertIn("No se aceptan animales", listing.description)

    def test_listing_contract_failure_is_visible(self) -> None:
        discovery = WebsiteDiscovery(
            "11986",
            "https://www.alquilerseguro.es/viviendas/11986",
        )

        with self.assertRaisesRegex(ValueError, "missing title"):
            parse_listing("<html></html>", discovery)

    def test_source_fetches_sitemap_then_exact_detail_url(self) -> None:
        requests: list[str] = []

        def fetch(url: str) -> str:
            requests.append(url)
            return SITEMAP if url == SITEMAP_URL else LISTING

        source = AlquilerSeguroSource(fetch, minimum_detail_interval_seconds=0)
        discovery = source.discover()[0]
        listing = source.fetch_listing(discovery)

        self.assertIsNotNone(listing)
        self.assertEqual(
            requests,
            [SITEMAP_URL, "https://www.alquilerseguro.es/viviendas/11986"],
        )


if __name__ == "__main__":
    unittest.main()
