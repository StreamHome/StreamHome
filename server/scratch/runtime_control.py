from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


RUNTIME_ROOT_KEY = "STREAMHOME_INSTANCE_ROOT"
RUNTIME_SERVICE_KEY = "STREAMHOME_SERVICE"
RUNTIME_TOKEN_KEY = "STREAMHOME_INSTANCE_TOKEN"
SERVICE_NAMES = {"backend", "web", "maintenance"}
TERM_TIMEOUT_SECONDS = 4.0
KILL_TIMEOUT_SECONDS = 2.0
POLL_INTERVAL_SECONDS = 0.1


@dataclass(frozen=True)
class ProcessInfo:
    pid: int
    ppid: int
    pgid: int
    sid: int
    start_ticks: str
    cwd: str
    command: str
    environment: dict[str, str]


def canonical_root(raw_root: str | Path) -> Path:
    root = Path(raw_root).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ValueError(f"StreamHome root is not a directory: {root}")
    return root


def atomic_write(path: Path, content: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(content, encoding="utf-8")
        try:
            temporary.chmod(mode)
        except OSError:
            pass
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def read_proc_stat(pid: int) -> tuple[int, int, int, str]:
    raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    closing = raw.rfind(")")
    if closing < 0:
        raise ValueError("invalid proc stat")
    fields = raw[closing + 2 :].split()
    return int(fields[1]), int(fields[2]), int(fields[3]), fields[19]


def read_proc_environment(pid: int) -> dict[str, str]:
    raw = Path(f"/proc/{pid}/environ").read_bytes()
    environment: dict[str, str] = {}
    for item in raw.split(b"\0"):
        if not item or b"=" not in item:
            continue
        key, value = item.split(b"=", 1)
        environment[key.decode("utf-8", "replace")] = value.decode("utf-8", "replace")
    return environment


def read_proc_command(pid: int) -> str:
    raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    command = " ".join(
        part.decode("utf-8", "replace")
        for part in raw.split(b"\0")
        if part
    )
    if command:
        return command
    return Path(f"/proc/{pid}/comm").read_text(encoding="utf-8").strip()


def inspect_process(pid: int) -> ProcessInfo | None:
    try:
        ppid, pgid, sid, start_ticks = read_proc_stat(pid)
        try:
            cwd = str(Path(f"/proc/{pid}/cwd").resolve(strict=True))
        except (FileNotFoundError, OSError):
            cwd = ""
        try:
            environment = read_proc_environment(pid)
        except (FileNotFoundError, PermissionError, ProcessLookupError, OSError):
            environment = {}
        try:
            command = read_proc_command(pid)
        except (FileNotFoundError, PermissionError, ProcessLookupError, OSError):
            command = ""
        return ProcessInfo(
            pid=pid,
            ppid=ppid,
            pgid=pgid,
            sid=sid,
            start_ticks=start_ticks,
            cwd=cwd,
            command=command,
            environment=environment,
        )
    except (FileNotFoundError, PermissionError, ProcessLookupError, OSError, ValueError, IndexError):
        return None


def process_snapshot() -> dict[int, ProcessInfo]:
    processes: dict[int, ProcessInfo] = {}
    proc_root = Path("/proc")
    if not proc_root.is_dir():
        return processes
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        info = inspect_process(int(entry.name))
        if info is not None:
            processes[info.pid] = info
    return processes


def path_is_within(candidate: str, root: Path) -> bool:
    if not candidate:
        return False
    try:
        Path(candidate).resolve(strict=False).relative_to(root)
        return True
    except (OSError, ValueError):
        return False


def marked_service(info: ProcessInfo, root: Path) -> str:
    if info.environment.get(RUNTIME_ROOT_KEY) != str(root):
        return ""
    service = info.environment.get(RUNTIME_SERVICE_KEY, "")
    return service if service in SERVICE_NAMES else ""


def legacy_service(info: ProcessInfo, root: Path) -> str:
    command = info.command
    if "server/scratch/maintenance_server.py" in command and path_is_within(info.cwd, root):
        return "maintenance"

    server_root = root / "server"
    if path_is_within(info.cwd, server_root):
        if ("uvicorn" in command and "main:app" in command) or re.search(r"(^|\s)main\.py(\s|$)", command):
            return "backend"

    web_root = root / "web"
    if path_is_within(info.cwd, web_root):
        if (
            ("npm" in command and "run" in command and "server" in command)
            or ("tsx" in command and "server.ts" in command)
            or re.search(r"(^|\s)server\.ts(\s|$)", command)
        ):
            return "web"
    return ""


def owned_service(info: ProcessInfo, root: Path) -> str:
    return marked_service(info, root) or legacy_service(info, root)


def read_runtime_record(root: Path, service: str) -> dict[str, object]:
    path = root / ".run" / f"{service}.runtime.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def record_matches(info: ProcessInfo, root: Path, service: str, record: dict[str, object]) -> bool:
    try:
        recorded_pid = int(record.get("pid", 0))
    except (TypeError, ValueError):
        return False
    if recorded_pid != info.pid:
        return False
    expected_start = str(record.get("start_ticks") or "")
    if expected_start and expected_start != info.start_ticks:
        return False
    expected_root = str(record.get("root") or "")
    if expected_root and expected_root != str(root):
        return False
    expected_service = str(record.get("service") or "")
    if expected_service and expected_service != service:
        return False
    expected_token = str(record.get("token") or "")
    if expected_token:
        return (
            info.environment.get(RUNTIME_ROOT_KEY) == str(root)
            and info.environment.get(RUNTIME_SERVICE_KEY) == service
            and info.environment.get(RUNTIME_TOKEN_KEY) == expected_token
        )
    return owned_service(info, root) == service


def record_service(root: Path, service: str, pid: int, token: str) -> int:
    if service not in {"backend", "web"}:
        raise ValueError(f"Unsupported recorded service: {service}")
    if not token:
        raise ValueError("Runtime token is empty")
    info = inspect_process(pid)
    if info is None:
        raise RuntimeError(f"{service} process {pid} exited before it could be recorded")
    if (
        info.environment.get(RUNTIME_ROOT_KEY) != str(root)
        or info.environment.get(RUNTIME_SERVICE_KEY) != service
        or info.environment.get(RUNTIME_TOKEN_KEY) != token
    ):
        raise RuntimeError(f"{service} process {pid} does not carry the expected StreamHome identity")
    payload = {
        "version": 1,
        "root": str(root),
        "service": service,
        "token": token,
        "pid": info.pid,
        "ppid": info.ppid,
        "pgid": info.pgid,
        "sid": info.sid,
        "start_ticks": info.start_ticks,
        "recorded_at": time.time(),
    }
    run_dir = root / ".run"
    atomic_write(run_dir / f"{service}.runtime.json", json.dumps(payload, sort_keys=True) + "\n")
    atomic_write(run_dir / f"{service}.pid", f"{info.pid}\n")
    atomic_write(run_dir / f"{service}.start", f"{info.start_ticks}\n")
    atomic_write(run_dir / f"{service}.pgid", f"{info.pgid}\n")
    atomic_write(run_dir / f"{service}.token", f"{token}\n")
    return 0


def service_matches(root: Path, service: str, pid: int) -> bool:
    info = inspect_process(pid)
    if info is None:
        return False
    record = read_runtime_record(root, service)
    return bool(record) and record_matches(info, root, service, record)


def ancestor_pids(snapshot: dict[int, ProcessInfo], pid: int) -> set[int]:
    ancestors: set[int] = {pid}
    current = snapshot.get(pid)
    while current is not None and current.ppid > 0 and current.ppid not in ancestors:
        ancestors.add(current.ppid)
        current = snapshot.get(current.ppid)
    return ancestors


def discover_owned(snapshot: dict[int, ProcessInfo], root: Path) -> dict[int, str]:
    protected = ancestor_pids(snapshot, os.getpid())
    owned = {
        pid: service
        for pid, info in snapshot.items()
        if pid not in protected and (service := owned_service(info, root))
    }
    changed = True
    while changed:
        changed = False
        for pid, info in snapshot.items():
            if pid in protected or pid in owned or info.ppid not in owned:
                continue
            owned[pid] = owned[info.ppid]
            changed = True
    return owned


def safe_owned_groups(
    snapshot: dict[int, ProcessInfo],
    owned: dict[int, str],
) -> set[int]:
    current_group = os.getpgrp()
    groups: set[int] = set()
    members_by_group: dict[int, set[int]] = {}
    for info in snapshot.values():
        members_by_group.setdefault(info.pgid, set()).add(info.pid)
    for pid in owned:
        info = snapshot.get(pid)
        if info is None or info.pgid <= 1 or info.pgid == current_group:
            continue
        visible_members = members_by_group.get(info.pgid, set())
        if visible_members and visible_members.issubset(owned):
            groups.add(info.pgid)
    return groups


def signal_owned_processes(root: Path, selected_signal: signal.Signals) -> tuple[dict[int, str], set[int]]:
    snapshot = process_snapshot()
    owned = discover_owned(snapshot, root)
    groups = safe_owned_groups(snapshot, owned)
    for pgid in sorted(groups):
        try:
            os.killpg(pgid, selected_signal)
        except (ProcessLookupError, PermissionError, OSError):
            pass
    for pid in sorted(owned, reverse=True):
        info = snapshot.get(pid)
        if info is None or info.pgid in groups:
            continue
        try:
            os.kill(pid, selected_signal)
        except (ProcessLookupError, PermissionError, OSError):
            pass
    return owned, groups


def wait_for_owned_exit(root: Path, timeout: float) -> dict[int, str]:
    deadline = time.monotonic() + timeout
    remaining: dict[int, str] = {}
    while True:
        remaining = discover_owned(process_snapshot(), root)
        if not remaining or time.monotonic() >= deadline:
            return remaining
        time.sleep(POLL_INTERVAL_SECONDS)


def terminate_owned_processes(root: Path) -> tuple[set[str], dict[int, str]]:
    stopped_services: set[str] = set()
    initial, _ = signal_owned_processes(root, signal.SIGTERM)
    stopped_services.update(initial.values())
    remaining = wait_for_owned_exit(root, TERM_TIMEOUT_SECONDS)
    if remaining:
        final, _ = signal_owned_processes(root, signal.SIGKILL)
        stopped_services.update(final.values())
        remaining = wait_for_owned_exit(root, KILL_TIMEOUT_SECONDS)
    return stopped_services, remaining


def port_is_available(port: int) -> bool:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind(("0.0.0.0", port))
        probe.listen(1)
        return True
    except OSError:
        return False
    finally:
        probe.close()


def listening_socket_inodes(port: int) -> set[str]:
    inodes: set[str] = set()
    hexadecimal_port = f"{port:04X}"
    for table in (Path("/proc/net/tcp"), Path("/proc/net/tcp6")):
        try:
            lines = table.read_text(encoding="utf-8").splitlines()[1:]
        except (FileNotFoundError, PermissionError, OSError):
            continue
        for line in lines:
            fields = line.split()
            if len(fields) < 10 or fields[3] != "0A":
                continue
            local_address = fields[1]
            if ":" not in local_address or local_address.rsplit(":", 1)[1].upper() != hexadecimal_port:
                continue
            inodes.add(fields[9])
    return inodes


def listener_pids_from_proc(port: int) -> set[int]:
    inodes = listening_socket_inodes(port)
    if not inodes:
        return set()
    socket_targets = {f"socket:[{inode}]" for inode in inodes}
    pids: set[int] = set()
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            descriptors = (entry / "fd").iterdir()
            if any(os.readlink(descriptor) in socket_targets for descriptor in descriptors):
                pids.add(int(entry.name))
        except (FileNotFoundError, PermissionError, ProcessLookupError, OSError):
            continue
    return pids


def listener_pids_from_tools(port: int) -> set[int]:
    if shutil.which("lsof"):
        try:
            result = subprocess.run(
                ["lsof", "-nP", "-t", f"-iTCP:{port}", "-sTCP:LISTEN"],
                text=True,
                capture_output=True,
                timeout=3,
                check=False,
            )
            pids = {
                int(line)
                for line in result.stdout.splitlines()
                if line.strip().isdigit()
            }
            if pids:
                return {pid for pid in pids if inspect_process(pid) is not None}
        except (OSError, subprocess.SubprocessError):
            pass
    if shutil.which("ss"):
        try:
            result = subprocess.run(
                ["ss", "-H", "-ltnp", f"sport = :{port}"],
                text=True,
                capture_output=True,
                timeout=3,
                check=False,
            )
            pids = {int(candidate) for candidate in re.findall(r"pid=([1-9][0-9]*)", result.stdout)}
            if pids:
                return {pid for pid in pids if inspect_process(pid) is not None}
        except (OSError, subprocess.SubprocessError):
            pass
    if shutil.which("fuser"):
        try:
            result = subprocess.run(
                ["fuser", f"{port}/tcp"],
                text=True,
                capture_output=True,
                timeout=3,
                check=False,
            )
            pids = {
                int(candidate)
                for candidate in result.stdout.split()
                if candidate.isdigit()
            }
            return {pid for pid in pids if inspect_process(pid) is not None}
        except (OSError, subprocess.SubprocessError):
            pass
    return set()


def listener_pids(port: int) -> set[int]:
    return listener_pids_from_proc(port) | listener_pids_from_tools(port)


def process_diagnostic(info: ProcessInfo | None, pid: int) -> str:
    if info is None:
        return f"PID {pid} (details unavailable)"
    command = info.command or "unknown command"
    if len(command) > 240:
        command = command[:237] + "..."
    cwd = info.cwd or "unknown cwd"
    return (
        f"PID {pid}, PPID {info.ppid}, PGID {info.pgid}, SID {info.sid}, "
        f"cwd={cwd}, command={command}"
    )


def cleanup_service_records(root: Path, services: Iterable[str]) -> None:
    run_dir = root / ".run"
    for service in services:
        for suffix in ("pid", "start", "pgid", "token", "runtime.json"):
            try:
                (run_dir / f"{service}.{suffix}").unlink()
            except FileNotFoundError:
                pass
    try:
        (run_dir / "runtime.token").unlink()
    except FileNotFoundError:
        pass


def stop_runtime(root: Path, ports: list[int], quiet: bool, startup: bool) -> int:
    stopped_services, remaining = terminate_owned_processes(root)

    for _ in range(2):
        occupied = [port for port in ports if not port_is_available(port)]
        if not occupied:
            break
        owned_listener_found = False
        snapshot = process_snapshot()
        for port in occupied:
            for pid in listener_pids(port):
                info = snapshot.get(pid) or inspect_process(pid)
                service = owned_service(info, root) if info is not None else ""
                if service:
                    stopped_services.add(service)
                    owned_listener_found = True
        if not owned_listener_found:
            break
        _, remaining = terminate_owned_processes(root)

    remaining = discover_owned(process_snapshot(), root)
    failed = False
    if remaining:
        failed = True
        for pid, service in sorted(remaining.items()):
            print(
                f"[StreamHome] {service} process survived TERM and KILL: "
                f"{process_diagnostic(inspect_process(pid), pid)}.",
                file=sys.stderr,
            )

    occupied_ports = [port for port in ports if not port_is_available(port)]
    for port in occupied_ports:
        failed = True
        pids = listener_pids(port)
        if not pids:
            print(
                f"[StreamHome] Port {port} is still listening, but its owner could not be inspected. "
                "Shutdown cannot be reported as successful.",
                file=sys.stderr,
            )
            continue
        snapshot = process_snapshot()
        for pid in sorted(pids):
            info = snapshot.get(pid) or inspect_process(pid)
            service = owned_service(info, root) if info is not None else ""
            ownership = (
                f"surviving StreamHome {service} process"
                if service
                else "unrelated process"
            )
            print(
                f"[StreamHome] Port {port} is still owned by {ownership}: "
                f"{process_diagnostic(info, pid)}. It was not stopped.",
                file=sys.stderr,
            )

    if not failed:
        cleanup_service_records(root, SERVICE_NAMES)
        if not quiet:
            for service in ("maintenance", "web", "backend"):
                if service in stopped_services:
                    print(f"[StreamHome] Stopped {service}.")
    elif startup and not quiet:
        print("[StreamHome] Startup recovery could not release every required port.", file=sys.stderr)
    return 1 if failed else 0


def parse_ports(values: list[str]) -> list[int]:
    ports: list[int] = []
    for raw in values:
        try:
            port = int(raw, 10)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(f"Invalid port: {raw}") from exc
        if not 1 <= port <= 65535:
            raise argparse.ArgumentTypeError(f"Invalid port: {raw}")
        if port not in ports:
            ports.append(port)
    return ports


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="StreamHome Linux runtime ownership controller")
    subparsers = parser.add_subparsers(dest="command", required=True)

    record = subparsers.add_parser("record")
    record.add_argument("--root", required=True)
    record.add_argument("--service", required=True, choices=("backend", "web"))
    record.add_argument("--pid", required=True, type=int)
    record.add_argument("--token", required=True)

    matches = subparsers.add_parser("matches")
    matches.add_argument("--root", required=True)
    matches.add_argument("--service", required=True, choices=("backend", "web"))
    matches.add_argument("--pid", required=True, type=int)

    stop = subparsers.add_parser("stop")
    stop.add_argument("--root", required=True)
    stop.add_argument("--port", action="append", default=[])
    stop.add_argument("--quiet", action="store_true")
    stop.add_argument("--startup", action="store_true")
    return parser


def main() -> int:
    if os.name != "posix" or not Path("/proc").is_dir():
        print("[StreamHome] Linux /proc process inspection is required.", file=sys.stderr)
        return 2
    parser = build_parser()
    arguments = parser.parse_args()
    try:
        root = canonical_root(arguments.root)
        if arguments.command == "record":
            return record_service(root, arguments.service, arguments.pid, arguments.token)
        if arguments.command == "matches":
            return 0 if service_matches(root, arguments.service, arguments.pid) else 1
        if arguments.command == "stop":
            ports = parse_ports(arguments.port)
            if not ports:
                raise ValueError("At least one --port is required")
            return stop_runtime(root, ports, arguments.quiet, arguments.startup)
    except (OSError, RuntimeError, ValueError, argparse.ArgumentTypeError) as exc:
        print(f"[StreamHome] Runtime control failed: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
