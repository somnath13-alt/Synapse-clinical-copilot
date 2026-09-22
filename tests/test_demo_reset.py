from __future__ import annotations

import sqlite3
from dataclasses import replace

from fastapi.testclient import TestClient

from backend import database
from backend.config import DEFAULT_RESET_CONFIRMATION, Settings
from backend.main import create_app


RESET_BODY = {"confirmation": DEFAULT_RESET_CONFIRMATION}


def _table_names(settings: Settings) -> set[str]:
    with database.managed_connection(settings.database_path) as connection:
        return {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }


def test_successful_reset_removes_mutation_and_restores_baseline(
    client: TestClient, settings: Settings
) -> None:
    with database.managed_connection(settings.database_path) as connection:
        connection.execute("CREATE TABLE disposable_mutation (value TEXT)")

    response = client.post("/api/demo/reset", json=RESET_BODY)

    assert response.status_code == 200
    assert response.json() == {
        "status": "reset",
        "baseline": "synthetic-pa-v1",
        "schema_version": 1,
    }
    assert "disposable_mutation" not in _table_names(settings)
    assert {"source_document", "source_document_version", "evidence_item", "interaction", "audit_event"} <= _table_names(settings)


def test_repeated_reset_is_deterministic(client: TestClient, settings: Settings) -> None:
    first = client.post("/api/demo/reset", json=RESET_BODY)
    first_state = _table_names(settings)
    second = client.post("/api/demo/reset", json=RESET_BODY)

    assert first.json() == second.json()
    assert _table_names(settings) == first_state
    with database.managed_connection(settings.database_path) as connection:
        assert connection.execute(
            "SELECT document_version_id FROM source_document_version WHERE is_current = 1 AND document_id = 'DOC-SYN-POL-VEL'"
        ).fetchone() == ("DV-SYN-POL-VEL-V1",)


def test_incorrect_confirmation_is_rejected(
    client: TestClient, settings: Settings
) -> None:
    before = settings.database_path.read_bytes()

    response = client.post("/api/demo/reset", json={"confirmation": "wrong"})

    assert response.status_code == 403
    assert settings.database_path.read_bytes() == before


def test_reset_is_rejected_when_demo_mode_is_disabled(settings: Settings) -> None:
    disabled = replace(settings, demo_mode=False)
    with TestClient(create_app(disabled)) as client:
        before = settings.database_path.read_bytes()
        response = client.post("/api/demo/reset", json=RESET_BODY)

    assert response.status_code == 403
    assert settings.database_path.read_bytes() == before


def test_reset_failure_preserves_previous_database(
    client: TestClient, settings: Settings, monkeypatch
) -> None:
    with database.managed_connection(settings.database_path) as connection:
        connection.execute("CREATE TABLE preserved_marker (value TEXT NOT NULL)")
        connection.execute("INSERT INTO preserved_marker VALUES ('keep me')")

    def fail_validation(database_path) -> None:
        raise database.DatabaseValidationError("simulated unsafe detail")

    monkeypatch.setattr(database, "_validate_database", fail_validation)
    response = client.post("/api/demo/reset", json=RESET_BODY)

    assert response.status_code == 503
    assert response.json() == {
        "detail": "Demo reset failed; the existing database was preserved."
    }
    with database.managed_connection(settings.database_path) as connection:
        assert connection.execute("SELECT value FROM preserved_marker").fetchone() == (
            "keep me",
        )
    assert list(settings.data_directory.glob(".*.reset-*.tmp")) == []


def test_reset_database_still_enforces_foreign_keys(
    client: TestClient, settings: Settings
) -> None:
    assert client.post("/api/demo/reset", json=RESET_BODY).status_code == 200

    with database.managed_connection(settings.database_path) as connection:
        connection.execute("CREATE TABLE parent (id INTEGER PRIMARY KEY)")
        connection.execute(
            "CREATE TABLE child (parent_id INTEGER REFERENCES parent(id))"
        )
    try:
        with database.managed_connection(settings.database_path) as connection:
            connection.execute("INSERT INTO child VALUES (999)")
    except sqlite3.IntegrityError:
        pass
    else:
        raise AssertionError("The reset database did not enforce foreign keys")


def test_reset_creates_complete_synthetic_demo_tables(
    client: TestClient, settings: Settings
) -> None:
    assert client.post("/api/demo/reset", json=RESET_BODY).status_code == 200
    assert {
        "source_document", "source_document_version", "evidence_item", "interaction",
        "supported_claim", "citation", "knowledge_assertion", "assertion_supersession",
        "feedback", "review", "knowledge_update", "audit_event",
    } <= _table_names(settings)
