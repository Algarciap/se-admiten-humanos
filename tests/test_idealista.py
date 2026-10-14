"""Contract tests for the official Idealista Search API adapter."""

import json
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import parse_qs

from se_admiten_humanos.acquisition import AcquisitionRunner
from se_admiten_humanos.idealista import (
    AUTH_URL,
    SEARCH_URL,
    IdealistaApiAdapter,
    IdealistaApiClient,
    IdealistaApiError,
    IdealistaSearchConfig,
)
from se_admiten_humanos.models import User
from se_admiten_humanos.persistence import RentalStore
from se_admiten_humanos.search_profile import SearchProfile
from se_admiten_humanos.telegram import DeliveryAttempt


class _Response(BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def api_listing(**overrides):
    listing = {
        "propertyCode": "123456",
        "url": "https://www.idealista.com/inmueble/123456/",
        "price": 900.0,
        "municipality": "Madrid",
        "district": "Centro",
        "neighborhood": "Lavapiés-Embajadores",
        "address": "Calle de prueba, 1",
        "showAddress": False,
        "rooms": 0,
        "size": 39.6,
        "furnished": True,
        "description": "Se admiten mascotas.",
        "suggestedTexts": {"title": "Estudio en alquiler"},
    }
    listing.update(overrides)
    return listing


class IdealistaTests(unittest.TestCase):
    def test_search_parameters_are_bounded_for_madrid_rentals(self) -> None:
        config = IdealistaSearchConfig(max_price_eur=950)

        parameters = config.parameters(1)

        self.assertEqual(parameters["operation"], "rent")
        self.assertEqual(parameters["propertyType"], "homes")
        self.assertEqual(parameters["maxPrice"], "950")
        self.assertEqual(parameters["maxItems"], "50")
        self.assertEqual(parameters["numPage"], "1")
        self.assertEqual(parameters["sinceDate"], "T")

    def test_invalid_search_limits_fail_before_any_request(self) -> None:
        with self.assertRaisesRegex(ValueError, "page limit"):
            IdealistaSearchConfig(max_pages=0)
        with self.assertRaisesRegex(ValueError, "latitude,longitude"):
            IdealistaSearchConfig(center="Madrid")

    def test_adapter_normalizes_studio_without_exposing_hidden_address(self) -> None:
        client = Mock()
        client.search.return_value = {
            "elementList": [api_listing()],
            "totalPages": 1,
        }
        adapter = IdealistaApiAdapter(client, IdealistaSearchConfig())

        listing = tuple(adapter.fetch())[0]

        self.assertEqual(listing.source_id, "idealista")
        self.assertEqual(listing.external_id, "123456")
        self.assertEqual(listing.price_eur, 900)
        self.assertEqual(listing.bedrooms, 0)
        self.assertEqual(listing.area_sqm, 40)
        self.assertEqual(listing.municipality, "Madrid")
        self.assertEqual(listing.district, "Centro")
        self.assertEqual(listing.address, "")
        self.assertTrue(listing.is_furnished)
        self.assertIn("Lavapiés-Embajadores", listing.location)

    def test_adapter_follows_only_configured_pages(self) -> None:
        client = Mock()
        client.search.side_effect = [
            {"elementList": [api_listing(propertyCode="one")], "totalPages": 8},
            {"elementList": [api_listing(propertyCode="two")], "totalPages": 8},
        ]
        adapter = IdealistaApiAdapter(
            client,
            IdealistaSearchConfig(max_pages=2),
        )

        listings = tuple(adapter.fetch())

        self.assertEqual([listing.external_id for listing in listings], ["one", "two"])
        self.assertEqual(client.search.call_count, 2)

    def test_missing_result_array_fails_visibly(self) -> None:
        client = Mock()
        client.search.return_value = {"totalPages": 1}

        with self.assertRaisesRegex(IdealistaApiError, "elementList"):
            tuple(
                IdealistaApiAdapter(client, IdealistaSearchConfig()).fetch()
            )

    def test_untrusted_listing_url_fails_visibly(self) -> None:
        client = Mock()
        client.search.return_value = {
            "elementList": [api_listing(url="https://idealista.example/listing/1")],
            "totalPages": 1,
        }

        with self.assertRaisesRegex(IdealistaApiError, "invalid URL"):
            tuple(
                IdealistaApiAdapter(client, IdealistaSearchConfig()).fetch()
            )

    def test_adapter_runs_through_matching_and_notification_pipeline(self) -> None:
        client = Mock()
        client.search.return_value = {
            "elementList": [api_listing()],
            "totalPages": 1,
        }
        notifier = Mock()

        def deliver(result):
            return tuple(
                DeliveryAttempt(
                    match_id=outcome.match.match_id,
                    user_id=outcome.profile.user_id,
                    status="sent",
                )
                for outcome in result.delivery_candidates
            )

        notifier.deliver.side_effect = deliver
        with tempfile.TemporaryDirectory() as temporary_directory:
            with RentalStore(Path(temporary_directory) / "rentals.sqlite3") as store:
                store.save_user(User(id="user-1", display_name="User"))
                store.save_search_profile(
                    SearchProfile(
                        id="search-1",
                        user_id="user-1",
                        name="Madrid",
                        max_rent_eur=950,
                        municipality="Madrid",
                    )
                )
                result = AcquisitionRunner(store, notifier).run(
                    IdealistaApiAdapter(client, IdealistaSearchConfig())
                )

        self.assertEqual(result.status, "succeeded")
        self.assertEqual(result.listings_seen, 1)
        self.assertEqual(result.notifications_sent, 1)

    @patch("se_admiten_humanos.idealista.urlopen")
    def test_client_uses_oauth_then_bearer_search(self, mocked_urlopen: Mock) -> None:
        mocked_urlopen.side_effect = [
            _Response(
                json.dumps(
                    {"access_token": "temporary-token", "expires_in": 3600}
                ).encode()
            ),
            _Response(json.dumps({"elementList": [], "totalPages": 0}).encode()),
        ]
        client = IdealistaApiClient("api-key", "api-secret")

        result = client.search({"operation": "rent"})

        self.assertEqual(result["elementList"], [])
        auth_request = mocked_urlopen.call_args_list[0].args[0]
        search_request = mocked_urlopen.call_args_list[1].args[0]
        self.assertEqual(auth_request.full_url, AUTH_URL)
        self.assertTrue(auth_request.get_header("Authorization").startswith("Basic "))
        self.assertEqual(
            parse_qs(auth_request.data.decode()),
            {"grant_type": ["client_credentials"], "scope": ["read"]},
        )
        self.assertEqual(search_request.full_url, SEARCH_URL)
        self.assertEqual(
            search_request.get_header("Authorization"),
            "Bearer temporary-token",
        )
        self.assertEqual(parse_qs(search_request.data.decode()), {"operation": ["rent"]})

    def test_credentials_are_required(self) -> None:
        with self.assertRaisesRegex(ValueError, "key and secret"):
            IdealistaApiClient("", "")


if __name__ == "__main__":
    unittest.main()
