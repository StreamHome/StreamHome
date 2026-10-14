from __future__ import annotations

import ast
from pathlib import Path
import unittest


SERVER_ROOT = Path(__file__).resolve().parents[2]


def parsed(relative: str) -> ast.Module:
    path = SERVER_ROOT / relative
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def function_node(relative: str, name: str) -> ast.AsyncFunctionDef | ast.FunctionDef:
    for node in ast.walk(parsed(relative)):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{relative} does not define {name}")


def effect_names(relative: str, name: str) -> set[str]:
    node = function_node(relative, name)
    return {
        child.attr
        for child in ast.walk(node)
        if isinstance(child, ast.Attribute)
        and isinstance(child.value, ast.Name)
        and child.value.id == "EffectClass"
    }


def called_names(relative: str, name: str) -> set[str]:
    node = function_node(relative, name)
    names: set[str] = set()
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        if isinstance(child.func, ast.Name):
            names.add(child.func.id)
        elif isinstance(child.func, ast.Attribute):
            names.add(child.func.attr)
    return names


class WriterCoordinationContractTests(unittest.TestCase):
    """Source-level integration guards that do not import the application runtime."""

    def test_ingestion_admits_all_effects_before_processing(self) -> None:
        self.assertEqual(
            effect_names("services/queue.py", "_process_task"),
            {"DB_WRITE", "DURABLE_MEDIA_WRITE", "DISPOSABLE_CACHE_WRITE", "CLOUD_WRITE"},
        )
        self.assertIn("_process_admitted_task", called_names("services/queue.py", "_process_task"))

    def test_background_writers_declare_their_effects(self) -> None:
        expected = {
            ("services/tmdb.py", "_cache_worker"): {"DB_WRITE", "DURABLE_MEDIA_WRITE"},
            ("services/vibe_analysis.py", "analyze_entity"): {"DB_WRITE", "DURABLE_MEDIA_WRITE"},
            ("services/vibe_analysis.py", "backfill_catalog_vibes"): {"DB_WRITE", "DURABLE_MEDIA_WRITE"},
            ("services/recommendation.py", "recommendation_worker"): {"DB_WRITE", "DURABLE_MEDIA_WRITE"},
            ("services/hevc_compressor.py", "_worker_loop"): {"DB_WRITE", "DURABLE_MEDIA_WRITE", "DISPOSABLE_CACHE_WRITE"},
            ("services/playback_prep.py", "_run_ffmpeg_job"): {"DISPOSABLE_CACHE_WRITE"},
            ("services/playback_prep.py", "prepare"): {"DISPOSABLE_CACHE_WRITE"},
            ("services/playback_prep.py", "_schedule_remaining_after_baseline"): {"DISPOSABLE_CACHE_WRITE"},
        }
        for (path, function), effects in expected.items():
            with self.subTest(path=path, function=function):
                self.assertEqual(effect_names(path, function), effects)

    def test_backup_and_cloud_mutations_use_distinct_effects(self) -> None:
        self.assertEqual(effect_names("services/backup.py", "create_backup"), {"DATABASE_SNAPSHOT"})
        self.assertEqual(effect_names("services/rclone.py", "run"), {"CLOUD_WRITE"})
        self.assertNotIn("runtime_work", called_names("services/rclone.py", "open_stream"))

    def test_hidden_request_writers_are_explicit(self) -> None:
        self.assertEqual(effect_names("routes/auth.py", "resolve_auth"), {"DB_WRITE"})
        self.assertEqual(
            effect_names("services/integration_auth.py", "authenticate_integration_token"),
            {"DB_WRITE"},
        )
        self.assertEqual(
            effect_names("routes/playback.py", "ensure_source_metadata"),
            {"DB_WRITE", "DURABLE_MEDIA_WRITE"},
        )
        self.assertEqual(
            effect_names("routes/playback.py", "playback_response_lease"),
            {"PLAYBACK_STREAM", "DISPOSABLE_CACHE_WRITE"},
        )
        self.assertEqual(
            effect_names("routes/setup.py", "_drive_job"),
            {"DB_WRITE", "CONFIG_WRITE"},
        )
        self.assertEqual(
            effect_names("routes/auth.py", "update_session_policy"),
            {"DB_WRITE", "CONFIG_WRITE"},
        )
        self.assertEqual(
            effect_names("routes/queue.py", "delete_download"),
            {"DB_WRITE", "DISPOSABLE_CACHE_WRITE"},
        )
        self.assertEqual(
            effect_names("routes/update.py", "install_update"),
            {"DB_WRITE"},
        )
        self.assertEqual(
            effect_names("routes/update.py", "cancel_pending_update"),
            {"DB_WRITE"},
        )
        constants = {
            child.value
            for child in ast.walk(parsed("application.py"))
            if isinstance(child, ast.Constant) and isinstance(child.value, str)
        }
        self.assertIn("/api/setup/readiness", constants)
        self.assertIn("/api/system/settings", constants)
        self.assertIn("/api/update/policy", constants)

    def test_candidate_drive_mutations_are_cloud_writes(self) -> None:
        node = function_node("services/rclone.py", "_mutates_cloud_remote")
        constants = {
            child.value
            for child in ast.walk(node)
            if isinstance(child, ast.Constant) and isinstance(child.value, str)
        }
        self.assertIn("rcat", constants)
        self.assertIn("mkdir", constants)
        self.assertNotIn("RCLONE_REMOTE_PATH", constants)

    def test_progressive_response_owns_actual_stream_lifetime(self) -> None:
        calls = called_names("routes/playback.py", "seekable_source_response")
        self.assertIn("admit", calls)
        self.assertIn("LeaseBoundPlaybackResponse", calls)
        response_class = next(node for node in parsed("routes/playback.py").body
            if isinstance(node, ast.ClassDef) and node.name == "LeaseBoundPlaybackResponse")
        response_calls = {node.func.attr for node in ast.walk(response_class)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}
        self.assertIn("bind", response_calls)
        self.assertIn("release", response_calls)
        self.assertIn("release", calls)
        self.assertEqual(
            effect_names("routes/playback.py", "seekable_source_response"),
            {"PLAYBACK_STREAM"},
        )

    def test_adaptive_and_auxiliary_playback_responses_are_lease_bound(self) -> None:
        for function in (
            "serve_ingest_preview_manifest",
            "serve_ingest_preview_asset",
            "serve_master_manifest",
            "serve_hls_asset",
            "serve_playback_subtitles",
        ):
            with self.subTest(function=function):
                calls = called_names("routes/playback.py", function)
                if function == "serve_playback_subtitles":
                    self.assertIn("_serve_playback_subtitles_locked", calls)
                    self.assertIn("media_lock", calls)
                    calls |= called_names("routes/playback.py", "_serve_playback_subtitles_locked")
                self.assertIn("playback_response_lease", calls)
                self.assertIn("LeaseBoundPlaybackResponse", calls)

    def test_threaded_ingestion_preserves_context_for_child_association(self) -> None:
        wrapper = function_node("services/ffmpeg.py", "download_and_merge")
        node = function_node("services/ffmpeg.py", "_download_and_merge_controlled")
        self.assertIn("_download_and_merge_controlled", called_names("services/ffmpeg.py", "download_and_merge"))
        self.assertIn("run_cancellation_safe_thread", called_names("services/ffmpeg.py", "_download_and_merge_controlled"))
        calls = [child for tree in (wrapper, node) for child in ast.walk(tree) if isinstance(child, ast.Call)]
        self.assertFalse(
            any(
                isinstance(call.func, ast.Attribute)
                and call.func.attr in {"to_thread", "run_in_executor"}
                for call in calls
            )
        )

    def test_lifecycle_sensitive_thread_writes_use_cancellation_safe_join(self) -> None:
        expected = {
            ("services/backup.py", "_create_admitted_backup"),
            ("services/queue.py", "_process_admitted_task"),
            ("services/vibe_analysis.py", "_analyze_entity_admitted"),
        }
        for path, function in expected:
            with self.subTest(path=path, function=function):
                calls = called_names(path, function)
                if path == "services/vibe_analysis.py":
                    self.assertIn("_analyze_owned_entity", calls)
                    self.assertIn("media_ownership", calls)
                    calls |= called_names(path, "_analyze_owned_entity")
                self.assertIn("run_cancellation_safe_thread", calls)

    def test_backup_verification_listing_and_pair_mutations_use_threaded_lock_boundary(self) -> None:
        admitted = function_node("services/backup.py", "_create_admitted_backup")
        self.assertIn(
            "run_cancellation_safe_thread",
            called_names("services/backup.py", "_create_admitted_backup"),
        )
        finalizer = next(
            child
            for child in ast.walk(admitted)
            if isinstance(child, ast.FunctionDef) and child.name == "run_backup"
        )
        finalizer_calls = {
            child.func.attr
            for child in ast.walk(finalizer)
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute)
        } | {
            child.func.id
            for child in ast.walk(finalizer)
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Name)
        }
        self.assertIn("fsync", finalizer_calls)
        self.assertIn("validate_backup_database", finalizer_calls)
        self.assertIn("create_manifest", finalizer_calls)
        for function in (
            "delete_backup",
            "prune_old_backups",
            "get_local_backups_async",
            "sync_backups_to_cloud",
        ):
            with self.subTest(function=function):
                source = function_node("services/backup.py", function)
                self.assertTrue(
                    any(
                        isinstance(child, ast.Name) and child.id == "BACKUP_LOCK"
                        for child in ast.walk(source)
                    )
                )
        self.assertIn(
            "run_cancellation_safe_thread",
            called_names("services/backup.py", "get_local_backups_async"),
        )
        daily_calls = called_names("application.py", "_daily_backup_worker")
        self.assertIn("get_local_backups_async", daily_calls)
        self.assertIn("prune_old_backups", daily_calls)

    def test_authority_sensitive_update_reads_use_canonical_strict_parser(self) -> None:
        self.assertIn(
            "read_update_journal",
            called_names("services/update.py", "_read_update_state_strict_unlocked"),
        )
        self.assertIn(
            "read_update_journal",
            called_names("lifecycle/recovery.py", "_update_plan"),
        )

    def test_playback_prepare_admits_cache_before_mutating_helper(self) -> None:
        calls = called_names("services/playback_prep.py", "prepare")
        self.assertIn("runtime_work", calls)
        self.assertIn("_prepare_admitted", calls)
        self.assertNotIn("mkdir", calls)
        self.assertNotIn("touch", calls)
        self.assertNotIn("clear_preparation_error", calls)
        self.assertIn(
            "runtime_work",
            called_names("services/playback_prep.py", "_schedule_remaining_after_baseline"),
        )

    def test_direct_tmdb_cache_rejection_is_deferred_without_duplicate_tasks(self) -> None:
        self.assertIn("_defer_direct_cache", called_names("services/tmdb.py", "cache_media_locally"))
        defer_node = function_node("services/tmdb.py", "_defer_direct_cache")
        calls = called_names("services/tmdb.py", "_defer_direct_cache")
        self.assertIn("create_task", calls)
        self.assertTrue(
            any(
                isinstance(child, ast.Compare)
                and any(isinstance(operator, ast.In) for operator in child.ops)
                for child in ast.walk(defer_node)
            )
        )


if __name__ == "__main__":
    unittest.main()
