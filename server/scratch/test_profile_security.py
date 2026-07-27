import unittest
from pathlib import Path

from services.profile_security import hash_profile_pin, validate_profile_pin, verify_profile_pin


class ProfilePinSecurityTests(unittest.TestCase):
    def test_pin_hash_never_contains_plaintext_and_verifies(self):
        pin_hash = hash_profile_pin("4815")

        self.assertNotIn("4815", pin_hash)
        self.assertTrue(verify_profile_pin("4815", pin_hash))
        self.assertFalse(verify_profile_pin("4816", pin_hash))

    def test_pin_validation_accepts_only_four_to_eight_digits(self):
        self.assertEqual(validate_profile_pin(" 4815 "), "4815")
        for invalid in ("123", "123456789", "12a4", "four"):
            with self.assertRaises(ValueError):
                validate_profile_pin(invalid)

    def test_profile_api_never_serializes_pin_or_hash_fields(self):
        server_root = Path(__file__).resolve().parents[1]
        models_source = (server_root / "models.py").read_text(encoding="utf-8")
        main_source = (server_root / "main.py").read_text(encoding="utf-8")
        response_source = models_source.split("class ProfileResponse", 1)[1].split("class EpisodeResponse", 1)[0]

        self.assertNotIn("pin:", response_source)
        self.assertNotIn("pin_hash", response_source)
        self.assertIn('@app.post("/api/profiles/{profile_id}/unlock")', main_source)
        self.assertIn("verify_profile_pin(req.pin, profile.pin_hash)", main_source)


if __name__ == "__main__":
    unittest.main()
