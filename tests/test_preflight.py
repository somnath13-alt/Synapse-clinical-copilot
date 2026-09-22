from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from backend import database
from backend.config import Settings
from backend.main import create_app


def test_preflight_ready(settings: Settings, monkeypatch) -> None:
    monkeypatch.setattr(database, "probe_fts5", lambda connection: True)
    with TestClient(create_app(settings)) as client:
        response = client.get("/api/preflight")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ready"
    assert all(check["ok"] for check in payload["checks"].values())


def test_preflight_degraded_when_fts5_is_unavailable(
    settings: Settings, monkeypatch
) -> None:
    monkeypatch.setattr(database, "probe_fts5", lambda connection: False)
    with TestClient(create_app(settings)) as client:
        response = client.get("/api/preflight")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "degraded"
    assert payload["checks"]["fts5"] == {
        "ok": False,
        "required": False,
        "message": (
            "Optional FTS5 capability is unavailable; structured retrieval remains supported."
        ),
    }


def test_preflight_required_failure_is_safe(settings: Settings, monkeypatch) -> None:
    monkeypatch.setattr(database, "check_data_directory_writable", lambda path: False)
    with TestClient(create_app(settings)) as client:
        response = client.get("/api/preflight")

    assert response.status_code == 503
    payload = response.json()
    assert payload["status"] == "not_ready"
    assert payload["checks"]["data_directory"]["ok"] is False
    response_text = response.text
    assert str(settings.data_directory) not in response_text
    assert "PermissionError" not in response_text
    assert "Traceback" not in response_text

