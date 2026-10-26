from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import textwrap
import unittest
import uuid

from lifecycle import state


class UpdateShellLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.temp = Path(self.temporary.name)
        self.repository_root = Path(__file__).resolve().parents[3]
        script = (self.repository_root / "scripts/runtime/update.sh").read_text(encoding="utf-8")
        prefix, marker, _ = script.partition('case "${1:-}" in')
        self.assertTrue(marker)
        self.library = self.temp / "update-functions.sh"
        self.library.write_text(prefix, encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _shell(
        self,
        script: str,
        *,
        environment: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        if environment:
            env.update(environment)
        return subprocess.run(
            ["bash", "-c", f'source "$1"\n{script}', "bash", str(self.library)],
            cwd=self.repository_root,
            env=env,
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout,
        )

    def test_managed_database_effects_and_rollback_stop_before_mutation_on_permission_failure(self):
        database = self.temp/'canonical.db';database.write_bytes(b'unchanged canonical sentinel')
        for command in ('create_database_checkpoint', 'restore_database_checkpoint'):
            result = self._shell('STREAMHOME_MANAGED_UPDATE=true\n'
                'UPDATE_DATABASE="$FIXTURE_DATABASE"\n'
                'managed_update_runtime() { return 41; }\n'+command,
                environment={'FIXTURE_DATABASE': str(database)})
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(database.read_bytes(), b'unchanged canonical sentinel')

    def test_managed_readiness_probe_uses_typed_port_without_legacy_environment(self):
        result = self._shell('''
STREAMHOME_MANAGED_UPDATE=true
WEB_PORT=43127
read_env() { echo legacy_environment_read >&2; return 1; }
python3() { printf '%s\\n' "$2"; }
runtime_endpoints_ready aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa fixture
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, '43127\n')
        self.assertNotIn('legacy_environment_read', result.stderr)

    def test_managed_interrupted_rollback_uses_bound_environments_without_legacy_staging_or_setup(self):
        status = self.temp/'interrupted.json'
        status.write_text(json.dumps({'phase': 'recovering', 'previous_commit': 'a'*40,
            'target_commit': 'e'*40, 'transaction_id': str(uuid.uuid4()), 'staging_dir': '',
            'web_artifacts_swapped': True, 'web_dependencies_swapped': True,
            'python_dependencies_changed': True, 'runtime_committed': False,
            'lifecycle_generation': 3, 'lifecycle_phase': 'exclusive'}))
        checkpoint = self.temp/'bound-checkpoint.db';checkpoint.write_bytes(b'synthetic checkpoint')
        result = self._shell('''
STREAMHOME_MANAGED_UPDATE=true
STATUS_FILE="$FIXTURE_STATUS";UPDATE_DATABASE_CHECKPOINT="$FIXTURE_CHECKPOINT"
ROOT_DIR="$FIXTURE_ROOT";RUN_DIR="$FIXTURE_ROOT";WEB_PORT=3000
LIFECYCLE_ENABLED=true
initialize_update_paths() { :; }
lifecycle_cli() { return 5; }
initialize_lifecycle_authority() { :; }
acquire_update_lock() { :; }
cleanup() { :; }
runtime_endpoints_ready() { return 1; }
write_state() { printf 'state:%s\\n' "$1"; }
stop_installed_runtime_with_target_lifecycle() { echo stopped; }
preserve_unexpected_worktree_changes() { echo preserved; }
git() { if [[ "$3" == rev-parse ]]; then printf '%040d\\n' 0 | tr 0 e; else echo reset; fi; }
restore_database_checkpoint() { echo checkpoint; }
managed_update_runtime() { printf 'helper:%s\\n' "$1"; }
recovery_staging_is_safe() { echo unexpected_legacy_staging >&2; return 1; }
installed_release_has_guarded_bootstrap() { return 0; }
stop_maintenance() { :; }
start_installed_update_runtime() { echo start; }
promote_recovered_guarded_runtime() { echo promote; }
cleanup_update_checkpoint() { echo cleanup; }
recover_interrupted_release
''', environment={'FIXTURE_STATUS': str(status), 'FIXTURE_CHECKPOINT': str(checkpoint),
                'FIXTURE_ROOT': str(self.temp)})
        self.assertEqual(result.returncode, 12, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ['state:rolling_back', 'stopped',
            'helper:assert_rollback', 'preserved', 'reset', 'checkpoint', 'helper:bind_previous',
            'state:rolling_back', 'helper:verify_previous', 'state:recovering', 'start',
            'state:rolled_back', 'promote', 'cleanup'])
        self.assertNotIn('unexpected_legacy_staging', result.stderr)

    def test_accepted_managed_recovery_replaces_old_backend_and_retains_exact_previous_selection(self):
        for same_commit in (False, True):
            previous = 'a'*40;target = previous if same_commit else 'e'*40
            script = '''
STREAMHOME_MANAGED_UPDATE=true
OLD_COMMIT="$FIXTURE_PREVIOUS";TARGET_COMMIT="$FIXTURE_TARGET"
UPDATE_RECOVERY_COMMIT="$OLD_COMMIT";UPDATE_RECOVERY_TARGET=previous
TRANSACTION_ID=fixture
stop_installed_runtime_with_target_lifecycle() { echo stopped; }
runtime_backend_live() { echo stale_health_must_not_settle; return 0; }
start_installed_update_runtime() { echo guarded_start; }
stop_maintenance() { :; }
runtime_endpoints_ready() { printf 'endpoints:%s\\n' "$1"; }
commit_updated_runtime() { printf 'accept:%s\\n' "$1"; }
wait_for_committed_runtime() { printf 'ready:%s\\n' "$1"; }
cleanup_update_checkpoint() { echo cleanup; }
write_state() { printf 'state:%s:%s\\n' "$1" "$5"; }
record_post_commit_recovery_failure() { echo unexpected_failure; }
recover_accepted_update_runtime "$OLD_COMMIT"
'''
            result = self._shell(script, environment={'FIXTURE_PREVIOUS': previous, 'FIXTURE_TARGET': target})
            with self.subTest(same_commit=same_commit):
                self.assertEqual(result.returncode, 12, result.stderr)
                self.assertEqual(result.stdout.splitlines(), ['stopped', 'state:recovering:'+previous,
                    'guarded_start', 'endpoints:'+previous, 'accept:'+previous, 'state:recovering:'+previous,
                    'ready:'+previous, 'cleanup', 'state:rolled_back:'+previous])

    def test_accepted_recovery_stop_failure_preserves_acceptance_without_start_or_cleanup(self):
        result = self._shell('''
STREAMHOME_MANAGED_UPDATE=true
OLD_COMMIT=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
TARGET_COMMIT=eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee
UPDATE_RECOVERY_COMMIT="$OLD_COMMIT";UPDATE_RECOVERY_TARGET=previous
stop_installed_runtime_with_target_lifecycle() { return 1; }
start_installed_update_runtime() { echo must_not_start; }
cleanup_update_checkpoint() { echo must_not_cleanup; }
record_post_commit_recovery_failure() { printf 'preserved:%s\\n' "$1"; }
recover_accepted_update_runtime "$OLD_COMMIT"
''')
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stdout, 'preserved:'+'a'*40+'\n')
        result = self._shell('OLD_COMMIT=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n'
            'TARGET_COMMIT=eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee\n'
            'validate_lifecycle_authority() { return 0; }\n'
            'write_state() { return 1; }\n'
            'stop_installed_runtime_with_target_lifecycle() { printf unexpected-stop; }\n'
            'rollback_release')
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('unexpected-stop', result.stdout)

    def test_managed_script_paths_are_typed_data_and_refuse_stale_or_injected_context(self):
        operation = str(uuid.uuid4())
        context = {'schema_version': 1, 'operation_id': operation, 'generation': 2,
            'runtime_directory': '/var/lib/streamhome', 'database': '/var/lib/streamhome/database.db',
            'update_log': '/var/lib/streamhome/update.log',
            'staging_directory': '/var/cache/streamhome/update-preparation/'+operation,
            'previous_python': '/opt/streamhome/runtime/python/previous/venv/bin/python',
            'candidate_python': '/opt/streamhome/runtime/python/candidate/venv/bin/python',
            'database_checkpoint': '/var/lib/streamhome/update-checkpoints/'+operation+'/database.db',
            'recovery_commit': 'a'*40,
            'recovery_target': 'candidate',
            'web_port': 3002, 'public_origin': ''}
        script = ('STREAMHOME_MANAGED_UPDATE=true\nROOT_DIR=/opt/streamhome\n'
            'LIFECYCLE_OPERATION_ID="$FIXTURE_OPERATION"\nLIFECYCLE_GENERATION=2\n'
            'read_managed_update_context() { printf "%s" "$FIXTURE_CONTEXT"; }\n'
            'initialize_update_paths || exit 1\n'
            'printf "%s\\n" "$RUN_DIR" "$UPDATE_DATABASE" "$WEB_PORT" "$PUBLIC_URL"')
        def execute(value):
            return self._shell(script, environment={'FIXTURE_OPERATION': operation,
                'FIXTURE_CONTEXT': json.dumps(value)})
        result = execute(context)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, '/var/lib/streamhome\n/var/lib/streamhome/database.db\n3002\n\n')
        for changes in ({'generation': True}, {'generation': 3}, {'extra': 1},
                {'database': 'relative/database.db'}, {'web_port': 8000}, {'recovery_commit': 'invalid'},
                {'public_origin': 'https://example.invalid/\nRUN_DIR=other'}):
            with self.subTest(changes=changes):
                self.assertNotEqual(execute({**context, **changes}).returncode, 0)
        marker = self.temp/'must-not-execute'
        value = {**context, 'public_origin': '$(touch '+str(marker)+')'}
        self.assertEqual(execute(value).returncode, 0)
        self.assertFalse(marker.exists())

    def test_managed_context_worker_is_directly_parented_by_registered_bash_even_inside_command_substitution(self):
        observed = self.temp/'context-peer.json';worker = self.temp/'context-worker.py'
        worker.write_text('import json,os,sys\nfrom pathlib import Path\n'
            'Path('+repr(str(observed))+').write_text(json.dumps({"pid":os.getpid(),"parent":os.getppid(),"packet":json.load(sys.stdin)}))\n'
            'print("{\\"context\\":true}")\n')
        source = self.library.read_text()
        start = source.index('read_managed_update_context() {')
        end = source.index('\ninitialize_update_paths()', start)
        function = source[start:end].replace('/usr/local/libexec/streamhome/provision', str(worker))
        script = ('STREAMHOME_MANAGED_UPDATE=true\nROOT_DIR=/opt/streamhome\nLIFECYCLE_GENERATION=2\n'
            'LIFECYCLE_OPERATION_ID='+str(uuid.uuid4())+'\n'+function+
            '\ncontext="$(read_managed_update_context)" || exit 1\nprintf "%s\\n" "$BASHPID" "$context"\n')
        result = self._shell(script, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        parent, context = result.stdout.splitlines()
        self.assertEqual(json.loads(context), {'context': True})
        self.assertEqual(json.loads(observed.read_text())['parent'], int(parent))
        self.assertEqual(json.loads(observed.read_text())['packet'], {'schema_version': 1, 'generation': 2})

    def test_managed_runtime_refusal_never_falls_back_to_legacy_process_or_mutable_environment_paths(self):
        commands = {
            'start_maintenance': 'maintenance',
            'stop_installed_runtime_with_target_lifecycle': 'stop',
            'activate_prepared_web': 'bind_candidate',
            'restore_previous_web': 'bind_previous',
            'validate_current_python_environment': 'verify_previous',
            'start_installed_update_runtime': 'start',
        }
        for command, expected in commands.items():
            with self.subTest(command=command):
                result = self._shell('STREAMHOME_MANAGED_UPDATE=true\n'
                    'managed_update_runtime() { printf "runtime:%s\\n" "$1"; return 41; }\n'
                    'prepare_lifecycle_for_guarded_start() { return 0; }\n'
                    'ensure_guarded_commit_token() { return 0; }\n'
                    'write_state() { printf unexpected-write; return 97; }\n'
                    + command)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, 'runtime:'+expected+'\n', result.stderr)
        result = self._shell('STREAMHOME_MANAGED_UPDATE=true\n'
            'MAINTENANCE_PID=999999\nstop_maintenance\nrecord_prepared_setup_state')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, '')
        result = self._shell('STREAMHOME_MANAGED_UPDATE=true\ninstall_python_wheelhouse')
        self.assertNotEqual(result.returncode, 0)

    def test_retention_failure_blocks_exact_lifecycle_completion_and_preserves_owner_flags(self):
        script = """
LIFECYCLE_ENABLED=true
LIFECYCLE_PRESERVE=false
LIFECYCLE_OPERATION_ID=00000000-0000-4000-8000-000000000001
LIFECYCLE_GENERATION=2
LIFECYCLE_PHASE=verifying
ROOT_DIR="$FIXTURE_REPOSITORY"
retain_bound_update_before_completion() { printf 'retention\\n'; return "$FIXTURE_RETENTION_CODE"; }
lifecycle_cli() { printf completion > "$FIXTURE_COMPLETION_MARKER"; return 0; }
release_lifecycle_authority
result=$?
printf 'result=%s enabled=%s\\n' "$result" "$LIFECYCLE_ENABLED"
"""
        for retained in (False, True):
            with self.subTest(retained=retained):
                marker = self.temp / ('completion-' + str(retained))
                result = self._shell(script, environment={"FIXTURE_REPOSITORY": str(self.repository_root),
                    "FIXTURE_RETENTION_CODE": "0" if retained else "1",
                    "FIXTURE_COMPLETION_MARKER": str(marker)})
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('retention\n', result.stdout)
                self.assertEqual(marker.exists(), retained)
                self.assertIn('result=0 enabled=false' if retained else 'result=1 enabled=true', result.stdout)

    def test_fallback_stop_materializes_legacy_or_split_body_from_exact_target(self):
        for split, missing in ((False, False), (True, False), (True, True)):
            with self.subTest(split=split, missing=missing):
                root = self.temp / ("target-" + str(split) + "-" + str(missing))
                root.mkdir()
                (root / ".run").mkdir()
                helper = root / "server/scratch/runtime_control.py"
                helper.parent.mkdir(parents=True)
                helper.write_text("# Exact synthetic target helper\n")
                body = ('#!/usr/bin/env bash\n'
                    '[[ "$1" == --quiet && -d "$STREAMHOME_ROOT_OVERRIDE/.run" ]] || exit 81\n'
                    'printf target > "$STREAMHOME_ROOT_OVERRIDE/stop-proof"\n')
                if split:
                    (root / "stop.sh").write_text((self.repository_root / "stop.sh").read_text())
                    if not missing:
                        internal = root / "scripts/runtime/stop.sh"
                        internal.parent.mkdir(parents=True)
                        internal.write_text(body)
                else:
                    (root / "stop.sh").write_text(body)
                def git(*args):
                    return subprocess.run(["git", "-C", str(root), *args], check=True,
                        text=True, capture_output=True).stdout.strip()
                git("init", "-q")
                git("add", "stop.sh", "server")
                if split and not missing:
                    git("add", "scripts")
                git("-c", "user.name=Qualification", "-c", "user.email=qualification@example.invalid",
                    "commit", "-qm", "Synthetic lifecycle target")
                target = git("rev-parse", "HEAD")
                # Neither body may come from a subsequently changed checkout.
                (root / "stop.sh").write_text("exit 82\n")
                if split and not missing:
                    internal.write_text("exit 83\n")
                result = self._shell('ROOT_DIR="$FIXTURE_ROOT"\nRUN_DIR="$ROOT_DIR/.run"\n'
                    'STAGING_DIR="$ROOT_DIR/absent-stage"\nTARGET_COMMIT="$FIXTURE_TARGET"\n'
                    'stop_installed_runtime_with_target_lifecycle',
                    environment={"FIXTURE_ROOT": str(root), "FIXTURE_TARGET": target})
                self.assertEqual(result.returncode == 0, not missing, result.stderr)
                self.assertEqual((root / "stop-proof").exists(), not missing)
                self.assertEqual(list((root / ".run").iterdir()), [])

    def _committed_runtime_probe(
        self,
        payloads: list[dict[str, object]],
        script: str = "wait_for_committed_runtime \"$TARGET_COMMIT\"",
    ) -> tuple[subprocess.CompletedProcess[str], list[dict[str, object]]]:
        fixture = self.temp / f"health-fixture-{uuid.uuid4()}"
        fixture.mkdir()
        responses = fixture / "responses.json"
        responses.write_text(json.dumps(payloads), encoding="utf-8")
        observations = fixture / "observations.jsonl"
        # Patch only the Python child of the actual shell function. No sockets,
        # runtime imports or wall-clock waits are used; exhaustion fails promptly
        # rather than hiding an unexpected infinite polling loop.
        (fixture / "sitecustomize.py").write_text(
            textwrap.dedent("""\
                import json
                import os
                import time
                import urllib.request
                from pathlib import Path

                responses = json.loads(Path(os.environ['HEALTH_RESPONSES']).read_text())
                observations = Path(os.environ['HEALTH_OBSERVATIONS'])

                class Response:
                    status = 200
                    def __init__(self, payload):
                        self.payload = payload
                    def __enter__(self):
                        return self
                    def __exit__(self, *args):
                        return False
                    def read(self):
                        return json.dumps(self.payload).encode('utf-8')

                class Opener:
                    def open(self, url, timeout):
                        if url != 'http://127.0.0.1:8000/api/health/live':
                            raise SystemExit(89)
                        if not responses:
                            raise SystemExit(88)
                        payload = responses.pop(0)
                        with observations.open('a') as handle:
                            handle.write(json.dumps(payload) + '\\n')
                        return Response(payload)

                urllib.request.build_opener = lambda *args: Opener()
                time.sleep = lambda seconds: None
                """),
            encoding="utf-8",
        )
        result = self._shell(
            f"TARGET_COMMIT={'a' * 40}\n{script}",
            environment={
                "PYTHONPATH": str(fixture),
                "HEALTH_RESPONSES": str(responses),
                "HEALTH_OBSERVATIONS": str(observations),
            },
            timeout=5,
        )
        polls = [json.loads(line) for line in observations.read_text().splitlines()]
        return result, polls

    def _live_payload(self, **changes: object) -> dict[str, object]:
        return {
            "status": "live",
            "buildId": "a" * 12,
            "recoveryCompleted": True,
            "recoverySucceeded": True,
            "workersStarted": False,
            "errorCode": None,
            **changes,
        }

    def _git(self, repository: Path, *arguments: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(repository), *arguments],
            text=True,
            capture_output=True,
            check=True,
        )
        return result.stdout.strip()

    def _commit(self, repository: Path, message: str) -> str:
        self._git(repository, "add", ".")
        self._git(repository, "commit", "-m", message)
        return self._git(repository, "rev-parse", "HEAD")

    def _support(self, repository: Path, commit: str, *, environment: dict[str, str] | None = None) -> str:
        result = self._shell(
            f'ROOT_DIR={shlex_quote(str(repository))}\n'
            f'OLD_COMMIT={shlex_quote(commit)}\n'
            "installed_release_lifecycle_support",
            environment=environment,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def test_support_detection_distinguishes_absent_present_and_partial(self) -> None:
        repository = self.temp / "history"
        repository.mkdir()
        self._git(repository, "init")
        self._git(repository, "config", "user.name", "StreamHome Test")
        self._git(repository, "config", "user.email", "streamhome-test@example.invalid")
        (repository / "marker").write_text("pre-phase1\n", encoding="utf-8")
        absent = self._commit(repository, "pre phase one")

        lifecycle = repository / "server" / "lifecycle"
        lifecycle.mkdir(parents=True)
        (lifecycle / "state.py").write_text("# state\n", encoding="utf-8")
        partial = self._commit(repository, "partial phase one")

        (lifecycle / "cli.py").write_text("# cli\n", encoding="utf-8")
        present = self._commit(repository, "phase one")

        self.assertEqual(self._support(repository, absent), "PRE_PHASE1_CONFIRMED")
        self.assertEqual(self._support(repository, partial), "INDETERMINATE")
        self.assertEqual(self._support(repository, present), "PHASE1_CONFIRMED")

    def test_support_detection_fails_closed_for_missing_object_and_repository(self) -> None:
        repository = self.temp / "repository"
        repository.mkdir()
        self._git(repository, "init")
        self.assertEqual(self._support(repository, "f" * 40), "INDETERMINATE")

        not_a_repository = self.temp / "not-a-repository"
        not_a_repository.mkdir()
        self.assertEqual(self._support(not_a_repository, "a" * 40), "INDETERMINATE")

    def test_support_detection_fails_closed_when_git_cat_file_fails(self) -> None:
        repository = self.temp / "repository"
        repository.mkdir()
        fake_bin = self.temp / "fake-bin"
        fake_bin.mkdir()
        fake_git = fake_bin / "git"
        fake_git.write_text("#!/usr/bin/env bash\nexit 2\n", encoding="utf-8")
        fake_git.chmod(0o755)
        environment = {"PATH": f"{fake_bin}:{os.environ.get('PATH', '')}"}
        self.assertEqual(
            self._support(repository, "a" * 40, environment=environment),
            "INDETERMINATE",
        )

    def _make_lifecycle_installation(self) -> Path:
        root = self.temp / f"installation-{uuid.uuid4()}"
        (root / "server").mkdir(parents=True)
        shutil.copytree(self.repository_root / "server" / "lifecycle", root / "server" / "lifecycle")
        shutil.copy2(self.repository_root / "update.sh", root / "update.sh")
        (root / "scripts/runtime").mkdir(parents=True)
        shutil.copy2(self.repository_root / "scripts/runtime/update.sh", root / "scripts/runtime/update.sh")
        (root / "start.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
        return root

    def _journal(
        self,
        root: Path,
        operation_id: str,
        generation: int,
        phase: str = "reserved",
        detail_phase: str = "queued",
    ) -> Path:
        run_dir = root / ".run"
        run_dir.mkdir(mode=0o700, exist_ok=True)
        path = run_dir / "update-state.json"
        path.write_text(
            json.dumps(
                {
                    "phase": detail_phase,
                    "transaction_id": operation_id,
                    "lifecycle_generation": generation,
                    "lifecycle_phase": phase,
                    "target_commit": "a" * 40,
                    "previous_commit": "b" * 40,
                    "runtime_committed": False,
                    "runtime_committed_at": None,
                }
            ),
            encoding="utf-8",
        )
        path.chmod(0o600)
        return path

    def _lifecycle_variables(self, root: Path, operation_id: str, generation: int, status: Path) -> str:
        return "\n".join(
            (
                f"ROOT_DIR={shlex_quote(str(root))}",
                f"RUN_DIR={shlex_quote(str(root / '.run'))}",
                f"STATUS_FILE={shlex_quote(str(status))}",
                f"TRANSACTION_ID={shlex_quote(operation_id)}",
                f"LIFECYCLE_OPERATION_ID={shlex_quote(operation_id)}",
                f"LIFECYCLE_GENERATION={generation}",
                "LIFECYCLE_PHASE=reserved",
                "LIFECYCLE_ENABLED=true",
                "LIFECYCLE_HANDOFF_REQUESTED=false",
            )
        )

    def test_recovery_transition_updates_shared_authority_and_journal(self) -> None:
        root = self._make_lifecycle_installation()
        claimed = state.claim_operation_with_credential(
            root, operation_type="update", owner_kind="updater"
        )
        operation = claimed.state.active_operation
        journal = self._journal(root, operation.operation_id, claimed.state.generation)
        result = self._shell(
            self._lifecycle_variables(root, operation.operation_id, claimed.state.generation, journal)
            + "\nmark_lifecycle_recovery_required",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            state.inspect_state(root).active_operation.phase,
            state.LifecyclePhase.RECOVERY_REQUIRED,
        )
        self.assertEqual(json.loads(journal.read_text())["lifecycle_phase"], "recovery_required")

    def test_recovery_transition_survives_journal_reconciliation_failure(self) -> None:
        root = self._make_lifecycle_installation()
        claimed = state.claim_operation_with_credential(
            root, operation_type="update", owner_kind="updater"
        )
        operation = claimed.state.active_operation
        invalid_status = root / ".run" / "journal-is-a-directory"
        invalid_status.mkdir()
        result = self._shell(
            self._lifecycle_variables(
                root, operation.operation_id, claimed.state.generation, invalid_status
            )
            + "\nmark_lifecycle_recovery_required",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            state.inspect_state(root).active_operation.phase,
            state.LifecyclePhase.RECOVERY_REQUIRED,
        )

    def test_recovery_required_is_terminal_for_automatic_shell_recovery(self) -> None:
        root = self._make_lifecycle_installation()
        claimed = state.claim_operation_with_credential(
            root, operation_type="update", owner_kind="updater"
        )
        operation = claimed.state.active_operation
        state.transition_operation_with_credential(
            root,
            operation_id=operation.operation_id,
            generation=claimed.state.generation,
            expected_phase="reserved",
            next_phase="recovery_required",
        )
        journal = self._journal(root, operation.operation_id, claimed.state.generation)
        result = self._shell(
            self._lifecycle_variables(root, operation.operation_id, claimed.state.generation, journal)
            + "\ninitialize_lifecycle_authority || exit $?\n"
            + 'printf "phase=%s enabled=%s\\n" "$LIFECYCLE_PHASE" "$LIFECYCLE_ENABLED"',
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(json.loads(journal.read_text())["lifecycle_phase"], "reserved")
        self.assertEqual(
            state.inspect_state(root).active_operation.phase,
            state.LifecyclePhase.RECOVERY_REQUIRED,
        )

    def test_guarded_start_passes_exact_shared_lifecycle_identity(self) -> None:
        root = self._make_lifecycle_installation()
        (root / ".run").mkdir(mode=0o700, exist_ok=True)
        captured = root / "guarded-environment.json"
        (root / "start.sh").write_text(
            "#!/usr/bin/env bash\n"
            "python3 - <<'PY'\n"
            "import json,os\n"
            f"open({str(captured)!r}, 'w', encoding='utf-8').write(json.dumps({{\n"
            " 'mode': os.environ.get('STREAMHOME_LIFECYCLE_STARTUP_MODE'),\n"
            " 'operation': os.environ.get('STREAMHOME_LIFECYCLE_OPERATION_ID'),\n"
            " 'generation': os.environ.get('STREAMHOME_LIFECYCLE_GENERATION'),\n"
            " 'first_adoption': os.environ.get('STREAMHOME_LIFECYCLE_FIRST_ADOPTION'),\n"
            " 'transaction': os.environ.get('STREAMHOME_UPDATE_TRANSACTION'),\n"
            "}))\n"
            "PY\n",
            encoding="utf-8",
        )
        (root / "start.sh").chmod(0o755)
        claimed = state.claim_operation_with_credential(
            root,
            operation_type="update",
            owner_kind="updater",
        )
        operation = claimed.state.active_operation
        transaction = operation.operation_id
        journal = self._journal(
            root,
            transaction,
            claimed.state.generation,
            detail_phase="starting",
        )
        result = self._shell(
            "\n".join(
                (
                    f"ROOT_DIR={shlex_quote(str(root))}",
                    f"RUN_DIR={shlex_quote(str(root / '.run'))}",
                    f"TRANSACTION_ID={shlex_quote(transaction)}",
                    f"LIFECYCLE_OPERATION_ID={shlex_quote(transaction)}",
                    f"LIFECYCLE_GENERATION={claimed.state.generation}",
                    "LIFECYCLE_ENABLED=true",
                    f"STATUS_FILE={shlex_quote(str(journal))}",
                    "COMMIT_TOKEN=guarded-token",
                    "COMMIT_FILE=",
                    "start_installed_update_runtime",
                )
            )
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(captured.read_text(encoding="utf-8"))
        self.assertEqual(payload["mode"], "update_verify")
        self.assertEqual(payload["operation"], transaction)
        self.assertEqual(payload["generation"], str(claimed.state.generation))
        self.assertEqual(payload["first_adoption"], "false")
        self.assertEqual(payload["transaction"], transaction)

    def test_first_adoption_guarded_start_does_not_invent_shared_identity(self) -> None:
        root = self._make_lifecycle_installation()
        (root / ".run").mkdir(mode=0o700, exist_ok=True)
        captured = root / "first-adoption-environment.json"
        (root / "start.sh").write_text(
            "#!/usr/bin/env bash\n"
            "python3 - <<'PY'\n"
            "import json,os\n"
            f"open({str(captured)!r}, 'w', encoding='utf-8').write(json.dumps({{\n"
            " 'operation': os.environ.get('STREAMHOME_LIFECYCLE_OPERATION_ID'),\n"
            " 'generation': os.environ.get('STREAMHOME_LIFECYCLE_GENERATION'),\n"
            " 'first_adoption': os.environ.get('STREAMHOME_LIFECYCLE_FIRST_ADOPTION'),\n"
            "}))\n"
            "PY\n",
            encoding="utf-8",
        )
        (root / "start.sh").chmod(0o755)
        transaction = str(uuid.uuid4())
        result = self._shell(
            "\n".join(
                (
                    f"ROOT_DIR={shlex_quote(str(root))}",
                    f"RUN_DIR={shlex_quote(str(root / '.run'))}",
                    f"TRANSACTION_ID={shlex_quote(transaction)}",
                    "LIFECYCLE_OPERATION_ID=",
                    "LIFECYCLE_GENERATION=",
                    "LIFECYCLE_ENABLED=false",
                    "COMMIT_TOKEN=guarded-token",
                    "COMMIT_FILE=",
                    "start_installed_update_runtime",
                )
            )
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(captured.read_text(encoding="utf-8"))
        self.assertEqual(payload["first_adoption"], "true")
        self.assertEqual(payload["operation"], "")
        self.assertEqual(payload["generation"], "")

    def test_recovery_required_cannot_be_reacquired_by_guarded_start(self) -> None:
        root = self._make_lifecycle_installation()
        (root / "start.sh").write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        (root / "start.sh").chmod(0o755)
        claimed = state.claim_operation_with_credential(
            root,
            operation_type="update",
            owner_kind="updater",
        )
        operation = claimed.state.active_operation
        state.transition_operation_with_credential(
            root,
            operation_id=operation.operation_id,
            generation=claimed.state.generation,
            expected_phase="reserved",
            next_phase="recovery_required",
        )
        journal = self._journal(
            root,
            operation.operation_id,
            claimed.state.generation,
            "recovery_required",
        )
        result = self._shell(
            self._lifecycle_variables(
                root,
                operation.operation_id,
                claimed.state.generation,
                journal,
            )
            + "\nLIFECYCLE_PHASE=recovery_required\n"
            + "COMMIT_TOKEN=guarded-token\n"
            + "COMMIT_FILE=\n"
            + "start_installed_update_runtime",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(
            state.inspect_state(root).active_operation.phase,
            state.LifecyclePhase.RECOVERY_REQUIRED,
        )
        self.assertEqual(json.loads(journal.read_text())["lifecycle_phase"], "recovery_required")

    def test_successful_protected_commit_establishes_no_rollback_boundary(self) -> None:
        root = self.temp / "commit-boundary"
        run_dir = root / ".run"
        run_dir.mkdir(parents=True)
        commit_file = run_dir / "update-commit.token"
        commit_file.write_text("token", encoding="utf-8")
        result = self._shell(
            "\n".join(
                (
                    f"ROOT_DIR={shlex_quote(str(root))}",
                    f"RUN_DIR={shlex_quote(str(run_dir))}",
                    f"COMMIT_FILE={shlex_quote(str(commit_file))}",
                    "COMMIT_TOKEN=token",
                    f"TRANSACTION_ID={shlex_quote(str(uuid.uuid4()))}",
                    f"TARGET_COMMIT={'a' * 40}",
                    "CUTOVER_STARTED=true",
                    "ACTIVATION_STARTED=true",
                    "curl() { printf 200; }",
                    "commit_updated_runtime",
                    'printf "%s %s %s %s %s\\n" "$NO_ROLLBACK_BOUNDARY" "$CUTOVER_STARTED" "$ACTIVATION_STARTED" "$LIFECYCLE_PRESERVE" "$PRESERVE_RECOVERY_ARTIFACTS"',
                )
            )
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "true false false true true")
        self.assertFalse(commit_file.exists())

    def test_cleanup_never_rolls_back_after_no_rollback_boundary(self) -> None:
        marker = self.temp / "rollback-called"
        result = self._shell(
            "\n".join(
                (
                    "CUTOVER_STARTED=true",
                    "ACTIVATION_STARTED=true",
                    "RUNTIME_HEALTHY=false",
                    "RECOVERY_IN_PROGRESS=false",
                    "NO_ROLLBACK_BOUNDARY=true",
                    "PRESERVE_MAINTENANCE=true",
                    "LIFECYCLE_ENABLED=false",
                    f"rollback_release() {{ touch {shlex_quote(str(marker))}; }}",
                    "release_lock() { :; }",
                    "cleanup",
                )
            )
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(marker.exists())

    def test_indeterminate_commit_response_disables_automatic_rollback(self) -> None:
        root = self.temp / "indeterminate-commit"
        run_dir = root / ".run"
        run_dir.mkdir(parents=True)
        result = self._shell(
            "\n".join(
                (
                    f"ROOT_DIR={shlex_quote(str(root))}",
                    f"RUN_DIR={shlex_quote(str(run_dir))}",
                    "COMMIT_FILE=",
                    "COMMIT_TOKEN=token",
                    f"TRANSACTION_ID={shlex_quote(str(uuid.uuid4()))}",
                    f"TARGET_COMMIT={'b' * 40}",
                    "CUTOVER_STARTED=true",
                    "ACTIVATION_STARTED=true",
                    "curl() { printf 503; }",
                    "commit_updated_runtime",
                    "result=$?",
                    'printf "%s %s %s %s\\n" "$result" "$NO_ROLLBACK_BOUNDARY" "$CUTOVER_STARTED" "$LIFECYCLE_PRESERVE"',
                )
            )
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "2 true false true")

    def test_committed_runtime_wait_stops_on_explicit_completed_startup_failure(self) -> None:
        for code in (
            "update_lifecycle_identity_missing",
            "update_completion_fence_failed",
            "update_completion_drain_timeout",
            "update_lifecycle_unresolved",
            "worker_start_failed",
        ):
            with self.subTest(code=code):
                failure = self._live_payload(errorCode=code)
                result, polls = self._committed_runtime_probe([failure])
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(polls, [failure])

    def test_committed_runtime_wait_still_stops_on_mandatory_recovery_failure(self) -> None:
        failure = self._live_payload(
            recoverySucceeded=False, errorCode="catalog_recovery_failed"
        )
        result, polls = self._committed_runtime_probe([failure])
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(polls, [failure])

    def test_committed_runtime_wait_allows_long_recovery_and_worker_startup_gap(self) -> None:
        recovering = self._live_payload(
            recoveryCompleted=False,
            recoverySucceeded=False,
            errorCode="update_lifecycle_pending",
        )
        starting_workers = self._live_payload()
        ready = self._live_payload(workersStarted=True)
        # Exceed the separate 30-poll unavailability limit with valid pending
        # health. Neither mandatory recovery nor the worker-start gap times out.
        payloads = [recovering] * 35 + [starting_workers] * 35 + [ready]
        result, polls = self._committed_runtime_probe(payloads)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(polls, payloads)

    def test_committed_runtime_wait_does_not_classify_foreign_build_error(self) -> None:
        payloads = [
            self._live_payload(buildId="b" * 12, errorCode="worker_start_failed"),
            self._live_payload(workersStarted=True),
        ]
        result, polls = self._committed_runtime_probe(payloads)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(polls, payloads)

    def test_committed_runtime_wait_preserves_worker_ready_lifecycle_pending_handoff(self) -> None:
        ready = self._live_payload(
            workersStarted=True, errorCode="update_lifecycle_pending"
        )
        result, polls = self._committed_runtime_probe([ready])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(polls, [ready])

    def test_explicit_post_commit_startup_failure_preserves_candidate_without_rollback(self) -> None:
        rollback = self.temp / "post-commit-rollback"
        journal = self.temp / "post-commit-journal"
        result, polls = self._committed_runtime_probe(
            [self._live_payload(errorCode="update_lifecycle_identity_missing")],
            "\n".join(
                (
                    "COMMIT_TOKEN=fixture-token",
                    "TRANSACTION_ID=fixture-transaction",
                    "CUTOVER_STARTED=true",
                    "ACTIVATION_STARTED=true",
                    "RUNTIME_HEALTHY=false",
                    "PRESERVE_MAINTENANCE=true",
                    "installed_release_has_guarded_bootstrap() { return 0; }",
                    "curl() { printf 200; }",
                    f'write_state() {{ printf "%s\\n" "$@" >> {shlex_quote(str(journal))}; }}',
                    f"rollback_release() {{ touch {shlex_quote(str(rollback))}; }}",
                    "release_lock() { :; }",
                    'promote_recovered_guarded_runtime "$TARGET_COMMIT"',
                    "result=$?",
                    'printf "%s %s %s %s %s %s\\n" "$result" "$NO_ROLLBACK_BOUNDARY" "$CUTOVER_STARTED" "$ACTIVATION_STARTED" "$LIFECYCLE_PRESERVE" "$PRESERVE_RECOVERY_ARTIFACTS"',
                    "cleanup",
                )
            ),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "1 true false false true true")
        self.assertEqual(len(polls), 1)
        self.assertIn("post_commit_recovery_failed", journal.read_text())
        self.assertFalse(rollback.exists())

    def test_post_commit_failure_retains_recovery_without_rollback(self) -> None:
        source = (self.repository_root / "scripts/runtime/update.sh").read_text(encoding="utf-8")
        failure = source[
            source.index("record_post_commit_recovery_failure()"):
            source.index("installed_release_has_guarded_bootstrap()")
        ]
        self.assertIn('write_state \\\n        "recovering"', failure)
        self.assertIn('"post_commit_recovery_failed"', failure)
        self.assertIn("NO_ROLLBACK_BOUNDARY=true", failure)
        self.assertNotIn("rollback_release", failure)

    def test_phase_five_cutover_and_recovery_use_canonical_boundaries(self) -> None:
        source = (self.repository_root / "scripts/runtime/update.sh").read_text(encoding="utf-8")
        validate = source[
            source.index("validate_lifecycle_authority()"):
            source.index("advance_lifecycle_phase()")
        ]
        self.assertIn("validate-controller", validate)
        self.assertIn("request_process_local_cutover", source)
        wait = source[
            source.index("wait_for_committed_runtime()"):
            source.index("record_post_commit_recovery_failure()")
        ]
        self.assertIn('"http://127.0.0.1:8000/api/health/live"', wait)
        self.assertIn('live.get("recoverySucceeded") is True', wait)
        self.assertNotIn('"http://127.0.0.1:8000/api/health"', wait)


def shlex_quote(value: str) -> str:
    import shlex

    return shlex.quote(value)


if __name__ == "__main__":
    unittest.main()
