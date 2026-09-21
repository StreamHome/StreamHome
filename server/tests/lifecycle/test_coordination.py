from __future__ import annotations

import asyncio
from contextvars import Context
from dataclasses import replace
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid

from lifecycle.coordination import (
    AdmissionClosedError,
    ChildProcessIdentity,
    EffectClass,
    FenceConflictError,
    FenceIdentity,
    LeaseStateError,
    RuntimeCoordinator,
    get_runtime_coordinator,
    read_process_start_identity,
    run_cancellation_safe_thread,
    use_runtime_coordinator_for_testing,
)


class RuntimeCoordinatorTests(unittest.IsolatedAsyncioTestCase):
    async def test_admission_registers_and_releases_exact_work(self) -> None:
        coordinator = RuntimeCoordinator()
        async with coordinator.work("test.writer", [EffectClass.DB_WRITE]) as lease:
            self.assertEqual(lease.effects, frozenset({EffectClass.DB_WRITE}))
            self.assertEqual(len(coordinator.active_work()), 1)
        self.assertEqual(coordinator.active_work(), ())
        with self.assertRaises(LeaseStateError):
            _ = lease.effects

    async def test_multiple_classes_close_atomically_and_unrelated_class_stays_open(self) -> None:
        coordinator = RuntimeCoordinator()
        snapshot = coordinator.close_admission(
            [EffectClass.DB_WRITE, EffectClass.CLOUD_WRITE],
            operation_id=str(uuid.uuid4()),
            generation=9,
        )
        self.assertEqual(
            snapshot.effects,
            frozenset({EffectClass.DB_WRITE, EffectClass.CLOUD_WRITE}),
        )
        for effect in (EffectClass.DB_WRITE, EffectClass.CLOUD_WRITE):
            with self.assertRaises(AdmissionClosedError):
                async with coordinator.work("test.blocked", [effect]):
                    pass
        async with coordinator.work("test.cache", [EffectClass.DISPOSABLE_CACHE_WRITE]):
            self.assertEqual(len(coordinator.active_work()), 1)

    async def test_lease_ids_are_unique(self) -> None:
        coordinator = RuntimeCoordinator()
        first_scope = coordinator.work("test.first", [EffectClass.DB_WRITE])
        first = await first_scope.__aenter__()
        # Clear inherited nesting by creating the second root in a fresh task.
        second_id: str | None = None

        async def second_root() -> None:
            nonlocal second_id
            async with coordinator.work("test.second", [EffectClass.DB_WRITE]) as second:
                second_id = second.lease_id

        await asyncio.create_task(second_root(), context=Context())
        self.assertNotEqual(first.lease_id, second_id)
        await first_scope.__aexit__(None, None, None)

    async def test_context_exception_releases_lease(self) -> None:
        coordinator = RuntimeCoordinator()
        with self.assertRaisesRegex(RuntimeError, "boom"):
            async with coordinator.work("test.exception", [EffectClass.DB_WRITE]):
                raise RuntimeError("boom")
        self.assertEqual(coordinator.active_work(), ())

    async def test_double_release_is_rejected(self) -> None:
        coordinator = RuntimeCoordinator()
        lease = coordinator.admit("test.release", [EffectClass.DB_WRITE])
        lease.release()
        with self.assertRaises(LeaseStateError):
            lease.release()

    async def test_explicit_lease_can_bind_across_task_boundary(self) -> None:
        coordinator = RuntimeCoordinator()
        lease = coordinator.admit("test.stream", [EffectClass.PLAYBACK_STREAM])

        async def consumer() -> str | None:
            with coordinator.bind(lease):
                current = coordinator.current_lease()
                return current.lease_id if current else None

        self.assertEqual(await asyncio.create_task(consumer()), lease.lease_id)
        lease.release()

    async def test_existing_playback_is_drained_but_new_playback_is_rejected(self) -> None:
        coordinator = RuntimeCoordinator()
        existing = coordinator.admit("test.stream", [EffectClass.PLAYBACK_STREAM])
        snapshot = coordinator.close_admission(
            [EffectClass.PLAYBACK_STREAM],
            operation_id=str(uuid.uuid4()),
            generation=7,
        )
        with self.assertRaises(AdmissionClosedError):
            coordinator.admit("test.late-stream", [EffectClass.PLAYBACK_STREAM])
        timed_out = await coordinator.drain(snapshot, 0)
        self.assertTrue(timed_out.timed_out)
        self.assertEqual(timed_out.remaining[0]["owner"], "test.stream")
        existing.release()
        self.assertTrue((await coordinator.drain(snapshot, 0.5)).complete)

    async def test_unfinished_scope_remains_visible(self) -> None:
        coordinator = RuntimeCoordinator()
        scope = coordinator.work("test.leaked", [EffectClass.DB_WRITE])
        await scope.__aenter__()
        self.assertEqual(coordinator.active_work()[0]["owner"], "test.leaked")
        await scope.__aexit__(None, None, None)

    async def test_process_singleton_can_be_isolated_for_tests(self) -> None:
        original = get_runtime_coordinator()
        isolated = RuntimeCoordinator()
        with use_runtime_coordinator_for_testing(isolated):
            self.assertIs(get_runtime_coordinator(), isolated)
        self.assertIs(get_runtime_coordinator(), original)

    async def test_close_and_admit_share_one_race_boundary(self) -> None:
        coordinator = RuntimeCoordinator()
        operation_id = str(uuid.uuid4())
        ready = asyncio.Event()
        release = asyncio.Event()

        async def existing_writer() -> None:
            async with coordinator.work("test.writer", [EffectClass.DB_WRITE]):
                ready.set()
                await release.wait()

        task = asyncio.create_task(existing_writer())
        await ready.wait()
        snapshot = coordinator.close_admission(
            [EffectClass.DB_WRITE], operation_id=operation_id, generation=4
        )
        self.assertEqual(len(snapshot.lease_ids), 1)
        with self.assertRaises(AdmissionClosedError):
            async with coordinator.work("test.late", [EffectClass.DB_WRITE]):
                pass
        release.set()
        await task
        await coordinator.drain(snapshot, 0.5)

    async def test_concurrent_admit_or_close_has_no_untracked_writer(self) -> None:
        for _ in range(100):
            coordinator = RuntimeCoordinator()
            operation_id = str(uuid.uuid4())
            barrier = threading.Barrier(2)
            outcome: dict[str, object] = {}

            def writer() -> None:
                barrier.wait()
                try:
                    outcome["lease"] = coordinator.admit(
                        "test.racer", [EffectClass.DB_WRITE]
                    )
                except AdmissionClosedError:
                    outcome["rejected"] = True

            def closer() -> None:
                barrier.wait()
                outcome["snapshot"] = coordinator.close_admission(
                    [EffectClass.DB_WRITE], operation_id=operation_id, generation=1
                )

            writer_thread = threading.Thread(target=writer)
            closer_thread = threading.Thread(target=closer)
            writer_thread.start()
            closer_thread.start()
            writer_thread.join()
            closer_thread.join()

            snapshot = outcome["snapshot"]
            lease = outcome.get("lease")
            if lease is not None:
                self.assertIn(lease.lease_id, snapshot.lease_ids)
                lease.release()
            else:
                self.assertTrue(outcome.get("rejected"))
                self.assertFalse(snapshot.lease_ids)

    async def test_nested_work_reuses_and_conservatively_widens_root_lease(self) -> None:
        coordinator = RuntimeCoordinator()
        async with coordinator.work("test.root", [EffectClass.DB_WRITE]) as root:
            async with coordinator.work("test.nested", [EffectClass.CLOUD_WRITE]) as nested:
                self.assertIs(root, nested)
                self.assertEqual(
                    nested.effects,
                    frozenset({EffectClass.DB_WRITE, EffectClass.CLOUD_WRITE}),
                )
            self.assertIn(EffectClass.CLOUD_WRITE, root.effects)
            self.assertEqual(len(coordinator.active_work()), 1)

    async def test_context_propagates_to_child_task(self) -> None:
        coordinator = RuntimeCoordinator()
        async with coordinator.work("test.root", [EffectClass.DB_WRITE]) as root:
            async def child() -> str:
                async with coordinator.work("test.child", [EffectClass.CONFIG_WRITE]) as lease:
                    return lease.lease_id

            self.assertEqual(await asyncio.create_task(child()), root.lease_id)
            self.assertIn(EffectClass.CONFIG_WRITE, root.effects)

    async def test_child_nested_scope_outlives_root_without_becoming_invisible(self) -> None:
        coordinator = RuntimeCoordinator()
        child_entered = asyncio.Event()
        child_release = asyncio.Event()
        root_scope = coordinator.work("test.root", [EffectClass.DB_WRITE])
        root = await root_scope.__aenter__()

        async def child() -> None:
            async with coordinator.work("test.child", [EffectClass.DB_WRITE]) as nested:
                self.assertIs(nested, root)
                child_entered.set()
                await child_release.wait()

        child_task = asyncio.create_task(child())
        await child_entered.wait()
        await root_scope.__aexit__(None, None, None)
        self.assertEqual(coordinator.active_work()[0]["lease_id"], root.lease_id)
        snapshot = coordinator.close_admission(
            [EffectClass.DB_WRITE], operation_id=str(uuid.uuid4()), generation=1
        )
        self.assertIn(root.lease_id, snapshot.lease_ids)
        self.assertTrue((await coordinator.drain(snapshot, 0)).timed_out)

        child_release.set()
        await child_task
        self.assertEqual(coordinator.active_work(), ())
        self.assertTrue((await coordinator.drain(snapshot, 0.5)).complete)

    async def test_multiple_nested_scopes_finalize_once_after_root_release(self) -> None:
        coordinator = RuntimeCoordinator()
        entered = [asyncio.Event(), asyncio.Event()]
        releases = [asyncio.Event(), asyncio.Event()]
        root_scope = coordinator.work("test.root", [EffectClass.DB_WRITE])
        root = await root_scope.__aenter__()

        async def child(index: int) -> None:
            async with coordinator.work("test.child", [EffectClass.DB_WRITE]) as nested:
                self.assertIs(nested, root)
                entered[index].set()
                await releases[index].wait()

        tasks = [asyncio.create_task(child(index)) for index in range(2)]
        await asyncio.gather(*(event.wait() for event in entered))
        await root_scope.__aexit__(None, None, None)
        releases[0].set()
        await tasks[0]
        self.assertEqual(len(coordinator.active_work()), 1)
        releases[1].set()
        await tasks[1]
        self.assertEqual(coordinator.active_work(), ())

    async def test_nested_exception_and_cancellation_release_scope_ownership(self) -> None:
        for mode in ("exception", "cancel"):
            with self.subTest(mode=mode):
                coordinator = RuntimeCoordinator()
                entered = asyncio.Event()
                hold = asyncio.Event()
                root_scope = coordinator.work("test.root", [EffectClass.DB_WRITE])
                await root_scope.__aenter__()

                async def child() -> None:
                    async with coordinator.work("test.child", [EffectClass.DB_WRITE]):
                        entered.set()
                        if mode == "exception":
                            raise RuntimeError("nested failure")
                        await hold.wait()

                task = asyncio.create_task(child())
                await entered.wait()
                await root_scope.__aexit__(None, None, None)
                if mode == "cancel":
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                else:
                    with self.assertRaisesRegex(RuntimeError, "nested failure"):
                        await task
                self.assertEqual(coordinator.active_work(), ())

    async def test_concurrent_nested_work_uses_one_root(self) -> None:
        coordinator = RuntimeCoordinator()
        async with coordinator.work("test.root", [EffectClass.DB_WRITE]) as root:
            async def widen(effect: EffectClass) -> str:
                async with coordinator.work("test.child", [effect]) as nested:
                    await asyncio.sleep(0)
                    return nested.lease_id

            ids = await asyncio.gather(
                widen(EffectClass.CLOUD_WRITE),
                widen(EffectClass.CONFIG_WRITE),
            )
            self.assertEqual(ids, [root.lease_id, root.lease_id])
            self.assertEqual(len(coordinator.active_work()), 1)

    async def test_nested_widening_is_rejected_after_effect_closes(self) -> None:
        coordinator = RuntimeCoordinator()
        async with coordinator.work("test.root", [EffectClass.DB_WRITE]) as root:
            snapshot = coordinator.close_admission(
                [EffectClass.CLOUD_WRITE], operation_id=str(uuid.uuid4()), generation=2
            )
            with self.assertRaises(AdmissionClosedError):
                async with coordinator.work("test.nested", [EffectClass.CLOUD_WRITE]):
                    pass
            self.assertNotIn(EffectClass.CLOUD_WRITE, root.effects)
            await coordinator.drain(snapshot, 0)

    async def test_nested_existing_closed_effect_can_continue(self) -> None:
        coordinator = RuntimeCoordinator()
        async with coordinator.work("test.root", [EffectClass.DB_WRITE]) as root:
            snapshot = coordinator.close_admission(
                [EffectClass.DB_WRITE], operation_id=str(uuid.uuid4()), generation=2
            )
            async with coordinator.work("test.nested", [EffectClass.DB_WRITE]) as nested:
                self.assertIs(root, nested)
            result = await coordinator.drain(snapshot, 0)
            self.assertTrue(result.timed_out)

    async def test_drain_waits_only_for_pre_fence_matching_leases(self) -> None:
        coordinator = RuntimeCoordinator()
        db_release = asyncio.Event()
        cache_release = asyncio.Event()

        async def hold(owner: str, effect: EffectClass, release: asyncio.Event) -> None:
            async with coordinator.work(owner, [effect]):
                await release.wait()

        db_task = asyncio.create_task(hold("test.db", EffectClass.DB_WRITE, db_release))
        cache_task = asyncio.create_task(
            hold("test.cache", EffectClass.DISPOSABLE_CACHE_WRITE, cache_release)
        )
        await asyncio.sleep(0)
        snapshot = coordinator.close_admission(
            [EffectClass.DB_WRITE], operation_id=str(uuid.uuid4()), generation=3
        )
        cache_release.set()
        await cache_task
        result = await coordinator.drain(snapshot, 0.01)
        self.assertTrue(result.timed_out)
        self.assertEqual(len(result.remaining), 1)
        db_release.set()
        await db_task
        await coordinator.drain(snapshot, 0.5)

    async def test_empty_drain_completes(self) -> None:
        coordinator = RuntimeCoordinator()
        snapshot = coordinator.close_admission(
            [EffectClass.DB_WRITE], operation_id=str(uuid.uuid4()), generation=1
        )
        result = await coordinator.drain(snapshot, 0)
        self.assertTrue(result.complete)
        self.assertFalse(result.timed_out)
        self.assertEqual(result.remaining, ())

    async def test_timeout_retains_closure_and_snapshot(self) -> None:
        coordinator = RuntimeCoordinator()
        release = asyncio.Event()
        task = asyncio.create_task(self._hold_db(coordinator, release))
        await asyncio.sleep(0)
        snapshot = coordinator.close_admission(
            [EffectClass.DB_WRITE], operation_id=str(uuid.uuid4()), generation=1
        )
        result = await coordinator.drain(snapshot, 0)
        self.assertTrue(result.timed_out)
        with self.assertRaises(AdmissionClosedError):
            async with coordinator.work("test.blocked", [EffectClass.DB_WRITE]):
                pass
        release.set()
        await task
        await coordinator.drain(snapshot, 0.5)

    async def test_cancellation_is_a_signal_not_forced_release(self) -> None:
        coordinator = RuntimeCoordinator()
        release = asyncio.Event()
        observed = asyncio.Event()

        async def work() -> None:
            async with coordinator.work("test.cancel", [EffectClass.DB_WRITE]) as lease:
                while not lease.cancellation_requested:
                    await asyncio.sleep(0)
                observed.set()
                await release.wait()

        task = asyncio.create_task(work())
        await asyncio.sleep(0)
        snapshot = coordinator.close_admission(
            [EffectClass.DB_WRITE], operation_id=str(uuid.uuid4()), generation=1
        )
        self.assertEqual(coordinator.request_cancellation(snapshot), 1)
        await observed.wait()
        result = await coordinator.drain(snapshot, 0)
        self.assertTrue(result.timed_out)
        release.set()
        await task
        await coordinator.drain(snapshot, 0.5)

    async def test_child_identity_is_attached_and_removed_exactly(self) -> None:
        coordinator = RuntimeCoordinator()
        async with coordinator.work("test.child", [EffectClass.DISPOSABLE_CACHE_WRITE]) as lease:
            identity = lease.attach_child(os.getpid(), "12345")
            projection = coordinator.active_work()[0]
            self.assertEqual(
                projection["child_processes"],
                [{"pid": os.getpid(), "process_start_identity": "12345"}],
            )
            lease.detach_child(identity)
            self.assertEqual(coordinator.active_work()[0]["child_processes"], [])
            with self.assertRaises(LeaseStateError):
                lease.detach_child(identity)

    async def test_unreaped_child_keeps_lease_visible_without_leaking_context(self) -> None:
        coordinator = RuntimeCoordinator()
        scope = coordinator.work("test.unreaped-child", [EffectClass.DB_WRITE])
        lease = await scope.__aenter__()
        identity = lease.attach_child(os.getpid(), "12345")
        await scope.__aexit__(None, None, None)
        self.assertIsNone(coordinator.current_lease())
        self.assertEqual(coordinator.active_work()[0]["lease_id"], lease.lease_id)
        lease.detach_child(identity)
        self.assertEqual(coordinator.active_work(), ())

    async def test_timeout_diagnostics_include_safe_child_identity(self) -> None:
        coordinator = RuntimeCoordinator()
        scope = coordinator.work("test.child-timeout", [EffectClass.DB_WRITE])
        lease = await scope.__aenter__()
        lease.attach_child(os.getpid(), "67890")
        snapshot = coordinator.close_admission(
            [EffectClass.DB_WRITE], operation_id=str(uuid.uuid4()), generation=1
        )
        result = await coordinator.drain(snapshot, 0)
        self.assertEqual(
            result.remaining[0]["child_processes"],
            [{"pid": os.getpid(), "process_start_identity": "67890"}],
        )
        await scope.__aexit__(None, None, None)
        lease.detach_child(ChildProcessIdentity(os.getpid(), "67890"))
        self.assertTrue((await coordinator.drain(snapshot, 0.5)).complete)

    async def test_last_of_multiple_children_finalizes_pending_release(self) -> None:
        coordinator = RuntimeCoordinator()
        lease = coordinator.admit("test.children", [EffectClass.DB_WRITE])
        first = lease.attach_child(os.getpid(), "111")
        second = lease.attach_child(os.getpid(), "222")
        lease.release()
        lease.detach_child(first)
        self.assertEqual(len(coordinator.active_work()), 1)
        lease.detach_child(second)
        self.assertEqual(coordinator.active_work(), ())
        with self.assertRaises(LeaseStateError):
            lease.detach_child(second)

    async def test_release_racing_last_child_detach_finalizes_once(self) -> None:
        for _ in range(100):
            coordinator = RuntimeCoordinator()
            lease = coordinator.admit("test.child-race", [EffectClass.DB_WRITE])
            child = lease.attach_child(os.getpid(), "333")
            barrier = threading.Barrier(2)
            errors: list[BaseException] = []

            def release() -> None:
                try:
                    barrier.wait()
                    lease.release()
                except BaseException as exc:
                    errors.append(exc)

            def detach() -> None:
                try:
                    barrier.wait()
                    lease.detach_child(child)
                except BaseException as exc:
                    errors.append(exc)

            threads = [threading.Thread(target=release), threading.Thread(target=detach)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(errors, [])
            self.assertEqual(coordinator.active_work(), ())

    async def test_cancellation_safe_thread_keeps_lease_and_domain_lock_until_completion(self) -> None:
        coordinator = RuntimeCoordinator()
        domain_lock = asyncio.Lock()
        thread_started = threading.Event()
        thread_release = threading.Event()
        contender_acquired = asyncio.Event()

        def blocking_write() -> str:
            thread_started.set()
            thread_release.wait()
            return "complete"

        async def writer() -> None:
            async with coordinator.work("test.thread", [EffectClass.DATABASE_SNAPSHOT]):
                async with domain_lock:
                    await run_cancellation_safe_thread(blocking_write)

        async def contender() -> None:
            async with domain_lock:
                contender_acquired.set()

        writer_task = asyncio.create_task(writer())
        while not thread_started.is_set():
            await asyncio.sleep(0)
        snapshot = coordinator.close_admission(
            [EffectClass.DATABASE_SNAPSHOT],
            operation_id=str(uuid.uuid4()),
            generation=1,
        )
        writer_task.cancel()
        contender_task = asyncio.create_task(contender())
        await asyncio.sleep(0)
        self.assertFalse(contender_acquired.is_set())
        self.assertTrue((await coordinator.drain(snapshot, 0)).timed_out)

        thread_release.set()
        with self.assertRaises(asyncio.CancelledError):
            await writer_task
        await contender_task
        self.assertTrue(contender_acquired.is_set())
        self.assertTrue((await coordinator.drain(snapshot, 0.5)).complete)

    async def test_cancellation_safe_thread_can_signal_business_cancellation(self) -> None:
        thread_started = threading.Event()
        stop_requested = threading.Event()

        def cancellable_work() -> None:
            thread_started.set()
            stop_requested.wait()

        task = asyncio.create_task(
            run_cancellation_safe_thread(
                cancellable_work,
                cancellation_callback=stop_requested.set,
            )
        )
        while not thread_started.is_set():
            await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1.0)
        self.assertTrue(stop_requested.is_set())

    async def test_cancellation_safe_thread_propagates_root_lease_context(self) -> None:
        coordinator = RuntimeCoordinator()
        async with coordinator.work("test.thread-context", [EffectClass.DB_WRITE]) as lease:
            observed = await run_cancellation_safe_thread(
                lambda: coordinator.current_lease().lease_id
                if coordinator.current_lease() is not None
                else None
            )
            self.assertEqual(observed, lease.lease_id)

    async def test_multiple_drain_waiters_are_all_notified(self) -> None:
        coordinator = RuntimeCoordinator()
        lease = coordinator.admit("test.waiters", [EffectClass.DB_WRITE])
        snapshot = coordinator.close_admission(
            [EffectClass.DB_WRITE], operation_id=str(uuid.uuid4()), generation=1
        )
        waiters = [asyncio.create_task(coordinator.drain(snapshot, 1.0)) for _ in range(4)]
        await asyncio.sleep(0)
        lease.release()
        results = await asyncio.gather(*waiters)
        self.assertTrue(all(result.complete and not result.timed_out for result in results))
        self.assertEqual(coordinator.active_work(), ())

    async def test_conflicting_fence_is_rejected(self) -> None:
        coordinator = RuntimeCoordinator()
        coordinator.close_admission(
            [EffectClass.DB_WRITE], operation_id=str(uuid.uuid4()), generation=1
        )
        with self.assertRaises(FenceConflictError):
            coordinator.close_admission(
                [EffectClass.DB_WRITE], operation_id=str(uuid.uuid4()), generation=2
            )

    async def test_exact_fence_release_reopens_only_its_owned_effects(self) -> None:
        coordinator = RuntimeCoordinator()
        existing = coordinator.admit("test.pre-fence", [EffectClass.DB_WRITE])
        first = coordinator.close_admission(
            [EffectClass.DB_WRITE], operation_id=str(uuid.uuid4()), generation=1
        )
        second = coordinator.close_admission(
            [EffectClass.CLOUD_WRITE], operation_id=str(uuid.uuid4()), generation=2
        )
        old_drain = asyncio.create_task(coordinator.drain(first, 1.0))
        await asyncio.sleep(0)

        coordinator.release_fence(first)

        with self.assertRaises(LeaseStateError):
            await old_drain
        lease = coordinator.admit("test.reopened", [EffectClass.DB_WRITE])
        lease.release()
        existing.release()
        with self.assertRaises(AdmissionClosedError):
            coordinator.admit("test.still-closed", [EffectClass.CLOUD_WRITE])
        with self.assertRaises(LeaseStateError):
            await coordinator.drain(first, 0)
        self.assertTrue((await coordinator.drain(second, 0)).complete)

    async def test_release_rejects_wrong_tampered_or_foreign_snapshot(self) -> None:
        coordinator = RuntimeCoordinator()
        snapshot = coordinator.close_admission(
            [EffectClass.DB_WRITE], operation_id=str(uuid.uuid4()), generation=7
        )
        wrong_operation = replace(
            snapshot,
            fence=FenceIdentity(str(uuid.uuid4()), snapshot.fence.generation),
        )
        wrong_generation = replace(
            snapshot,
            fence=FenceIdentity(snapshot.fence.operation_id, 8),
        )
        tampered = replace(snapshot, lease_ids=frozenset({str(uuid.uuid4())}))

        for candidate in (wrong_operation, wrong_generation, tampered):
            with self.subTest(candidate=candidate):
                with self.assertRaises(LeaseStateError):
                    coordinator.release_fence(candidate)
        with self.assertRaises(LeaseStateError):
            RuntimeCoordinator().release_fence(snapshot)
        with self.assertRaises(AdmissionClosedError):
            coordinator.admit("test.blocked", [EffectClass.DB_WRITE])

        coordinator.release_fence(snapshot)

    async def test_stale_or_duplicate_fence_release_cannot_reopen_newer_closure(self) -> None:
        coordinator = RuntimeCoordinator()
        old_snapshot = coordinator.close_admission(
            [EffectClass.DB_WRITE], operation_id=str(uuid.uuid4()), generation=1
        )
        coordinator.release_fence(old_snapshot)
        with self.assertRaises(LeaseStateError):
            coordinator.release_fence(old_snapshot)

        new_snapshot = coordinator.close_admission(
            [EffectClass.DB_WRITE], operation_id=str(uuid.uuid4()), generation=2
        )
        with self.assertRaises(LeaseStateError):
            coordinator.release_fence(old_snapshot)
        with self.assertRaises(AdmissionClosedError):
            coordinator.admit("test.newer-fence", [EffectClass.DB_WRITE])
        coordinator.release_fence(new_snapshot)

    async def test_release_covers_all_effects_owned_by_one_overlapping_fence(self) -> None:
        coordinator = RuntimeCoordinator()
        operation_id = str(uuid.uuid4())
        first = coordinator.close_admission(
            [EffectClass.DB_WRITE], operation_id=operation_id, generation=3
        )
        overlapping = coordinator.close_admission(
            [EffectClass.DB_WRITE, EffectClass.CONFIG_WRITE],
            operation_id=operation_id,
            generation=3,
        )

        coordinator.release_fence(first)

        for effect in (EffectClass.DB_WRITE, EffectClass.CONFIG_WRITE):
            lease = coordinator.admit("test.reopened", [effect])
            lease.release()
        with self.assertRaises(LeaseStateError):
            coordinator.release_fence(overlapping)

    async def test_diagnostics_are_sanitized_and_bounded_to_known_fields(self) -> None:
        coordinator = RuntimeCoordinator()
        async with coordinator.work("test.safe-label", [EffectClass.DB_WRITE]):
            projection = coordinator.active_work()[0]
            self.assertEqual(
                set(projection),
                {"lease_id", "owner", "effects", "age_seconds", "cancellation_requested", "child_processes"},
            )
        with self.assertRaises(ValueError):
            async with coordinator.work("https://secret.example/token=x", [EffectClass.DB_WRITE]):
                pass

    async def test_invalid_effect_and_empty_effects_are_rejected(self) -> None:
        coordinator = RuntimeCoordinator()
        with self.assertRaises(ValueError):
            async with coordinator.work("test.writer", []):
                pass
        with self.assertRaises(ValueError):
            async with coordinator.work("test.writer", ["unknown"]):
                pass

    async def test_stale_or_tampered_snapshot_is_rejected(self) -> None:
        coordinator = RuntimeCoordinator()
        snapshot = coordinator.close_admission(
            [EffectClass.DB_WRITE], operation_id=str(uuid.uuid4()), generation=1
        )
        other = RuntimeCoordinator()
        with self.assertRaises(LeaseStateError):
            await other.drain(snapshot, 0)

    async def _hold_db(self, coordinator: RuntimeCoordinator, release: asyncio.Event) -> None:
        async with coordinator.work("test.hold", [EffectClass.DB_WRITE]):
            await release.wait()


class ProcessIdentityTests(unittest.TestCase):
    def test_current_process_start_identity_is_safe_when_available(self) -> None:
        identity = read_process_start_identity(os.getpid())
        if os.name == "posix":
            self.assertIsNotNone(identity)
            self.assertRegex(identity or "", r"^[A-Za-z0-9_.:-]+$")

    def test_child_identity_rejects_sensitive_or_invalid_values(self) -> None:
        with self.assertRaises(ValueError):
            ChildProcessIdentity(0, None)
        with self.assertRaises(ValueError):
            ChildProcessIdentity(1, "secret value with spaces")


class ImportSafetyTests(unittest.TestCase):
    def test_import_does_not_load_application_modules_or_write_files(self) -> None:
        server_root = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory() as temporary:
            script = (
                "import json, os, pathlib, sys; "
                f"sys.path.insert(0, {str(server_root)!r}); "
                "before=sorted(p.name for p in pathlib.Path('.').iterdir()); "
                "import lifecycle.coordination; "
                "after=sorted(p.name for p in pathlib.Path('.').iterdir()); "
                "forbidden=sorted(name for name in ('config','db','main','models') if name in sys.modules); "
                "print(json.dumps({'before': before, 'after': after, 'forbidden': forbidden}))"
            )
            completed = subprocess.run(
                [sys.executable, "-B", "-c", script],
                cwd=temporary,
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(
            completed.stdout.strip(),
            '{"before": [], "after": [], "forbidden": []}',
        )


if __name__ == "__main__":
    unittest.main()
