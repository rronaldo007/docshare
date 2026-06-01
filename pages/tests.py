from django.core import mail
from django.test import TestCase
from django.urls import reverse


class PublicPagesTests(TestCase):
    def test_public_pages_load_without_login(self):
        for name in ("home", "about", "contact"):
            resp = self.client.get(reverse(name))
            self.assertEqual(resp.status_code, 200, name)

    def test_contact_form_sends_email(self):
        resp = self.client.post(
            reverse("contact"),
            {"name": "Ada", "email": "ada@example.com", "message": "Hello there"},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("Ada", mail.outbox[0].body)

    def test_contact_form_rejects_invalid_email(self):
        resp = self.client.post(
            reverse("contact"),
            {"name": "Ada", "email": "not-an-email", "message": "Hi"},
        )
        self.assertEqual(resp.status_code, 200)  # re-renders with errors
        self.assertEqual(len(mail.outbox), 0)
