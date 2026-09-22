from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.config import DEFAULT_RESET_CONFIRMATION, Settings
from backend.main import create_app


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    data_directory = tmp_path / "data"
    return Settings(
        project_root=tmp_path,
        data_directory=data_directory,
        database_path=data_directory / "test.sqlite3",
        frontend_directory=tmp_path / "frontend",
        demo_mode=True,
        demo_reset_confirmation=DEFAULT_RESET_CONFIRMATION,
    )


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings)) as test_client:
        yield test_client

