import os
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from services.backup import resolve_backup_file, validate_backup_database


def run() -> None:
    for unsafe in (
        "../database.db",
        "..\\database.db",
        "%2e%2e%5cdatabase.db",
        "backup_20260722_120000.db/other",
        "arbitrary.db",
    ):
        assert resolve_backup_file(unsafe) is None

    with tempfile.TemporaryDirectory() as directory:
        invalid = Path(directory) / "invalid.db"
        invalid.write_text("not sqlite", encoding="utf-8")
        try:
            validate_backup_database(invalid)
        except ValueError:
            pass
        else:
            raise AssertionError("A non-SQLite restore candidate was accepted")

        incomplete = Path(directory) / "incomplete.db"
        connection = sqlite3.connect(incomplete)
        connection.execute("CREATE TABLE movie (id TEXT PRIMARY KEY)")
        connection.commit()
        connection.close()
        try:
            validate_backup_database(incomplete)
        except ValueError:
            pass
        else:
            raise AssertionError("An incompatible database schema was accepted")

    print("Backup path and database validation checks passed.")


if __name__ == "__main__":
    run()
