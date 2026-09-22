"""SQLite lifecycle, foundation schema, readiness probes, and demo reset."""

from __future__ import annotations

import os
import sqlite3
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from backend.config import Settings


EXPECTED_SCHEMA_VERSION = 1
FOUNDATION_BASELINE = "foundation-empty-v1"
DEMO_BASELINE = "synthetic-pa-v1"
BUSY_TIMEOUT_MILLISECONDS = 5_000
_SCHEMA_PATH = Path(__file__).with_name("schema.sql")
_INITIALIZATION_RESET_LOCK = threading.RLock()


class SchemaError(RuntimeError):
    """Raised when the on-disk schema is incompatible or invalid."""


class DatabaseValidationError(RuntimeError):
    """Raised when a newly constructed reset database fails validation."""


@contextmanager
def managed_connection(database_path: Path) -> Iterator[sqlite3.Connection]:
    """Open one transactional connection with required SQLite safeguards."""

    connection = sqlite3.connect(database_path, timeout=5.0)
    try:
        connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MILLISECONDS}")
        connection.execute("PRAGMA foreign_keys = ON")
        foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()
        if foreign_keys is None or foreign_keys[0] != 1:
            raise DatabaseValidationError("SQLite foreign-key enforcement is unavailable")
        yield connection
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def _read_user_version(connection: sqlite3.Connection) -> int:
    row = connection.execute("PRAGMA user_version").fetchone()
    if row is None:
        raise SchemaError("Unable to read the SQLite schema version")
    return int(row[0])


def _verify_foundation_metadata(connection: sqlite3.Connection) -> None:
    try:
        row = connection.execute(
            "SELECT schema_version, baseline FROM foundation_metadata WHERE singleton = 1"
        ).fetchone()
    except sqlite3.DatabaseError as exc:
        raise SchemaError("Foundation schema metadata is missing or invalid") from exc

    expected = (EXPECTED_SCHEMA_VERSION, FOUNDATION_BASELINE)
    if row != expected:
        raise SchemaError("Foundation schema metadata does not match the expected baseline")


def _initialize_database_unlocked(database_path: Path) -> None:
    database_path.parent.mkdir(parents=True, exist_ok=True)
    with managed_connection(database_path) as connection:
        user_version = _read_user_version(connection)
        if user_version == 0:
            connection.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
        elif user_version != EXPECTED_SCHEMA_VERSION:
            raise SchemaError(
                f"Unsupported SQLite schema version: {user_version}; "
                f"expected {EXPECTED_SCHEMA_VERSION}"
            )

        if _read_user_version(connection) != EXPECTED_SCHEMA_VERSION:
            raise SchemaError("SQLite schema initialization did not set the expected version")
        _verify_foundation_metadata(connection)


def initialize_database(settings: Settings) -> None:
    """Initialize or verify the configured foundation database."""

    with _INITIALIZATION_RESET_LOCK:
        _initialize_database_unlocked(settings.database_path)


def probe_fts5(connection: sqlite3.Connection) -> bool:
    """Functionally verify FTS5 using a temporary virtual table."""

    try:
        connection.execute(
            "CREATE VIRTUAL TABLE temp.synapse_fts5_probe USING fts5(content)"
        )
        connection.execute(
            "INSERT INTO temp.synapse_fts5_probe(content) VALUES ('probe')"
        )
        row = connection.execute(
            "SELECT content FROM temp.synapse_fts5_probe "
            "WHERE synapse_fts5_probe MATCH ?",
            ("probe",),
        ).fetchone()
        return row == ("probe",)
    except sqlite3.DatabaseError:
        return False
    finally:
        try:
            connection.execute("DROP TABLE IF EXISTS temp.synapse_fts5_probe")
        except sqlite3.DatabaseError:
            pass


def check_data_directory_writable(data_directory: Path) -> bool:
    """Test write/create/delete behavior without revealing the local path."""

    probe_path: Path | None = None
    try:
        data_directory.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            prefix=".synapse-write-probe-", dir=data_directory, delete=False
        ) as probe:
            probe.write(b"ok")
            probe_path = Path(probe.name)
        probe_path.unlink()
        return True
    except OSError:
        return False
    finally:
        if probe_path is not None:
            try:
                probe_path.unlink(missing_ok=True)
            except OSError:
                pass


def preflight_checks(settings: Settings) -> tuple[str, dict[str, dict[str, Any]]]:
    """Run safe readiness checks and classify required versus optional failures."""

    checks: dict[str, dict[str, Any]] = {
        "application": {"ok": True, "required": True},
        "database": {"ok": False, "required": True},
        "foreign_keys": {"ok": False, "required": True},
        "fts5": {"ok": False, "required": False},
        "data_directory": {
            "ok": check_data_directory_writable(settings.data_directory),
            "required": True,
        },
    }
    if not checks["data_directory"]["ok"]:
        checks["data_directory"]["message"] = "The local data directory is not writable."

    try:
        with managed_connection(settings.database_path) as connection:
            connection.execute("SELECT 1").fetchone()
            checks["database"]["ok"] = True
            checks["foreign_keys"]["ok"] = (
                connection.execute("PRAGMA foreign_keys").fetchone() == (1,)
            )
            checks["fts5"]["ok"] = probe_fts5(connection)
    except (OSError, sqlite3.Error, DatabaseValidationError):
        checks["database"]["message"] = "The local database is unavailable."
        checks["foreign_keys"]["message"] = (
            "SQLite foreign-key enforcement could not be verified."
        )

    if not checks["foreign_keys"]["ok"] and "message" not in checks["foreign_keys"]:
        checks["foreign_keys"]["message"] = "SQLite foreign-key enforcement is disabled."
    if not checks["fts5"]["ok"]:
        checks["fts5"]["message"] = (
            "Optional FTS5 capability is unavailable; structured retrieval remains supported."
        )

    required_failed = any(
        not check["ok"] for check in checks.values() if check["required"]
    )
    optional_failed = any(
        not check["ok"] for check in checks.values() if not check["required"]
    )
    status = "not_ready" if required_failed else "degraded" if optional_failed else "ready"
    return status, checks


def _validate_database(database_path: Path) -> None:
    with managed_connection(database_path) as connection:
        if _read_user_version(connection) != EXPECTED_SCHEMA_VERSION:
            raise DatabaseValidationError("Reset database has an unexpected schema version")
        _verify_foundation_metadata(connection)
        if connection.execute("PRAGMA foreign_keys").fetchone() != (1,):
            raise DatabaseValidationError("Reset database does not enforce foreign keys")
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise DatabaseValidationError("Reset database contains foreign-key violations")
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
        if integrity != ("ok",):
            raise DatabaseValidationError("Reset database failed its integrity check")


def _remove_temporary_database_files(database_path: Path) -> None:
    for suffix in ("", "-journal", "-shm", "-wal"):
        try:
            Path(f"{database_path}{suffix}").unlink(missing_ok=True)
        except OSError:
            pass


def reset_demo_database(settings: Settings) -> None:
    """Atomically replace the demo database with the complete seeded baseline."""

    with _INITIALIZATION_RESET_LOCK:
        settings.database_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{settings.database_path.name}.reset-",
            suffix=".tmp",
            dir=settings.database_path.parent,
        )
        os.close(descriptor)
        temporary_path = Path(temporary_name)
        try:
            _initialize_database_unlocked(temporary_path)
            from backend import demo

            temporary_settings = Settings(
                project_root=settings.project_root,
                data_directory=settings.data_directory,
                database_path=temporary_path,
                frontend_directory=settings.frontend_directory,
                demo_mode=settings.demo_mode,
                demo_reset_confirmation=settings.demo_reset_confirmation,
            )
            with managed_connection(temporary_path) as connection:
                connection.executescript(
                    Path(__file__).with_name("demo_schema.sql").read_text(encoding="utf-8")
                )
                demo._seed(connection, temporary_settings)
            _validate_database(temporary_path)
            os.replace(temporary_path, settings.database_path)
        except BaseException:
            _remove_temporary_database_files(temporary_path)
            raise
