"""Managed platform and runtime pins, independent of application dependencies."""
from __future__ import annotations

from pathlib import Path
import platform
import sys


NODE_VERSION = "24.22.0"
NPM_VERSION = "11.19.1"
NODE_SHA256 = "cc2f601cee82d9a5a233302fe307abc600727035fd7d2c3a3571755989e95008"
RCLONE_VERSION = "1.74.4"
RCLONE_SHA256 = "fe435e0c36228e7c2f116a8701f01127bb1f694005fc11d1f27186c8bca4115d"
PREPARATION_STAGES = (
    "Downloading the selected StreamHome source",
    "Downloading and verifying the application Node.js runtime",
    "Creating the permanent Python environment",
    "Installing web dependencies with the application npm",
    "Building the paired web bundle",
    "Installing the application Rclone runtime",
    "Verifying the complete runtime generation",
)


class UnsupportedPlatform(RuntimeError):
    code = "unsupported_managed_platform"


def require_supported_platform(*, require_systemd: bool = True) -> None:
    release = platform.freedesktop_os_release()
    if (sys.platform != "linux" or release.get("ID") != "ubuntu" or release.get("VERSION_ID") != "24.04"
            or platform.machine() != "x86_64" or platform.libc_ver()[0] != "glibc"
            or sys.version_info[:3] != (3, 12, 3)):
        raise UnsupportedPlatform("Managed installation requires Ubuntu 24.04 x86_64, glibc and Python 3.12.3.")
    if require_systemd and (Path("/proc/1/comm").read_text().strip() != "systemd" or not Path("/sys/fs/cgroup/cgroup.controllers").is_file()):
        raise UnsupportedPlatform("Managed installation requires systemd as PID 1 and a unified cgroup v2 hierarchy.")
