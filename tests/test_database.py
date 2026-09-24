from __future__ import annotations

import sqlite3

import pytest

from backend import database
from backend.config import Settings


def _metadata(database_path) -> tuple[int, str]:
    with database.managed_connection(database_path) as connection:
        return connection.execute(
            "SELECT schema_version, baseline FROM foundation_metadata WHERE singleton = 1"
        ).fetchone()


def test_fresh_initialization_creates_only_foundation_metadata(
    settings: Settings,
) -> None:
    database.initialize_database(settings)

    with database.managed_connection(settings.database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (2,)
        assert _metadata(settings.database_path) == (2, "foundation-empty-v2")
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    assert tables == {"foundation_metadata"}


def test_repeated_initialization_is_idempotent(settings: Settings) -> None:
    database.initialize_database(settings)
    before = settings.database_path.read_bytes()

    database.initialize_database(settings)

    assert settings.database_path.read_bytes() == before
    assert _metadata(settings.database_path) == (2, "foundation-empty-v2")


def test_schema_v1_database_is_rejected_without_migration(settings: Settings) -> None:
    settings.data_directory.mkdir(parents=True)
    with sqlite3.connect(settings.database_path) as connection:
        connection.execute("PRAGMA user_version = 1")

    with pytest.raises(
        database.SchemaError,
        match="Unsupported SQLite schema version: 1; expected 2",
    ):
        database.initialize_database(settings)

    with sqlite3.connect(settings.database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (1,)


def test_unexpected_schema_version_is_rejected(settings: Settings) -> None:
    settings.data_directory.mkdir(parents=True)
    with sqlite3.connect(settings.database_path) as connection:
        connection.execute("PRAGMA user_version = 99")

    with pytest.raises(database.SchemaError, match="Unsupported SQLite schema version"):
        database.initialize_database(settings)

    with sqlite3.connect(settings.database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (99,)


def test_every_managed_connection_enables_foreign_keys(settings: Settings) -> None:
    settings.data_directory.mkdir(parents=True)
    for _ in range(2):
        with database.managed_connection(settings.database_path) as connection:
            assert connection.execute("PRAGMA foreign_keys").fetchone() == (1,)


def test_actual_foreign_key_violation_raises(settings: Settings) -> None:
    settings.data_directory.mkdir(parents=True)
    with database.managed_connection(settings.database_path) as connection:
        connection.execute("CREATE TABLE parent (id INTEGER PRIMARY KEY)")
        connection.execute(
            "CREATE TABLE child (parent_id INTEGER REFERENCES parent(id))"
        )

    with pytest.raises(sqlite3.IntegrityError):
        with database.managed_connection(settings.database_path) as connection:
            connection.execute("INSERT INTO child(parent_id) VALUES (404)")


def test_managed_connection_rolls_back_and_closes(settings: Settings) -> None:
    settings.data_directory.mkdir(parents=True)
    with database.managed_connection(settings.database_path) as connection:
        connection.execute("CREATE TABLE transaction_probe (value TEXT)")

    failed_connection = None
    with pytest.raises(RuntimeError, match="force rollback"):
        with database.managed_connection(settings.database_path) as connection:
            failed_connection = connection
            connection.execute("INSERT INTO transaction_probe VALUES ('not committed')")
            raise RuntimeError("force rollback")

    assert failed_connection is not None
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        failed_connection.execute("SELECT 1")
    with database.managed_connection(settings.database_path) as connection:
        assert connection.execute("SELECT * FROM transaction_probe").fetchall() == []
