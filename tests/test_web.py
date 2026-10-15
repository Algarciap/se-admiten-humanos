"""End-to-end WSGI tests for public onboarding."""

import io
import tempfile
import unittest
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import urlencode

from se_admiten_humanos.persistence import RentalStore
from se_admiten_humanos.web import WebApplication, WebSettings


class WebTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary_directory.name) / "rentals.sqlite3"
        self.app = WebApplication(
            WebSettings(
                database_path=str(self.database),
                bot_username="SeAdmitenHumanosBot",
                secure_cookies=False,
            )
        )

    def tearDown(self):
        self.temporary_directory.cleanup()

    def request(self, method="GET", path="/", form=None, cookies=""):
        payload = urlencode(form or {}).encode()
        captured = {}

        def start_response(status, headers):
            captured["status"] = status
            captured["headers"] = headers

        environ = {
            "REQUEST_METHOD": method,
            "PATH_INFO": path,
            "CONTENT_TYPE": "application/x-www-form-urlencoded",
            "CONTENT_LENGTH": str(len(payload)),
            "HTTP_COOKIE": cookies,
            "wsgi.input": io.BytesIO(payload),
        }
        body = b"".join(self.app(environ, start_response)).decode()
        return captured["status"], captured["headers"], body

    @staticmethod
    def response_cookies(headers):
        result = {}
        for name, value in headers:
            if name == "Set-Cookie":
                parsed = SimpleCookie()
                parsed.load(value)
                result.update({key: morsel.value for key, morsel in parsed.items()})
        return result

    def valid_form(self, csrf):
        return {
            "csrf_token": csrf,
            "display_name": "Ana",
            "email": "ana@example.com",
            "renter_name": "Ana",
            "sender_name": "Ana",
            "max_rent_eur": "1100",
            "urgent": "on",
            "stretch_max_rent_eur": "1200",
            "preferred_min_bedrooms": "1",
            "property_type_home": "on",
            "allow_studios": "on",
            "allow_unknown_bedrooms": "on",
            "move_in_asap": "on",
            "acceptable_districts": "Centro, Chamberí",
            "furnished": "any",
            "rental_duration": "both",
            "monthly_income_eur": "2500",
            "job": "Técnica de sonido",
            "contract_type": "Indefinido",
            "pet_count": "2",
            "pet_species": "gato",
            "pet_notes": "Tranquilos",
        }

    def test_registration_creates_profile_session_and_telegram_link(self):
        status, headers, page = self.request()
        csrf = self.response_cookies(headers)["humanos_csrf"]

        status, headers, page = self.request(
            "POST",
            "/profile",
            self.valid_form(csrf),
            f"humanos_csrf={csrf}",
        )

        self.assertEqual(status, "200 OK")
        self.assertIn("Tu búsqueda está lista", page)
        self.assertIn("https://t.me/SeAdmitenHumanosBot?start=", page)
        session = self.response_cookies(headers)["humanos_session"]
        with RentalStore(self.database) as store:
            profiles = store.load_enabled_search_profiles()
            self.assertEqual(len(profiles), 1)
            self.assertEqual(profiles[0].max_rent_eur, 1100)
            self.assertTrue(profiles[0].urgent)
            self.assertEqual(profiles[0].stretch_max_rent_eur, 1200)
            self.assertEqual(profiles[0].accepted_property_types[0].value, "home")
            self.assertEqual(profiles[0].acceptable_districts, ("Centro", "Chamberí"))
            self.assertTrue(profiles[0].requires_pet_acceptance)
            self.assertEqual(len(profiles[0].accepted_rental_durations), 2)
            token_count = store.connection.execute(
                "select count(*) from telegram_link_tokens"
            ).fetchone()[0]
            self.assertEqual(token_count, 1)

        status, _, edit_page = self.request(
            cookies=f"humanos_csrf={csrf}; humanos_session={session}"
        )
        self.assertEqual(status, "200 OK")
        self.assertIn("Actualiza tus datos", edit_page)
        self.assertIn('value="1100"', edit_page)

    def test_profile_post_requires_matching_csrf_cookie(self):
        status, _, page = self.request(
            "POST", "/profile", self.valid_form("attacker"), ""
        )

        self.assertEqual(status, "403 Forbidden")
        self.assertIn("caducado", page)
        with RentalStore(self.database) as store:
            self.assertEqual(store.load_enabled_search_profiles(), ())

    def test_validation_error_does_not_store_partial_user(self):
        _, headers, _ = self.request()
        csrf = self.response_cookies(headers)["humanos_csrf"]
        form = self.valid_form(csrf)
        form["max_rent_eur"] = "not-a-number"

        status, _, page = self.request(
            "POST", "/profile", form, f"humanos_csrf={csrf}"
        )

        self.assertEqual(status, "422 Unprocessable Entity")
        self.assertIn("número entero", page)
        with RentalStore(self.database) as store:
            user_count = store.connection.execute("select count(*) from users").fetchone()[0]
            self.assertEqual(user_count, 0)

    def test_exceptional_ceiling_must_exceed_preferred_budget(self):
        _, headers, _ = self.request()
        csrf = self.response_cookies(headers)["humanos_csrf"]
        form = self.valid_form(csrf)
        form["stretch_max_rent_eur"] = "1100"

        status, _, page = self.request(
            "POST", "/profile", form, f"humanos_csrf={csrf}"
        )

        self.assertEqual(status, "422 Unprocessable Entity")
        self.assertIn("mayor que tu presupuesto preferido", page)

    def test_health_endpoint_is_machine_readable(self):
        status, headers, body = self.request(path="/health")

        self.assertEqual(status, "200 OK")
        self.assertEqual(body, '{"status": "ok"}')
        self.assertIn(("Content-Type", "application/json; charset=utf-8"), headers)

    def test_search_options_are_grouped_in_compact_sections(self):
        status, _, page = self.request()

        self.assertEqual(status, "200 OK")
        self.assertIn('class="option-groups"', page)
        self.assertIn("<h3>Urgencia</h3>", page)
        self.assertIn("<h3>Tipo</h3>", page)
        self.assertIn("<h3>Flexibilidad</h3>", page)
        self.assertNotIn('class="checks"', page)
        self.assertIn("Presupuesto preferido al mes (€)", page)
        self.assertNotIn("Presupuesto preferido al mes <span>€</span>", page)
        self.assertIn("fieldset{padding:0 0 22px}", page)
        self.assertIn(
            "legend{float:left;width:100%;padding:0;margin:0 0 10px}", page
        )
        self.assertIn("legend+.grid{clear:both}", page)
        self.assertIn(
            ".cards{border-top:2px solid var(--ink);padding-top:20px}", page
        )
        self.assertIn(".cards article{border-top:0;padding-top:0}", page)

    def test_first_visit_has_no_search_assumptions(self):
        status, _, page = self.request()

        self.assertEqual(status, "200 OK")
        self.assertIn('name="max_rent_eur" value=""', page)
        self.assertIn('name="preferred_min_bedrooms" value=""', page)
        self.assertIn('name="pet_count" value=""', page)
        self.assertIn('name="pet_species" value=""', page)
        self.assertNotIn('name="property_type_home" checked', page)
        self.assertNotIn('name="allow_studios" checked', page)
        self.assertNotIn('name="allow_unknown_bedrooms" checked', page)
        self.assertNotIn('name="move_in_asap" checked', page)
        self.assertIn('<option value="" disabled selected>Elige una opción</option>', page)
        self.assertIn("Tus criterios, tu búsqueda", page)

    def test_animals_are_optional(self):
        _, headers, _ = self.request()
        csrf = self.response_cookies(headers)["humanos_csrf"]
        form = self.valid_form(csrf)
        form["pet_count"] = ""
        form["pet_species"] = ""
        form["pet_notes"] = ""

        status, _, _ = self.request(
            "POST", "/profile", form, f"humanos_csrf={csrf}"
        )

        self.assertEqual(status, "200 OK")
        with RentalStore(self.database) as store:
            profiles = store.load_enabled_search_profiles()
            renter = store.load_primary_renter_profile(profiles[0].user_id)
            self.assertFalse(profiles[0].requires_pet_acceptance)
            self.assertEqual(renter.pets, ())


if __name__ == "__main__":
    unittest.main()
