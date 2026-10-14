"""Disposable multi-origin HTTP, native HLS and acceptance regressions."""
from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack, ExitStack, closing
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

import db as db_module
from config import settings
from models import DownloadTask, Movie
from lifecycle.coordination import get_runtime_coordinator
from services import controlled_source, ffmpeg
from services import queue as worker, state
from services.ingestion_headers import encode_source_headers
from services.controlled_source import SourceRelay, controlled_media_input, is_controlled_input
from services.hls_context import SourceContextPolicy
from services.ingestion_errors import write_task_diagnostics
from services.ingestion_security import UnsafeIngestionSource


CONTEXT = {"Origin": "https://player.example", "Referer": "https://player.example/"}
ROOT_HEADERS = {**CONTEXT, "Referer": "https://player.example/watch/private?token=synthetic-referer-secret#fragment", "Cookie": "synthetic=root-only", "Authorization": "Bearer synthetic-root"}


class HLSContextSourceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.servers = []
        self.routes = {}
        self.requests = []
        self.gated = set()
        self.gate_status = 404
        self.transient_failures = 0
        self.peers = [await self.origin() for _ in range(4)]
        self.patches = ExitStack()
        self.patches.enter_context(patch.object(settings, "TEMP_DIR", str(self.root / "temp")))

    async def asyncTearDown(self):
        for server in self.servers:
            server.close()
            await server.wait_closed()
        self.patches.close()
        self.temporary.cleanup()
        self.assertFalse(any(item["owner"] == "ingestion.source-relay"
                             for item in get_runtime_coordinator().active_work()))

    async def origin(self):
        async def serve(reader, writer):
            try:
                lines = (await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)).decode("iso-8859-1").split("\r\n")
                method, path, _ = lines[0].split(" ", 2)
                headers = {line.split(":", 1)[0].lower(): line.split(":", 1)[1].strip()
                           for line in lines[1:] if ":" in line}
                port = writer.get_extra_info("sockname")[1]
                status, response_headers, body = self.routes.get((port, path), (404, {}, b""))
                if (port, path) in self.gated and any(headers.get(name.lower()) != value for name, value in CONTEXT.items()):
                    status, body = self.gate_status, b""
                elif (port, path) in self.gated and self.transient_failures:
                    self.transient_failures -= 1
                    status, body = 503, b""
                self.requests.append((port, path, headers, status))
                response_headers = {**response_headers, "Content-Length": str(len(body)), "Connection": "close"}
                wire = [f"HTTP/1.1 {status} Synthetic"] + [f"{key}: {value}" for key, value in response_headers.items()]
                writer.write(("\r\n".join(wire) + "\r\n\r\n").encode("iso-8859-1"))
                if method != "HEAD":
                    writer.write(body)
                await writer.drain()
            except (ConnectionError, OSError, asyncio.IncompleteReadError):
                pass
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except (ConnectionError, OSError):
                    pass
        server = await asyncio.start_server(serve, "127.0.0.1", 0)
        self.servers.append(server)
        return f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"

    def route(self, peer, path, body=b"", status=200, headers=None):
        self.routes[(int(peer.rsplit(":", 1)[1]), path)] = (status, headers or {}, body)

    async def native_fixture(self):
        command = [shutil.which("ffmpeg"), "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=25",
                   "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-t", "16", "-c:v", "libx264",
                   "-preset", "ultrafast", "-threads", "1", "-g", "25", "-sc_threshold", "0", "-c:a", "aac",
                   "-f", "hls", "-hls_time", "1", "-hls_playlist_type", "vod", "-start_number", "100",
                   "-hls_segment_filename", str(self.root / "segment_%03d.ts"), str(self.root / "variant.m3u8")]
        result = await asyncio.to_thread(subprocess.run, command, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        playlist = (self.root / "variant.m3u8").read_text()
        original = await asyncio.to_thread(subprocess.run, [shutil.which("ffmpeg"), "-v", "error", "-y", "-i",
            str(self.root / "variant.m3u8"), "-c", "copy", str(self.root / "accepted-source.mp4")], capture_output=True, timeout=30)
        self.assertEqual(original.returncode, 0, original.stderr.decode())
        segment_paths = [line for line in playlist.splitlines() if not line.startswith("#") and line]
        self.assertEqual(len(segment_paths), 16)
        for index, filename in enumerate(segment_paths):
            peer = self.peers[1 + index % 3]
            path = f"/part_{index:03d}.jpg?signature=synthetic-query-secret"
            self.route(peer, path, (self.root / filename).read_bytes(), headers={"Content-Type": "image/jpeg"})
            playlist = playlist.replace(filename, peer + path)
            if 6 <= index <= 13:
                self.gated.add((int(peer.rsplit(":", 1)[1]), path))
        self.route(self.peers[0], "/variant.txt", playlist.encode(), headers={"Content-Type": "text/plain"})
        self.route(self.peers[0], "/master.txt", b"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=160x90\nvariant.txt\n")
        return self.peers[0] + "/master.txt"

    async def test_native_eight_late_segments_reject_partial_output_then_preserve_all_frames(self):
        source = await self.native_fixture()
        original_routes = {key: self.routes[key] for key in self.gated}
        for key in self.gated:
            self.routes[key] = (404, {}, b"")
        output = self.root / "healthy.mp4"
        shutil.copyfile(self.root / "accepted-source.mp4", output)
        original_hash = hashlib.sha256(output.read_bytes()).hexdigest()
        metrics, native_results = [], []
        run_native = ffmpeg._run_ffmpeg_sync
        def observe_native(*args, **kwargs):
            result = run_native(*args, **kwargs)
            native_results.append(result)
            return result
        with patch.object(ffmpeg, "_run_ffmpeg_sync", observe_native), patch.object(ffmpeg, "update_task_metrics", side_effect=lambda *args, **kwargs: metrics.append(args)):
            success, failure = await ffmpeg.download_and_merge("native-denied", source, None, ROOT_HEADERS,
                str(output), 16, preview_enabled=True, client_address="127.0.0.1")
        self.assertFalse(success)
        self.assertEqual(failure.code, "SOURCE_NOT_FOUND")
        self.assertFalse(failure.retryable)
        self.assertEqual(len(native_results), 1, "Source failure must not trigger preview-only fallback")
        self.assertTrue(native_results[0][0], "Exercise native exit zero with partial source media")
        self.assertEqual(hashlib.sha256(output.read_bytes()).hexdigest(), original_hash)
        self.assertFalse(list(self.root.glob("*.part.mp4")))
        self.assertTrue(all(args[1] < 100 for args in metrics))
        diagnostic = Path(failure.diagnostics_path).read_text()
        snapshot = json.loads(next(line for line in diagnostic.splitlines() if line.startswith('{"attempt"')))
        denied_indices = {int(path[6:9]) for _, path, _, status in self.requests if path.startswith("/part_") and status == 404}
        self.assertEqual(denied_indices, set(range(6, 14)))
        self.assertEqual(snapshot["failures"][0]["sequence"], 106)
        self.assertGreaterEqual(snapshot["status_counts"]["404"], 8)
        self.assertEqual(snapshot["failures"][0]["start_seconds"], 6)
        self.assertIn("native result before source rejection", diagnostic)
        for sensitive in (source, "synthetic-query-secret", "synthetic-root", "synthetic-referer-secret", "https://player.example"):
            self.assertNotIn(sensitive, diagnostic)
        self.routes.update(original_routes)
        self.requests.clear()
        with patch.object(ffmpeg, "update_task_metrics") as metrics_mock:
            success, failure = await ffmpeg.download_and_merge("native-automatic", source, None, ROOT_HEADERS,
                str(output), 16, preview_enabled=False, client_address="127.0.0.1", attempt=2)
        self.assertTrue(success, failure)
        self.assertTrue(all(call.args[1] < 100 for call in metrics_mock.call_args_list))
        probe = await asyncio.to_thread(subprocess.run, [shutil.which("ffprobe"), "-v", "error", "-count_frames", "-select_streams", "v:0",
            "-show_entries", "stream=nb_read_frames,duration", "-of", "json", str(output)], capture_output=True, timeout=30)
        self.assertEqual(probe.returncode, 0, probe.stderr.decode())
        video = json.loads(probe.stdout)["streams"][0]
        self.assertEqual(int(video["nb_read_frames"]), 400)
        self.assertAlmostEqual(float(video["duration"]), 16, delta=0.05)
        segments = [item for item in self.requests if item[1].startswith("/part_")]
        self.assertEqual({int(item[1][6:9]) for item in segments}, set(range(16)))
        for _, _, headers, status in segments:
            self.assertEqual(status, 200)
            self.assertEqual(headers.get("origin"), CONTEXT["Origin"])
            self.assertEqual(headers.get("referer"), CONTEXT["Referer"])
            self.assertNotIn("authorization", headers)
            self.assertNotIn("cookie", headers)

    async def test_real_redirect_chain_automatically_preserves_context_and_input_snapshot(self):
        root, foreign, second, _ = self.peers
        self.route(root, "/root", status=302, headers={"Location": foreign + "/foreign"})
        self.route(foreign, "/foreign", status=302, headers={"Location": second + "/second"})
        self.route(second, "/second", status=302, headers={"Location": root + "/final"})
        self.route(root, "/final", b"synthetic payload")
        relay = SourceRelay(client_address="127.0.0.1")
        submitted = dict(ROOT_HEADERS)
        await relay.start()
        try:
            local = await relay.prepare_input(root + "/root", submitted)
            submitted["Origin"] = "https://changed.example"
            submitted["Referer"] = "https://changed.example/private"
            async with httpx.AsyncClient(trust_env=False) as consumer:
                self.assertEqual((await consumer.get(local)).content, b"synthetic payload")
        finally:
            await relay.close()
        self.assertFalse(is_controlled_input(local))
        for _, path, headers, _ in self.requests:
            if path in ("/foreign", "/second", "/final"):
                self.assertEqual(headers["origin"], CONTEXT["Origin"])
                self.assertNotIn("cookie", headers)
                self.assertNotIn("authorization", headers)
                self.assertEqual(headers["referer"], ROOT_HEADERS["Referer"] if path == "/final" else CONTEXT["Referer"])

    async def test_native_source_processing_error_cannot_accept_partial_frames(self):
        source = await self.native_fixture()
        for key in self.gated:
            status, headers, body = self.routes[key]
            # Actual upstream wire bytes: HTTPX decodes valid UTF-8 obs-text,
            # which cannot be re-encoded as an ISO-8859-1 relay header.
            headers = {**headers, "Content-Type": "日本語".encode("utf-8").decode("iso-8859-1")}
            self.routes[key] = status, headers, body
        output = self.root / "healthy.mp4"
        shutil.copyfile(self.root / "accepted-source.mp4", output)
        original_hash = hashlib.sha256(output.read_bytes()).hexdigest()
        native_results = []
        real_native = ffmpeg._run_ffmpeg_sync
        def observe(*args, **kwargs):
            result = real_native(*args, **kwargs)
            native_results.append(result)
            return result
        with patch.object(ffmpeg, "_run_ffmpeg_sync", observe), patch.object(ffmpeg, "update_task_metrics") as metrics:
            success, failure = await ffmpeg.download_and_merge("native-protocol-error", source, None, ROOT_HEADERS,
                str(output), 16, preview_enabled=False, client_address="127.0.0.1")
        self.assertTrue(native_results[0][0], "Native exit zero must not hide relay processing errors")
        self.assertFalse(success)
        self.assertEqual(hashlib.sha256(output.read_bytes()).hexdigest(), original_hash)
        self.assertTrue(all(call.args[1] < 100 for call in metrics.call_args_list))
        self.assertFalse(list(self.root.glob("*.part.mp4")))
        diagnostic = Path(failure.diagnostics_path).read_text()
        self.assertIn('"error_kind": "UnicodeEncodeError"', diagnostic)
        self.assertIn('"decision": "automatic-context"', diagnostic)
        self.assertNotIn("日本語", diagnostic)
        self.assertNotIn("synthetic-query-secret", diagnostic)

    async def test_actual_queue_preserves_accepted_catalog_then_retries_transient_source_with_fresh_relays(self):
        source = await self.native_fixture()
        original_routes = {key: self.routes[key] for key in self.gated}
        for key in self.gated:
            self.routes[key] = (404, {}, b"")
        database = self.root / "queue.db"
        engine = create_async_engine(f"sqlite+aiosqlite:///{database}")
        media_dir = self.root / "media"
        target = media_dir / "Movies/Synthetic_2026_TMDB_5001/Synthetic_2026.mp4"
        target.parent.mkdir(parents=True)
        shutil.copyfile(self.root / "accepted-source.mp4", target)
        old_hash = hashlib.sha256(target.read_bytes()).hexdigest()
        url = "/media/Movies/Synthetic_2026_TMDB_5001/Synthetic_2026.mp4"
        metrics, relay_owners, attempts, delays = [], [], [], []
        async with engine.begin() as connection:
            await connection.run_sync(SQLModel.metadata.create_all)
        async with AsyncSession(engine) as session:
            session.add(Movie(id="m_5001", tmdb_id=5001, title="Synthetic", description="Previous", thumbnail_url="",
                video_url=url, duration="1m", release_year=2026, availability="available", source_fingerprint="accepted-old"))
            await session.commit()
        async def seed(task_id):
            async with AsyncSession(engine) as session:
                session.add(DownloadTask(id=task_id, tmdb_id=5001, media_type="movie", video_url=source,
                    headers_str=encode_source_headers(ROOT_HEADERS, {}), status="PENDING", private_source_allowed=True, created_at="synthetic"))
                await session.commit()
        def metric(task_id, progress, **kwargs):
            if progress == 100:
                with closing(sqlite3.connect(database)) as connection:
                    status = connection.execute("SELECT status FROM downloadtask WHERE id=?", (task_id,)).fetchone()[0]
                self.assertEqual(status, "COMPLETED", "100% requires committed publication acceptance")
            metrics.append((task_id, progress))
        real_download, real_probe, real_sleep = worker.download_and_merge, worker.probe_media_stream, asyncio.sleep
        async def download(**kwargs):
            attempts.append((kwargs["task_id"], kwargs["attempt"]))
            return await real_download(**kwargs)
        async def probe(*args, **kwargs):
            result = await real_probe(*args, **kwargs)
            if kwargs["task_id"] == "queue-transient":
                self.transient_failures = 1
            return result
        async def fast_sleep(delay, *args, **kwargs):
            if delay in (5, 10):
                delays.append(delay)
                return await real_sleep(0)
            return await real_sleep(delay, *args, **kwargs)
        real_init = SourceRelay.__init__
        def observe_relay(relay, *args, **kwargs):
            real_init(relay, *args, **kwargs)
            relay_owners.append(relay)
        try:
            with ExitStack() as stack:
                stack.enter_context(patch.object(settings, "MEDIA_DIR", str(media_dir)))
                stack.enter_context(patch.object(worker, "engine", engine))
                stack.enter_context(patch.object(db_module, "engine", engine))
                stack.enter_context(patch.object(worker, "download_and_merge", download))
                stack.enter_context(patch.object(worker, "probe_media_stream", probe))
                stack.enter_context(patch.object(worker, "notify_video_sender", AsyncMock()))
                stack.enter_context(patch.object(worker.tmdb_client, "fetch_movie_metadata", AsyncMock(return_value={"title": "Synthetic", "releaseYear": 2026})))
                stack.enter_context(patch.object(worker.vibe_analysis_manager, "enqueue", AsyncMock()))
                stack.enter_context(patch.object(worker.DownloadQueueManager, "_schedule_playback_baseline", AsyncMock()))
                stack.enter_context(patch.object(worker.DownloadQueueManager, "_reserve_storage", return_value=None))
                stack.enter_context(patch.object(state, "update_task_metrics", metric))
                stack.enter_context(patch.object(ffmpeg, "update_task_metrics", metric))
                stack.enter_context(patch.object(worker.asyncio, "sleep", fast_sleep))
                stack.enter_context(patch.object(SourceRelay, "__init__", observe_relay))
                await seed("queue-denied")
                await worker.DownloadQueueManager()._process_task("queue-denied")
                async with AsyncSession(engine) as session:
                    task, movie = await session.get(DownloadTask, "queue-denied"), await session.get(Movie, "m_5001")
                    self.assertEqual(task.status, "FAILED")
                    self.assertIn("SOURCE_NOT_FOUND", task.error_message)
                    self.assertEqual(movie.video_url, url)
                    self.assertEqual(movie.source_fingerprint, "accepted-old")
                    self.assertEqual(movie.availability, "available")
                self.assertEqual(hashlib.sha256(target.read_bytes()).hexdigest(), old_hash)
                self.assertEqual(attempts, [], "Terminal probe failure must stop before native download")
                self.assertNotIn(("queue-denied", 100), metrics)
                self.routes.update(original_routes)
                await seed("queue-transient")
                await worker.DownloadQueueManager()._process_task("queue-transient")
                async with AsyncSession(engine) as session:
                    task, movie = await session.get(DownloadTask, "queue-transient"), await session.get(Movie, "m_5001")
                    self.assertEqual(task.status, "COMPLETED", task.error_message)
                    self.assertEqual(movie.video_url, url)
                    self.assertAlmostEqual(movie.probed_duration, 16, delta=0.1)
                    self.assertNotEqual(movie.source_fingerprint, "accepted-old")
                self.assertEqual([attempt for task, attempt in attempts if task == "queue-transient"], [1, 2])
                self.assertEqual(delays, [5])
                self.assertIn(("queue-transient", 100), metrics)
                self.assertNotEqual(hashlib.sha256(target.read_bytes()).hexdigest(), old_hash)
                for relay in relay_owners:
                    self.assertTrue(relay.closed)
                    self.assertEqual(relay.resources, {})
                    self.assertFalse(relay.tasks)
                capabilities = [relay.capability for relay in relay_owners]
                self.assertEqual(len(capabilities), len(set(capabilities)))
        finally:
            await engine.dispose()

    async def test_close_time_read_failure_rejects_native_candidate(self):
        self.route(self.peers[0], "/direct.mp4", b"complete synthetic payload")
        original = controlled_source.validated_stream_request
        calls = 0
        async def open_response(*args, **kwargs):
            nonlocal calls
            response = await original(*args, **kwargs)
            calls += 1
            if calls == 2:
                contents = await response.aread()
                async def chunks(*_):
                    yield contents
                async def fail_close():
                    raise httpx.ReadError("synthetic close failure")
                response.aiter_bytes = chunks
                response.aclose = fail_close
            return response
        def native(task_id, command, duration, preview=False):
            local = command[command.index("-i") + 1]
            self.assertEqual(httpx.get(local, trust_env=False).status_code, 200)
            Path(command[-1]).write_bytes(b"unaccepted candidate")
            return True, "synthetic native success"
        output = self.root / "accepted.mp4"
        output.write_bytes(b"accepted bytes")
        with patch.object(controlled_source, "validated_stream_request", open_response), patch.object(ffmpeg, "_run_ffmpeg_sync", native), patch.object(ffmpeg, "update_task_metrics"):
            success, failure = await ffmpeg.download_and_merge("close-race", self.peers[0] + "/direct.mp4", None, {},
                str(output), 1, preview_enabled=False, client_address="127.0.0.1")
        self.assertFalse(success)
        self.assertEqual(output.read_bytes(), b"accepted bytes")
        self.assertIn('"phase": "response-close"', Path(failure.diagnostics_path).read_text())

    async def test_preparation_404_remains_terminal_when_response_close_also_fails(self):
        self.route(self.peers[0], "/missing", status=404)
        original = controlled_source.validated_stream_request
        async def open_response(*args, **kwargs):
            response = await original(*args, **kwargs)
            close = response.aclose
            async def fail_close():
                await close()
                raise httpx.ReadError("synthetic secondary close failure")
            response.aclose = fail_close
            return response
        output = self.root / "never-created.mp4"
        with patch.object(controlled_source, "validated_stream_request", open_response), patch.object(ffmpeg, "_run_ffmpeg_sync") as native:
            success, failure = await ffmpeg.download_and_merge("preparation-close", self.peers[0] + "/missing", None, {},
                str(output), 1, preview_enabled=False, client_address="127.0.0.1")
        self.assertFalse(success)
        self.assertEqual(failure.code, "SOURCE_NOT_FOUND")
        self.assertFalse(failure.retryable)
        native.assert_not_called()
        diagnostic = Path(failure.diagnostics_path).read_text()
        snapshot = json.loads(next(line for line in diagnostic.splitlines() if line.startswith('{"attempt"')))
        self.assertEqual(snapshot["failures"][0]["error_kind"], "HTTPStatusError")
        self.assertEqual(snapshot["failures"][0]["http_status"], 404)
        self.assertEqual(snapshot["failures"][1]["error_kind"], "ReadError")
        self.assertEqual(snapshot["failures"][1]["phase"], "preparation-close")

    async def test_cancel_during_relay_close_keeps_lease_and_rejects_candidate(self):
        self.route(self.peers[0], "/direct.mp4", b"synthetic source")
        output = self.root / "accepted.mp4"
        output.write_bytes(b"accepted original")
        closing, release = asyncio.Event(), asyncio.Event()
        local_urls = []
        real_close = SourceRelay.close
        async def delayed_close(relay):
            closing.set()
            await release.wait()
            await real_close(relay)
        def native(task_id, command, duration, preview=False):
            local_urls.append(command[command.index("-i") + 1])
            Path(command[-1]).write_bytes(b"unaccepted native candidate")
            return True, "native success before cancellation"
        baseline = len(get_runtime_coordinator().active_work())
        with patch.object(SourceRelay, "close", delayed_close), patch.object(ffmpeg, "_run_ffmpeg_sync", native), patch.object(ffmpeg, "update_task_metrics"):
            task = asyncio.create_task(ffmpeg.download_and_merge("close-cancel", self.peers[0] + "/direct.mp4", None, {},
                str(output), 1, preview_enabled=False, client_address="127.0.0.1"))
            await asyncio.wait_for(closing.wait(), 5)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            self.assertEqual(len(get_runtime_coordinator().active_work()), baseline + 1)
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(len(get_runtime_coordinator().active_work()), baseline)
        self.assertEqual(output.read_bytes(), b"accepted original")
        self.assertFalse(list(self.root.glob("*.part.mp4")))
        self.assertTrue(local_urls)
        self.assertTrue(all(not is_controlled_input(url) for url in local_urls))

    async def test_clean_preview_failure_can_fall_back_without_sticky_source_state(self):
        self.route(self.peers[0], "/direct.mp4", b"synthetic source")
        output = self.root / "fallback.mp4"
        calls = []
        def native(task_id, command, duration, preview=False):
            calls.append(preview)
            Path(command[-1]).write_bytes(b"complete native candidate")
            return (False, "synthetic preview encoder error") if preview else (True, "native success")
        with patch.object(ffmpeg, "_run_ffmpeg_sync", native), patch.object(ffmpeg, "update_task_metrics"):
            success, failure = await ffmpeg.download_and_merge("preview-fallback", self.peers[0] + "/direct.mp4", None, {},
                str(output), 1, preview_enabled=True, client_address="127.0.0.1")
        self.assertTrue(success, failure)
        self.assertEqual(calls, [True, False])
        self.assertEqual(output.read_bytes(), b"complete native candidate")

    async def test_preparation_downgrade_has_safe_policy_evidence(self):
        async def redirect(request):
            return httpx.Response(302, headers={"Location": "http://target.example/private?secret=synthetic-secret"}, request=request)
        async with httpx.AsyncClient(transport=httpx.MockTransport(redirect), trust_env=False) as pinned:
            with patch("services.ingestion_security.pinned_source_client", return_value=pinned), patch("services.ingestion_security.validate_url", AsyncMock()):
                with self.assertRaises(UnsafeIngestionSource):
                    async with controlled_media_input(self.peers[0].replace("http:", "https:") + "/master?secret=synthetic-secret", headers=ROOT_HEADERS,
                        task_id="downgrade", client_address="127.0.0.1"):
                        self.fail("Downgrade must fail before yielding an input")
        text = (self.root / "temp/queue-errors/downgrade.log").read_text()
        self.assertIn('"decision": "redirect-policy-rejected"', text)
        for value in ("source.example", "target.example", "synthetic-secret", "synthetic-root", "player.example"):
            self.assertNotIn(value, text)

    async def test_preparation_and_child_policy_failures_persist_safe_decisions(self):
        with self.assertRaises(UnsafeIngestionSource):
            async with controlled_media_input("http://169.254.169.254/latest/secret", task_id="root-blocked", client_address="127.0.0.1"):
                self.fail("Blocked root must not yield a local input")
        root_diagnostic = (self.root / "temp/queue-errors/root-blocked.log").read_text()
        self.assertIn('"decision": "root-validation-rejected"', root_diagnostic)
        self.assertNotIn("169.254.169.254", root_diagnostic)
        self.route(self.peers[0], "/missing?signature=synthetic-secret", status=404)
        with self.assertRaises(httpx.HTTPStatusError):
            async with controlled_media_input(self.peers[0] + "/missing?signature=synthetic-secret", headers=ROOT_HEADERS,
                task_id="preparation", input_role="audio", attempt=3, client_address="127.0.0.1"):
                self.fail("Missing root must not yield a local input")
        text = (self.root / "temp/queue-errors/preparation.log").read_text()
        self.assertIn('"phase": "preparation"', text)
        self.assertIn('"decision": "source-origin"', text)
        self.assertNotIn("synthetic-secret", text)
        self.assertNotIn("synthetic-root", text)
        self.route(self.peers[0], "/blocked", b"#EXTM3U\n#EXTINF:1,\nhttp://169.254.169.254/latest/secret\n#EXT-X-ENDLIST\n")
        with self.assertRaises(UnsafeIngestionSource):
            async with controlled_media_input(self.peers[0] + "/blocked", task_id="blocked", client_address="127.0.0.1") as local:
                async with httpx.AsyncClient(trust_env=False) as consumer:
                    playlist = (await consumer.get(local)).text
                    segment = next(line for line in playlist.splitlines() if line.startswith("http"))
                    self.assertEqual((await consumer.get(segment)).status_code, 502)
        text = (self.root / "temp/queue-errors/blocked.log").read_text()
        self.assertIn('"decision": "source-validation-rejected"', text)
        self.assertNotIn("169.254.169.254", text)
        self.assertNotIn("/latest/secret", text)

    async def test_video_and_audio_relays_keep_independent_context_on_the_same_cdn(self):
        video, audio, cdn, _ = self.peers
        for root, role in ((video, "video"), (audio, "audio")):
            self.route(root, "/master", f"#EXTM3U\n#EXTINF:1,\n{cdn}/{role}.ts\n#EXT-X-ENDLIST\n".encode())
            self.route(cdn, f"/{role}.ts", role.encode())
        async with AsyncExitStack() as inputs:
            locals = []
            for root, role in ((video, "video"), (audio, "audio")):
                locals.append(await inputs.enter_async_context(controlled_media_input(root + "/master",
                    headers={"Origin": f"https://{role}-player.example", "Cookie": f"synthetic={role}"},
                    input_role=role, client_address="127.0.0.1")))
            async with httpx.AsyncClient(trust_env=False) as consumer:
                for local in locals:
                    playlist = (await consumer.get(local)).text
                    segment = next(line for line in playlist.splitlines() if line.startswith("http"))
                    self.assertEqual((await consumer.get(segment)).status_code, 200)
        for _, path, headers, _ in self.requests:
            if path in ("/video.ts", "/audio.ts"):
                role = path.removeprefix("/").removesuffix(".ts")
                self.assertEqual(headers["origin"], f"https://{role}-player.example")
                self.assertNotIn("cookie", headers)

    async def test_repeated_uri_occurrences_and_diagnostic_bounds(self):
        relay = SourceRelay(task_id="occurrences")
        relay.base_url = "http://127.0.0.1:1/synthetic-capability"
        relay.context_policy = SourceContextPolicy.capture("https://source.example/playlist", {})
        rewritten = relay.rewrite_manifest(b"#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:20\n#EXT-X-BYTERANGE:10@0\n#EXTINF:2,\nshared.ts\n#EXT-X-BYTERANGE:10@10\n#EXTINF:3,\nshared.ts\n", "https://source.example/playlist", {})
        paths = [line for line in rewritten.decode().splitlines() if line.startswith("http")]
        self.assertEqual(len(set(paths)), 2)
        resources = list(relay.resources.values())
        self.assertEqual([r.occurrence.sequence for r in resources], [20, 21])
        self.assertEqual([r.occurrence.start_seconds for r in resources], [0, 2])
        self.assertEqual([r.occurrence.byte_range for r in resources], ["10@0", "10@10"])
        for index in range(30):
            relay.record_failure(httpx.ReadError("private URL and header values are not persisted"), resource=resources[index % 2], request_id=index)
        self.assertEqual(relay.failure_count, 30)
        self.assertEqual(len(relay.failures), 8)
        relay.persist_failure()
        for _ in range(10):
            path = write_task_diagnostics("occurrences", "bounded", "x" * 100000)
        self.assertLessEqual(Path(path).stat().st_size, 256 * 1024)
        await relay.close()


if __name__ == "__main__":
    unittest.main()
