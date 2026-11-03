from django.test import SimpleTestCase


class HealthTests(SimpleTestCase):
    def test_health_is_public_and_has_no_private_payload(self):
        response = self.client.get("/health/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})
