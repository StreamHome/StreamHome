"""Real queue claims survive only independently current recovery bundles."""
from __future__ import annotations

import json
import unittest

from sqlmodel.ext.asyncio.session import AsyncSession

from models import Episode, Movie, MovieResponse
from routes import playback
from lifecycle.media_work import media_ownership
from services.media_source import resolve_media_source
from tests.media import test_reconstruction_language_bundles as fixtures
from tests.media import test_language_refresh as media_fixtures


class ReconstructionClaimAuthority(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, *, episode=False, portable="missing", stale_db=False,
                       changed_video=False, db_claim=True, portable_claim=None, lazy=False, source_only=False):
        helper = fixtures.ReconstructionLanguageBundles()
        fixture, selected, metadata = await helper.fixture(episode=episode)
        model = Episode if episode else Movie
        identity = "ep_801_s1_e1" if episode else "m_801"
        try:
            async with AsyncSession(fixture.engine) as session:
                item = await session.get(model, identity)
                before = json.loads(item.analysis_metadata_str)
                self.assertEqual(before["sender_quality"], "720p")
                self.assertEqual(before["video_fingerprint"], selected.video_fingerprint)
                if stale_db:
                    before["video_fingerprint"] = "stale-video"
                    before["source_fingerprint"] = "stale-source"
                    item.source_fingerprint = "stale-source"
                if not db_claim:
                    before.pop("sender_quality")
                    # Old measured labels are never historical submission proof.
                    before["quality"] = "2160p"
                    item.quality = "2160p"
                if source_only:
                    before.pop("video_fingerprint", None)
                item.analysis_metadata_str = json.dumps(before)
                await session.commit()
            for path in metadata.parent.glob("metadata*.json"):
                data = json.loads(path.read_text())
                if portable == "missing":
                    data.pop("analysis", None)
                elif portable == "malformed":
                    data["analysis"] = "not-json"
                elif portable == "partial":
                    data["analysis"] = {"quality": "2160p"}
                if portable_claim:
                    data["analysis"] = {**json.loads(metadata.read_text())["analysis"], "sender_quality": portable_claim}
                path.write_text(json.dumps(data))
            if changed_video:
                original_fingerprint = selected.video_fingerprint
                await media_fixtures.make_media(selected.local_path, frequency=550)
                self.assertNotEqual((await resolve_media_source(selected.catalog_path, check_cloud=False)).video_fingerprint,
                    original_fingerprint)
            if lazy:
                async with AsyncSession(fixture.engine, expire_on_commit=False) as session:
                    item = await session.get(model, identity)
                    observed = await resolve_media_source(item.video_url, check_cloud=False)
                    async with media_ownership(identity):
                        await playback.ensure_source_metadata(session, item, observed)
            else:
                await fixture.manager.sync_media_from_disk(raise_on_critical_failure=True)
            expected = None if changed_video else (portable_claim if stale_db else "720p" if db_claim else portable_claim)
            # Inspect reconstruction before API/lazy consumers can repair it.
            portable_after = json.loads(metadata.read_text())
            async with AsyncSession(fixture.engine) as session:
                item = await session.get(model, identity)
                after = json.loads(item.analysis_metadata_str)
                self.assertEqual(after.get("sender_quality"), expected)
                self.assertEqual(portable_after["analysis"].get("sender_quality"), expected)
                self.assertEqual(after["width"], 320)
                self.assertEqual(after["height"], 180)
                self.assertNotEqual(item.quality, "720p")
                self.assertNotEqual(item.quality, "2160p")
                actual = await resolve_media_source(item.video_url, check_cloud=False)
                self.assertEqual(after["video_fingerprint"], actual.video_fingerprint)
                catalog = await session.get(Movie, "tv_801") if episode else item
                response = MovieResponse.from_db(catalog, [item] if episode else None)
                contract = response.episodes[0] if episode else response
                self.assertEqual(contract.analysis.get("sender_quality"), expected)
                self.assertEqual(contract.analysis["width"], 320)
                self.assertEqual(contract.analysis, portable_after["analysis"])
        finally:
            await fixture.asyncTearDown()

    async def test_movie_missing_malformed_partial_portable_keeps_genuine_db_claim(self):
        for portable in ("missing", "malformed", "partial"):
            with self.subTest(portable=portable):
                await self.exercise(portable=portable)

    async def test_episode_missing_malformed_partial_portable_keeps_genuine_db_claim(self):
        for portable in ("missing", "malformed", "partial"):
            with self.subTest(portable=portable):
                await self.exercise(episode=True, portable=portable)

    async def test_stale_db_cannot_borrow_current_portable_proof(self):
        for episode in (False, True):
            with self.subTest(episode=episode):
                await self.exercise(episode=episode, portable="current", stale_db=True, portable_claim="1080p")

    async def test_independently_current_db_claim_takes_precedence(self):
        for episode in (False, True):
            with self.subTest(episode=episode):
                await self.exercise(episode=episode, portable="current", portable_claim="1080p")

    async def test_old_measured_labels_do_not_invent_submission_claim(self):
        for episode in (False, True):
            with self.subTest(episode=episode):
                await self.exercise(episode=episode, db_claim=False, portable="partial")

    async def test_physical_video_change_drops_old_bundles_before_api(self):
        for episode in (False, True):
            with self.subTest(episode=episode):
                await self.exercise(episode=episode, portable="current", changed_video=True)

    async def test_lazy_refresh_also_rejects_stale_db_claim(self):
        await self.exercise(portable="current", stale_db=True, portable_claim="1080p", lazy=True)

    async def test_lazy_refresh_drops_claim_on_physical_video_change(self):
        await self.exercise(episode=True, portable="current", changed_video=True, lazy=True)

    async def test_source_only_db_proof_keeps_genuine_claim_without_portable_analysis(self):
        await self.exercise(portable="missing", source_only=True)

    async def test_stale_db_claim_is_absent_when_current_portable_has_no_claim(self):
        await self.exercise(episode=True, portable="partial", stale_db=True)


if __name__ == "__main__":
    unittest.main()
