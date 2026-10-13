"""Regression tests for fail-closed recipient selection."""

import unittest

from se_admiten_humanos.contact_policy import approved_contact_email, email_domain_allowed


class ContactPolicyTests(unittest.TestCase):
    def test_without_allowlist_fails_closed(self) -> None:
        self.assertEqual(approved_contact_email("Contacto: agente@agencia.example", ""), "")

    def test_only_explicit_domain_is_selected(self) -> None:
        text = "Avisos: noreply@portal.example. Contacto: agente@agencia.example"
        self.assertEqual(
            approved_contact_email(text, "agencia.example"),
            "agente@agencia.example",
        )

    def test_deceptive_lookalike_domain_is_rejected(self) -> None:
        self.assertFalse(
            email_domain_allowed(
                "agente@agencia.example.evil.test",
                "agencia.example",
            )
        )


if __name__ == "__main__":
    unittest.main()
