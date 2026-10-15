"""Tests for the Redpiso public-sitemap adapter."""

import html
import json
import unittest

from se_admiten_humanos.models import RentalDuration, WebsiteDiscovery
from se_admiten_humanos.redpiso import SITEMAP_URL, RedpisoSource, parse_listing, parse_sitemap


URL = "https://www.redpiso.es/inmueble/piso-en-alquiler-en-calle-luna-centro-madrid-RP1332026157718"
SITEMAP = f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>{URL}</loc><lastmod>2026-10-15T10:00:00Z</lastmod></url>
  <url><loc>https://www.redpiso.es/inmueble/piso-vacacional-en-alquiler-en-madrid-RP1</loc></url>
  <url><loc>https://www.redpiso.es/inmueble/piso-en-venta-en-madrid-RP2</loc></url>
  <url><loc>https://www.redpiso.es/inmueble/piso-alquiler-temporal-centro-madrid-RP3</loc></url>
</urlset>"""

SCHEMA = {
    "@context": "https://schema.org",
    "@graph": [
        {
            "@type": "RealEstateListing",
            "identifier": "RP1332026157718",
            "name": "Piso en alquiler en Calle Luna, Centro, Madrid",
            "description": "Piso de 48 m² en Madrid",
            "offers": {"price": 900, "priceCurrency": "EUR"},
            "address": {"addressLocality": "Madrid", "addressRegion": "Madrid"},
        }
    ],
}
NUXT = [
    {"property": 1},
    "<p>Vivienda amueblada con 1 dormitorio. Se aceptan mascotas.</p>",
]
LISTING = f"""<html><head>
<script type="application/ld+json">{json.dumps(SCHEMA)}</script>
</head><body>
<div id="property_meta" data-office-name="Redpiso Centro" data-office-email="centro@redpiso.es"></div>
<script type="application/json" id="__NUXT_DATA__">{json.dumps(NUXT)}</script>
</body></html>"""


class RedpisoTests(unittest.TestCase):
    def test_sitemap_keeps_only_long_term_madrid_rentals(self) -> None:
        discoveries = parse_sitemap(SITEMAP)

        self.assertEqual(len(discoveries), 2)
        self.assertEqual(discoveries[0].external_id, "RP1332026157718")
        self.assertEqual(discoveries[0].source_last_modified, "2026-10-15T10:00:00Z")

    def test_sitemap_contract_failure_is_visible(self) -> None:
        with self.assertRaisesRegex(ValueError, "no Madrid residential rentals"):
            parse_sitemap("<urlset></urlset>")

    def test_listing_uses_full_description_as_pet_evidence(self) -> None:
        discovery = WebsiteDiscovery("RP1332026157718", URL)

        listing = parse_listing(LISTING, discovery)

        self.assertEqual(listing.source_id, "redpiso")
        self.assertEqual(listing.price_eur, 900)
        self.assertEqual(listing.bedrooms, 1)
        self.assertEqual(listing.area_sqm, 48)
        self.assertTrue(listing.is_furnished)
        self.assertEqual(listing.contact_email, "centro@redpiso.es")
        self.assertIn("Se aceptan mascotas", listing.description)
        self.assertIs(listing.rental_duration, RentalDuration.LONG_TERM)

    def test_source_fetches_public_sitemap_then_exact_detail(self) -> None:
        requests: list[str] = []

        def fetch(url: str) -> str:
            requests.append(url)
            return SITEMAP if url == SITEMAP_URL else LISTING

        source = RedpisoSource(fetch, minimum_detail_interval_seconds=0)
        discovery = source.discover()[0]
        listing = source.fetch_listing(discovery)

        self.assertEqual(listing.external_id, "RP1332026157718")
        self.assertEqual(requests, [SITEMAP_URL, URL])

    def test_listing_identity_mismatch_fails_visibly(self) -> None:
        changed = html.unescape(LISTING).replace("RP1332026157718", "RP999")
        with self.assertRaisesRegex(ValueError, "identity"):
            parse_listing(changed, WebsiteDiscovery("RP1332026157718", URL))


if __name__ == "__main__":
    unittest.main()
