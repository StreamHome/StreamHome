"""Canonical engine receipt policy; execute only in a disposable installation."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sqlalchemy import text
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

import db as db_module
from config import settings
from models import MediaOperationReceipt
from lifecycle.media_publication import MediaMutation
from services.media_updates import synchronize_local_asset


class CanonicalAcceptanceDurability(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        await db_module.engine.dispose()

    async def test_canonical_wal_commit_is_full_before_evidence_cleanup(self):
        engine = db_module.engine
        async with engine.begin() as connection:
            await connection.run_sync(SQLModel.metadata.create_all)
        with tempfile.TemporaryDirectory(prefix="canonical-receipt-") as temporary:
            media, staging = Path(temporary) / "media", Path(temporary) / "temp"
            target = media / "Movies" / "Durable" / "subtitle_en.vtt"
            target.parent.mkdir(parents=True)
            staging.mkdir()
            target.write_bytes(b"original")
            cleanup_observations = []
            real_cleanup = MediaMutation.cleanup

            async def cleanup(mutation):
                async with AsyncSession(engine) as fresh:
                    receipt = await fresh.get(MediaOperationReceipt, mutation.operation_id)
                    synchronous = (await fresh.execute(text("PRAGMA synchronous"))).scalar_one()
                    journal = (await fresh.execute(text("PRAGMA journal_mode"))).scalar_one()
                self.assertIsNotNone(receipt)
                self.assertEqual(receipt.media_id, mutation.media_id)
                self.assertEqual(synchronous, 2)
                self.assertEqual(journal.lower(), "wal")
                self.assertTrue((mutation.root / "operation.json").is_file())
                self.assertTrue((mutation.root / "asset-0").is_file())
                cleanup_observations.append(mutation.operation_id)
                await real_cleanup(mutation)

            with patch.object(settings, "MEDIA_DIR", str(media)), patch.object(settings, "TEMP_DIR", str(staging)), \
                    patch.object(MediaMutation, "cleanup", cleanup):
                async with AsyncSession(engine) as session:
                    async with MediaMutation("m_durable_receipt") as mutation:
                        mutation.capture_local(str(target))
                        target.write_bytes(b"accepted")
                        synchronize_local_asset(target)
                        await mutation.commit(session)
                    self.assertTrue(mutation.accepted)
                    self.assertFalse(mutation.root.exists())
                self.assertEqual(cleanup_observations, [mutation.operation_id])
                self.assertEqual(target.read_bytes(), b"accepted")
