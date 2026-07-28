import unittest
from pathlib import Path

from fastapi import HTTPException

from models import AuthSession, Profile
from services.profile_security import grant_profile_access, hash_profile_pin, require_profile_access, validate_profile_pin, verify_profile_pin


class FakeSession:
    def __init__(self, profile: Profile, auth_session: AuthSession):
        self.profile = profile
        self.auth_session = auth_session
        self.commits = 0

    async def get(self, model, item_id):
        if model is Profile and item_id == self.profile.id:
            return self.profile
        if model is AuthSession and item_id == self.auth_session.id:
            return self.auth_session
        return None

    def add(self, item):
        del item

    async def commit(self):
        self.commits += 1


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


class ProfileAccessGrantTests(unittest.IsolatedAsyncioTestCase):
    async def test_protected_profile_requires_matching_session_grant_and_pin_version(self):
        profile = Profile(id="protected", name="Protected", pin_enabled=True, pin_hash="hash", pin_version=3)
        auth_session = AuthSession(id="session", user_id=1, created_at=1, last_seen_at=1, expires_at=999)
        db = FakeSession(profile, auth_session)

        with self.assertRaises(HTTPException) as denied:
            await require_profile_access(db, auth_session, profile.id)
        self.assertEqual(denied.exception.status_code, 403)

        await grant_profile_access(db, auth_session, profile)
        self.assertEqual(auth_session.selected_profile_id, profile.id)
        self.assertEqual(auth_session.selected_profile_pin_version, 3)
        self.assertIs(await require_profile_access(db, auth_session, profile.id), profile)

        profile.pin_version = 4
        with self.assertRaises(HTTPException):
            await require_profile_access(db, auth_session, profile.id)


if __name__ == "__main__":
    unittest.main()
