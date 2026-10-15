"""Tests for the Tecnocasa public-sitemap adapter."""

import gzip
import html
import json
import unittest

from se_admiten_humanos.models import WebsiteDiscovery
from se_admiten_humanos.tecnocasa import (
    SITEMAP_URLS,
    TecnocasaSource,
    parse_listing,
    parse_sitemap,
)


SITEMAP = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://www.tecnocasa.es/alquiler/piso/madrid/madrid/539317.html</loc><lastmod>2026-10-15</lastmod></url>
  <url><loc>https://www.tecnocasa.es/alquiler/local-comercial/madrid/madrid/12.html</loc></url>
  <url><loc>https://www.tecnocasa.es/venta/piso/madrid/madrid/13.html</loc></url>
  <url><loc>https://www.tecnocasa.es/alquiler/piso/madrid/mostoles/14.html</loc></url>
</urlset>"""

ESTATE = {
    "id": 539317,
    "title": "Piso en alquiler en Tetuán",
    "price": "900 € / Mes",
    "numeric_price": 900,
    "numeric_surface": "54.00",
    "rooms": "2 dorm.",
    "address": "C. Aranjuez",
    "city": {"title": "Madrid"},
    "district": {"title": "Cuatro Caminos"},
    "description": "<p>Piso amueblado. No se admiten animales.</p>",
    "data": [{"label": "Dormitorios", "valore": 2}],
    "agency": {"name": "Tecnocasa Cuatro Caminos", "email": "md075@tecnocasa.es"},
}
LISTING = f'<html><body><estate-detail :estate="{html.escape(json.dumps(ESTATE))}"></estate-detail></body></html>'


class TecnocasaTests(unittest.TestCase):
    def test_sitemap_keeps_only_madrid_city_residential_rentals(self) -> None:
        discoveries = parse_sitemap(SITEMAP)

        self.assertEqual(len(discoveries), 1)
        self.assertEqual(discoveries[0].external_id, "539317")
        self.assertEqual(discoveries[0].source_last_modified, "2026-10-15")

    def test_sitemap_contract_failure_is_visible(self) -> None:
        with self.assertRaisesRegex(ValueError, "no Madrid residential rentals"):
            parse_sitemap("<urlset></urlset>")

    def test_listing_parses_embedded_estate_and_pet_evidence(self) -> None:
        discovery = WebsiteDiscovery(
            "539317",
            "https://www.tecnocasa.es/alquiler/piso/madrid/madrid/539317.html",
        )

        listing = parse_listing(LISTING, discovery)

        self.assertEqual(listing.source_id, "tecnocasa")
        self.assertEqual(listing.price_eur, 900)
        self.assertEqual(listing.bedrooms, 2)
        self.assertEqual(listing.area_sqm, 54)
        self.assertEqual(listing.municipality, "Madrid")
        self.assertEqual(listing.district, "Cuatro Caminos")
        self.assertTrue(listing.is_furnished)
        self.assertIn("No se admiten animales", listing.description)

    def test_source_reads_both_compressed_sitemaps_and_exact_detail(self) -> None:
        requests: list[str] = []

        def fetch(url: str) -> bytes:
            requests.append(url)
            if url in SITEMAP_URLS:
                unique = SITEMAP.replace("539317", "539318", 1) if url == SITEMAP_URLS[1] else SITEMAP
                return gzip.compress(unique.encode())
            return LISTING.encode()

        source = TecnocasaSource(fetch, minimum_detail_interval_seconds=0)
        discoveries = source.discover()
        listing = source.fetch_listing(discoveries[0])

        self.assertEqual([item.external_id for item in discoveries], ["539317", "539318"])
        self.assertEqual(listing.external_id, "539317")
        self.assertEqual(requests[:2], list(SITEMAP_URLS))

    def test_listing_identity_mismatch_fails_visibly(self) -> None:
        discovery = WebsiteDiscovery(
            "1", "https://www.tecnocasa.es/alquiler/piso/madrid/madrid/1.html"
        )
        with self.assertRaisesRegex(ValueError, "identity"):
            parse_listing(LISTING, discovery)


if __name__ == "__main__":
    unittest.main()
