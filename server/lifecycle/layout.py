"""Shared read-only path selection for runtime and offline lifecycle consumers.

Only the fixed managed application root selects the protected descriptor.
Arbitrary environment variables and a descriptor for another checkout cannot
redirect a legacy/development database. Resolving paths never initializes them.
"""
from __future__ import annotations

from dataclasses import dataclass
import argparse
from pathlib import Path

from . import installation


class LayoutError(ValueError):
    code = "invalid_installation_layout"


@dataclass(frozen=True)
class RuntimeLayout:
    application: Path
    configuration: Path
    state: Path
    durable_runtime: Path
    runtime: Path
    cache: Path
    descriptor: installation.DescriptorSnapshot | None

    @property
    def managed(self) -> bool:
        return self.descriptor is not None

    @property
    def database(self) -> Path:
        return self.state / "database.db"

    @property
    def backup(self) -> Path:
        return self.state / "backup"

    @property
    def lifecycle(self) -> Path:
        return self.durable_runtime / "lifecycle"

    @property
    def domains(self) -> Path:
        return self.configuration.parent / "domains" if self.managed else self.configuration


def layout_for_root(root: Path) -> RuntimeLayout:
    if not root.is_absolute() or ".." in root.parts:
        raise LayoutError("An absolute canonical application root is required.")
    try:
        if root.resolve(strict=True) != root or not root.is_dir():
            raise LayoutError("The application root must be a real canonical directory.")
    except OSError as exc:
        raise LayoutError("The application root is unavailable.") from exc
    if root != installation.STANDARD_PATHS.application:
        return RuntimeLayout(root, root / "server", root / "server", root / ".run", root / ".run", root / "bin", None)
    try:
        snapshot = installation.inspect_installation()
    except installation.InstallationEvidenceError as exc:
        raise LayoutError("The protected managed installation descriptor is invalid.") from exc
    if snapshot is None or snapshot.descriptor.paths.application != root:
        raise LayoutError("The managed application root has no matching installation descriptor.")
    paths = snapshot.descriptor.paths
    return RuntimeLayout(
        root, paths.configuration / "application", paths.state, paths.state,
        paths.runtime, paths.cache, snapshot,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect shared StreamHome runtime paths without initializing them.")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--field", choices=("database", "backup", "configuration", "state", "durable_runtime", "runtime", "cache"), required=True)
    arguments = parser.parse_args()
    try:
        selected = layout_for_root(arguments.root)
    except LayoutError as exc:
        parser.exit(2, f"StreamHome layout: {exc}\n")
    print(getattr(selected, arguments.field))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
