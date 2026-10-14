"""Accepted episode aliases share native SQLite/local/cloud mutation outcomes."""
from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
import shutil
import time
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI, Request
from sqlalchemy import event
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from config import settings
from db import get_session
from models import AuthSession, Episode, MediaOperationReceipt, Movie, MovieResponse, Profile, User
from lifecycle import media_publication
from routes import playback
from routes.auth import get_current_user
from services import media_updates, queue
from services.media_source import clear_cloud_object_cache, playback_source_fingerprint, resolve_media_source
from services.playback_prep import PlaybackPrepService
from services.rclone import RcloneResult, rclone_service
from tests.media import test_analysis_provenance as fixtures
from tests.media import test_language_refresh as media_fixtures
from tests.media import test_startup_language_authority as startup_fixtures


class PortableAliasMutations(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = fixtures.CatalogAnalysisProvenance()
        await self.fixture.asyncSetUp()
        self.addAsyncCleanup(self.fixture.asyncTearDown)
        self.enterContext(patch.object(queue.tmdb_client, "fetch_episode_metadata", AsyncMock(return_value={"title": "Episode 1"})))
        await startup_fixtures.StartupLanguageAuthority().accept(self.fixture, task_id="alias-accepted", media_type="series",
            submitted="tr", frequency=330, created_at="2026-10-13T00:00:00")
        self.identity = "ep_801_s1_e1"
        async with AsyncSession(self.fixture.engine) as session:
            item = await session.get(Episode, self.identity)
            self.assertEqual(item.title, "Episode 1")
            self.source = await resolve_media_source(item.video_url, check_cloud=False)
        self.metadata = self.source.local_path.parent / ".metadata"
        self.markers = {"intro": [{"start": 0.1, "end": 0.5}]}
        self.enterContext(patch.object(media_updates, "schedule_audio_preparation", AsyncMock()))

    def aliases(self, directory=None):
        return {name: (directory or self.metadata) / name for name in ("metadata.json", "metadata_s1_e1.json")}

    def legacy_alias(self, *, remote=False):
        directory = self.remote.parent.parent if remote else self.source.local_path.parent.parent.parent
        return directory / ".metadata" / "metadata_s1_e1.json"

    def create_legacy_alias(self, *, remote=False, data=None):
        path = self.legacy_alias(remote=remote)
        path.parent.mkdir(exist_ok=True)
        payload = json.loads(self.aliases()["metadata.json"].read_text()) if data is None else data
        path.write_text(json.dumps(payload))
        return path

    async def delete_episode(self):
        async with AsyncSession(self.fixture.engine) as session:
            await session.delete(await session.get(Episode, self.identity))
            await session.commit()

    async def recover_directory_order(self, *, legacy_first):
        real_walk = queue.os.walk
        legacy_directory = str(self.legacy_alias().parent)
        def reordered(*args, **kwargs):
            entries = list(real_walk(*args, **kwargs))
            entries.sort(key=lambda entry: (entry[0] != legacy_directory, entry[0]), reverse=not legacy_first)
            yield from entries
        with patch.object(queue.os, "walk", reordered):
            await self.fixture.manager.sync_media_from_disk(raise_on_critical_failure=True)

    async def assert_coherent(self, *, markers=None, subtitle_count=None, external_count=None, remote=False):
        async with AsyncSession(self.fixture.engine) as session:
            item = await session.get(Episode, self.identity)
            catalog = await session.get(Movie, "tv_801")
            response = MovieResponse.from_db(catalog, [item]).episodes[0]
            payload = media_updates.metadata_payload(item)
            if markers is not None:
                self.assertEqual(item.skip_markers, markers)
            if subtitle_count is not None:
                self.assertEqual(len(item.subtitles), subtitle_count)
            if external_count is not None:
                self.assertEqual(len([track for track in item.audio_metadata if track.get("source") == "external"]), external_count)
            self.assertEqual(response.skip_markers, item.skip_markers)
            self.assertEqual(response.analysis, payload["analysis"])
            self.assertEqual(payload["analysis"]["source_fingerprint"], payload["source_fingerprint"])
        directories = [self.metadata] + ([self.remote / ".metadata"] if remote else [])
        for directory in directories:
            for path in self.aliases(directory).values():
                data = json.loads(path.read_text())
                for field in ("skip_markers", "subtitles", "audio_metadata", "languages", "source_fingerprint", "analysis"):
                    self.assertEqual(data[field], payload[field], (path, field))
                self.assertEqual((data["tmdb_id"], data["season"], data["episode"]), (801, 1, 1))

    async def markers_update(self):
        async with AsyncSession(self.fixture.engine, expire_on_commit=False) as session:
            await media_updates.update_skip_markers(self.identity, self.markers, session)

    async def subtitles_update(self, *, remove=False, track_id="tr"):
        async def prepare(_url, destination, **kwargs):
            destination.write_text("WEBVTT\n\n00:00.000 --> 00:01.000\nAccepted subtitle\n")
        async with AsyncSession(self.fixture.engine, expire_on_commit=False) as session:
            if remove:
                await media_updates.remove_subtitle(self.identity, track_id, session)
            else:
                with patch.object(media_updates, "prepare_subtitle_asset", prepare):
                    await media_updates.upsert_subtitle(self.identity, track_id, language="tr", label="Turkish",
                        url="https://source.example/subtitle", headers={}, client_address="", db=session)

    async def audio_update(self, *, remove=False):
        input_audio = await media_fixtures.make_external(self.fixture.root / "input-audio.mkv")
        async def prepare(_url, temporary_root, **kwargs):
            prepared = temporary_root / "audio.m4a"
            shutil.copy2(input_audio, prepared)
            return prepared
        async with AsyncSession(self.fixture.engine, expire_on_commit=False) as session:
            if remove:
                await media_updates.remove_audio(self.identity, "tr", session)
            else:
                with patch.object(media_updates, "prepare_audio_asset", prepare):
                    await media_updates.upsert_audio(self.identity, "tr", url="https://source.example/audio",
                        headers={}, client_address="", source_type="auto", db=session)

    async def recover(self, *, compatibility_first=False):
        real_walk = queue.os.walk
        def reordered(*args, **kwargs):
            for root, directories, files in real_walk(*args, **kwargs):
                yield root, directories, sorted(files, reverse=compatibility_first)
        with patch.object(queue.os, "walk", reordered):
            await self.fixture.manager.sync_media_from_disk(raise_on_critical_failure=True)

    async def protected_markers(self):
        user = User(id=902, email="aliases@example.test", password_hash="unused")
        auth = AuthSession(id="alias-session", user_id=902, created_at=time.time(), last_seen_at=time.time(), expires_at=time.time() + 3600)
        async with AsyncSession(self.fixture.engine, expire_on_commit=False) as session:
            session.add(user)
            session.add(auth)
            session.add(Profile(id="alias-profile", name="Aliases"))
            await session.commit()
        async def sessions():
            async with AsyncSession(self.fixture.engine, expire_on_commit=False) as session:
                yield session
        app = FastAPI()
        @app.middleware("http")
        async def attach(request: Request, call_next):
            request.state.auth_session = auth
            return await call_next(request)
        app.include_router(playback.router)
        app.dependency_overrides[get_session] = sessions
        app.dependency_overrides[get_current_user] = lambda: user
        service = PlaybackPrepService()
        self.addAsyncCleanup(service.quiesce_for_update, timeout=10)
        with patch.object(playback, "playback_prep_service", service), patch.object(service, "prepare", AsyncMock()):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://aliases.test") as client:
                response = await client.post("/api/playback/runs", json={"movieId": "tv_801", "episodeId": self.identity, "profileId": "alias-profile"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["episodeId"], self.identity)
        async with AsyncSession(self.fixture.engine) as session:
            self.assertEqual((await session.get(Episode, self.identity)).skip_markers, self.markers)

    async def test_accepted_markers_sidecars_and_removals_survive_both_orders(self):
        await self.markers_update()
        await self.subtitles_update()
        await self.audio_update()
        await self.assert_coherent(markers=self.markers, subtitle_count=1, external_count=1)
        async with AsyncSession(self.fixture.engine) as session:
            receipts = {row.operation_id for row in (await session.exec(select(MediaOperationReceipt))).all()}
            self.assertEqual(len(receipts), 4)
        for order in (False, True):
            await self.recover(compatibility_first=order)
            await self.assert_coherent(markers=self.markers, subtitle_count=1, external_count=1)
        async with AsyncSession(self.fixture.engine) as session:
            self.assertEqual({row.operation_id for row in (await session.exec(select(MediaOperationReceipt))).all()}, receipts)
        await self.protected_markers()
        await self.subtitles_update(remove=True)
        await self.audio_update(remove=True)
        await self.assert_coherent(markers=self.markers, subtitle_count=0, external_count=0)
        await self.recover(compatibility_first=True)
        await self.assert_coherent(markers=self.markers, subtitle_count=0, external_count=0)

    async def test_historical_stale_alias_cannot_overwrite_canonical_or_bound_db(self):
        old_alias = self.aliases()["metadata_s1_e1.json"].read_bytes()
        await self.markers_update()
        self.aliases()["metadata_s1_e1.json"].write_bytes(old_alias)
        # Historical pre-fix accepted state: canonical and DB are current while
        # the untouched compatibility copy still contains the old marker value.
        await self.recover(compatibility_first=True)
        async with AsyncSession(self.fixture.engine) as session:
            self.assertEqual((await session.get(Episode, self.identity)).skip_markers, self.markers)
        self.assertEqual(json.loads(self.aliases()["metadata.json"].read_text())["skip_markers"], self.markers)

    async def test_reconstruction_preserves_distinct_subtitle_track_id_and_label(self):
        await self.subtitles_update(track_id="commentary")
        await self.recover(compatibility_first=True)
        await self.assert_coherent(subtitle_count=1)
        async with AsyncSession(self.fixture.engine) as session:
            track = (await session.get(Episode, self.identity)).subtitles[0]
            self.assertEqual((track["trackId"], track["language"], track["label"], track["fileName"]),
                ("commentary", "tr", "Turkish", "subtitle_commentary.vtt"))

    async def test_canonical_only_and_alias_only_legacy_recovery(self):
        await self.markers_update()
        canonical, alias = self.aliases().values()
        alias.unlink()
        await self.recover(compatibility_first=True)
        self.assertEqual(json.loads(canonical.read_text())["skip_markers"], self.markers)
        # Alias-only recovery must still reconstruct a missing catalog owner.
        if not alias.exists():
            shutil.copy2(canonical, alias)
        canonical.unlink()
        async with AsyncSession(self.fixture.engine) as session:
            await session.delete(await session.get(Episode, self.identity))
            await session.commit()
        await self.recover(compatibility_first=True)
        await self.assert_coherent(markers=self.markers)

    async def test_show_canonical_does_not_suppress_distinct_legacy_episode_aliases(self):
        show_metadata = self.source.local_path.parent.parent.parent / ".metadata"
        show_metadata.mkdir(exist_ok=True)
        payload = json.loads(self.aliases()["metadata.json"].read_text())
        show = {**payload, "season": None, "episode": None, "video_url": ""}
        (show_metadata / "metadata.json").write_text(json.dumps(show))
        for episode in (1, 2):
            directory = self.source.local_path.parent.parent / f"Episode_{episode}"
            if episode == 2:
                shutil.copytree(self.source.local_path.parent, directory)
                shutil.rmtree(directory / ".metadata")
            alias = {**payload, "season": 1, "episode": episode,
                "video_url": payload["video_url"].replace("Episode_1", f"Episode_{episode}")}
            (show_metadata / f"metadata_s1_e{episode}.json").write_text(json.dumps(alias))
        shutil.rmtree(self.metadata)
        async with AsyncSession(self.fixture.engine) as session:
            await session.delete(await session.get(Episode, self.identity))
            await session.commit()
        await self.recover(compatibility_first=True)
        async with AsyncSession(self.fixture.engine) as session:
            for episode in (1, 2):
                self.assertIsNotNone(await session.get(Episode, f"ep_801_s1_e{episode}"))

    async def cloud_mode(self):
        if not rclone_service.executable():
            self.skipTest("rclone required")
        self.enterContext(patch.object(settings, "STORAGE_ENGINE", "CLOUD"))
        self.enterContext(patch.object(settings, "RCLONE_REMOTE_PATH", f"isolated:{self.fixture.root / 'remote'}"))
        config = self.fixture.root / "rclone.conf"
        config.write_text("[isolated]\ntype = local\n")
        self.enterContext(patch.object(rclone_service, "config_path", config))
        self.remote = self.fixture.root / "remote" / Path(self.source.relative_path).parent
        shutil.copytree(self.source.local_path.parent, self.remote)
        clear_cloud_object_cache()
        self.addCleanup(clear_cloud_object_cache)

    async def test_real_cloud_and_local_mirror_aliases_share_marker_sidecar_acceptance(self):
        await self.cloud_mode()
        await self.markers_update()
        await self.subtitles_update()
        await self.audio_update()
        await self.assert_coherent(markers=self.markers, subtitle_count=1, external_count=1, remote=True)
        await self.recover(compatibility_first=True)
        await self.assert_coherent(markers=self.markers, subtitle_count=1, external_count=1, remote=True)
        await self.subtitles_update(remove=True)
        await self.audio_update(remove=True)
        await self.assert_coherent(markers=self.markers, subtitle_count=0, external_count=0, remote=True)

    async def test_all_local_and_cloud_aliases_are_captured_before_first_alias_effect(self):
        await self.cloud_mode()
        originals = {path: path.read_bytes() for directory in (self.metadata, self.remote / ".metadata")
            for path in self.aliases(directory).values()}
        real_write = media_updates.atomic_json_write
        observed = []
        def write(path, data):
            if path in self.aliases().values() and not observed:
                manifest = next(Path(settings.TEMP_DIR).glob("media-operations/*/operation.json"))
                self.assertEqual(len(json.loads(manifest.read_text())["entries"]), 4)
                for original, content in originals.items():
                    self.assertEqual(original.read_bytes(), content)
                observed.append(path)
            real_write(path, data)
        with patch.object(media_updates, "atomic_json_write", write):
            await self.markers_update()
        self.assertEqual(len(observed), 1)
        await self.assert_coherent(markers=self.markers, remote=True)

    async def test_cloud_only_alias_acceptance_and_commit_rejection(self):
        await self.cloud_mode()
        shutil.rmtree(self.source.local_path.parent)
        await self.markers_update()
        remote_aliases = self.aliases(self.remote / ".metadata")
        originals = {path: path.read_bytes() for path in remote_aliases.values()}
        for path in remote_aliases.values():
            self.assertEqual(json.loads(path.read_text())["skip_markers"], self.markers)
        async with AsyncSession(self.fixture.engine, expire_on_commit=False) as session:
            with patch.object(session, "commit", AsyncMock(side_effect=RuntimeError("Rejected cloud-only mutation"))):
                with self.assertRaises(RuntimeError):
                    await media_updates.update_skip_markers(self.identity, {}, session)
        await media_publication.recover_media_operations()
        for path, content in originals.items():
            self.assertEqual(path.read_bytes(), content)
        self.assertFalse(self.metadata.exists())
        async with AsyncSession(self.fixture.engine) as session:
            self.assertEqual((await session.get(Episode, self.identity)).skip_markers, self.markers)

    async def test_local_alias_commit_rejection_restores_every_original(self):
        originals = {path: path.read_bytes() for path in self.aliases().values()}
        async with AsyncSession(self.fixture.engine, expire_on_commit=False) as session:
            with patch.object(session, "commit", AsyncMock(side_effect=RuntimeError("Rejected alias mutation"))):
                with self.assertRaises(RuntimeError):
                    await media_updates.update_skip_markers(self.identity, self.markers, session)
        for path, content in originals.items():
            self.assertEqual(path.read_bytes(), content)
        await self.assert_coherent(markers={})

    async def test_cloud_alias_rejection_retains_all_captures_and_replays_repeatedly(self):
        await self.cloud_mode()
        originals = {path: path.read_bytes() for directory in (self.metadata, self.remote / ".metadata") for path in self.aliases(directory).values()}
        real_run = rclone_service.run
        async def fail_compensation(*arguments, **kwargs):
            if arguments[0] == "copyto" and "media-operations" in arguments[1]:
                return RcloneResult(1, error_code="rclone_failed")
            return await real_run(*arguments, **kwargs)
        async with AsyncSession(self.fixture.engine, expire_on_commit=False) as session:
            with patch.object(session, "commit", AsyncMock(side_effect=RuntimeError("Rejected alias mutation"))), patch.object(rclone_service, "run", fail_compensation):
                with self.assertRaises(RuntimeError):
                    await media_updates.update_skip_markers(self.identity, self.markers, session)
        manifest = next(Path(settings.TEMP_DIR).glob("media-operations/*/operation.json"))
        entries = json.loads(manifest.read_text())["entries"]
        self.assertEqual(len(entries), 4, "Both local and both cloud aliases were captured before publication")
        for _ in range(2):
            with patch.object(rclone_service, "run", fail_compensation), self.assertRaises(RuntimeError):
                await media_publication.recover_media_operations()
            self.assertTrue(manifest.is_file())
        await media_publication.recover_media_operations()
        await media_publication.recover_media_operations()
        for path, content in originals.items():
            self.assertEqual(path.read_bytes(), content)
        self.assertFalse(manifest.exists())
        await self.assert_coherent(markers={}, remote=True)

    async def test_repeated_cancellation_waits_for_alias_rollback(self):
        originals = {path: path.read_bytes() for path in self.aliases().values()}
        entered, release = asyncio.Event(), asyncio.Event()
        real_persist = media_updates.persist_portable_metadata
        async def pause(*args, **kwargs):
            await real_persist(*args, **kwargs)
            entered.set()
            await release.wait()
        async with AsyncSession(self.fixture.engine, expire_on_commit=False) as session:
            with patch.object(media_updates, "persist_portable_metadata", pause):
                task = asyncio.create_task(media_updates.update_skip_markers(self.identity, self.markers, session))
                await asyncio.wait_for(entered.wait(), 10)
                task.cancel()
                task.cancel()
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
        for path, content in originals.items():
            self.assertEqual(path.read_bytes(), content)
        await self.assert_coherent(markers={})

    async def test_actual_after_commit_acknowledgment_failure_preserves_all_aliases(self):
        async with AsyncSession(self.fixture.engine, expire_on_commit=False) as session:
            @event.listens_for(session.sync_session, "after_commit")
            def fail_ack(_):
                raise RuntimeError("Accepted alias acknowledgment lost")
            with self.assertRaises(RuntimeError):
                await media_updates.update_skip_markers(self.identity, self.markers, session)
        await self.assert_coherent(markers=self.markers)
        self.assertFalse(list(Path(settings.TEMP_DIR).glob("media-operations/*/operation.json")))

    async def test_existing_show_root_alias_is_updated_and_row_deleted_recovery_keeps_markers(self):
        legacy = self.create_legacy_alias()
        await self.markers_update()
        self.assertEqual(json.loads(legacy.read_text())["skip_markers"], self.markers)
        for order in (True, False):
            await self.delete_episode()
            await self.recover_directory_order(legacy_first=order)
            await self.assert_coherent(markers=self.markers)

    async def test_historical_divergent_show_root_alias_cannot_author_db_before_canonical(self):
        legacy = self.create_legacy_alias()
        original = legacy.read_bytes()
        await self.markers_update()
        for order in (True, False):
            legacy.write_bytes(original)
            await self.delete_episode()
            await self.recover_directory_order(legacy_first=order)
            await self.assert_coherent(markers=self.markers)

    async def test_show_root_alias_only_reconstructs_missing_episode(self):
        await self.markers_update()
        self.create_legacy_alias()
        shutil.rmtree(self.metadata)
        await self.delete_episode()
        await self.recover_directory_order(legacy_first=True)
        await self.assert_coherent(markers=self.markers)

    async def test_show_root_different_owner_source_and_generic_show_are_not_overwritten(self):
        payload = json.loads(self.aliases()["metadata.json"].read_text())
        generic = self.legacy_alias().with_name("metadata.json")
        generic.parent.mkdir(exist_ok=True)
        generic.write_text(json.dumps({**payload, "season": None, "episode": None, "video_url": ""}))
        generic_original = generic.read_bytes()
        distinct = generic.with_name("metadata_s1_e2.json")
        episode_two = self.source.local_path.parent.parent / "Episode_2"
        episode_two.mkdir(exist_ok=True)
        shutil.copy2(self.source.local_path, episode_two / self.source.local_path.name)
        distinct.write_text(json.dumps({**payload, "episode": 2,
            "video_url": payload["video_url"].replace("Episode_1", "Episode_2")}))
        distinct_original = distinct.read_bytes()
        for changed in ({"episode": 2}, {"video_url": "/media/Series/Other/Season_1/Episode_1/video.mkv"},
            {"tmdb_id": 802}, {"tmdb_id": 801.5}, {"season": True}):
            with self.subTest(changed=changed):
                legacy = self.create_legacy_alias(data={**payload, **changed})
                original = legacy.read_bytes()
                await self.markers_update()
                self.assertEqual(legacy.read_bytes(), original)
                self.assertEqual(generic.read_bytes(), generic_original)
                self.assertEqual(distinct.read_bytes(), distinct_original)
                inventory = queue._recovery_metadata_inventory(str(settings.MEDIA_DIR))
                self.assertNotIn((str(legacy.parent), legacy.name),
                    [(root, name) for root, names in inventory for name in names])
                for order in (True, False):
                    await self.recover_directory_order(legacy_first=order)
                    await self.assert_coherent(markers=self.markers)
                    await self.delete_episode()
                    await self.recover_directory_order(legacy_first=order)
                    await self.assert_coherent(markers=self.markers)
                    async with AsyncSession(self.fixture.engine) as session:
                        self.assertIsNotNone(await session.get(Episode, "ep_801_s1_e2"))

    async def test_healthy_db_selects_current_source_and_row_deleted_ambiguity_fails_closed(self):
        await self.markers_update()
        payload = json.loads(self.aliases()["metadata.json"].read_text())
        alternate = self.source.local_path.with_name("alternate.mkv")
        shutil.copy2(self.source.local_path, alternate)
        legacy = self.create_legacy_alias(data={**payload,
            "video_url": payload["video_url"].rsplit("/", 1)[0] + "/alternate.mkv", "skip_markers": {}})
        originals = {path: path.read_bytes() for path in (*self.aliases().values(), legacy)}
        for order in (True, False):
            await self.recover_directory_order(legacy_first=order)
            await self.assert_coherent(markers=self.markers)
            for path, content in originals.items():
                self.assertEqual(json.loads(path.read_text()), json.loads(content))
            self.assertEqual(legacy.read_bytes(), originals[legacy])
            async with AsyncSession(self.fixture.engine) as session:
                self.assertEqual((await session.get(Episode, self.identity)).video_url, self.source.catalog_path)
        # Ordinary healthy reconstruction serializes canonical JSON again. The
        # no-authority refusal below must preserve its exact current evidence.
        originals = {path: path.read_bytes() for path in (*self.aliases().values(), legacy)}
        await self.delete_episode()
        for order in (True, False):
            with self.assertRaises(queue.MediaRecoveryRequired):
                await self.recover_directory_order(legacy_first=order)
            async with AsyncSession(self.fixture.engine) as session:
                self.assertIsNone(await session.get(Episode, self.identity))
            for path, content in originals.items():
                self.assertEqual(path.read_bytes(), content)

    async def test_cloud_show_root_aliases_are_captured_before_effect_without_sqlite_writer(self):
        await self.cloud_mode()
        local, remote = self.create_legacy_alias(), self.create_legacy_alias(remote=True)
        originals = {path: path.read_bytes() for path in [*self.aliases().values(),
            *self.aliases(self.remote / ".metadata").values(), local, remote]}
        real_write, real_run = media_updates.atomic_json_write, rclone_service.run
        observed, writers = [], []
        def write(path, data):
            if path in self.aliases().values() and not observed:
                manifest = next(Path(settings.TEMP_DIR).glob("media-operations/*/operation.json"))
                self.assertEqual(len(json.loads(manifest.read_text())["entries"]), 6)
                for original, content in originals.items():
                    self.assertEqual(original.read_bytes(), content)
                observed.append(path)
            real_write(path, data)
        async def run(*args, **kwargs):
            if args[0] == "lsjson" and args[1].endswith("/.metadata/metadata_s1_e1.json") and not writers:
                async def independent_write():
                    async with AsyncSession(self.fixture.engine) as session:
                        session.add(Profile(id="legacy-cloud-writer", name="Independent"))
                        await session.commit()
                await asyncio.wait_for(independent_write(), 5)
                writers.append(True)
            return await real_run(*args, **kwargs)
        with patch.object(media_updates, "atomic_json_write", write), patch.object(rclone_service, "run", run):
            await self.markers_update()
        self.assertEqual((len(observed), len(writers)), (1, 1))
        await self.subtitles_update()
        await self.audio_update()
        await self.assert_coherent(markers=self.markers, subtitle_count=1, external_count=1, remote=True)
        self.assertEqual(json.loads(local.read_text()), json.loads(remote.read_text()))
        self.assertEqual(json.loads(local.read_text()), json.loads(self.aliases()["metadata.json"].read_text()))

    async def test_cloud_only_existing_local_and_remote_show_root_aliases_share_acceptance(self):
        await self.cloud_mode()
        local, remote = self.create_legacy_alias(), self.create_legacy_alias(remote=True)
        payload = json.loads(local.read_text())
        shutil.rmtree(self.source.local_path.parent)
        # The synthetic local-to-cloud move must establish actual provider
        # binding before accepting a mutation whose DB row will be deleted.
        current = await resolve_media_source(self.source.catalog_path, require_cloud=True, refresh_cloud=True)
        self.assertFalse(current.local_exists)
        self.assertTrue(current.cloud_exists)
        fingerprint = playback_source_fingerprint(current, payload["audio_metadata"])
        payload.update(video_fingerprint=current.video_fingerprint, source_fingerprint=fingerprint)
        payload["analysis"].update(video_fingerprint=current.video_fingerprint, source_fingerprint=fingerprint)
        async with AsyncSession(self.fixture.engine) as session:
            item = await session.get(Episode, self.identity)
            item.analysis_metadata_str = json.dumps(payload["analysis"])
            item.source_fingerprint = fingerprint
            await session.commit()
        for path in (local, remote, *self.aliases(self.remote / ".metadata").values()):
            path.write_text(json.dumps(payload))
        await self.markers_update()
        for path in (local, remote, *self.aliases(self.remote / ".metadata").values()):
            self.assertEqual(json.loads(path.read_text())["skip_markers"], self.markers)
        self.assertFalse(self.metadata.exists())
        await self.delete_episode()
        await self.recover_directory_order(legacy_first=True)
        async with AsyncSession(self.fixture.engine) as session:
            self.assertEqual((await session.get(Episode, self.identity)).skip_markers, self.markers)

    async def test_cloud_wrong_source_and_owner_legacy_aliases_are_preserved(self):
        await self.cloud_mode()
        payload = json.loads(self.aliases()["metadata.json"].read_text())
        for changed in ({"tmdb_id": 802}, {"video_url": "/media/Series/Other/Season_1/Episode_1/video.mkv"}):
            with self.subTest(changed=changed):
                local = self.create_legacy_alias(data={**payload, **changed})
                remote = self.create_legacy_alias(remote=True, data={**payload, **changed})
                originals = {path: path.read_bytes() for path in (local, remote)}
                await self.markers_update()
                for path, content in originals.items():
                    self.assertEqual(path.read_bytes(), content)
                for order in (True, False):
                    await self.recover_directory_order(legacy_first=order)
                    await self.assert_coherent(markers=self.markers, remote=True)

    async def test_cloud_show_root_alias_rejection_preserves_repeated_compensation_evidence(self):
        await self.cloud_mode()
        local, remote = self.create_legacy_alias(), self.create_legacy_alias(remote=True)
        originals = {path: path.read_bytes() for path in [*self.aliases().values(),
            *self.aliases(self.remote / ".metadata").values(), local, remote]}
        real_run = rclone_service.run
        async def fail_compensation(*args, **kwargs):
            if args[0] == "copyto" and "media-operations" in args[1]:
                return RcloneResult(1, error_code="rclone_failed")
            return await real_run(*args, **kwargs)
        async with AsyncSession(self.fixture.engine, expire_on_commit=False) as session:
            with patch.object(session, "commit", AsyncMock(side_effect=RuntimeError("Rejected legacy alias"))), \
                patch.object(rclone_service, "run", fail_compensation):
                with self.assertRaises(RuntimeError):
                    await media_updates.update_skip_markers(self.identity, self.markers, session)
        manifest = next(Path(settings.TEMP_DIR).glob("media-operations/*/operation.json"))
        self.assertEqual(len(json.loads(manifest.read_text())["entries"]), 6)
        for _ in range(2):
            with patch.object(rclone_service, "run", fail_compensation), self.assertRaises(RuntimeError):
                await media_publication.recover_media_operations()
            self.assertTrue(manifest.exists())
        await media_publication.recover_media_operations()
        await media_publication.recover_media_operations()
        for path, content in originals.items():
            self.assertEqual(path.read_bytes(), content)
        self.assertFalse(manifest.exists())

    async def test_repeated_cancellation_restores_existing_show_root_aliases(self):
        await self.cloud_mode()
        local, remote = self.create_legacy_alias(), self.create_legacy_alias(remote=True)
        originals = {path: path.read_bytes() for path in [*self.aliases().values(),
            *self.aliases(self.remote / ".metadata").values(), local, remote]}
        entered, release = asyncio.Event(), asyncio.Event()
        real_persist = media_updates.persist_portable_metadata
        async def pause(*args, **kwargs):
            await real_persist(*args, **kwargs)
            entered.set()
            await release.wait()
        async with AsyncSession(self.fixture.engine, expire_on_commit=False) as session:
            with patch.object(media_updates, "persist_portable_metadata", pause):
                task = asyncio.create_task(media_updates.update_skip_markers(self.identity, self.markers, session))
                await asyncio.wait_for(entered.wait(), 10)
                task.cancel()
                task.cancel()
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
        for path, content in originals.items():
            self.assertEqual(path.read_bytes(), content)

    async def test_cloud_show_root_alias_uncertainty_rejects_before_any_alias_effect(self):
        await self.cloud_mode()
        local, remote = self.create_legacy_alias(), self.create_legacy_alias(remote=True)
        originals = {path: path.read_bytes() for path in [*self.aliases().values(),
            *self.aliases(self.remote / ".metadata").values(), local, remote]}
        real_run = rclone_service.run
        async def unavailable(*args, **kwargs):
            if args[0] == "lsjson" and args[1].endswith("/.metadata/metadata_s1_e1.json"):
                return RcloneResult(1, error_code="rclone_failed")
            return await real_run(*args, **kwargs)
        with patch.object(rclone_service, "run", unavailable), self.assertRaises(media_updates.MediaUpdateFailure):
            await self.markers_update()
        for path, content in originals.items():
            self.assertEqual(path.read_bytes(), content)
        await self.assert_coherent(markers={})

    async def competing_cloud_sources(self):
        await self.cloud_mode()
        await self.markers_update()
        payload = json.loads(self.aliases()["metadata.json"].read_text())
        current_video = self.remote / self.source.local_path.name
        obsolete_video = self.remote / "obsolete.mkv"
        shutil.copy2(current_video, obsolete_video)
        self.source.local_path.unlink()
        current = await resolve_media_source(self.source.catalog_path, require_cloud=True, refresh_cloud=True)
        obsolete_url = self.source.catalog_path.rsplit("/", 1)[0] + "/obsolete.mkv"
        obsolete = await resolve_media_source(obsolete_url, require_cloud=True, refresh_cloud=True)
        self.assertFalse(current.local_exists)
        self.assertTrue(current.cloud_exists and obsolete.cloud_exists)
        self.assertNotEqual(current.video_fingerprint, obsolete.video_fingerprint)
        fingerprint = playback_source_fingerprint(current, payload["audio_metadata"])
        payload.update(video_fingerprint=current.video_fingerprint, source_fingerprint=fingerprint)
        payload["analysis"].update(video_fingerprint=current.video_fingerprint, source_fingerprint=fingerprint)
        async with AsyncSession(self.fixture.engine) as session:
            item = await session.get(Episode, self.identity)
            item.analysis_metadata_str = json.dumps(payload["analysis"])
            item.source_fingerprint = fingerprint
            await session.commit()
        for directory in (self.metadata, self.remote / ".metadata"):
            for path in self.aliases(directory).values():
                path.write_text(json.dumps(payload))
        old = copy.deepcopy(payload)
        old_fingerprint = playback_source_fingerprint(obsolete, old["audio_metadata"])
        old.update(video_url=obsolete_url, video_fingerprint=obsolete.video_fingerprint,
            source_fingerprint=old_fingerprint, skip_markers={})
        old["analysis"].update(video_fingerprint=obsolete.video_fingerprint, source_fingerprint=old_fingerprint)
        self.create_legacy_alias(data=old)
        self.create_legacy_alias(remote=True, data=old)
        return current, current_video, obsolete_video

    def cloud_evidence(self, current_video, obsolete_video):
        paths = [*self.aliases().values(), *self.aliases(self.remote / ".metadata").values(),
            self.legacy_alias(), self.legacy_alias(remote=True), obsolete_video]
        if current_video.is_file():
            paths.append(current_video)
        return {path: path.read_bytes() for path in paths}

    async def assert_cloud_refusal(self, evidence, *, expected_row=True):
        for order in (True, False):
            with self.assertRaises(queue.MediaRecoveryRequired):
                await self.recover_directory_order(legacy_first=order)
            for path, content in evidence.items():
                self.assertEqual(path.read_bytes(), content)
            async with AsyncSession(self.fixture.engine) as session:
                item = await session.get(Episode, self.identity)
                if expected_row:
                    self.assertEqual(item.video_url, self.source.catalog_path)
                    self.assertEqual(item.skip_markers, self.markers)
                else:
                    self.assertIsNone(item)

    async def test_fresh_cloud_db_video_and_full_source_proof_select_current_original(self):
        current, current_video, obsolete_video = await self.competing_cloud_sources()
        old_local, old_remote = self.legacy_alias().read_bytes(), self.legacy_alias(remote=True).read_bytes()
        real_run = rclone_service.run
        stats = []
        async def run(*args, **kwargs):
            if args[0] == "lsjson" and args[1] == current.cloud_path:
                self.assertEqual(self.fixture.engine.sync_engine.pool.checkedout(), 0,
                    "Cloud authority observation must occur after the DB read session closes")
                async def independent_write():
                    async with AsyncSession(self.fixture.engine) as session:
                        session.add(Profile(id=f"cloud-authority-writer-{len(stats)}", name="Independent"))
                        await session.commit()
                await asyncio.wait_for(independent_write(), 5)
                stats.append(args[1])
            return await real_run(*args, **kwargs)
        with patch.object(rclone_service, "run", run):
            for source_only in (False, True):
                if source_only:
                    async with AsyncSession(self.fixture.engine) as session:
                        item = await session.get(Episode, self.identity)
                        analysis = json.loads(item.analysis_metadata_str)
                        analysis.pop("video_fingerprint")
                        item.analysis_metadata_str = json.dumps(analysis)
                        await session.commit()
                for order in (True, False):
                    await self.recover_directory_order(legacy_first=order)
                    await self.assert_coherent(markers=self.markers, remote=True)
                    async with AsyncSession(self.fixture.engine) as session:
                        self.assertEqual((await session.get(Episode, self.identity)).video_url, current.catalog_path)
                    self.assertEqual(self.legacy_alias().read_bytes(), old_local)
                    self.assertEqual(self.legacy_alias(remote=True).read_bytes(), old_remote)
        self.assertEqual(stats, [current.cloud_path] * 4, "Every ambiguity resolution forces an actual fresh stat")
        self.assertTrue(current_video.is_file() and obsolete_video.is_file())

    async def test_cloud_ambiguity_without_db_row_retains_originals_and_no_winner(self):
        _, current_video, obsolete_video = await self.competing_cloud_sources()
        await self.delete_episode()
        await self.assert_cloud_refusal(self.cloud_evidence(current_video, obsolete_video), expected_row=False)

    async def test_cloud_current_portable_cannot_authorize_stale_or_null_db_proof(self):
        _, current_video, obsolete_video = await self.competing_cloud_sources()
        evidence = self.cloud_evidence(current_video, obsolete_video)
        for fingerprint in ("stale-db-source", None):
            with self.subTest(fingerprint=fingerprint):
                async with AsyncSession(self.fixture.engine) as session:
                    item = await session.get(Episode, self.identity)
                    analysis = json.loads(item.analysis_metadata_str)
                    analysis["video_fingerprint"] = "stale-db-video" if fingerprint else None
                    item.analysis_metadata_str = json.dumps(analysis)
                    item.source_fingerprint = fingerprint
                    await session.commit()
                await self.assert_cloud_refusal(evidence)
                async with AsyncSession(self.fixture.engine) as session:
                    self.assertEqual((await session.get(Episode, self.identity)).source_fingerprint, fingerprint)

    async def test_stale_cached_cloud_stat_cannot_authorize_physically_replaced_video(self):
        current, current_video, obsolete_video = await self.competing_cloud_sources()
        await media_fixtures.make_media(current_video, frequency=987)
        cached = await resolve_media_source(current.catalog_path, require_cloud=True)
        self.assertEqual(cached.video_fingerprint, current.video_fingerprint, "Fixture retains an actually stale positive cache")
        await self.assert_cloud_refusal(self.cloud_evidence(current_video, obsolete_video))

    async def test_actual_missing_cloud_object_cannot_borrow_cached_or_inventory_proof(self):
        current, current_video, obsolete_video = await self.competing_cloud_sources()
        real_run = rclone_service.run
        stale_inventory = await real_run("lsf", settings.RCLONE_REMOTE_PATH, "--recursive", "--files-only", timeout=60, output_limit=None)
        self.assertTrue(stale_inventory.ok)
        current_video.unlink()
        stats = []
        async def run(*args, **kwargs):
            if args[0] == "lsf":
                return stale_inventory  # A successful inventory can become stale before the stat.
            result = await real_run(*args, **kwargs)
            if args[0] == "lsjson" and args[1] == current.cloud_path:
                self.assertFalse(result.ok, "The real local provider confirms that this object is absent")
                stats.append(result.error_code)
            return result
        with patch.object(rclone_service, "run", run):
            await self.assert_cloud_refusal(self.cloud_evidence(current_video, obsolete_video))
        self.assertEqual(len(stats), 2)
        self.assertFalse(current_video.exists())

    async def test_cloud_stat_provider_uncertainty_rejects_cached_positive_authority(self):
        current, current_video, obsolete_video = await self.competing_cloud_sources()
        real_run = rclone_service.run
        stats = []
        async def run(*args, **kwargs):
            if args[0] == "lsjson" and args[1] == current.cloud_path:
                stats.append(args[1])
                return RcloneResult(1, error_code="rclone_failed")
            return await real_run(*args, **kwargs)
        with patch.object(rclone_service, "run", run):
            await self.assert_cloud_refusal(self.cloud_evidence(current_video, obsolete_video))
        self.assertEqual(stats, [current.cloud_path] * 2)

    async def test_nonambiguous_cloud_recovery_does_not_add_per_item_authority_stats(self):
        current, _, _ = await self.competing_cloud_sources()
        self.legacy_alias().unlink()
        self.legacy_alias(remote=True).unlink()
        real_run = rclone_service.run
        stats = []
        async def run(*args, **kwargs):
            if args[0] == "lsjson" and args[1] == current.cloud_path:
                stats.append(args[1])
            return await real_run(*args, **kwargs)
        with patch.object(rclone_service, "run", run):
            await self.recover_directory_order(legacy_first=True)
        self.assertEqual(stats, [])
        async with AsyncSession(self.fixture.engine) as session:
            item = await session.get(Episode, self.identity)
            self.assertEqual((item.video_url, item.skip_markers), (current.catalog_path, self.markers))

    async def uniform_cloud_fixture(self, *, movie=False):
        """Move one actually accepted Movie/Episode and sidecars to the local provider."""
        model = Movie if movie else Episode
        if movie:
            await startup_fixtures.StartupLanguageAuthority().accept(self.fixture, task_id="cloud-movie-accepted",
                media_type="movie", submitted="tr", frequency=441, created_at="2026-10-14T00:00:00")
            self.identity = "m_801"
            async with AsyncSession(self.fixture.engine) as session:
                self.source = await resolve_media_source((await session.get(Movie, self.identity)).video_url, check_cloud=False)
            self.metadata = self.source.local_path.parent / ".metadata"
        await self.markers_update()
        await self.subtitles_update(track_id="accepted-tr")
        await self.audio_update()
        payload = json.loads((self.metadata / "metadata.json").read_text())
        async with AsyncSession(self.fixture.engine) as session:
            payload["duration"] = (await session.get(model, self.identity)).duration
        self.assertEqual(payload["analysis"]["sender_quality"], "720p")
        self.assertEqual(payload["audio_metadata"][0]["language"], "es")
        self.assertEqual(payload["audio_metadata"][1]["languageOverride"], {"source": "sender", "language": "tr"})
        await self.cloud_mode()
        self.source.local_path.unlink()
        current = await resolve_media_source(self.source.catalog_path, require_cloud=True, refresh_cloud=True)
        self.assertFalse(current.local_exists)
        self.assertTrue(current.cloud_exists)
        fingerprint = playback_source_fingerprint(current, payload["audio_metadata"])
        payload.update(video_fingerprint=current.video_fingerprint, source_fingerprint=fingerprint)
        payload["analysis"].update(video_fingerprint=current.video_fingerprint, source_fingerprint=fingerprint)
        async with AsyncSession(self.fixture.engine) as session:
            item = await session.get(model, self.identity)
            item.analysis_metadata_str = json.dumps(payload["analysis"])
            item.source_fingerprint = fingerprint
            await session.commit()
        self.write_cloud_payload(payload)
        return model, payload, current

    def write_cloud_payload(self, payload):
        for directory in (self.metadata, self.remote / ".metadata"):
            for path in directory.glob("metadata*.json"):
                path.write_text(json.dumps(payload))

    async def assert_uniform_cloud(self, model, expected):
        async with AsyncSession(self.fixture.engine) as session:
            item = await session.get(model, self.identity)
            self.assertEqual(item.video_url, expected["video_url"])
            for field in ("skip_markers", "subtitles", "audio_metadata", "languages", "source_fingerprint",
                "quality", "duration", "probed_duration", "width", "height", "codec", "container", "frame_rate"):
                self.assertEqual(getattr(item, field), expected[field], field)
            self.assertEqual(json.loads(item.analysis_metadata_str), expected["analysis"])
            parent = item if model is Movie else await session.get(Movie, "tv_801")
            response = MovieResponse.from_db(parent, None if model is Movie else [item])
            consumer = response if model is Movie else response.episodes[0]
            self.assertEqual(consumer.skip_markers, expected["skip_markers"])
            self.assertEqual(consumer.languages, expected["languages"])
            self.assertEqual(consumer.subtitles, expected["subtitles"])
            self.assertEqual(consumer.analysis, expected["analysis"])
        for path in self.metadata.glob("metadata*.json"):
            data = json.loads(path.read_text())
            for field in ("skip_markers", "subtitles", "audio_metadata", "languages", "analysis", "source_fingerprint"):
                self.assertEqual(data[field], expected[field], (path.name, field))
            self.assertEqual(data["language"], "tr")
            if "stream_manifest" in data:
                physical = [stream for stream in data["stream_manifest"] if stream["type"] == "audio"]
                self.assertEqual([stream["language"] for stream in physical], ["es", "en"])

    async def exercise_current_db_cloud(self, *, movie):
        model, accepted, current = await self.uniform_cloud_fixture(movie=movie)
        variants = (("Captured legitimate title", ""), ("Film", "credentials are not set"), ("Film", "")) if movie else (
            ("Episode legitimate title", ""), ("Pilot", "historic stream. description"), ("Pilot", ""))
        real_run = rclone_service.run
        stats = []
        async def run(*args, **kwargs):
            if args[0] == "lsjson" and args[1] == current.cloud_path:
                self.assertEqual(self.fixture.engine.sync_engine.pool.checkedout(), 0)
                stats.append(args[1])
            return await real_run(*args, **kwargs)
        with patch.object(rclone_service, "run", run):
            for source_only in (False, True):
                for title, description in variants:
                    for order in (True, False):
                        with self.subTest(source_only=source_only, title=title, description=description, order=order):
                            async with AsyncSession(self.fixture.engine) as session:
                                item = await session.get(model, self.identity)
                                item.title, item.description = title, description
                                analysis = copy.deepcopy(accepted["analysis"])
                                if source_only:
                                    analysis.pop("video_fingerprint")
                                item.analysis_metadata_str = json.dumps(analysis)
                                await session.commit()
                            historical = copy.deepcopy(accepted)
                            historical.update(skip_markers={}, subtitles=[], audio_metadata=[], languages=["en"], language="en", analysis={})
                            self.write_cloud_payload(historical)
                            await self.recover_directory_order(legacy_first=order)
                            await self.assert_uniform_cloud(model, accepted)
                            for path in (self.remote / ".metadata").glob("metadata*.json"):
                                self.assertEqual(json.loads(path.read_text()), historical, "Inventory selection does not publish remotely")
        self.assertEqual(stats, [current.cloud_path] * 12)

    async def test_episode_current_cloud_db_bundle_is_independent_of_title_description_and_full_source_proof(self):
        await self.exercise_current_db_cloud(movie=False)

    async def test_movie_current_cloud_db_bundle_is_independent_of_title_description_and_full_source_proof(self):
        await self.exercise_current_db_cloud(movie=True)

    async def exercise_current_portable_cloud(self, *, movie):
        model, accepted, _ = await self.uniform_cloud_fixture(movie=movie)
        variants = (("Captured legitimate title", ""), ("Film", "credentials are not set"), ("Film", "")) if movie else (
            ("Episode legitimate title", ""), ("Pilot", "historic stream. description"), ("Pilot", ""))
        for proof in ("stale-video-source", None):
            for title, description in variants:
                with self.subTest(proof=proof, title=title, description=description):
                    async with AsyncSession(self.fixture.engine) as session:
                        item = await session.get(model, self.identity)
                        item.title, item.description = title, description
                        item.skip_markers, item.subtitles, item.audio_metadata, item.languages = {}, [], [], ["en"]
                        item.source_fingerprint = proof
                        item.analysis_metadata_str = json.dumps({"video_fingerprint": proof})
                        item.probed_duration, item.width, item.height, item.quality = None, None, None, "Source"
                        await session.commit()
                    self.write_cloud_payload(accepted)
                    await self.recover_directory_order(legacy_first=proof is None)
                    await self.assert_uniform_cloud(model, accepted)
        # An absent DB row uses the portable bundle's own source proof as well.
        async with AsyncSession(self.fixture.engine) as session:
            await session.delete(await session.get(model, self.identity))
            await session.commit()
        self.write_cloud_payload(accepted)
        await self.recover_directory_order(legacy_first=True)
        await self.assert_uniform_cloud(model, accepted)

    async def test_episode_current_portable_cloud_bundle_replaces_stale_null_or_absent_db(self):
        await self.exercise_current_portable_cloud(movie=False)

    async def test_movie_current_portable_cloud_bundle_replaces_stale_null_or_absent_db(self):
        await self.exercise_current_portable_cloud(movie=True)

    async def exercise_unbound_cloud(self, *, movie):
        model, accepted, current = await self.uniform_cloud_fixture(movie=movie)
        unbound = copy.deepcopy(accepted)
        unbound.update(video_fingerprint=None, source_fingerprint=None, skip_markers={})
        unbound["analysis"].update(video_fingerprint=None, source_fingerprint=None)
        self.write_cloud_payload(unbound)
        external = next(track for track in accepted["audio_metadata"] if track.get("source") == "external")
        preserved_assets = {path: path.read_bytes() for path in (self.remote / self.source.local_path.name,
            self.remote / "subtitle_accepted-tr.vtt", self.remote / "audio" / external["fileName"])}
        async with AsyncSession(self.fixture.engine) as session:
            item = await session.get(model, self.identity)
            item.analysis_metadata_str = json.dumps(unbound["analysis"])
            item.source_fingerprint = None
            await session.commit()
        real_run = rclone_service.run
        stats = []
        async def run(*args, **kwargs):
            if args[0] == "lsjson" and args[1] == current.cloud_path:
                self.assertEqual(self.fixture.engine.sync_engine.pool.checkedout(), 0)
                stats.append(args[1])
            return await real_run(*args, **kwargs)
        with patch.object(rclone_service, "run", run):
            await self.recover_directory_order(legacy_first=True)
        self.assertEqual(stats, [current.cloud_path], "The actual differing unbound bundles require fresh observation")
        async with AsyncSession(self.fixture.engine) as session:
            item = await session.get(model, self.identity)
            self.assertEqual((item.skip_markers, item.subtitles, item.audio_metadata, item.languages), ({}, [], [], []))
            self.assertEqual((item.quality, item.duration, item.width, item.height, item.probed_duration),
                ("Source", "Unknown", None, None, None))
            analysis = json.loads(item.analysis_metadata_str)
            self.assertNotIn("sender_quality", analysis)
            self.assertNotIn("measurement_version", analysis)
            self.assertEqual(analysis["video_fingerprint"], current.video_fingerprint)
        for path, content in preserved_assets.items():
            self.assertEqual(path.read_bytes(), content, "Unknown authority preserves actual physical assets")
        for path in self.metadata.glob("metadata*.json"):
            self.assertNotIn("stream_manifest", json.loads(path.read_text()), "An unbound manifest is not current physical evidence")

    async def test_episode_unbound_cloud_bundles_do_not_retain_stale_mutables_or_measures(self):
        await self.exercise_unbound_cloud(movie=False)

    async def test_movie_unbound_cloud_bundles_do_not_retain_stale_mutables_or_measures(self):
        await self.exercise_unbound_cloud(movie=True)

    async def test_unknown_cloud_observation_cannot_authorize_null_db_bundle(self):
        from services.media_source import ResolvedMediaSource
        model, accepted, current = await self.uniform_cloud_fixture()
        async with AsyncSession(self.fixture.engine) as session:
            item = await session.get(model, self.identity)
            item.analysis_metadata_str = json.dumps({"video_fingerprint": None})
            item.source_fingerprint = None
            await session.commit()
        before = {path: path.read_bytes() for path in [*(self.metadata.glob("metadata*.json")),
            *((self.remote / ".metadata").glob("metadata*.json")), self.remote / self.source.local_path.name]}
        with patch.object(queue, "resolve_media_source", AsyncMock(return_value=current)), \
            patch.object(ResolvedMediaSource, "video_fingerprint", property(lambda _: None)):
            with self.assertRaises(queue.MediaRecoveryRequired):
                await self.recover_directory_order(legacy_first=True)
        for path, content in before.items():
            self.assertEqual(path.read_bytes(), content)
        async with AsyncSession(self.fixture.engine) as session:
            item = await session.get(model, self.identity)
            self.assertIsNone(item.source_fingerprint)
            self.assertEqual(item.skip_markers, self.markers)


    async def local_original_fixture(self, *, movie=True, alternative=True):
        import subprocess
        model = Movie if movie else Episode
        if movie:
            await startup_fixtures.StartupLanguageAuthority().accept(self.fixture, task_id="explicit-movie-accepted",
                media_type="movie", submitted="tr", frequency=441, created_at="2026-10-14T00:00:00")
            self.identity = "m_801"
            async with AsyncSession(self.fixture.engine) as session:
                self.source = await resolve_media_source((await session.get(Movie, self.identity)).video_url, check_cloud=False)
            self.metadata = self.source.local_path.parent / ".metadata"
        await self.markers_update()
        await self.subtitles_update(track_id="accepted-tr")
        await self.audio_update()
        original = self.source.local_path
        other = original.with_name("other-original.mp4")
        if alternative:
            result = await asyncio.to_thread(subprocess.run, [shutil.which("ffmpeg"), "-nostdin", "-v", "error", "-y",
                "-f", "lavfi", "-i", "color=blue:size=128x96:rate=5:duration=2", "-c:v", "libx264", "-preset", "ultrafast",
                "-threads", "1", str(other)], capture_output=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr.decode())
        async with AsyncSession(self.fixture.engine) as session:
            item = await session.get(model, self.identity)
            expected = {field: copy.deepcopy(getattr(item, field)) for field in (
                "video_url", "skip_markers", "audio_metadata", "languages", "subtitles", "width", "height",
                "probed_duration", "duration", "quality", "source_fingerprint")}
            expected["analysis"] = json.loads(item.analysis_metadata_str)
            self.assertEqual(expected["analysis"]["sender_quality"], "720p")
            receipts = {row.operation_id for row in (await session.exec(select(MediaOperationReceipt))).all()}
            self.assertGreaterEqual(len(receipts), 3)
        assets = {path: path.read_bytes() for path in original.parent.rglob("*") if path.is_file() and ".metadata" not in path.parts}
        return model, expected, receipts, assets, other

    async def recover_original_order(self, other, *, other_first):
        real_listdir = queue.os.listdir
        def ordered(path):
            names = real_listdir(path)
            if Path(path).resolve() == other.parent.resolve():
                names.sort(key=lambda name: (name != other.name, name), reverse=not other_first)
            return names
        with patch.object(queue.os, "listdir", ordered):
            await self.fixture.manager.sync_media_from_disk(raise_on_critical_failure=True)

    async def assert_local_original(self, model, expected, receipts, assets):
        async with AsyncSession(self.fixture.engine) as session:
            item = await session.get(model, self.identity)
            for field, value in expected.items():
                self.assertEqual(json.loads(item.analysis_metadata_str) if field == "analysis" else getattr(item, field), value, field)
            self.assertEqual({row.operation_id for row in (await session.exec(select(MediaOperationReceipt))).all()}, receipts)
            parent = item if model is Movie else await session.get(Movie, "tv_801")
            response = MovieResponse.from_db(parent, None if model is Movie else [item])
            consumer = response if model is Movie else response.episodes[0]
            self.assertEqual((consumer.video_url, consumer.skip_markers, consumer.analysis),
                (expected["video_url"], self.markers, expected["analysis"]))
        for path, content in assets.items():
            self.assertEqual(path.read_bytes(), content, path.name)
        for path in self.metadata.glob("metadata*.json"):
            payload = json.loads(path.read_text())
            self.assertEqual(payload["video_url"], expected["video_url"])
            for field in ("skip_markers", "audio_metadata", "languages", "subtitles", "analysis", "source_fingerprint"):
                self.assertEqual(payload[field], expected[field], field)
            physical = [track["language"] for track in payload["stream_manifest"] if track["type"] == "audio"]
            self.assertEqual(physical, ["es", "en"])
            self.assertEqual(payload["audio_metadata"][1]["languageOverride"], {"source": "sender", "language": "tr"})

    async def exercise_explicit_original(self, *, movie):
        model, expected, receipts, assets, other = await self.local_original_fixture(movie=movie)
        for source_only in (False, True):
            for other_first in (True, False):
                with self.subTest(source_only=source_only, other_first=other_first):
                    if source_only:
                        async with AsyncSession(self.fixture.engine) as session:
                            item = await session.get(model, self.identity)
                            analysis = json.loads(item.analysis_metadata_str)
                            analysis.pop("video_fingerprint")
                            item.analysis_metadata_str = json.dumps(analysis)
                            await session.commit()
                    await self.recover_original_order(other, other_first=other_first)
                    await self.assert_local_original(model, expected, receipts, assets)

    async def test_movie_explicit_local_original_preserves_accepted_bundle_and_receipts_in_both_orders(self):
        await self.exercise_explicit_original(movie=True)

    async def test_episode_explicit_local_original_control_preserves_accepted_bundle_in_both_orders(self):
        await self.exercise_explicit_original(movie=False)

    async def test_movie_missing_or_stale_contained_declaration_uses_only_current_db_source(self):
        model, expected, receipts, assets, other = await self.local_original_fixture()
        payload = json.loads((self.metadata / "metadata.json").read_text())
        for declaration in (None, expected["video_url"].rsplit("/", 1)[0] + "/missing.mkv"):
            for source_only in (False, True):
                with self.subTest(declaration=declaration, source_only=source_only):
                    current = copy.deepcopy(payload)
                    current["video_url"] = declaration
                    (self.metadata / "metadata.json").write_text(json.dumps(current))
                    async with AsyncSession(self.fixture.engine) as session:
                        item = await session.get(model, self.identity)
                        analysis = copy.deepcopy(expected["analysis"])
                        if source_only:
                            analysis.pop("video_fingerprint")
                        item.analysis_metadata_str = json.dumps(analysis)
                        await session.commit()
                    await self.recover_original_order(other, other_first=True)
                    await self.assert_local_original(model, expected, receipts, assets)

    async def test_movie_foreign_unsafe_wrong_source_or_owner_metadata_cannot_replace_healthy_db(self):
        model, expected, receipts, assets, other = await self.local_original_fixture()
        canonical = self.metadata / "metadata.json"
        payload = json.loads(canonical.read_text())
        foreign = self.source.local_path.parent.parent / "Foreign" / "foreign.mkv"
        foreign.parent.mkdir()
        shutil.copy2(self.source.local_path, foreign)
        foreign_bytes = foreign.read_bytes()
        variants = ({"video_url": expected["video_url"].rsplit("/", 1)[0] + "/" + other.name},
            {"video_url": "/media/Movies/Foreign/foreign.mkv"}, {"video_url": "/media/Movies/../../escape.mkv"},
            {"video_url": expected["video_url"] + "?source=other"}, {"tmdb_id": 802}, {"tmdb_id": 801.5})
        for changed in variants:
            for order in (True, False):
                with self.subTest(changed=changed, order=order):
                    canonical.write_text(json.dumps({**payload, **changed, "skip_markers": {}}))
                    before = canonical.read_bytes()
                    await self.recover_original_order(other, other_first=order)
                    self.assertEqual(canonical.read_bytes(), before)
                    async with AsyncSession(self.fixture.engine) as session:
                        item = await session.get(Movie, self.identity)
                        for field, value in expected.items():
                            self.assertEqual(json.loads(item.analysis_metadata_str) if field == "analysis" else getattr(item, field), value)
                        self.assertIsNone(await session.get(Movie, "m_802"))
                        self.assertEqual({row.operation_id for row in (await session.exec(select(MediaOperationReceipt))).all()}, receipts)
                    for path, content in assets.items():
                        self.assertEqual(path.read_bytes(), content)
                    self.assertEqual(foreign.read_bytes(), foreign_bytes)

    async def test_movie_multiple_legacy_originals_without_current_db_authority_fail_closed(self):
        model, _, _, assets, other = await self.local_original_fixture()
        canonical = self.metadata / "metadata.json"
        payload = json.loads(canonical.read_text())
        payload.pop("video_url")
        canonical.write_text(json.dumps(payload))
        async with AsyncSession(self.fixture.engine) as session:
            item = await session.get(model, self.identity)
            item.analysis_metadata_str, item.source_fingerprint = "{}", None
            await session.commit()
        for absent in (False, True):
            if absent:
                async with AsyncSession(self.fixture.engine) as session:
                    await session.delete(await session.get(model, self.identity))
                    await session.commit()
            for order in (True, False):
                before = canonical.read_bytes()
                with self.assertRaises(queue.MediaRecoveryRequired):
                    await self.recover_original_order(other, other_first=order)
                self.assertEqual(canonical.read_bytes(), before)
                for path, content in assets.items():
                    self.assertEqual(path.read_bytes(), content)
                async with AsyncSession(self.fixture.engine) as session:
                    item = await session.get(model, self.identity)
                    if absent:
                        self.assertIsNone(item)
                    else:
                        self.assertEqual((item.analysis_metadata_str, item.source_fingerprint), ("{}", None))

    async def test_movie_missing_declared_original_never_promotes_remaining_alternative(self):
        model, _, _, assets, other = await self.local_original_fixture()
        canonical = self.metadata / "metadata.json"
        before = canonical.read_bytes()
        self.source.local_path.unlink()
        remaining = {path: content for path, content in assets.items() if path != self.source.local_path}
        for order in (True, False):
            with self.assertRaises(queue.MediaRecoveryRequired):
                await self.recover_original_order(other, other_first=order)
            self.assertEqual(canonical.read_bytes(), before)
            for path, content in remaining.items():
                self.assertEqual(path.read_bytes(), content)
            async with AsyncSession(self.fixture.engine) as session:
                item = await session.get(model, self.identity)
                self.assertEqual((item.video_url, item.availability), ("", "cached"))
                self.assertEqual(item.skip_markers, self.markers)

    async def test_movie_legacy_single_original_reconstructs_without_declaration(self):
        model, expected, receipts, assets, other = await self.local_original_fixture(alternative=False)
        canonical = self.metadata / "metadata.json"
        payload = json.loads(canonical.read_text())
        payload.pop("video_url")
        canonical.write_text(json.dumps(payload))
        async with AsyncSession(self.fixture.engine) as session:
            await session.delete(await session.get(model, self.identity))
            await session.commit()
        await self.recover_original_order(other, other_first=True)
        await self.assert_local_original(model, expected, receipts, assets)


if __name__ == "__main__":
    unittest.main()
