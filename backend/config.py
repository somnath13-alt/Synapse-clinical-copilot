"""Small, immutable application configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


DEFAULT_RESET_CONFIRMATION = "RESET_SYNTHETIC_DEMO"


def _environment_flag(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True)
class Settings:
    """Filesystem and demo controls that can be replaced in tests."""

    project_root: Path
    data_directory: Path
    database_path: Path
    frontend_directory: Path
    demo_mode: bool
    demo_reset_confirmation: str

    @classmethod
    def from_environment(cls) -> "Settings":
        project_root = Path(__file__).resolve().parent.parent
        data_directory = Path(
            os.environ.get("SYNAPSE_DATA_DIRECTORY", project_root / "data")
        ).resolve()
        database_path = Path(
            os.environ.get("SYNAPSE_DATABASE_PATH", data_directory / "synapse.sqlite3")
        ).resolve()
        frontend_directory = Path(
            os.environ.get("SYNAPSE_FRONTEND_DIRECTORY", project_root / "frontend")
        ).resolve()
        return cls(
            project_root=project_root,
            data_directory=data_directory,
            database_path=database_path,
            frontend_directory=frontend_directory,
            demo_mode=_environment_flag("SYNAPSE_DEMO_MODE", True),
            demo_reset_confirmation=os.environ.get(
                "SYNAPSE_DEMO_RESET_CONFIRMATION", DEFAULT_RESET_CONFIRMATION
            ),
        )

