from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AppConfig:
    project_root: Path
    app_name: str
    environment: str
    database_engine: str
    database_url: str
    default_database_path: Path
    local_db_pointer_path: Path


def _project_root() -> Path:
    configured_root = os.getenv("SYNCRAFT_PROJECT_ROOT", "").strip()
    if configured_root:
        return Path(configured_root).expanduser().resolve()
    return Path.cwd().resolve()


def _default_app_data_dir() -> Path:
    home = Path.home()
    if os.name == "nt":
        base = Path(os.getenv("APPDATA") or (home / "AppData" / "Roaming"))
        return base / "Syncraft"
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / "Syncraft"
    base = Path(os.getenv("XDG_DATA_HOME") or (home / ".local" / "share"))
    return base / "syncraft"


def load_config() -> AppConfig:
    root = _project_root()
    app_data_dir = _default_app_data_dir()
    default_db_path = app_data_dir / "syncraft.db"
    local_db_pointer_path = app_data_dir / "local_db_path.txt"
    pointer_target = ""
    if local_db_pointer_path.exists():
        pointer_target = local_db_pointer_path.read_text(encoding="utf-8").strip()
    resolved_db_path = Path(pointer_target).expanduser() if pointer_target else default_db_path
    database_url = os.getenv("SYNCRAFT_DATABASE_URL", f"sqlite:///{resolved_db_path}")
    return AppConfig(
        project_root=root,
        app_name=os.getenv("SYNCRAFT_APP_NAME", "Syncraft"),
        environment=os.getenv("SYNCRAFT_ENV", "development"),
        database_engine=os.getenv("SYNCRAFT_DATABASE_ENGINE", "sqlite"),
        database_url=database_url,
        default_database_path=default_db_path,
        local_db_pointer_path=local_db_pointer_path,
    )
