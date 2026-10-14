"""HEVC publication acceptance and stream preservation in disposable fixtures."""
from __future__ import annotations

import asyncio
from contextlib import ExitStack
import copy
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
import uuid

from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel import SQLModel, select
from sqlmodel.ext.asyncio.session import AsyncSession

from config import settings
from lifecycle.coordination import EffectClass, RuntimeCoordinator, use_runtime_coordinator_for_testing
from lifecycle import media_publication
from lifecycle.media_work import MediaRecoveryRequired, media_lock, require_resolved_media
from models import MediaOperationReceipt, Movie
from services import hevc_compressor as hevc
from services import media_updates
from services import state as process_state
from services.playback_prep import playback_prep_service


ORIGINAL = b"original-video" * 800
OPTIMIZED = b"optimized-video" * 300
MANIFEST = [
    {"type": "video", "codec": "h264", "language": "und", "title": "", "default": True},
    {"type": "audio", "codec": "aac", "language": "en", "title": "English", "default": True},
    {"type": "audio", "codec": "aac", "language": "es", "title": "Spanish", "default": False},
    {"type": "subtitle", "codec": "subrip", "language": "en", "title": "Captions", "default": False},
]


def probe(codec: str, fingerprint: str) -> dict:
    streams = copy.deepcopy(MANIFEST)
    streams[0]["codec"] = codec
    return {"codec": codec, "container": "matroska", "probed_duration": 20.0,
        "width": 1280, "height": 720, "frame_rate": 24.0, "quality": "720p",
        "source_fingerprint": fingerprint, "video_fingerprint": fingerprint,
        "audio_metadata": [{"language": "en", "source": "embedded", "stream_index": 1}],
        "stream_manifest": streams, "analysis": {"quality_source": "measured"}}


class HEVCMutationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.media = self.root / "media"
        self.temp = self.root / "temp"
        self.media_id = "m_" + str(uuid.uuid4().int % 10**12)
        self.video = self.media / "Movies" / self.media_id / "video.mkv"
        self.video.parent.mkdir(parents=True)
        self.video.write_bytes(ORIGINAL)
        self.metadata = self.video.parent / ".metadata" / "metadata.json"
        self.metadata.parent.mkdir()
        self.old_metadata = {"id": self.media_id, "codec": "h264", "source_fingerprint": "original",
            "skip_markers": {"intro": {"start": 2, "end": 6}}, "quality": "720p"}
        self.metadata.write_text(json.dumps(self.old_metadata), encoding="utf-8")
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{self.root / 'database.db'}")
        async with self.engine.begin() as connection:
            await connection.run_sync(SQLModel.metadata.create_all)
        async with AsyncSession(self.engine) as session:
            session.add(Movie(id=self.media_id, title="HEVC fixture", description="", thumbnail_url="",
                video_url=f"/media/Movies/{self.media_id}/video.mkv", duration="20s", release_year=2026,
                codec="h264", quality="720p", source_fingerprint="original", hevc_compressed=False,
                skip_markers_str=json.dumps(self.old_metadata["skip_markers"])))
            await session.commit()
        self.stack = ExitStack()
        self.stack.enter_context(mock.patch.object(settings, "MEDIA_DIR", str(self.media)))
        self.stack.enter_context(mock.patch.object(settings, "TEMP_DIR", str(self.temp)))
        self.stack.enter_context(mock.patch.object(settings, "HEVC_COMPRESSION_MODE", "force"))
        self.stack.enter_context(mock.patch.object(hevc, "engine", self.engine))
        self.stack.enter_context(mock.patch.object(playback_prep_service, "reuse_verified_playback_cache", return_value=False))
        self.coordinator = RuntimeCoordinator()
        self.stack.enter_context(use_runtime_coordinator_for_testing(self.coordinator))
        self.real_sleep = asyncio.sleep
        self.real_probe = hevc.probe_completed_media
        self.real_create_process = asyncio.create_subprocess_exec
        self.worker = hevc.HEVCCompressorWorker()
        self.worker.is_running = True
        self.commands = []
        self.probes = []

    async def asyncTearDown(self):
        await self.worker.stop()
        self.stack.close()
        await self.engine.dispose()
        self.temporary.cleanup()

    def install_worker(self, *, probe_override=None, real_encoder=False):
        async def sleep(delay):
            if delay == 60.0:
                self.worker.is_running = False
            else:
                await self.real_sleep(0.02)

        async def encoder(*command, **kwargs):
            self.commands.append(command)
            if real_encoder:
                return await self.real_create_process(*command, **kwargs)
            Path(command[-1]).write_bytes(OPTIMIZED)
            return SimpleNamespace(returncode=0, stdout=None, stderr=None, wait=mock.AsyncMock(return_value=0))

        async def inspect_file(value):
            path = Path(value)
            return probe("h264", "original") if path.read_bytes() == ORIGINAL else probe("hevc", "optimized")

        async def real_inspect(value):
            result = await self.real_probe(value)
            self.probes.append(copy.deepcopy(result))
            return result

        self.stack.enter_context(mock.patch.object(hevc.asyncio, "sleep", new=sleep))
        self.stack.enter_context(mock.patch.object(self.worker, "_is_server_idle", mock.AsyncMock(return_value=True)))
        self.stack.enter_context(mock.patch.object(self.worker, "_next_candidate", mock.AsyncMock(return_value=(Movie, self.media_id, "/media/fixture"))))
        self.stack.enter_context(mock.patch.object(hevc, "resolve_media_source", mock.AsyncMock(return_value=SimpleNamespace(local_exists=True, local_path=self.video))))
        self.stack.enter_context(mock.patch.object(hevc.asyncio, "create_subprocess_exec", new=encoder))
        self.stack.enter_context(mock.patch.object(hevc, "probe_completed_media", new=probe_override or (real_inspect if real_encoder else inspect_file)))
        if not real_encoder:
            self.stack.enter_context(mock.patch.object(hevc, "register_process"))
            self.stack.enter_context(mock.patch.object(hevc, "unregister_process"))

    async def assert_state(self, *, accepted: bool):
        async with AsyncSession(self.engine) as session:
            movie = await session.get(Movie, self.media_id)
            receipts = (await session.exec(select(MediaOperationReceipt))).all()
            self.assertEqual(movie.hevc_compressed, accepted)
            self.assertEqual(movie.codec, "hevc" if accepted else "h264")
            self.assertEqual(bool(receipts), accepted)
        self.assertEqual(self.video.read_bytes(), OPTIMIZED if accepted else ORIGINAL)
        metadata = json.loads(self.metadata.read_text(encoding="utf-8"))
        self.assertEqual(metadata["codec"], "hevc" if accepted else "h264")
        self.assertEqual(metadata["skip_markers"], self.old_metadata["skip_markers"])
        self.assertFalse(list(self.temp.glob("media-operations/*/operation.json")))
        self.assertFalse(list(self.video.parent.glob("*.hevc-part*")))

    async def test_cancel_immediately_after_replace_restores_original(self):
        calls = 0
        async def inspect_file(path):
            nonlocal calls
            calls += 1
            if calls == 3:
                self.assertEqual(self.video.read_bytes(), OPTIMIZED)
                raise asyncio.CancelledError
            return probe("h264", "original") if calls == 1 else probe("hevc", "optimized")
        self.install_worker(probe_override=inspect_file)
        with self.assertRaises(asyncio.CancelledError):
            await self.worker._worker_loop()
        await self.assert_state(accepted=False)

    async def test_cancel_during_final_probe_restores_original(self):
        entered = asyncio.Event()
        calls = 0
        async def inspect_file(path):
            nonlocal calls
            calls += 1
            if calls == 3:
                entered.set()
                await asyncio.Event().wait()
            return probe("h264", "original") if calls == 1 else probe("hevc", "optimized")
        self.install_worker(probe_override=inspect_file)
        task = asyncio.create_task(self.worker._worker_loop())
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await self.assert_state(accepted=False)

    async def test_repeated_cancel_during_compensation_finishes_rollback(self):
        entered, release = asyncio.Event(), asyncio.Event()
        original_compensate = media_publication.MediaMutation.compensate
        calls = 0
        async def inspect_file(path):
            nonlocal calls
            calls += 1
            if calls == 3:
                raise asyncio.CancelledError
            return probe("h264", "original") if calls == 1 else probe("hevc", "optimized")
        async def delayed_compensate(mutation):
            entered.set()
            await release.wait()
            await original_compensate(mutation)
        self.install_worker(probe_override=inspect_file)
        with mock.patch.object(media_publication.MediaMutation, "compensate", new=delayed_compensate):
            task = asyncio.create_task(self.worker._worker_loop())
            await asyncio.wait_for(entered.wait(), 5)
            for _ in range(2):
                task.cancel()
                await self.real_sleep(0)
                self.assertFalse(task.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        await self.assert_state(accepted=False)

    async def commit_cancellation(self, *, fail: bool):
        entered, release = asyncio.Event(), asyncio.Event()
        original_commit = AsyncSession.commit
        async def delayed_commit(session):
            entered.set()
            await release.wait()
            if fail:
                raise RuntimeError("Injected catalog commit failure")
            await original_commit(session)
        self.install_worker()
        with mock.patch.object(AsyncSession, "commit", new=delayed_commit):
            task = asyncio.create_task(self.worker._worker_loop())
            await asyncio.wait_for(entered.wait(), 5)
            task.cancel()
            await self.real_sleep(0)
            self.assertFalse(task.done())
            release.set()
            if fail:
                await task
            else:
                with self.assertRaises(asyncio.CancelledError):
                    await task
        await self.assert_state(accepted=not fail)

    async def test_cancel_during_successful_commit_retains_accepted_video(self):
        await self.commit_cancellation(fail=False)

    async def test_cancel_during_failed_commit_restores_old_video_and_metadata(self):
        await self.commit_cancellation(fail=True)

    async def test_metadata_write_failure_restores_video_and_metadata(self):
        self.install_worker()
        with mock.patch.object(media_updates, "atomic_json_write", side_effect=OSError("Injected metadata failure")):
            await self.worker._worker_loop()
        await self.assert_state(accepted=False)

    async def test_catalog_commit_failure_restores_video_and_metadata(self):
        self.install_worker()
        with mock.patch.object(AsyncSession, "commit", new=mock.AsyncMock(side_effect=RuntimeError("Injected catalog failure"))):
            await self.worker._worker_loop()
        await self.assert_state(accepted=False)

    async def test_failed_rollback_keeps_original_evidence_and_blocks_playback(self):
        calls = 0
        async def inspect_file(path):
            nonlocal calls
            calls += 1
            if calls == 3:
                raise asyncio.CancelledError
            return probe("h264", "original") if calls == 1 else probe("hevc", "optimized")
        original_copy = shutil.copy2
        def copy(source, destination, *args, **kwargs):
            if str(destination).endswith(".rollback"):
                raise OSError("Injected rollback failure")
            return original_copy(source, destination, *args, **kwargs)
        self.install_worker(probe_override=inspect_file)
        with mock.patch.object(media_publication.shutil, "copy2", side_effect=copy):
            with self.assertRaises(asyncio.CancelledError):
                await self.worker._worker_loop()
        operations = list(self.temp.glob("media-operations/*/operation.json"))
        self.assertEqual(len(operations), 1)
        payload = json.loads(operations[0].read_text(encoding="utf-8"))
        backup = operations[0].parent / payload["entries"][0]["backup"]
        self.assertEqual(backup.read_bytes(), ORIGINAL)
        with self.assertRaises(MediaRecoveryRequired):
            require_resolved_media(self.media_id)
        # A later supported retry restores the original from retained evidence.
        mutation = media_publication.MediaMutation(self.media_id)
        mutation.root = operations[0].parent
        mutation.entries = payload["entries"]
        mutation.operation_id = payload["operation_id"]
        await mutation.compensate()
        await mutation.cleanup()
        await self.assert_state(accepted=False)

    async def real_stream_preservation(self, *, epoch=0.0, variable_frame_rate=False, container="mkv"):
        subtitle = self.root / "caption.srt"
        subtitle.write_text("1\n00:00:00,000 --> 00:00:01,500\nIsolated caption.\n", encoding="utf-8")
        command = [shutil.which("ffmpeg"), "-y", "-v", "error",
            "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=12:duration=2",
            "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=6:duration=2",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
            "-f", "lavfi", "-i", "sine=frequency=880:duration=2",
            "-i", str(subtitle), "-map", "0:v", "-map", "1:v", "-map", "2:a", "-map", "3:a", "-map", "4:s",
            "-c:v", "libx264", "-crf", "0", "-preset", "ultrafast", "-c:a", "aac", "-c:s", "srt",
            "-metadata:s:a:0", "language=eng", "-metadata:s:a:1", "language=spa",
            "-metadata:s:s:0", "language=eng", "-disposition:a:0", "default", "-disposition:a:1", "0",
            "-disposition:s:0", "forced+hearing_impaired",
            "-threads", "2", "-t", "2", str(self.video)]
        if variable_frame_rate:
            command[-1:-1] = ["-filter:v:0", "select=not(eq(n\\,5))", "-fps_mode:v:0", "vfr"]
        created = await asyncio.to_thread(subprocess.run, command, capture_output=True, text=True, timeout=20)
        self.assertEqual(created.returncode, 0, created.stderr)
        if container == "mp4":
            target = self.video.with_suffix(".mp4")
            command = [shutil.which("ffmpeg"), "-y", "-v", "error", "-copyts", "-i", str(self.video),
                "-map", "0", "-c", "copy", "-c:s", "mov_text", "-avoid_negative_ts", "disabled", str(target)]
            remuxed = await asyncio.to_thread(subprocess.run, command, capture_output=True, text=True, timeout=20)
            self.assertEqual(remuxed.returncode, 0, remuxed.stderr)
            self.video.unlink()
            self.video = target
            async with AsyncSession(self.engine) as session:
                movie = await session.get(Movie, self.media_id)
                movie.video_url = f"/media/Movies/{self.media_id}/{self.video.name}"
                session.add(movie)
                await session.commit()
        if epoch:
            shifted = self.root / "shifted.mkv"
            command = [shutil.which("ffmpeg"), "-y", "-v", "error", "-copyts", "-itsoffset", str(epoch),
                "-i", str(self.video), "-map", "0", "-c", "copy", "-avoid_negative_ts", "disabled", str(shifted)]
            shifted_result = await asyncio.to_thread(subprocess.run, command, capture_output=True, text=True, timeout=20)
            self.assertEqual(shifted_result.returncode, 0, shifted_result.stderr)
            shifted.replace(self.video)
        original_copy = self.root / f"original.{container}"
        shutil.copy2(self.video, original_copy)
        original = await self.real_probe(str(self.video))
        self.assertEqual(len(original["stream_manifest"]), 5)
        original_size = self.video.stat().st_size
        self.install_worker(real_encoder=True)
        await asyncio.wait_for(self.worker._worker_loop(), 30)
        final = await self.real_probe(str(self.video))
        self.assertEqual(final["codec"], "hevc", json.dumps(self.probes))
        self.assertTrue(self.worker._valid_transcode(original, final, original_size, self.video.stat().st_size))
        self.assertIn("disposition", original["stream_manifest"][0])
        identity = ("index", "type", "codec", "language", "title", "default", "disposition", "startTime", "timeBase")
        self.assertEqual([{key: stream[key] for key in identity} for stream in original["stream_manifest"] if stream["type"] != "video"],
            [{key: stream[key] for key in identity} for stream in final["stream_manifest"] if stream["type"] != "video"])
        self.assertEqual([stream["codec"] for stream in final["stream_manifest"] if stream["type"] == "video"], ["hevc", "h264"])
        async def packets(path):
            command = [shutil.which("ffprobe"), "-v", "error", "-show_packets", "-show_data_hash", "sha256",
                "-show_entries", "packet=stream_index,pts_time,dts_time,duration_time,size,data_hash", "-of", "json", str(path)]
            result = await asyncio.to_thread(subprocess.run, command, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)["packets"]
        before_packets, after_packets = await packets(original_copy), await packets(self.video)
        for index in (1, 2, 3, 4):
            before = [packet for packet in before_packets if packet["stream_index"] == index]
            after = [packet for packet in after_packets if packet["stream_index"] == index]
            self.assertEqual(before, after, f"Copied stream {index} packets or timestamps changed")
        # Primary video is intentionally lossy, while frame count and presentation
        # timeline remain the same despite encoder DTS reordering.
        original_pts = sorted(float(packet["pts_time"]) for packet in before_packets if packet["stream_index"] == 0)
        final_pts = sorted(float(packet["pts_time"]) for packet in after_packets if packet["stream_index"] == 0)
        if variable_frame_rate:
            self.assertEqual(len(original_pts), 23)
            gaps = [round(after - before, 3) for before, after in zip(original_pts, original_pts[1:])]
            self.assertGreater(max(gaps), 1.5 * min(gaps), "Fixture must contain a genuine irregular presentation gap")
        self.assertEqual(original_pts, final_pts)
        before_audio = [entry["timelineOffset"] for entry in original["audio_metadata"] if entry.get("timelineOffsetSource") == "embedded"]
        after_audio = [entry["timelineOffset"] for entry in final["audio_metadata"] if entry.get("timelineOffsetSource") == "embedded"]
        self.assertEqual(before_audio, after_audio)
        async with AsyncSession(self.engine) as session:
            self.assertTrue((await session.get(Movie, self.media_id)).hevc_compressed)
            self.assertEqual(len((await session.exec(select(MediaOperationReceipt))).all()), 1)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg and FFprobe required")
    async def test_real_ffmpeg_preserves_secondary_video_audio_and_subtitle_streams(self):
        await self.real_stream_preservation()

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg and FFprobe required")
    async def test_real_ffmpeg_preserves_nonzero_epoch_and_audio_video_offsets(self):
        await self.real_stream_preservation(epoch=5.123)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg and FFprobe required")
    async def test_real_ffmpeg_preserves_variable_frame_presentation_and_copied_audio(self):
        await self.real_stream_preservation(variable_frame_rate=True)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg and FFprobe required")
    async def test_real_ffmpeg_preserves_mp4_variable_frame_presentation_and_audio(self):
        await self.real_stream_preservation(variable_frame_rate=True, container="mp4")

    async def test_validation_rejects_relative_origin_shift_and_large_duration_loss(self):
        original = probe("h264", "original")
        final = probe("hevc", "optimized")
        for item in original["stream_manifest"] + final["stream_manifest"]:
            item.update(startTime=0.0, timeBase="1/1000")
        self.assertTrue(self.worker._valid_transcode(original, final, len(ORIGINAL), len(OPTIMIZED)))
        final["stream_manifest"][1]["startTime"] = 0.023
        self.assertFalse(self.worker._valid_transcode(original, final, len(ORIGINAL), len(OPTIMIZED)))
        final["stream_manifest"][1]["startTime"] = 0.0
        final["probed_duration"] -= 1.0
        self.assertFalse(self.worker._valid_transcode(original, final, len(ORIGINAL), len(OPTIMIZED)))
        final["probed_duration"] = original["probed_duration"]
        original["stream_manifest"][1]["duration"] = 20.0
        final["stream_manifest"][1]["duration"] = 10.0
        self.assertFalse(self.worker._valid_transcode(original, final, len(ORIGINAL), len(OPTIMIZED)))

    def observe_process_release(self):
        self.reaped_processes = []
        real_unregister = process_state.unregister_process

        def unregister(key):
            process = process_state.ACTIVE_PROCESSES[key]
            self.assertIsNotNone(process.returncode, "child must exit before registry removal")
            self.assertTrue(self.coordinator.active_work(), "writer lease must still own registry removal")
            self.reaped_processes.append(process)
            return real_unregister(key)

        self.stack.enter_context(mock.patch.object(hevc, "register_process", process_state.register_process))
        self.stack.enter_context(mock.patch.object(hevc, "unregister_process", unregister))

    async def assert_resource_rejection(self, original, old_metadata):
        self.assertEqual(self.video.read_bytes(), original)
        self.assertEqual(self.metadata.read_bytes(), old_metadata)
        async with AsyncSession(self.engine) as session:
            movie = await session.get(Movie, self.media_id)
            self.assertFalse(movie.hevc_compressed)
            self.assertEqual(movie.source_fingerprint, "original")
            self.assertEqual((await session.exec(select(MediaOperationReceipt))).all(), [])
        self.assertNotIn(f"hevc:{self.media_id}", process_state.ACTIVE_PROCESSES)
        self.assertNotIn(f"hevc:{self.media_id}", process_state.ACTIVE_PROCESS_LEASES)
        self.assertEqual(self.coordinator.active_work(), ())
        self.assertFalse(self.video.with_name(".video.hevc-part.mkv").exists())
        self.assertTrue(self.reaped_processes)
        self.assertTrue(all(process.returncode is not None for process in self.reaped_processes))

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg and FFprobe required")
    async def test_real_finite_encoder_automatically_stops_at_original_size_ceiling(self):
        command = [shutil.which("ffmpeg"), "-v", "error", "-nostdin", "-y",
            "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=24:duration=30",
            "-c:v", "libx264", "-crf", "51", "-preset", "slow", "-threads", "2", str(self.video)]
        created = await asyncio.to_thread(subprocess.run, command, capture_output=True, timeout=60)
        self.assertEqual(created.returncode, 0, created.stderr.decode())
        original, old_metadata = self.video.read_bytes(), self.metadata.read_bytes()
        self.install_worker(real_encoder=True)
        async def production_polling(delay):
            if delay == 60.0:
                self.worker.is_running = False
            else:
                await self.real_sleep(delay)
        self.stack.enter_context(mock.patch.object(hevc.asyncio, "sleep", production_polling))
        self.observe_process_release()
        real_supervise = self.worker._supervise_transcode
        observed = {}

        async def supervise(process, output, original_size, lease):
            real_stat = Path.stat

            def stat(path, *args, **kwargs):
                result = real_stat(path, *args, **kwargs)
                if path == output and result.st_size >= original_size and process.returncode is None:
                    observed.update(original_bytes=original_size, output_bytes=result.st_size,
                                    child_alive_at_ceiling=True, registered=process_state.ACTIVE_PROCESSES.get(f"hevc:{self.media_id}") is process)
                return result

            with mock.patch.object(Path, "stat", stat):
                return await real_supervise(process, output, original_size, lease)

        with mock.patch.object(self.worker, "_supervise_transcode", supervise):
            await asyncio.wait_for(self.worker._worker_loop(), 30)
        self.assertTrue(observed.get("child_alive_at_ceiling"), observed)
        self.assertTrue(observed["registered"])
        self.assertGreaterEqual(observed["output_bytes"], observed["original_bytes"])
        self.assertNotEqual(self.reaped_processes[0].returncode, 0, "resource rejection must stop the live encoder")
        await self.assert_resource_rejection(original, old_metadata)
        self.live_growth_observation = dict(observed, automatically_reaped=True, original_preserved=True,
                                            metadata_preserved=True, registry_released=True, leases_released=True)

    def install_blocked_encoder(self, *, output_bytes=1):
        self.install_worker()
        self.observe_process_release()
        self.child_started = asyncio.Event()

        async def encoder(*command, **kwargs):
            script = "from pathlib import Path; import sys,time; Path(sys.argv[1]).write_bytes(b'x'*int(sys.argv[2])); time.sleep(60)"
            process = await self.real_create_process(sys.executable, "-c", script, command[-1], str(output_bytes), **kwargs)
            self.child_started.set()
            return process

        self.stack.enter_context(mock.patch.object(hevc.asyncio, "create_subprocess_exec", encoder))

    async def test_output_equal_to_source_is_stopped_before_acceptance(self):
        self.install_blocked_encoder(output_bytes=len(ORIGINAL))
        await asyncio.wait_for(self.worker._worker_loop(), 5)
        await self.assert_resource_rejection(ORIGINAL, json.dumps(self.old_metadata).encode())

    async def test_elapsed_deadline_reaps_a_child_with_no_eligible_completion(self):
        self.install_blocked_encoder()
        self.worker.MAX_TRANSCODE_SECONDS = 0.05
        await asyncio.wait_for(self.worker._worker_loop(), 5)
        await self.assert_resource_rejection(ORIGINAL, json.dumps(self.old_metadata).encode())

    async def test_media_volume_floor_stops_child_before_storage_is_consumed(self):
        self.install_blocked_encoder()
        with mock.patch.object(hevc.shutil, "disk_usage", return_value=SimpleNamespace(free=self.worker.STORAGE_SAFETY_MARGIN_BYTES)):
            await asyncio.wait_for(self.worker._worker_loop(), 5)
        await self.assert_resource_rejection(ORIGINAL, json.dumps(self.old_metadata).encode())

    async def test_resource_watcher_failure_reaps_child_and_releases_lease(self):
        self.install_blocked_encoder()
        with mock.patch.object(hevc.shutil, "disk_usage", side_effect=OSError("Injected storage watcher failure")):
            await asyncio.wait_for(self.worker._worker_loop(), 5)
        await self.assert_resource_rejection(ORIGINAL, json.dumps(self.old_metadata).encode())

    async def test_output_size_watcher_failure_reaps_child_and_releases_lease(self):
        self.install_blocked_encoder()
        real_stat = Path.stat
        output = self.video.with_name(".video.hevc-part.mkv")

        def failed_stat(path, *args, **kwargs):
            if path == output and self.worker.active_process is not None:
                raise OSError("Injected output size watcher failure")
            return real_stat(path, *args, **kwargs)

        with mock.patch.object(Path, "stat", failed_stat):
            await asyncio.wait_for(self.worker._worker_loop(), 5)
        await self.assert_resource_rejection(ORIGINAL, json.dumps(self.old_metadata).encode())

    async def test_cancel_during_resource_supervision_reaps_child_before_registry_release(self):
        self.install_blocked_encoder()
        task = asyncio.create_task(self.worker._worker_loop())
        await asyncio.wait_for(self.child_started.wait(), 5)
        while f"hevc:{self.media_id}" not in process_state.ACTIVE_PROCESSES:
            await self.real_sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await self.assert_resource_rejection(ORIGINAL, json.dumps(self.old_metadata).encode())

    async def test_maintenance_cancellation_releases_child_and_media_before_backoff(self):
        self.install_blocked_encoder()
        patched_sleep = hevc.asyncio.sleep
        cooldown_seen = []

        async def sleep(delay):
            if delay == 5 * 60:
                self.assertEqual(self.coordinator.active_work(), ())
                self.assertFalse(media_lock(self.media_id).locked())
                cooldown_seen.append(True)
            await patched_sleep(delay)

        self.stack.enter_context(mock.patch.object(hevc.asyncio, "sleep", sleep))
        task = asyncio.create_task(self.worker._worker_loop())
        await asyncio.wait_for(self.child_started.wait(), 5)
        while f"hevc:{self.media_id}" not in process_state.ACTIVE_PROCESSES:
            await self.real_sleep(0)
        snapshot = self.coordinator.close_admission([EffectClass.DURABLE_MEDIA_WRITE], operation_id=str(uuid.uuid4()), generation=1)
        self.assertEqual(self.coordinator.request_cancellation(snapshot), 1)
        await asyncio.wait_for(task, 5)
        self.assertTrue((await self.coordinator.drain(snapshot, timeout=0)).complete)
        self.coordinator.release_fence(snapshot)
        self.assertEqual(cooldown_seen, [True])
        await self.assert_resource_rejection(ORIGINAL, json.dumps(self.old_metadata).encode())

    async def test_oversized_candidate_deferral_allows_next_local_candidate(self):
        first = await self.worker._next_candidate()
        self.assertEqual(first[1], self.media_id)
        async with AsyncSession(self.engine) as session:
            session.add(Movie(id="m_zzz_next", title="Next local candidate", description="", thumbnail_url="",
                video_url=f"/media/Movies/{self.media_id}/video.mkv", duration="20s", release_year=2026,
                codec="h264", hevc_compressed=False))
            await session.commit()
        self.install_blocked_encoder(output_bytes=len(ORIGINAL))
        await asyncio.wait_for(self.worker._worker_loop(), 5)
        next_candidate = await hevc.HEVCCompressorWorker._next_candidate(self.worker)
        self.assertEqual(next_candidate[1], "m_zzz_next")
        self.assertGreater(self.worker.deferred_candidates[self.media_id], hevc.time.monotonic())
        await self.assert_resource_rejection(ORIGINAL, json.dumps(self.old_metadata).encode())
