"""Tests for the Nestar public-sitemap adapter."""

import json
import unittest

from se_admiten_humanos.models import RentalDuration, WebsiteDiscovery
from se_admiten_humanos.nestar import SITEMAP_URL, NestarSource, parse_listing, parse_sitemap


URL = (
    "https://www.nestarhomes.es/vivienda/"
    "vivienda-de-2-habitaciones-en-calle-mayorazgomadrid-"
    "2200-22000828-00000115"
)
SITEMAP = f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>{URL}</loc><lastmod>2026-10-15</lastmod></url>
  <url><loc>https://www.nestarhomes.es/alquiler-pisos/madrid</loc></url>
  <url><loc>https://lookalike.test/vivienda/example-2200-22000828-00000116</loc></url>
</urlset>"""


def detail(operation="ALQUILER", external_id="2200/22000828/00000115"):
    price = 949.25 if operation == "ALQUILER" else None
    payload = {
        "props": {
            "pageProps": {
                "page": {"title": "Vivienda de 2 habitaciones en Madrid"},
                "property": {
                    "codigoUnidadAlquiler": external_id,
                    "ventaAlquiler": operation,
                    "ciudad": "MADRID",
                    "importeRecibofinal": price,
                    "importeVenta": 300000 if operation == "VENTA" else None,
                    "habitaciones": "2",
                    "metrosComputablesUA": "63.70",
                    "hasAmueblado": "No",
                    "STREET": "CALLE MAYORAZGO",
                    "tipoZona": {"infoAdicional": "Vallecas"},
                    "informacionGeneral": {
                        "argumentario": "Piso de larga duración.## Consulta condiciones para mascotas."
                    },
                },
            }
        }
    }
    return (
        '<html><script id="__NEXT_DATA__" type="application/json">'
        + json.dumps(payload)
        + "</script></html>"
    )


class NestarTests(unittest.TestCase):
    def test_sitemap_keeps_only_exact_property_details(self) -> None:
        discoveries = parse_sitemap(SITEMAP)

        self.assertEqual(len(discoveries), 1)
        self.assertEqual(discoveries[0].external_id, "2200-22000828-00000115")
        self.assertEqual(discoveries[0].source_last_modified, "2026-10-15")

    def test_sitemap_contract_failure_is_visible(self) -> None:
        with self.assertRaisesRegex(ValueError, "no property detail URLs"):
            parse_sitemap("<urlset></urlset>")

    def test_rental_detail_is_normalized_without_rounding_budget_down(self) -> None:
        listing = parse_listing(
            detail(), WebsiteDiscovery("2200-22000828-00000115", URL)
        )

        assert listing is not None
        self.assertEqual(listing.price_eur, 950)
        self.assertEqual(listing.municipality, "Madrid")
        self.assertEqual(listing.district, "Vallecas")
        self.assertEqual(listing.bedrooms, 2)
        self.assertEqual(listing.area_sqm, 63)
        self.assertFalse(listing.is_furnished)
        self.assertIs(listing.rental_duration, RentalDuration.LONG_TERM)
        self.assertIn("mascotas", listing.description)

    def test_sale_detail_is_explicitly_ignored(self) -> None:
        listing = parse_listing(
            detail("VENTA"), WebsiteDiscovery("2200-22000828-00000115", URL)
        )

        self.assertIsNone(listing)

    def test_identity_mismatch_fails_visibly(self) -> None:
        with self.assertRaisesRegex(ValueError, "identity"):
            parse_listing(
                detail(external_id="2200/22000828/99999999"),
                WebsiteDiscovery("2200-22000828-00000115", URL),
            )

    def test_source_fetches_public_sitemap_then_exact_detail(self) -> None:
        requests: list[str] = []

        def fetch(url: str) -> str:
            requests.append(url)
            return SITEMAP if url == SITEMAP_URL else detail()

        source = NestarSource(fetch, minimum_detail_interval_seconds=0)
        discovery = source.discover()[0]
        listing = source.fetch_listing(discovery)

        assert listing is not None
        self.assertEqual(listing.external_id, "2200-22000828-00000115")
        self.assertEqual(requests, [SITEMAP_URL, URL])


if __name__ == "__main__":
    unittest.main()
