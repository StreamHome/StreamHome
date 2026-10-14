import unittest
import tempfile
import time
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from models import AuthSession, Profile
from services.profile_security import grant_profile_access, hash_profile_pin, require_profile_access, validate_profile_pin, verify_profile_pin


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
        main_source = (server_root / "application.py").read_text(encoding="utf-8")
        response_source = models_source.split("class ProfileResponse", 1)[1].split("class EpisodeResponse", 1)[0]

        self.assertNotIn("pin:", response_source)
        self.assertNotIn("pin_hash", response_source)
        self.assertIn('@app.post("/api/profiles/{profile_id}/unlock")', main_source)
        self.assertIn("verify_profile_pin(req.pin, profile.pin_hash)", main_source)


class ProfileAccessGrantTests(unittest.IsolatedAsyncioTestCase):
    async def test_protected_profile_requires_matching_session_grant_and_pin_version(self):
        with tempfile.TemporaryDirectory(prefix="streamhome-profile-access-") as directory:
            engine = create_async_engine(f"sqlite+aiosqlite:///{Path(directory) / 'profiles.db'}")
            try:
                async with engine.begin() as connection:
                    await connection.run_sync(SQLModel.metadata.create_all)
                async with AsyncSession(engine, expire_on_commit=False) as db:
                    profile = Profile(id="protected", name="Protected", pin_enabled=True, pin_hash="hash", pin_version=3)
                    auth_session = AuthSession(id="session", user_id=1, created_at=time.time(), last_seen_at=time.time(), expires_at=time.time() + 3600)
                    db.add(profile)
                    db.add(auth_session)
                    await db.commit()
                    with self.assertRaises(HTTPException) as denied:
                        await require_profile_access(db, auth_session, profile.id)
                    self.assertEqual(denied.exception.status_code, 403)
                    await grant_profile_access(db, auth_session, profile)
                    self.assertEqual(auth_session.selected_profile_id, profile.id)
                    self.assertEqual(auth_session.selected_profile_pin_version, 3)
                    self.assertIs(await require_profile_access(db, auth_session, profile.id), profile)
                    profile.pin_version = 4
                    db.add(profile)
                    await db.commit()
                    with self.assertRaises(HTTPException):
                        await require_profile_access(db, auth_session, profile.id)
            finally:
                await engine.dispose()


if __name__ == "__main__":
    unittest.main()
