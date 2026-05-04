from __future__ import annotations

import logging
import os
from pathlib import Path

from pydantic import BaseModel, Field

from ..._metadata import app_name, app_slug

# --- Config ---

project_root = Path(__file__).parent.parent.parent.parent.parent
env_file = project_root / ".env"

if env_file.exists():
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip().strip("'\""))


class AppConfig(BaseModel):
    app_name: str = Field(default=app_name)

    @property
    def static_assets_path(self) -> Path:
        dist = Path(__file__).parent.parent.parent / "__dist__"
        return dist

    def __hash__(self) -> int:
        return hash(self.app_name)


# --- Logger ---

logger = logging.getLogger(app_name)
