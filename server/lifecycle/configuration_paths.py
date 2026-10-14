"""Read-only path resolution shared by configuration and offline controllers."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping


class ConfigurationPathError(ValueError):
    pass


def _file_value(path: Path, name: str) -> str | None:
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ConfigurationPathError("configured path evidence cannot be inspected") from exc
    values: list[str] = []
    for raw in lines:
        line = raw.strip().removeprefix("export ")
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() != name:
            continue
        value = value.strip()
        if value[:1] in {"'", '"'}:
            quote = value[0]
            end = value.find(quote, 1)
            if end < 0 or (value[end + 1:].strip() and not value[end + 1:].strip().startswith("#")):
                raise ConfigurationPathError("configured path syntax is ambiguous")
            value = value[1:end]
        else:
            for index, char in enumerate(value):
                if char == "#" and index > 0 and value[index - 1].isspace():
                    value = value[:index].rstrip()
                    break
        if "$" in value or "\\" in value or any(ord(char) < 32 for char in value):
            raise ConfigurationPathError("offline configured path needs resolved environment evidence")
        values.append(value)
    if len(values) > 1:
        raise ConfigurationPathError("configured path has duplicate declarations")
    return values[0] if values else None


def resolve_temp_directory(root: Path, *, environment: Mapping[str, str] | None = None) -> Path:
    """Environment precedence matches config's root/server dotenv load order."""
    selected_environment = os.environ if environment is None else environment
    value = selected_environment.get("TEMP_DIR")
    if value is None:
        for path in (root / ".env", root / "server" / ".env"):
            value = _file_value(path, "TEMP_DIR")
            if value is not None:
                break
    if value is None:
        return (root / "server" / "temp").resolve()
    if not isinstance(value, str) or any(ord(char) < 32 for char in value):
        raise ConfigurationPathError("configured temporary path is invalid")
    path = Path(value or ".")
    if not path.is_absolute():
        path = root / "server" / path
    return path.resolve()
